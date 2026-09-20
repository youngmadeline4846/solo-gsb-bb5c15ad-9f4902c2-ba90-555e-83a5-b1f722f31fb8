"""样本库位管理测试。

覆盖：
* 库位创建：编号唯一（重复 409）、容量 / 编号 / 名称形态校验（422）；
* 放入 / 转移 / 移出主流程：占用数同步增减、样本与库位版本递增、
  不可变移动记录逐条落库；样本详情返回当前位置与移动历史；
* 单事务校验：陈旧来源（当前库位不一致）、目标已满、样本 / 源 / 目标
  版本过期均返回 409，且整体回滚——不产生重复占位、超容量或半条记录；
* 并发争抢最后空位、并发转移同一样本：仅一个成功，占用不超容量、
  每样本最多一条位置记录；
* 移出库位后占用释放；库位中的样本禁止删除（409），移出后可删除；
* 库位详情（占用清单 + 移动历史）与样本移动历史接口。
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="audit-location-tests-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmp, "test_location.db")

import itertools
import threading

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


_seq = itertools.count(1)


def _new_sample(client, prefix="UT-LOC-S"):
    code = f"{prefix}-{next(_seq):03d}"
    r = client.post("/api/samples", json={"code": code, "name": "库位测试样本", "description": ""})
    assert r.status_code == 201, r.text
    return r.json()


def _new_location(client, capacity=2, prefix="UT-LOC"):
    code = f"{prefix}-{next(_seq):03d}"
    r = client.post("/api/locations", json={"code": code, "name": f"库位 {code}", "capacity": capacity})
    assert r.status_code == 201, r.text
    return r.json()


def _move(client, sample, to_loc, *, sample_ver, from_loc=None, from_ver=None,
          to_ver=None, operator="测试员", note=""):
    """按当前声明的位置 / 版本发起一次移动（to_loc=None 表示移出库位）。"""
    return client.post(
        f"/api/samples/{sample['id']}/location-move",
        json={
            "to_location_id": to_loc["id"] if to_loc else None,
            "operator": operator,
            "note": note,
            "expected_sample_version": sample_ver,
            "expected_from_location_id": from_loc["id"] if from_loc else None,
            "expected_from_version": from_ver,
            "expected_to_version": to_ver,
        },
    )


def _loc(client, location_id):
    return client.get(f"/api/locations/{location_id}").json()


def _placement(sample_id):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM sample_placements WHERE sample_id = ?", (sample_id,)
        ).fetchone()
    finally:
        conn.close()


def _move_count(sample_id=None, to_location_id=None):
    sql = "SELECT COUNT(*) FROM location_moves WHERE 1=1"
    args = []
    if sample_id is not None:
        sql += " AND sample_id = ?"
        args.append(sample_id)
    if to_location_id is not None:
        sql += " AND to_location_id = ?"
        args.append(to_location_id)
    conn = get_connection()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


# ---------- 库位创建 ----------

def test_create_location_validation(client):
    loc = _new_location(client, capacity=3)
    assert loc["occupied"] == 0
    assert loc["remaining"] == 3
    assert loc["version"] == 0

    # 编号重复 -> 409
    dup = client.post(
        "/api/locations",
        json={"code": loc["code"], "name": "重复编号", "capacity": 1},
    )
    assert dup.status_code == 409
    assert "已存在" in dup.json()["detail"]

    # 容量必须为正整数 -> 422
    for bad in (0, -1, 1.5, "abc"):
        r = client.post(
            "/api/locations",
            json={"code": f"UT-LOC-BAD-{next(_seq)}", "name": "x", "capacity": bad},
        )
        assert r.status_code == 422, bad
    # 编号 / 名称不能为空 -> 422
    assert client.post(
        "/api/locations", json={"code": "  ", "name": "x", "capacity": 1}
    ).status_code == 422
    assert client.post(
        "/api/locations", json={"code": "UT-LOC-EMPTY", "name": "", "capacity": 1}
    ).status_code == 422

    # 列表包含新库位
    codes = {l["code"] for l in client.get("/api/locations").json()}
    assert loc["code"] in codes


# ---------- 放入 / 转移 / 移出主流程 ----------

def test_place_transfer_checkout_flow(client):
    la = _new_location(client, capacity=2)
    lb = _new_location(client, capacity=1)
    s = _new_sample(client)
    sid = s["id"]

    # 样本详情：初始未入库
    d = client.get(f"/api/samples/{sid}").json()
    assert d["location"] is None
    assert d["location_moves"] == []

    # 放入 A（样本 v0 -> v1，A 占用 0->1，A 版本 0->1）
    r = _move(client, s, la, sample_ver=0, to_ver=0, note="首次入库")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sample_version"] == 1
    assert body["location"]["location_id"] == la["id"]
    assert body["location"]["location_version"] == 1
    la_now = _loc(client, la["id"])
    assert (la_now["occupied"], la_now["remaining"], la_now["version"]) == (1, 1, 1)
    assert [o["code"] for o in la_now["occupants"]] == [s["code"]]

    # 转移到 B（A 占用回落，B 占用 0->1；样本 v1 -> v2）
    r = _move(client, s, lb, sample_ver=1, from_loc=la, from_ver=1, to_ver=0)
    assert r.status_code == 201, r.text
    assert r.json()["sample_version"] == 2
    assert (_loc(client, la["id"])["occupied"], _loc(client, la["id"])["version"]) == (0, 2)
    assert (_loc(client, lb["id"])["occupied"], _loc(client, lb["id"])["version"]) == (1, 1)
    assert _placement(sid)["location_id"] == lb["id"]

    # 移动历史：两条不可变记录，新的在前，含版本快照
    moves = client.get(f"/api/samples/{sid}/location-moves").json()
    assert len(moves) == 2
    assert moves[0]["from_code"] == la["code"] and moves[0]["to_code"] == lb["code"]
    assert moves[1]["from_code"] is None and moves[1]["to_code"] == la["code"]
    assert moves[0]["sample_version_before"] == 1
    assert moves[0]["from_version_before"] == 1 and moves[0]["to_version_before"] == 0
    assert moves[1]["note"] == "首次入库"

    # 样本详情内联当前位置与历史
    d = client.get(f"/api/samples/{sid}").json()
    assert d["location"]["code"] == lb["code"]
    assert len(d["location_moves"]) == 2
    assert d["sample"]["version"] == 2

    # 移出库位（B 占用释放；样本 v2 -> v3）
    r = _move(client, s, None, sample_ver=2, from_loc=lb, from_ver=1)
    assert r.status_code == 201, r.text
    assert r.json()["location"] is None
    assert (_loc(client, lb["id"])["occupied"], _loc(client, lb["id"])["version"]) == (0, 2)
    assert _placement(sid) is None
    moves = client.get(f"/api/samples/{sid}/location-moves").json()
    assert moves[0]["to_code"] is None and moves[0]["from_code"] == lb["code"]


def test_move_to_same_location_rejected(client):
    la = _new_location(client, capacity=2)
    s = _new_sample(client)
    assert _move(client, s, la, sample_ver=0, to_ver=0).status_code == 201
    # 已在 A 中，再次放入 A -> 409（重复占位被拒绝）
    r = _move(client, s, la, sample_ver=1, from_loc=la, from_ver=1, to_ver=1)
    assert r.status_code == 409
    assert "重复" in r.json()["detail"]
    # 占用与版本不变
    assert (_loc(client, la["id"])["occupied"], _loc(client, la["id"])["version"]) == (1, 1)
    assert _move_count(sample_id=s["id"]) == 1


# ---------- 单事务校验与回滚 ----------

def test_stale_source_rejected_and_rolled_back(client):
    la = _new_location(client, capacity=2)
    lb = _new_location(client, capacity=2)
    s = _new_sample(client)
    assert _move(client, s, la, sample_ver=0, to_ver=0).status_code == 201  # 放入 A

    # 客户端以为样本未入库（声明源为 null），实际在 A -> 陈旧来源 409
    r = _move(client, s, lb, sample_ver=1, to_ver=0)
    assert r.status_code == 409
    assert "库位" in r.json()["detail"]

    # 客户端以为样本在 B，实际在 A -> 陈旧来源 409
    r = _move(client, s, lb, sample_ver=1, from_loc=lb, from_ver=0, to_ver=0)
    assert r.status_code == 409

    # 全部回滚：样本仍在 A，两侧占用 / 版本不变，无新增移动记录
    assert _placement(s["id"])["location_id"] == la["id"]
    assert (_loc(client, la["id"])["occupied"], _loc(client, la["id"])["version"]) == (1, 1)
    assert (_loc(client, lb["id"])["occupied"], _loc(client, lb["id"])["version"]) == (0, 0)
    assert _move_count(sample_id=s["id"]) == 1
    assert client.get(f"/api/samples/{s['id']}").json()["sample"]["version"] == 1


def test_full_location_rejected_and_rolled_back(client):
    la = _new_location(client, capacity=1)  # 只有一个空位
    s1, s2 = _new_sample(client), _new_sample(client)
    assert _move(client, s1, la, sample_ver=0, to_ver=0).status_code == 201  # 占满

    # 第二份样本放入 -> 409 目标已满
    r = _move(client, s2, la, sample_ver=0, to_ver=1)
    assert r.status_code == 409
    assert "已满" in r.json()["detail"]

    # 回滚：s2 未占位、占用不超容量、无半条移动记录、版本不变
    assert _placement(s2["id"]) is None
    la_now = _loc(client, la["id"])
    assert la_now["occupied"] == 1 and la_now["occupied"] <= la_now["capacity"]
    assert la_now["version"] == 1
    assert _move_count(sample_id=s2["id"]) == 0
    assert client.get(f"/api/samples/{s2['id']}").json()["sample"]["version"] == 0


def test_version_conflicts_rejected_and_rolled_back(client):
    la = _new_location(client, capacity=2)
    lb = _new_location(client, capacity=2)
    s = _new_sample(client)
    assert _move(client, s, la, sample_ver=0, to_ver=0).status_code == 201  # 样本 v1，A v1

    # 样本版本过期
    r = _move(client, s, lb, sample_ver=0, from_loc=la, from_ver=1, to_ver=0)
    assert r.status_code == 409 and "样本版本" in r.json()["detail"]
    # 源库位版本过期
    r = _move(client, s, lb, sample_ver=1, from_loc=la, from_ver=0, to_ver=0)
    assert r.status_code == 409 and "源库位版本" in r.json()["detail"]
    # 目标库位版本过期
    r = _move(client, s, lb, sample_ver=1, from_loc=la, from_ver=1, to_ver=5)
    assert r.status_code == 409 and "目标库位版本" in r.json()["detail"]

    # 三次失败均未产生任何变化
    assert _placement(s["id"])["location_id"] == la["id"]
    assert (_loc(client, la["id"])["occupied"], _loc(client, la["id"])["version"]) == (1, 1)
    assert (_loc(client, lb["id"])["occupied"], _loc(client, lb["id"])["version"]) == (0, 0)
    assert _move_count(sample_id=s["id"]) == 1
    assert client.get(f"/api/samples/{s['id']}").json()["sample"]["version"] == 1

    # 携带最新版本重试 -> 成功
    r = _move(client, s, lb, sample_ver=1, from_loc=la, from_ver=1, to_ver=0)
    assert r.status_code == 201


def test_move_request_shape_validation(client):
    la = _new_location(client, capacity=1)
    s = _new_sample(client)
    # 转移到目标库位但缺目标版本 -> 422
    r = client.post(
        f"/api/samples/{s['id']}/location-move",
        json={"to_location_id": la["id"], "operator": "x",
              "expected_sample_version": 0,
              "expected_from_location_id": None, "expected_from_version": None,
              "expected_to_version": None},
    )
    assert r.status_code == 422
    # 声明源库位但缺源版本 -> 422
    r = client.post(
        f"/api/samples/{s['id']}/location-move",
        json={"to_location_id": None, "operator": "x",
              "expected_sample_version": 0,
              "expected_from_location_id": la["id"], "expected_from_version": None,
              "expected_to_version": None},
    )
    assert r.status_code == 422
    # 操作人为空 -> 422
    r = client.post(
        f"/api/samples/{s['id']}/location-move",
        json={"to_location_id": la["id"], "operator": "  ",
              "expected_sample_version": 0,
              "expected_from_location_id": None, "expected_from_version": None,
              "expected_to_version": 0},
    )
    assert r.status_code == 422
    assert _move_count(sample_id=s["id"]) == 0


def test_move_not_found(client):
    la = _new_location(client, capacity=1)
    s = _new_sample(client)
    # 样本不存在 -> 404
    r = client.post(
        "/api/samples/9999999/location-move",
        json={"to_location_id": la["id"], "operator": "x",
              "expected_sample_version": 0,
              "expected_from_location_id": None, "expected_from_version": None,
              "expected_to_version": 0},
    )
    assert r.status_code == 404
    # 目标库位不存在 -> 404
    r = client.post(
        f"/api/samples/{s['id']}/location-move",
        json={"to_location_id": 9999999, "operator": "x",
              "expected_sample_version": 0,
              "expected_from_location_id": None, "expected_from_version": None,
              "expected_to_version": 0},
    )
    assert r.status_code == 404
    # 库位不存在 -> 404
    assert client.get("/api/locations/9999999").status_code == 404
    assert _move_count(sample_id=s["id"]) == 0


# ---------- 删除保护 ----------

def test_delete_guard_while_placed(client):
    la = _new_location(client, capacity=1)
    s = _new_sample(client)
    assert _move(client, s, la, sample_ver=0, to_ver=0).status_code == 201

    # 库位中的样本禁止删除
    r = client.delete(f"/api/samples/{s['id']}")
    assert r.status_code == 409
    assert la["code"] in r.json()["detail"]

    # 移出后可删除；移动历史（不可变记录）仍保留
    assert _move(client, s, None, sample_ver=1, from_loc=la, from_ver=1).status_code == 201
    assert client.delete(f"/api/samples/{s['id']}").status_code == 204
    assert _move_count(sample_id=s["id"]) == 2
    assert (_loc(client, la["id"])["occupied"], _loc(client, la["id"])["version"]) == (0, 2)


# ---------- 库位详情 / 移动历史 ----------

def test_location_detail_occupants_and_moves(client):
    la = _new_location(client, capacity=3)
    lb = _new_location(client, capacity=3)
    s1, s2 = _new_sample(client), _new_sample(client)
    assert _move(client, s1, la, sample_ver=0, to_ver=0).status_code == 201
    assert _move(client, s2, la, sample_ver=0, to_ver=1).status_code == 201
    assert _move(client, s1, lb, sample_ver=1, from_loc=la, from_ver=2, to_ver=0).status_code == 201

    da = _loc(client, la["id"])
    assert da["occupied"] == 1 and da["remaining"] == 2
    assert [o["code"] for o in da["occupants"]] == [s2["code"]]
    # A 的移动历史：s1 入、s2 入、s1 出（新的在前）
    assert [(m["sample_code"], m["from_code"], m["to_code"]) for m in da["moves"]] == [
        (s1["code"], la["code"], lb["code"]),
        (s2["code"], None, la["code"]),
        (s1["code"], None, la["code"]),
    ]
    db = _loc(client, lb["id"])
    assert [o["code"] for o in db["occupants"]] == [s1["code"]]


# ---------- 并发：争抢最后空位 ----------

def test_concurrent_last_slot_only_one_succeeds(client):
    """仅剩一个空位时两份样本并发放入：仅一个成功，绝不超容量或重复占位。"""
    la = _new_location(client, capacity=1)
    s1, s2 = _new_sample(client), _new_sample(client)

    results: list[int] = []
    lock = threading.Lock()

    def worker(sample):
        resp = _move(client, sample, la, sample_ver=0, to_ver=0)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (s1, s2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], results
    la_now = _loc(client, la["id"])
    assert la_now["occupied"] == 1 and la_now["occupied"] <= la_now["capacity"]
    assert la_now["version"] == 1
    assert len(la_now["occupants"]) == 1
    # 恰好一条位置记录与一条移动记录；失败者未留下任何痕迹
    placed = [_placement(s1["id"]), _placement(s2["id"])]
    assert sum(1 for p in placed if p is not None) == 1
    assert _move_count(to_location_id=la["id"]) == 1
    winner = s1 if placed[0] is not None else s2
    loser = s2 if placed[0] is not None else s1
    assert client.get(f"/api/samples/{winner['id']}").json()["sample"]["version"] == 1
    assert client.get(f"/api/samples/{loser['id']}").json()["sample"]["version"] == 0


def test_concurrent_same_sample_two_targets(client):
    """同一样本并发转移到两个不同库位：仅一个成功，样本只占一个库位。"""
    la = _new_location(client, capacity=2)
    lb = _new_location(client, capacity=2)
    lc = _new_location(client, capacity=2)
    s = _new_sample(client)
    assert _move(client, s, la, sample_ver=0, to_ver=0).status_code == 201  # 样本 v1，A v1

    results: list[int] = []
    lock = threading.Lock()

    def worker(target):
        resp = _move(client, s, target, sample_ver=1, from_loc=la, from_ver=1, to_ver=0)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(t,)) for t in (lb, lc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], results
    # 样本只在一个库位中；源库位恰好释放一次；目标侧合计只 +1
    placement = _placement(s["id"])
    assert placement is not None
    assert placement["location_id"] in (lb["id"], lc["id"])
    assert _loc(client, la["id"])["occupied"] == 0
    assert _loc(client, lb["id"])["occupied"] + _loc(client, lc["id"])["occupied"] == 1
    # 只有一条新移动记录，样本版本只 +1
    assert _move_count(sample_id=s["id"]) == 2
    assert client.get(f"/api/samples/{s['id']}").json()["sample"]["version"] == 2


def test_concurrent_place_same_unplaced_sample(client):
    """同一份未入库样本并发放入两个库位：仅一个成功，不产生重复占位。"""
    la = _new_location(client, capacity=2)
    lb = _new_location(client, capacity=2)
    s = _new_sample(client)

    results: list[int] = []
    lock = threading.Lock()

    def worker(target):
        resp = _move(client, s, target, sample_ver=0, to_ver=0)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(t,)) for t in (la, lb)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], results
    placement = _placement(s["id"])
    assert placement is not None
    assert placement["location_id"] in (la["id"], lb["id"])
    assert _loc(client, la["id"])["occupied"] + _loc(client, lb["id"])["occupied"] == 1
    assert _move_count(sample_id=s["id"]) == 1
    assert client.get(f"/api/samples/{s['id']}").json()["sample"]["version"] == 1
