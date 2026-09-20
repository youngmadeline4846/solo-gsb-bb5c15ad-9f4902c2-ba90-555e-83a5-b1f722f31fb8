"""样本分装谱系测试。

覆盖：
* 首次分装前设置初始数量 / 单位（仅一次、版本乐观锁、已设置后再改 409）；
* 单事务分装：编号唯一（请求内 / 全局）、单位继承一致、分装量为正、
  合计不超过可用余量、精确十进制（0.1 + 0.2）；
* 成功后创建子样本、扣减父样本余量、保存不可变批次（SHA-256 快照校验）；
* 子样本可继续分装（多级树）；父子树返回来源 / 批次 / 分装量 / 剩余量；
* 必须携带父样本版本：重复提交（编号已占用）/ 版本过期返回 409，
  且不产生任何部分记录；
* 批次详情接口（含完整性校验）；
* 已有后代（或本身由批次产生）的样本禁止删除（409）；
* 输入形态错误（非数字 / 零 / 负 / 浮点 / 空 items）返回 422。
"""
from __future__ import annotations

import os
import tempfile
import threading
import itertools

_tmp = tempfile.mkdtemp(prefix="audit-aliquot-tests-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmp, "test_aliquot.db")

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


_seq = itertools.count(1)


def _new_sample(client, prefix="UT-ALQ"):
    code = f"{prefix}-{next(_seq):03d}"
    r = client.post("/api/samples", json={"code": code, "name": "分装测试样本", "description": ""})
    assert r.status_code == 201, r.text
    return r.json()


def _set_qty(client, sid, amount="100", unit="mL", version=0):
    return client.put(
        f"/api/samples/{sid}/quantity",
        json={"initial_quantity": amount, "unit": unit, "expected_version": version},
    )


def _aliquot(client, sid, rows, version, operator="测试员", at=None, note=""):
    items = [
        {"code": c, "quantity": q, **({"name": n} if n else {})}
        for c, n, q in rows
    ]
    payload = {
        "items": items,
        "operator": operator,
        "expected_version": version,
        "note": note,
    }
    if at:
        payload["aliquot_at"] = at
    return client.post(f"/api/samples/{sid}/aliquots", json=payload)


# ---------- 初始数量 / 单位 ----------

def test_set_initial_quantity_once_with_version(client):
    s = _new_sample(client)
    sid = s["id"]

    # 未设置时分装 -> 409
    r = _aliquot(client, sid, [(f"{s['code']}-C1", "", "10")], 0)
    assert r.status_code == 409
    assert "初始数量" in r.json()["detail"]

    # 版本错误 -> 409，不写入
    bad = _set_qty(client, sid, "100", "mL", version=5)
    assert bad.status_code == 409
    detail = client.get(f"/api/samples/{sid}").json()
    assert detail["sample"]["initial_quantity"] is None

    # 正常设置（v0 -> v1）
    ok = _set_qty(client, sid, "100", "mL", version=0)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["initial_quantity"] == "100"
    assert body["remaining_quantity"] == "100"
    assert body["unit"] == "mL"
    assert body["version"] == 1

    # 仅可设置一次：再次设置 -> 409
    again = _set_qty(client, sid, "200", "g", version=1)
    assert again.status_code == 409
    detail = client.get(f"/api/samples/{sid}").json()
    assert detail["sample"]["initial_quantity"] == "100"
    assert detail["sample"]["unit"] == "mL"


def test_quantity_input_validation(client):
    s = _new_sample(client)
    sid = s["id"]
    for bad in ("0", "-1", "abc", "0.000", "1e3"):
        r = _set_qty(client, sid, bad, "mL", version=0)
        assert r.status_code == 422, (bad, r.status_code)
    # float 不允许（避免二进制误差）
    r = client.put(
        f"/api/samples/{sid}/quantity",
        json={"initial_quantity": 1.5, "unit": "mL", "expected_version": 0},
    )
    assert r.status_code == 422
    # 单位为空 / 过长
    assert _set_qty(client, sid, "10", "", 0).status_code == 422
    assert _set_qty(client, sid, "10", "x" * 17, 0).status_code == 422
    # 整数（int）允许
    assert _set_qty(client, sid, 10, "支", 0).status_code == 200
    assert client.get(f"/api/samples/{sid}").json()["sample"]["initial_quantity"] == "10"


# ---------- 分装主流程 ----------

def test_aliquot_success_updates_children_parent_and_batch(client):
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "100.00", "mL", 0).status_code == 200

    code_a, code_b = f"{s['code']}-A", f"{s['code']}-B"
    r = _aliquot(
        client, sid,
        [(code_a, "留样 A", "60"), (code_b, "", "30.5")],
        version=1, at="2026-09-20 10:00:00", note="首次分装",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["unit"] == "mL"
    assert body["total_quantity"] == "90.5"
    assert body["remaining_after"] == "9.50"  # 100.00 - 90.50，保留父余量刻度
    assert body["parent_version"] == 2
    assert len(body["items"]) == 2
    assert body["digest"]

    # 子样本已创建：初始数量 = 分装量，继承单位，记录谱系来源
    detail = client.get(f"/api/samples/{sid}").json()
    assert detail["sample"]["remaining_quantity"] == "9.50"
    assert detail["sample"]["version"] == 2
    child_a = next(x for x in client.get("/api/samples").json() if x["code"] == code_a)
    ca = client.get(f"/api/samples/{child_a['id']}").json()["sample"]
    assert ca["initial_quantity"] == "60"
    assert ca["remaining_quantity"] == "60"
    assert ca["unit"] == "mL"
    assert ca["parent_sample_id"] == sid
    assert ca["batch_id"] == body["batch_id"]

    # 批次详情
    b = client.get(f"/api/aliquot-batches/{body['batch_id']}")
    assert b.status_code == 200
    bd = b.json()
    assert bd["batch_no"] == body["batch_no"]
    assert bd["integrity_ok"] is True
    assert bd["total_quantity"] == "90.5"
    assert bd["remaining_before"] == "100.00"
    assert bd["remaining_after"] == "9.50"
    assert bd["parent_version_before"] == 1
    assert {i["child_code"] for i in bd["items"]} == {code_a, code_b}
    # 快照与明细数量一致
    assert {i["quantity"] for i in bd["snapshot"]["items"]} == {"60", "30.5"}

    # 不存在的批次 -> 404
    assert client.get("/api/aliquot-batches/9999999").status_code == 404


def test_exact_decimal_arithmetic(client):
    """0.1 + 0.2 在精确十进制下必须恰好等于 0.3 且不超过余量。"""
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "0.3", "g", 0).status_code == 200
    r = _aliquot(
        client, sid,
        [(f"{s['code']}-D1", "", "0.1"), (f"{s['code']}-D2", "", "0.2")],
        version=1,
    )
    assert r.status_code == 201, r.text
    assert r.json()["total_quantity"] == "0.3"
    assert r.json()["remaining_after"] == "0.0"


