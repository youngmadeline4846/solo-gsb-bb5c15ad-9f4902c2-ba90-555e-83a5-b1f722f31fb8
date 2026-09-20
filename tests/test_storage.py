"""样本库位管理测试。

覆盖：
* 创建库位（唯一编号、正整数容量；重复编号 409、非法输入 422）；
* 样本放入 / 转移：单事务内校验样本当前来源、目标剩余容量与
  样本 / 源 / 目标三方 expected_version；成功后占位、占用数、版本与
  不可变移动记录一并更新（SHA-256 完整性）；
* 陈旧来源（样本已被他人移动）、样本 / 源 / 目标版本过期、源 == 目标、
  目标已满均返回 409，且不产生重复占位、超容量或半条移动记录；
* 并发争抢最后空位：多线程同时放入仅一个成功，占用数与移动记录一致；
* 事务回滚：移动事务提交前回滚（模拟中途失败）不留任何痕迹；
* 不可变移动记录只增不改；样本删除同步释放占用，记录随样本级联清除。
"""
from __future__ import annotations

import os
import tempfile
import threading
import itertools

_tmp = tempfile.mkdtemp(prefix="audit-storage-tests-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmp, "test_storage.db")

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.storage import _move_locked


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


_seq = itertools.count(1)


def _new_sample(client, prefix="UT-STO"):
    code = f"{prefix}-{next(_seq):03d}"
    r = client.post("/api/samples", json={"code": code, "name": "库位测试样本", "description": ""})
    assert r.status_code == 201, r.text
    return r.json()


def _new_location(client, capacity=3, prefix="UT-LOC"):
    code = f"{prefix}-{next(_seq):03d}"
    r = client.post(
        "/api/storage/locations",
        json={"code": code, "name": f"测试库位 {code}", "capacity": capacity},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _move(client, sample_id, to_id, frm, sv, tv, fv=None, operator="库管员", at=None):
    payload = {
        "to_location_id": to_id,
        "from_location_id": frm,
        "expected_sample_version": sv,
        "expected_to_version": tv,
        "expected_from_version": fv,
        "operator": operator,
    }
    if at:
        payload["moved_at"] = at
    return client.post(f"/api/samples/{sample_id}/storage/move", json=payload)


# ---------- 库位创建 ----------

def test_create_location_and_duplicate(client):
    loc = _new_location(client, capacity=5)
    assert loc["capacity"] == 5
    assert loc["occupied_count"] == 0
    assert loc["remaining"] == 5
    assert loc["version"] == 1
    assert loc["occupants"] == []

    # 编号唯一 -> 409
    r = client.post(
        "/api/storage/locations",
        json={"code": loc["code"], "name": "重复", "capacity": 2},
    )
    assert r.status_code == 409
    assert "已存在" in r.json()["detail"]

    # 容量非法 / 缺失 -> 422
    for bad in (0, -1, 1.5, 100001):
        r = client.post(
            "/api/storage/locations",
            json={"code": f"BAD-{next(_seq)}", "name": "x", "capacity": bad},
        )
        assert r.status_code == 422, bad

    # 详情 404
    assert client.get("/api/storage/locations/9999999").status_code == 404


def test_list_locations_summary(client):
    data = client.get("/api/storage/locations").json()
    assert data["summary"]["location_count"] >= 1
    assert data["summary"]["capacity"] >= data["summary"]["occupied"]
    # 列表每项带占用清单
    assert all("occupants" in l for l in data["locations"])


# ---------- 放入 ----------

def test_store_sample_success(client):
    s = _new_sample(client)
    loc = _new_location(client, capacity=2)

    r = _move(client, s["id"], loc["id"], None, sv=0, tv=1, at="2026-09-20 09:00:00")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["move"]["action"] == "store"
    assert body["move"]["from_location_code"] is None
    assert body["move"]["to_location_code"] == loc["code"]
    assert body["to"]["occupied_count"] == 1
    assert body["to"]["remaining"] == 1
    assert body["to"]["version"] == 2
    assert body["sample"]["version"] == 1
    assert body["from"] is None
    assert body["move"]["integrity_ok"] is True

    # 样本当前位置
    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"]["id"] == loc["id"]
    assert st["current"]["occupied_count"] == 1
    assert st["sample_version"] == 1
    assert len(st["history"]) == 1
    assert st["history"][0]["integrity_ok"] is True

    # 库位占用清单包含该样本
    detail = client.get(f"/api/storage/locations/{loc['id']}").json()
    assert [o["sample_id"] for o in detail["occupants"]] == [s["id"]]

    # 样本详情内联 storage
    d = client.get(f"/api/samples/{s['id']}").json()
    assert d["storage"]["current"]["code"] == loc["code"]


def test_store_twice_stale_source_conflict(client):
    s = _new_sample(client)
    loc = _new_location(client, capacity=2)
    other = _new_location(client, capacity=2)

    assert _move(client, s["id"], loc["id"], None, 0, 1).status_code == 201

    # 再次以「库外放入」提交（from=null）-> 陈旧来源 409，不会产生第二占位
    r = _move(client, s["id"], other["id"], None, 1, other["version"])
    assert r.status_code == 409
    assert "来源" in r.json()["detail"]

    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"]["id"] == loc["id"]  # 仍在原库位
    # 只有一条移动记录
    assert len(st["history"]) == 1
    # 目标库位未被占用
    assert client.get(f"/api/storage/locations/{other['id']}").json()["occupied_count"] == 0


# ---------- 转移 ----------

def test_transfer_success_updates_counts_versions_and_history(client):
    s = _new_sample(client)
    a = _new_location(client, capacity=2)
    b = _new_location(client, capacity=2)

    assert _move(client, s["id"], a["id"], None, 0, 1).status_code == 201
    # 转移 a -> b：样本 v1、源 a v2、目标 b v1
    r = _move(client, s["id"], b["id"], a["id"], sv=1, tv=1, fv=2, at="2026-09-20 11:00:00")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["move"]["action"] == "transfer"
    assert body["move"]["from_location_code"] == a["code"]
    assert body["to"]["occupied_count"] == 1
    assert body["to"]["version"] == 2
    assert body["from"]["occupied_count"] == 0
    assert body["from"]["version"] == 3
    assert body["sample"]["version"] == 2

    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"]["id"] == b["id"]
    assert [m["action"] for m in st["history"]] == ["transfer", "store"]
    # 不可变：第一条记录仍记录旧版本快照
    first = next(m for m in st["history"] if m["action"] == "store")
    assert first["to_version_before"] == 1
    assert first["sample_version_before"] == 0

    # 移动历史全局 / 按库位筛选
    assert len(client.get("/api/storage/moves").json()["moves"]) >= 2
    only_a = client.get(f"/api/storage/moves?location_id={a['id']}").json()["moves"]
    assert all(
        m["from_location_id"] == a["id"] or m["to_location_id"] == a["id"] for m in only_a
    )
    assert any(m["sample_id"] == s["id"] and m["action"] == "transfer" for m in only_a)


def test_transfer_stale_source_after_another_move(client):
    s = _new_sample(client)
    a = _new_location(client, capacity=2)
    b = _new_location(client, capacity=2)
    c = _new_location(client, capacity=2)

    assert _move(client, s["id"], a["id"], None, 0, 1).status_code == 201
    # 页面 A 视角：a v2 / sample v1；此时另一会话先把样本转到 b
    assert _move(client, s["id"], b["id"], a["id"], 1, 1, 2).status_code == 201
    # 陈旧页面仍提交 a -> c（源版本也已过期）
    r = _move(client, s["id"], c["id"], a["id"], sv=1, tv=1, fv=2)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "来源" in detail or "版本" in detail
    # 样本停留在 b，c 未被占用，只有 store + transfer 两条记录
    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"]["id"] == b["id"]
    assert len(st["history"]) == 2
    assert client.get(f"/api/storage/locations/{c['id']}").json()["occupied_count"] == 0


def test_transfer_version_conflicts(client):
    s = _new_sample(client)
    a = _new_location(client, capacity=2)
    b = _new_location(client, capacity=2)
    assert _move(client, s["id"], a["id"], None, 0, 1).status_code == 201  # sample v1, a v2

    # 样本版本过期
    r = _move(client, s["id"], b["id"], a["id"], sv=0, tv=1, fv=2)
    assert r.status_code == 409 and "样本版本" in r.json()["detail"]
    # 目标版本过期
    # 先让 b 版本前进（另一样本放入再转走）
    helper = _new_sample(client)
    assert _move(client, helper["id"], b["id"], None, 0, 1).status_code == 201
    assert _move(client, helper["id"], a["id"], b["id"], 1, 2, 2).status_code == 201
    # b 现为 v3；用过期的 tv=1
    r = _move(client, s["id"], b["id"], a["id"], sv=1, tv=1, fv=3)
    assert r.status_code == 409 and "目标库位" in r.json()["detail"]
    # 源版本过期（a 现为 v4）
    r = _move(client, s["id"], b["id"], a["id"], sv=1, tv=3, fv=2)
    assert r.status_code == 409 and "源库位" in r.json()["detail"]

    # 失败后无任何新增记录，样本仍在 a
    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"]["id"] == a["id"]
    assert len(st["history"]) == 1


def test_transfer_same_location_rejected(client):
    s = _new_sample(client)
    a = _new_location(client, capacity=2)
    assert _move(client, s["id"], a["id"], None, 0, 1).status_code == 201
    r = _move(client, s["id"], a["id"], a["id"], sv=1, tv=2, fv=2)
    assert r.status_code == 409
    assert "无需重复" in r.json()["detail"]
    # 占用仍是 1，无新增记录
    assert client.get(f"/api/storage/locations/{a['id']}").json()["occupied_count"] == 1
    assert len(client.get(f"/api/samples/{s['id']}/storage").json()["history"]) == 1


def test_move_payload_validation(client):
    s = _new_sample(client)
    a = _new_location(client, capacity=2)
    base = {
        "to_location_id": a["id"],
        "expected_sample_version": 0,
        "expected_to_version": 1,
        "operator": "x",
    }
    # 转移缺源版本 -> 422
    r = client.post(
        f"/api/samples/{s['id']}/storage/move",
        json={**base, "from_location_id": a["id"]},
    )
    assert r.status_code == 422
    # 首次放入却带源版本 -> 422
    r = client.post(
        f"/api/samples/{s['id']}/storage/move",
        json={**base, "expected_from_version": 1},
    )
    assert r.status_code == 422
    # 操作人空 -> 422
    r = client.post(
        f"/api/samples/{s['id']}/storage/move",
        json={**base, "operator": "   "},
    )
    assert r.status_code == 422
    # 不存在的样本 / 目标库位 -> 404
    r = client.post(
        "/api/samples/9999999/storage/move", json=base
    )
    assert r.status_code == 404
    r = client.post(
        f"/api/samples/{s['id']}/storage/move",
        json={**base, "to_location_id": 9999999},
    )
    assert r.status_code == 404


# ---------- 容量 ----------

def test_fill_to_capacity_then_reject(client):
    cap = 2
    loc = _new_location(client, capacity=cap)
    s1 = _new_sample(client)
    s2 = _new_sample(client)
    s3 = _new_sample(client)
    assert _move(client, s1["id"], loc["id"], None, 0, 1).status_code == 201
    assert _move(client, s2["id"], loc["id"], None, 0, 2).status_code == 201

    detail = client.get(f"/api/storage/locations/{loc['id']}").json()
    assert detail["occupied_count"] == 2
    assert detail["remaining"] == 0
    assert detail["version"] == 3

    # 第三个样本：满员 409
    r = _move(client, s3["id"], loc["id"], None, 0, 3)
    assert r.status_code == 409
    assert "已满" in r.json()["detail"]

    # 转出一个后重新放入成功（占用数同步）
    other = _new_location(client, capacity=2)
    assert _move(client, s1["id"], other["id"], loc["id"], 1, 1, 3).status_code == 201
    # loc 回到 2/2 空位 1（version 4），s3 可放入
    assert _move(client, s3["id"], loc["id"], None, 0, 4).status_code == 201
    detail = client.get(f"/api/storage/locations/{loc['id']}").json()
    assert detail["occupied_count"] == 2
    assert {o["sample_id"] for o in detail["occupants"]} == {s2["id"], s3["id"]}


# ---------- 并发争抢最后空位 ----------

def test_concurrent_race_for_last_slot_only_one_wins(client):
    """容量 1 的库位，N 个库外样本并发放入：仅 1 个成功，无超容量 / 重复占位。"""
    loc = _new_location(client, capacity=1)
    samples = [_new_sample(client) for _ in range(6)]

    results = []
    lock = threading.Lock()

    def worker(sample):
        # 所有页面都看到空库位 v1、样本 v0
        resp = _move(client, sample["id"], loc["id"], None, sv=0, tv=1)
        with lock:
            results.append((sample["id"], resp.status_code))

    threads = [threading.Thread(target=worker, args=(s,)) for s in samples]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for _, code in results)
    assert statuses.count(201) == 1, results
    assert statuses.count(409) == len(samples) - 1, results

    detail = client.get(f"/api/storage/locations/{loc['id']}").json()
    assert detail["occupied_count"] == 1
    assert detail["remaining"] == 0
    assert len(detail["occupants"]) == 1
    winner = detail["occupants"][0]["sample_id"]

    # 每个样本至多占一个库位：本批样本中仅胜者有占位（其他测试 / 演示样本不计）
    ids = {s["id"] for s in samples}
    stored_samples = set()
    for l in client.get("/api/storage/locations").json()["locations"]:
        for o in l["occupants"]:
            if o["sample_id"] in ids:
                stored_samples.add(o["sample_id"])
    assert stored_samples == {winner}
    # 胜者在全库位中也只出现一次
    winner_count = sum(
        1
        for l in client.get("/api/storage/locations").json()["locations"]
        for o in l["occupants"]
        if o["sample_id"] == winner
    )
    assert winner_count == 1

    # 移动记录恰好一条成功写入（无半条 / 重复记录）
    moves = client.get(
        f"/api/storage/moves?location_id={loc['id']}&limit=500"
    ).json()["moves"]
    stores_into = [m for m in moves if m["to_location_id"] == loc["id"] and m["action"] == "store"]
    assert len(stores_into) == 1

    # 数据库 CHECK 约束真实存在：手工超容量写入必须失败
    conn = get_connection()
    try:
        with pytest.raises(Exception):
            conn.execute(
                "UPDATE storage_locations SET occupied_count = 2 WHERE id = ?",
                (loc["id"],),
            )
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


def test_concurrent_transfers_same_sample_no_duplicate(client):
    """同一样本并发转移到两个不同库位：仅一个成功，样本仍只占一个库位。"""
    s = _new_sample(client)
    a = _new_location(client, capacity=2)
    b = _new_location(client, capacity=2)
    c = _new_location(client, capacity=2)
    assert _move(client, s["id"], a["id"], None, 0, 1).status_code == 201  # sample v1, a v2

    results = []
    lock = threading.Lock()

    def worker(target_id, target_v):
        resp = _move(client, s["id"], target_id, a["id"], sv=1, tv=target_v, fv=2)
        with lock:
            results.append(resp.status_code)

    threads = [
        threading.Thread(target=worker, args=(b["id"], 1)),
        threading.Thread(target=worker, args=(c["id"], 1)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], results
    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"]["id"] in (b["id"], c["id"])
    # 恰好一次转移记录，源 a 只释放一次（占用 0），目标合计占用 1
    transfers = [m for m in st["history"] if m["action"] == "transfer"]
    assert len(transfers) == 1
    assert client.get(f"/api/storage/locations/{a['id']}").json()["occupied_count"] == 0
    occupied = sum(
        client.get(f"/api/storage/locations/{x['id']}").json()["occupied_count"]
        for x in (b, c)
    )
    assert occupied == 1


# ---------- 事务回滚（无半条记录） ----------

def test_rolled_back_transaction_leaves_no_trace(client):
    """直接在连接内执行移动核心后回滚（模拟提交前失败）：占位 / 占用数 /
    移动记录全部不存在，验证事务原子性。"""
    s = _new_sample(client)
    loc = _new_location(client, capacity=2)

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = _move_locked(
            conn,
            sample_id=s["id"],
            to_location_id=loc["id"],
            from_location_id=None,
            operator="回滚测试",
            moved_at="2026-09-20 12:00:00",
            note="随后回滚",
            expected_sample_version=0,
            expected_from_version=None,
            expected_to_version=1,
        )
        assert result["to"]["occupied_count"] == 1  # 事务内可见
        conn.rollback()  # 模拟进程崩溃 / 提交失败
    finally:
        conn.close()

    # 回滚后：无占位、占用数仍为 0、移动记录不存在
    st = client.get(f"/api/samples/{s['id']}/storage").json()
    assert st["current"] is None
    assert st["sample_version"] == 0
    assert st["history"] == []
    detail = client.get(f"/api/storage/locations/{loc['id']}").json()
    assert detail["occupied_count"] == 0
    assert detail["version"] == 1

    # 回滚后用相同版本可正常重试（未被「半条记录」污染）
    r = _move(client, s["id"], loc["id"], None, 0, 1)
    assert r.status_code == 201
    assert client.get(f"/api/storage/locations/{loc['id']}").json()["occupied_count"] == 1


def test_failed_request_leaves_consistent_counts(client):
    """连续触发多种 409 后，库位占用数与占位行数始终一致（无重复占位）。"""
    s = _new_sample(client)
    a = _new_location(client, capacity=1)
    b = _new_location(client, capacity=1)
    # 装满 a
    assert _move(client, s["id"], a["id"], None, 0, 1).status_code == 201
    # 各种失败：满员、版本错、来源错
    extra = _new_sample(client)
    for payload_kwargs in (
        dict(to=a["id"], frm=None, sv=0, tv=2),          # 已满
        dict(to=b["id"], frm=a["id"], sv=0, tv=1, fv=2),  # 样本版本错
        dict(to=b["id"], frm=b["id"], sv=1, tv=1, fv=1),  # 来源错
    ):
        r = _move(client, s["id"] if payload_kwargs["frm"] is not None else extra["id"],
                  payload_kwargs["to"], payload_kwargs["frm"],
                  payload_kwargs["sv"], payload_kwargs["tv"], payload_kwargs.get("fv"))
        assert r.status_code == 409, r.text

    conn = get_connection()
    try:
        # 占用数与实际占位数一致
        for loc_id in (a["id"], b["id"]):
            declared = conn.execute(
                "SELECT occupied_count FROM storage_locations WHERE id = ?", (loc_id,)
            ).fetchone()[0]
            actual = conn.execute(
                "SELECT COUNT(*) FROM sample_storage WHERE location_id = ?", (loc_id,)
            ).fetchone()[0]
            assert declared == actual
        # 每个样本至多一行占位
        rows = conn.execute(
            "SELECT sample_id, COUNT(*) c FROM sample_storage GROUP BY sample_id HAVING c > 1"
        ).fetchall()
        assert rows == []
    finally:
        conn.close()


# ---------- 样本删除联动 ----------

def test_delete_sample_releases_location(client):
    s = _new_sample(client)
    loc = _new_location(client, capacity=1)
    assert _move(client, s["id"], loc["id"], None, 0, 1).status_code == 201
    assert client.get(f"/api/storage/locations/{loc['id']}").json()["occupied_count"] == 1

    # 删除样本：占用数同步释放，移动记录随样本级联清除
    assert client.delete(f"/api/samples/{s['id']}").status_code == 204
    detail = client.get(f"/api/storage/locations/{loc['id']}").json()
    assert detail["occupied_count"] == 0
    assert detail["occupants"] == []
    moves = client.get(
        f"/api/storage/moves?location_id={loc['id']}"
    ).json()["moves"]
    assert moves == []
    assert client.get(f"/api/samples/{s['id']}/storage").status_code == 404

    # 释放后空位可再用
    other = _new_sample(client)
    assert _move(client, other["id"], loc["id"], None, 0, 2).status_code == 201
    assert client.get(f"/api/storage/locations/{loc['id']}").json()["occupied_count"] == 1


def test_demo_seed_data_present(client):
    """演示库位与「放入 -> 转移」历史开箱可见。"""
    locs = {l["code"]: l for l in client.get("/api/storage/locations").json()["locations"]}
    assert "LOC-FRZ-01" in locs and "LOC-FRZ-02" in locs
    frz1 = locs["LOC-FRZ-01"]
    assert frz1["occupied_count"] == frz1["capacity"] == 3
    assert frz1["remaining"] == 0
    samples = {s["code"]: s for s in client.get("/api/samples").json()}
    child_a = samples["SAM-DEMO-001-A"]
    st = client.get(f"/api/samples/{child_a['id']}/storage").json()
    assert st["current"]["code"] == "LOC-FRZ-02"
    actions = [m["action"] for m in st["history"]]
    assert "store" in actions and "transfer" in actions
    assert all(m["integrity_ok"] for m in st["history"])
