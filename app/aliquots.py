"""样本分装谱系（sample aliquot lineage）。

每个**样本**在首次分装前必须先设置**初始数量**与**计量单位**（如
``100 / mL``）；此后初始数量与单位不可修改。用户可从任一当前样本发起一次
**分装（批次）**：填写多个子样本编号、（可选）名称与各自的分装量。服务端在
单一写事务（``BEGIN IMMEDIATE``）内以 :class:`decimal.Decimal` **精确十进制**
完成全部校验：

* 子样本编号在全局唯一（请求内重复、与既有样本编号冲突均拒绝）；
* 单位一致（子样本继承父样本单位，请求中不可更改）；
* 每个分装量为严格正数（零 / 负数 / 非数字拒绝）；
* 分装量合计为严格正数且不超过父样本当前可用余量；
* 请求必须携带父样本当前版本号 ``expected_version``（乐观锁）。

校验通过后在**同一事务**内：创建全部子样本（各自初始数量 = 本次分装量）、
按合计扣减父样本余量、写入一条**不可变批次**及其明细，并对批次快照计算
SHA-256 摘要。任何一步失败整体回滚，绝不产生部分记录。重复提交（编号已被
第一次提交占用）或并发变更（版本过期 / 余量不足）一律返回 **409**。

批次（``aliquot_batches``）与明细（``aliquot_items``）一经写入即不可修改：
批次快照包含父样本当时状态与全部子样本分装量，下载 / 查看详情时现场复算
SHA-256 以校验完整性。已有后代（或本身由某批次产生）的样本禁止删除。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation, getcontext
from sqlite3 import Connection, IntegrityError
from typing import Any, Optional

# 十进制运算精度（上限校验在应用层另行处理，这里只保证四则运算与归一化足够）
getcontext().prec = 50

# 数量精度上限：整数位与小数位分别限制，避免无意义的超长数字
MAX_TOTAL_DIGITS = 30
MAX_DECIMAL_PLACES = 10

# 单位长度
MAX_UNIT_LENGTH = 16


class AliquotError(Exception):
    """业务校验失败，由路由层转换为 HTTPException（默认 409）。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 精确十进制 ----------

def clean_unit(unit: str) -> str:
    unit = (unit or "").strip()
    if not unit:
        raise ValueError("计量单位不能为空")
    if len(unit) > MAX_UNIT_LENGTH:
        raise ValueError(f"计量单位最长 {MAX_UNIT_LENGTH} 个字符")
    return unit


def clean_decimal(value: Any, field: str = "数量") -> str:
    """把输入（仅允许 int / str，拒绝 float 二进制误差）归一化为规范十进制字符串。

    * 拒绝 bool / float（float 不允许直接提交，避免 0.1+0.2 类二进制误差）；
    * 必须是有限、严格正数；
    * 总有效位数 ≤ 30、小数位 ≤ 10；
    * 返回 ``str(Decimal.normalize())`` 风格的规范文本（去除多余 0）。
    """
    if isinstance(value, bool):
        raise ValueError(f"{field}必须是正数")
    if isinstance(value, float):
        raise ValueError(f"{field}请以字符串或整数提交精确十进制（不接受浮点数）")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{field}必须是精确十进制字符串或整数")
    if not text:
        raise ValueError(f"{field}不能为空")
    # 拒绝科学计数法（1e3 / 1E-2）：要求显式十进制文本，保证存储与展示可预期
    if "e" in text or "E" in text:
        raise ValueError(f"{field}请使用普通十进制写法（不要使用科学计数法）：{text[:32]}")
    try:
        dec = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field}不是合法的十进制数字：{text[:32]}")
    if not dec.is_finite() or dec.is_nan():
        raise ValueError(f"{field}必须是有限数字")
    if dec <= 0:
        raise ValueError(f"{field}必须大于 0")
    sign, digits, exponent = dec.as_tuple()
    decimal_places = -exponent if exponent < 0 else 0
    integer_digits = len(digits) - decimal_places
    if decimal_places > MAX_DECIMAL_PLACES:
        raise ValueError(f"{field}小数位不能超过 {MAX_DECIMAL_PLACES} 位")
    if integer_digits > MAX_TOTAL_DIGITS:
        raise ValueError(f"{field}整数位不能超过 {MAX_TOTAL_DIGITS} 位")
    return format(dec, "f")