def test_aliquot_validation_failures_rollback(client):
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "10", "mL", 0).status_code == 200

    def samples_codes():
        return {x["code"] for x in client.get("/api/samples").json()}

    before = samples_codes()
    from app.db import get_connection

    def batch_count_for(parent):
        conn = get_connection()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM aliquot_batches WHERE parent_sample_id = ?", (parent,)
            ).fetchone()[0]
        finally:
            conn.close()

    assert batch_count_for(sid) == 0

    # 空 items -> 422
    r = client.post(
        f"/api/samples/{sid}/aliquots",
        json={"items": [], "operator": "x", "expected_version": 1},
    )
    assert r.status_code == 422

    # 分装量为零 / 负 / 非数字 -> 422
    for bad in ("0", "-3", "abc"):
        r = _aliquot(client, sid, [(f"{s['code']}-Z", "", bad)], 1)
        assert r.status_code == 422, bad

    # float 分装量 -> 422
    r = client.post(
        f"/api/samples/{sid}/aliquots",
        json={"items": [{"code": f"{s['code']}-F", "quantity": 1.5}],
              "operator": "x", "expected_version": 1},
    )
    assert r.status_code == 422

    # 请求内编号重复 -> 422（pydantic 模型校验）
    r = _aliquot(
        client, sid,
        [("DUP", "", "1"), ("DUP", "", "1")], 1,
    )
    assert r.status_code == 422

    # 与既有样本编号冲突 -> 409（单事务回滚，不产生部分记录）
    existing = next(x for x in client.get("/api/samples").json() if x["code"] == s["code"])
    r = _aliquot(client, sid, [(existing["code"], "", "1"), (f"{s['code']}-NEW", "", "1")], 1)
    assert r.status_code == 409
    after = samples_codes()
    assert f"{s['code']}-NEW" not in after, "冲突时不得创建其他子样本（部分记录）"

    # 合计超过余量 -> 409 且整体回滚
    r = _aliquot(
        client, sid,
        [(f"{s['code']}-X1", "", "9.5"), (f"{s['code']}-X2", "", "1")], 1,
    )
    assert r.status_code == 409
    assert "超过" in r.json()["detail"]
    assert f"{s['code']}-X1" not in samples_codes()

    # 合计等于余量允许成功；之后余量为 0 再分装 -> 409
    ok = _aliquot(client, sid, [(f"{s['code']}-ALL", "", "10")], 1)
    assert ok.status_code == 201
    zero = _aliquot(client, sid, [(f"{s['code']}-NOPE", "", "0.1")], 2)
    assert zero.status_code == 409
    assert f"{s['code']}-NOPE" not in samples_codes()

    # 全部失败过程中没有产生任何批次行（最后只有「合计等于余量」的 1 条）
    assert batch_count_for(sid) == 1
    assert client.get(f"/api/samples/{sid}").json()["sample"]["remaining_quantity"] == "0"


def test_optimistic_version_conflict_and_retry(client):
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "50", "mL", 0).status_code == 200  # v1

    # 第一次成功：v1 -> v2
    r1 = _aliquot(client, sid, [(f"{s['code']}-V1", "", "10")], 1)
    assert r1.status_code == 201

    # 携带过期版本 v1 重复提交相同 / 不同内容 -> 409
    r2 = _aliquot(client, sid, [(f"{s['code']}-V2", "", "10")], 1)
    assert r2.status_code == 409
    assert "版本" in r2.json()["detail"]

    # 用最新版本 v2 重试 -> 成功
    r3 = _aliquot(client, sid, [(f"{s['code']}-V2", "", "10")], 2)
    assert r3.status_code == 201
    assert client.get(f"/api/samples/{sid}").json()["sample"]["remaining_quantity"] == "30"


# ---------- 谱系树 / 继续分装 ----------

def test_lineage_tree_and_continue_from_child(client):
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "100", "mL", 0).status_code == 200
    r = _aliquot(
        client, sid,
        [(f"{s['code']}-A", "", "60"), (f"{s['code']}-B", "", "30")], 1,
    )
    assert r.status_code == 201
    child_a_id = next(
        x for x in r.json()["items"] if x["code"] == f"{s['code']}-A"
    )["child_sample_id"]

    # 父样本视角的树：根 -> A(+via), B；A/B 都是叶
    d = client.get(f"/api/samples/{sid}").json()
    tree = d["lineage"]["tree"]
    assert tree["id"] == sid
    assert {c["code"] for c in tree["children"]} == {f"{s['code']}-A", f"{s['code']}-B"}
    node_a = next(c for c in tree["children"] if c["id"] == child_a_id)
    assert node_a["via_batch"]["batch_id"] == r.json()["batch_id"]
    assert node_a["via_batch"]["aliquot_quantity"] == "60"

    # 从子样本 A 继续分装（子样本 v1）
    g = f"{s['code']}-A-1"
    r2 = _aliquot(client, child_a_id, [(g, "", "20")], 1)
    assert r2.status_code == 201
    assert r2.json()["unit"] == "mL"  # 单位继承自父样本

    # 从孙样本视角 GET，仍返回整棵以根为顶的家族树
    grand = next(x for x in client.get("/api/samples").json() if x["code"] == g)
    d2 = client.get(f"/api/samples/{grand['id']}").json()
    assert d2["lineage"]["root_sample_id"] == sid
    assert d2["lineage"]["current_sample_id"] == grand["id"]

    def find(n, target):
        if n["id"] == target:
            return n
        for c in n["children"]:
            hit = find(c, target)
            if hit:
                return hit
        return None

    root = d2["lineage"]["tree"]
    a_node = find(root, child_a_id)
    assert a_node["remaining_quantity"] == "40"
    g_node = find(root, grand["id"])
    assert g_node["parent_sample_id"] == child_a_id
    assert g_node["via_batch"]["aliquot_quantity"] == "20"