# ---------- 样本状态读取 ----------

def _require_sample(conn: Connection, sample_id: int) -> Any:
    row = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if row is None:
        raise AliquotError("样本不存在", status_code=404)
    return row


def quantity_dict(row: Any) -> dict[str, Any]:
    """样本行中与分装谱系相关的字段（供 main._sample_dict 复用）。"""
    return {
        "initial_quantity": row["initial_quantity"],
        "remaining_quantity": row["remaining_quantity"],
        "unit": row["unit"],
        "parent_sample_id": row["parent_sample_id"],
        "batch_id": row["batch_id"],
        "version": row["version"],
    }


def has_quantity(row: Any) -> bool:
    return row["initial_quantity"] is not None


# ---------- 初始数量 / 单位 ----------

def set_initial_quantity(
    conn: Connection,
    sample_id: int,
    initial_quantity: str,
    unit: str,
    expected_version: int,
) -> dict[str, Any]:
    """首次分装前为父样本设置初始数量与单位（仅允许一次）。

    在 ``BEGIN IMMEDIATE`` 单事务内校验乐观锁版本；设置成功后版本 +1。
    已设置过初始数量、或版本与客户端所见不一致均返回 409，不产生写入。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _require_sample(conn, sample_id)
        if row["version"] != expected_version:
            raise AliquotError(
                f"样本版本已变化（当前 v{row['version']}，提交基于 v{expected_version}），"
                "请刷新后重试"
            )
        if has_quantity(row):
            raise AliquotError(
                "该样本已设置初始数量与单位；初始数量首次分装前仅可设置一次，"
                "之后不可修改"
            )
        cur = conn.execute(
            "UPDATE samples SET initial_quantity = ?, remaining_quantity = ?, "
            "unit = ?, version = version + 1 WHERE id = ? AND version = ?",
            (initial_quantity, initial_quantity, unit, sample_id, expected_version),
        )
        if cur.rowcount != 1:  # 极端并发下条件更新落空
            raise AliquotError("样本版本冲突，请刷新后重试")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"sample_id": sample_id, "initial_quantity": initial_quantity,
            "remaining_quantity": initial_quantity, "unit": unit,
            "version": expected_version + 1}


# ---------- 发起分装 ----------

def _next_batch_no(conn: Connection, parent_id: int, code: str) -> str:
    """批次编号：ALQ-<父样本编号>-<该父样本累计批次数 + 1>。"""
    count = conn.execute(
        "SELECT COUNT(*) FROM aliquot_batches WHERE parent_sample_id = ?",
        (parent_id,),
    ).fetchone()[0]
    safe_code = "".join(ch if ch.isalnum() else "-" for ch in (code or ""))[:40]
    return f"ALQ-{safe_code}-{count + 1:03d}"


def batch_digest(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_aliquot(
    conn: Connection,
    parent_id: int,
    operator: str,
    items: list[dict[str, Any]],
    aliquot_at: Optional[str] = None,
    note: str = "",
    expected_version: Optional[int] = None,
) -> dict[str, Any]:
    """在单一写事务内校验并执行一次分装。

    成功后：创建子样本（各自初始数量 = 分装量）、扣减父样本余量、
    写入不可变批次与明细。任何校验失败整体回滚，不产生部分记录。
    """
    if expected_version is None:
        raise AliquotError("请求必须携带父样本版本号 expected_version")

    conn.execute("BEGIN IMMEDIATE")
    try:
        parent = _require_sample(conn, parent_id)
        # 乐观锁：版本不符即拒绝（重复提交 / 并发变更）
        if parent["version"] != expected_version:
            raise AliquotError(
                f"父样本版本已变化（当前 v{parent['version']}，提交基于 "
                f"v{expected_version}），可能已被他人分装或修改，请刷新后重试"
            )
        if not has_quantity(parent):
            raise AliquotError(
                "该样本尚未设置初始数量与单位，请先在首次分装前设置后再发起分装",
                status_code=409,
            )
        if not items:
            raise AliquotError("请至少填写一个子样本（编号 + 分装量）")

        unit = parent["unit"]
        now = aliquot_at or _now()

        # 1) 逐行校验：编号非空、请求内不重复、分装量为正
        seen: set[str] = set()
        clean_items: list[dict[str, str]] = []
        for idx, item in enumerate(items, start=1):
            code = (item.get("code") or "").strip()
            if not code:
                raise AliquotError(f"第 {idx} 个子样本编号不能为空")
            if code in seen:
                raise AliquotError(f"子样本编号 {code} 在本次请求中重复")
            seen.add(code)
            try:
                amount = clean_decimal(item.get("quantity"), field=f"子样本 {code} 的分装量")
            except ValueError as exc:
                # 输入形态错误（非数字 / 零 / 负数 / 超长）按 422 返回
                raise AliquotError(str(exc), status_code=422)
            name = (item.get("name") or "").strip() or code
            if len(name) > 128:
                raise AliquotError(f"子样本 {code} 的名称最长 128 个字符", status_code=422)
            clean_items.append({"code": code, "name": name, "quantity": amount})

        # 2) 编号全局唯一（与既有样本冲突；含第一次提交已占用 -> 重复提交 409）
        for ci in clean_items:
            exists = conn.execute(
                "SELECT 1 FROM samples WHERE code = ?", (ci["code"],)
            ).fetchone()
            if exists:
                raise AliquotError(f"子样本编号 {ci['code']} 已存在，编号必须全局唯一")

        # 3) 合计为正且不超过可用余量（精确十进制）
        total = Decimal("0")
        for ci in clean_items:
            total += Decimal(ci["quantity"])
        remaining = Decimal(parent["remaining_quantity"])
        if total > remaining:
            raise AliquotError(
                f"分装量合计 {format(total, 'f')} {unit} 超过父样本可用余量 "
                f"{format(remaining, 'f')} {unit}"
            )

        # 4) 创建不可变批次（先建行拿到 batch id；快照在子样本落库后回填，
        #    提交前对外不可见，保证最终行与快照 / 摘要一致）
        batch_no = _next_batch_no(conn, parent_id, parent["code"])
        new_remaining = format(remaining - total, "f")
        batch_cur = conn.execute(
            "INSERT INTO aliquot_batches (batch_no, parent_sample_id, parent_code, "
            "parent_version_before, unit, total_quantity, remaining_before, "
            "remaining_after, operator, aliquot_at, note, item_count, "
            "snapshot, digest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', ?)",
            (
                batch_no, parent_id, parent["code"], expected_version,
                unit, format(total, "f"), parent["remaining_quantity"],
                new_remaining, operator, now, note, len(clean_items), now,
            ),
        )
        batch_id = batch_cur.lastrowid

        # 5) 创建子样本（继承单位；初始数量 = 本次分装量；记录谱系来源）
        child_ids: list[int] = []
        for ci in clean_items:
            cur = conn.execute(
                "INSERT INTO samples (code, name, description, created_at, "
                "initial_quantity, remaining_quantity, unit, version, "
                "parent_sample_id, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    ci["code"], ci["name"],
                    f"由父样本 {parent['code']} 于批次 {batch_no} 分装产生 "
                    f"（分装量 {ci['quantity']} {unit}）",
                    now, ci["quantity"], ci["quantity"], unit, parent_id, batch_id,
                ),
            )
            child_ids.append(cur.lastrowid)

        # 6) 写入批次明细
        for seq, (ci, child_id) in enumerate(zip(clean_items, child_ids), start=1):
            conn.execute(
                "INSERT INTO aliquot_items (batch_id, seq, child_sample_id, "
                "child_code, quantity, unit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, seq, child_id, ci["code"], ci["quantity"], unit, now),
            )

        # 7) 固化不可变快照 + SHA-256 摘要
        snapshot = {
            "batch_no": batch_no,
            "parent_sample_id": parent_id,
            "parent_code": parent["code"],
            "parent_version_before": expected_version,
            "unit": unit,
            "total_quantity": format(total, "f"),
            "remaining_before": parent["remaining_quantity"],
            "remaining_after": new_remaining,
            "operator": operator,
            "aliquot_at": now,
            "note": note,
            "items": [
                {"seq": seq, "child_sample_id": cid,
                 "child_code": ci["code"], "child_name": ci["name"],
                 "quantity": ci["quantity"]}
                for seq, (ci, cid) in enumerate(zip(clean_items, child_ids), start=1)
            ],
        }
        digest = batch_digest(snapshot)
        conn.execute(
            "UPDATE aliquot_batches SET snapshot = ?, digest = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), digest, batch_id),
        )

        # 8) 条件更新扣减父样本余量并递增版本（并发兜底）
        upd = conn.execute(
            "UPDATE samples SET remaining_quantity = ?, version = version + 1 "
            "WHERE id = ? AND version = ?",
            (new_remaining, parent_id, expected_version),
        )
        if upd.rowcount != 1:
            raise AliquotError("父样本版本冲突，请刷新后重试")

        conn.commit()
    except AliquotError:
        conn.rollback()
        raise
    except IntegrityError as exc:
        # 唯一约束等：统一映射为 409（编号冲突 / 批次号冲突），事务整体回滚
        conn.rollback()
        raise AliquotError(f"分装因唯一约束冲突失败：{exc}")
    except Exception:
        conn.rollback()
        raise

    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "parent_sample_id": parent_id,
        "unit": unit,
        "total_quantity": format(total, "f"),
        "remaining_after": new_remaining,
        "parent_version": expected_version + 1,
        "child_sample_ids": child_ids,
        "items": [
            {"seq": seq, "child_sample_id": cid, **ci}
            for seq, (ci, cid) in enumerate(zip(clean_items, child_ids), start=1)
        ],
        "digest": digest,
        "aliquot_at": now,
    }


# ---------- 谱系树 ----------

def _root_id(conn: Connection, sample_id: int) -> int:
    """沿 parent_sample_id 上溯到谱系根（未设置 parent 的最初父样本）。"""
    current = sample_id
    guard = 0
    while True:
        row = conn.execute(
            "SELECT id, parent_sample_id FROM samples WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            raise AliquotError("样本不存在", status_code=404)
        if row["parent_sample_id"] is None:
            return row["id"]
        current = row["parent_sample_id"]
        guard += 1
        if guard > 10000:  # 谱系理论上是树，防御性上限
            raise AliquotError("样本谱系层级异常（存在环）")


def _sample_node(conn: Connection, row: Any) -> dict[str, Any]:
    """谱系树中的一个样本节点（含数量 / 单位 / 来源批次边）。"""
    node = {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "initial_quantity": row["initial_quantity"],
        "remaining_quantity": row["remaining_quantity"],
        "unit": row["unit"],
        "version": row["version"],
        "parent_sample_id": row["parent_sample_id"],
        "batch_id": row["batch_id"],
        "via_batch": None,
        "children": [],
    }
    if row["batch_id"] is not None:
        edge = conn.execute(
            "SELECT b.id, b.batch_no, i.quantity AS aliquot_quantity, i.unit, "
            "b.aliquot_at, b.operator, b.total_quantity "
            "FROM aliquot_items i JOIN aliquot_batches b ON b.id = i.batch_id "
            "WHERE i.child_sample_id = ?",
            (row["id"],),
        ).fetchone()
        if edge is not None:
            node["via_batch"] = {
                "batch_id": edge["id"],
                "batch_no": edge["batch_no"],
                "aliquot_quantity": edge["aliquot_quantity"],
                "unit": edge["unit"],
                "aliquot_at": edge["aliquot_at"],
                "operator": edge["operator"],
            }
    return node


def build_lineage_tree(conn: Connection, sample_id: int) -> dict[str, Any]:
    """返回以谱系根为顶、包含当前样本整个父子家族的树。

    节点含初始数量 / 剩余量 / 单位；非根节点的 ``via_batch`` 给出它由哪个
    批次、以多少分装量分出（页面据此展示来源、批次、分装量与剩余量）。
    """
    root_id = _root_id(conn, sample_id)
    rows = conn.execute(
        "WITH RECURSIVE tree AS ( "
        "  SELECT * FROM samples WHERE id = ? "
        "  UNION ALL "
        "  SELECT s.* FROM samples s JOIN tree t ON s.parent_sample_id = t.id "
        ") SELECT * FROM tree",
        (root_id,),
    ).fetchall()
    by_id = {r["id"]: _sample_node(conn, r) for r in rows}
    root: Optional[dict[str, Any]] = None
    for node in by_id.values():
        pid = node["parent_sample_id"]
        if pid is None or pid not in by_id:
            root = node
        else:
            by_id[pid]["children"].append(node)
    for node in by_id.values():
        node["children"].sort(key=lambda n: (n["batch_id"] or 0, n["id"]))
    if root is None:  # 理论不会发生（至少有当前样本）
        raise AliquotError("谱系数据不完整", status_code=404)
    return {"root_sample_id": root_id, "current_sample_id": sample_id, "tree": root}


# ---------- 批次详情 ----------

def get_batch_detail(conn: Connection, batch_id: int) -> dict[str, Any]:
    """不可变批次详情：批次头 + 子样本明细 + 快照完整性现场校验。"""
    row = conn.execute(
        "SELECT * FROM aliquot_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise AliquotError("分装批次不存在", status_code=404)

    item_rows = conn.execute(
        "SELECT i.*, s.name AS child_name, s.remaining_quantity AS child_remaining "
        "FROM aliquot_items i LEFT JOIN samples s ON s.id = i.child_sample_id "
        "WHERE i.batch_id = ? ORDER BY i.seq",
        (batch_id,),
    ).fetchall()
    items = [
        {
            "seq": r["seq"],
            "child_sample_id": r["child_sample_id"],
            "child_code": r["child_code"],
            "child_name": r["child_name"],
            "quantity": r["quantity"],
            "unit": r["unit"],
            "child_remaining_quantity": r["child_remaining"],
        }
        for r in item_rows
    ]

    parent = conn.execute(
        "SELECT id, code, name, remaining_quantity, unit FROM samples WHERE id = ?",
        (row["parent_sample_id"],),
    ).fetchone()

    snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
    integrity_ok = (
        snapshot is not None and batch_digest(snapshot) == row["digest"]
    )
    return {
        "id": row["id"],
        "batch_no": row["batch_no"],
        "parent_sample_id": row["parent_sample_id"],
        "parent_code": row["parent_code"],
        "parent": (
            {"id": parent["id"], "code": parent["code"], "name": parent["name"],
             "remaining_quantity": parent["remaining_quantity"], "unit": parent["unit"]}
            if parent is not None else None
        ),
        "parent_version_before": row["parent_version_before"],
        "unit": row["unit"],
        "total_quantity": row["total_quantity"],
        "remaining_before": row["remaining_before"],
        "remaining_after": row["remaining_after"],
        "operator": row["operator"],
        "aliquot_at": row["aliquot_at"],
        "note": row["note"],
        "item_count": row["item_count"],
        "digest": row["digest"],
        "created_at": row["created_at"],
        "snapshot": snapshot,
        "integrity_ok": integrity_ok,
        "items": items,
    }


# ---------- 删除保护 ----------

def deletion_blocked_reason(conn: Connection, sample_id: int) -> Optional[str]:
    """已有后代（或本身由批次产生）的样本禁止删除。

    返回非空中文原因即应拒绝删除（409）；返回 None 表示谱系维度允许删除。
    """
    row = conn.execute(
        "SELECT code, parent_sample_id, batch_id FROM samples WHERE id = ?",
        (sample_id,),
    ).fetchone()
    if row is None:
        return None  # 是否存在由路由层 404 判断
    if row["parent_sample_id"] is not None or row["batch_id"] is not None:
        return (
            f"样本 {row['code']} 由分装批次产生（谱系不可变），禁止删除；"
            "如需调整请在父样本链路中处理"
        )
    child_count = conn.execute(
        "SELECT COUNT(*) FROM samples WHERE parent_sample_id = ?", (sample_id,)
    ).fetchone()[0]
    if child_count:
        return (
            f"样本 {row['code']} 已有 {child_count} 个分装后代，"
            "禁止删除（批次记录不可变）"
        )
    return None