def test_lineage_endpoint(client):
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "10", "g", 0).status_code == 200
    r = _aliquot(client, sid, [(f"{s['code']}-C", "", "4")], 1)
    assert r.status_code == 201
    lr = client.get(f"/api/samples/{sid}/lineage")
    assert lr.status_code == 200
    assert lr.json()["tree"]["children"][0]["code"] == f"{s['code']}-C"
    # 不存在样本 -> 404
    assert client.get("/api/samples/9999999/lineage").status_code == 404


# ---------- 删除保护 ----------

def test_delete_protected_samples(client):
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "10", "mL", 0).status_code == 200
    r = _aliquot(client, sid, [(f"{s['code']}-K", "", "4")], 1)
    assert r.status_code == 201
    child_id = r.json()["items"][0]["child_sample_id"]

    # 父样本已有后代 -> 409
    assert client.delete(f"/api/samples/{sid}").status_code == 409
    assert client.get(f"/api/samples/{sid}").status_code == 200

    # 子样本由批次产生 -> 409
    assert client.delete(f"/api/samples/{child_id}").status_code == 409
    assert client.get(f"/api/samples/{child_id}").status_code == 200

    # 批次仍可查（不可变记录保留）
    assert client.get(f"/api/aliquot-batches/{r.json()['batch_id']}").status_code == 200


# ---------- 并发：重复提交 / 并发变更 ----------

def test_concurrent_submissions_only_one_succeeds(client):
    """两个携带相同父版本的并发分装，只允许一个成功（BEGIN IMMEDIATE + 版本条件更新）。"""
    s = _new_sample(client)
    sid = s["id"]
    assert _set_qty(client, sid, "100", "mL", 0).status_code == 200  # v1

    results: list[int] = []
    lock = threading.Lock()

    def worker(idx):
        # 每个线程用独立连接（TestClient 内部每次请求新建连接）
        payload = {
            "items": [{"code": f"{s['code']}-T{idx}", "quantity": "60"}],
            "operator": f"并发-{idx}",
            "expected_version": 1,
        }
        resp = client.post(f"/api/samples/{sid}/aliquots", json=payload)
        with lock:
            results.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], results
    detail = client.get(f"/api/samples/{sid}").json()
    # 只扣减一次 60
    assert detail["sample"]["remaining_quantity"] == "40"
    assert detail["sample"]["version"] == 2
    codes = {x["code"] for x in client.get("/api/samples").json()}
    children = [c for c in (f"{s['code']}-T0", f"{s['code']}-T1") if c in codes]
    assert len(children) == 1
    # 只有一个批次
    from app.db import get_connection

    conn = get_connection()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM aliquot_batches WHERE parent_sample_id = ?", (sid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
