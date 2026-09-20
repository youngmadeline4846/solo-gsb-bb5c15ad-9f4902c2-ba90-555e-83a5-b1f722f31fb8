"""样本库位管理（sample storage locations）。

用户可创建带**唯一编号**与**容量**（可容纳样本数）的库位（冰箱层架、留样柜
格位等），并把样本**放入**库位或在库位间**转移**。每个样本最多占用一个库位：
样本当前位置由 ``sample_storage`` 中以 ``sample_id`` 为主键的唯一一行记录。

一次放入 / 转移在单一写事务（``BEGIN IMMEDIATE``）内完成全部校验与写入：

* 样本当前库位必须与请求声明的 ``from_location_id`` 一致（首次放入为 ``null``），
  样本已被他人移动 → 409（陈旧来源）；
* 目标库位剩余容量必须充足（``occupied_count < capacity``）；
* 必须同时携带样本、源库位、目标库位三者的 ``expected_version``（乐观锁），
  任一版本过期 → 409；
* 源 / 目标相同（重复占位）→ 409。

校验通过后在**同一事务**内：更新样本占位行、目标库位占用数 +1（version +1）、
源库位占用数 -1（version +1）、样本 version +1，并写入一条**不可变移动记录**
（``storage_moves``，含移动前各版本快照与 SHA-256 摘要）。占用数的增减与样本
版本推进都使用带版本与容量条件的 UPDATE（``WHERE version = ?`` 等），任一条件
落空即整体回滚——并发争抢最后空位时只有一个事务能成功，失败绝不产生重复占位、
超容量或半条移动记录。

``storage_moves`` 一经写入即不可修改、不可删除（应用层不提供更新 / 删除接口），
查看时现场复算 SHA-256 给出完整性结论。库位本身也只提供创建 / 查询接口。
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime
from sqlite3 import Connection, IntegrityError
from typing import Any, Optional


class StorageError(Exception):
    """业务校验失败，由路由层转换为 HTTPException（默认 409）。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# 容量上限：仅作输入合理性限制（实际占用数始终以库位 capacity 为准）
MAX_CAPACITY = 100_000

ACTION_STORE = "store"          # 放入（此前不在任何库位）
ACTION_TRANSFER = "transfer"    # 转移（源库位 -> 目标库位）


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 不可变快照摘要 ----------

def move_snapshot(
    *,
    sample_id: int,
    sample_code: str,
    from_location_id: Optional[int],
    from_location_code: Optional[str],
    to_location_id: int,
    to_location_code: str,
    operator: str,
    moved_at: str,
    note: str,
    sample_version_before: int,
    from_version_before: Optional[int],
    to_version_before: int,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "sample_code": sample_code,
        "from_location_id": from_location_id,
        "from_location_code": from_location_code,
        "to_location_id": to_location_id,
        "to_location_code": to_location_code,
        "operator": operator,
        "moved_at": moved_at,
        "note": note,
        "sample_version_before": sample_version_before,
        "from_version_before": from_version_before,
        "to_version_before": to_version_before,
    }


def move_digest(snapshot: dict[str, Any]) -> str:
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- 库位创建 ----------

def create_location(conn: Connection, code: str, name: str, capacity: int) -> dict[str, Any]:
    """创建库位：编号全局唯一、容量为正整数。编号冲突返回 409。"""
    row = conn.execute(
        "SELECT id FROM storage_locations WHERE code = ?", (code,)
    ).fetchone()
    if row is not None:
        raise StorageError(f"库位编号 {code} 已存在，库位编号必须唯一")
    try:
        cur = conn.execute(
            "INSERT INTO storage_locations (code, name, capacity, occupied_count, "
            "version, created_at) VALUES (?, ?, ?, 0, 1, ?)",
            (code, name, int(capacity), _now()),
        )
        conn.commit()
    except IntegrityError as exc:
        # 并发下另一请求抢先占用同一编号
        raise StorageError(f"库位编号 {code} 已存在，库位编号必须唯一（{exc}）")
    return get_location_detail(conn, cur.lastrowid)


# ---------- 视图序列化 ----------

def _location_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "capacity": row["capacity"],
        "occupied_count": row["occupied_count"],
        "remaining": row["capacity"] - row["occupied_count"],
        "version": row["version"],
        "created_at": row["created_at"],
    }


def _occupants(conn: Connection, location_id: int) -> list[dict[str, Any]]:
    """库位占用清单：当前位于该库位的全部样本。"""
    rows = conn.execute(
        "SELECT ss.sample_id AS sample_id, s.code AS code, s.name AS name, "
        "ss.placed_at AS placed_at, ss.placed_by AS placed_by, ss.last_move_id AS last_move_id "
        "FROM sample_storage ss JOIN samples s ON s.id = ss.sample_id "
        "WHERE ss.location_id = ? ORDER BY ss.placed_at, ss.sample_id",
        (location_id,),
    ).fetchall()
    return [
        {
            "sample_id": r["sample_id"],
            "code": r["code"],
            "name": r["name"],
            "placed_at": r["placed_at"],
            "placed_by": r["placed_by"],
            "last_move_id": r["last_move_id"],
        }
        for r in rows
    ]


def get_location_detail(conn: Connection, location_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM storage_locations WHERE id = ?", (location_id,)
    ).fetchone()
    if row is None:
        raise StorageError("库位不存在", status_code=404)
    detail = _location_dict(row)
    detail["occupants"] = _occupants(conn, location_id)
    return detail


def list_locations(conn: Connection) -> dict[str, Any]:
    """全部库位 + 各自占用清单 + 总览统计。"""
    rows = conn.execute(
        "SELECT * FROM storage_locations ORDER BY code, id"
    ).fetchall()
    locations = []
    total_capacity = total_occupied = 0
    for r in rows:
        d = _location_dict(r)
        d["occupants"] = _occupants(conn, r["id"])
        locations.append(d)
        total_capacity += d["capacity"]
        total_occupied += d["occupied_count"]
    return {
        "locations": locations,
        "summary": {
            "location_count": len(locations),
            "capacity": total_capacity,
            "occupied": total_occupied,
            "remaining": total_capacity - total_occupied,
        },
    }


def _move_dict(row: Any) -> dict[str, Any]:
    d = {
        "id": row["id"],
        "sample_id": row["sample_id"],
        "sample_code": row["sample_code"],
        "action": row["action"],
        "from_location_id": row["from_location_id"],
        "from_location_code": row["from_location_code"],
        "to_location_id": row["to_location_id"],
        "to_location_code": row["to_location_code"],
        "operator": row["operator"],
        "moved_at": row["moved_at"],
        "note": row["note"],
        "sample_version_before": row["sample_version_before"],
        "from_version_before": row["from_version_before"],
        "to_version_before": row["to_version_before"],
        "created_at": row["created_at"],
        "digest": row["digest"],
    }
    # 不可变记录：现场复算快照 SHA-256
    snapshot = json.loads(row["snapshot"]) if row["snapshot"] else None
    d["integrity_ok"] = snapshot is not None and move_digest(snapshot) == row["digest"]
    return d


_MOVE_SELECT = (
    "SELECT * FROM storage_moves "
)


def _query_moves(
    conn: Connection,
    limit: int = 100,
    sample_id: Optional[int] = None,
    location_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if sample_id is not None:
        where.append("sample_id = ?")
        params.append(sample_id)
    if location_id is not None:
        where.append("(from_location_id = ? OR to_location_id = ?)")
        params.extend([location_id, location_id])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = conn.execute(
        _MOVE_SELECT + clause + " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_move_dict(r) for r in rows]


def list_moves(
    conn: Connection,
    limit: int = 100,
    sample_id: Optional[int] = None,
    location_id: Optional[int] = None,
) -> dict[str, Any]:
    return {"moves": _query_moves(conn, limit, sample_id, location_id), "limit": limit}


def get_sample_storage(conn: Connection, sample_id: int) -> dict[str, Any]:
    """样本当前位置（含库位详情）+ 该样本全部不可变移动记录。"""
    sample = conn.execute(
        "SELECT id, code, name, version FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    if sample is None:
        raise StorageError("样本不存在", status_code=404)
    slot = conn.execute(
        "SELECT location_id, placed_at, placed_by, last_move_id "
        "FROM sample_storage WHERE sample_id = ?",
        (sample_id,),
    ).fetchone()
    current = None
    if slot is not None:
        loc = conn.execute(
            "SELECT * FROM storage_locations WHERE id = ?", (slot["location_id"],)
        ).fetchone()
        # 库位理论上不会缺失（库位无删除接口，外键 RESTRICT 兜底）
        if loc is not None:
            current = _location_dict(loc)
            current.update(
                {"placed_at": slot["placed_at"], "placed_by": slot["placed_by"],
                 "last_move_id": slot["last_move_id"]}
            )
    return {
        "sample_id": sample_id,
        "sample_code": sample["code"],
        "sample_version": sample["version"],
        "current": current,
        "history": _query_moves(conn, 500, sample_id=sample_id),
    }


# ---------- 放入 / 转移（核心事务） ----------

def _move_locked(
    conn: Connection,
    sample_id: int,
    to_location_id: int,
    from_location_id: Optional[int],
    operator: str,
    moved_at: str,
    note: str,
    expected_sample_version: int,
    expected_from_version: Optional[int],
    expected_to_version: int,
) -> dict[str, Any]:
    """事务核心：调用方必须已持有写锁（``BEGIN IMMEDIATE`` 或外层写事务）。"""
    sample = conn.execute(
        "SELECT id, code, version FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    if sample is None:
        raise StorageError("样本不存在", status_code=404)

    target = conn.execute(
        "SELECT * FROM storage_locations WHERE id = ?", (to_location_id,)
    ).fetchone()
    if target is None:
        raise StorageError("目标库位不存在", status_code=404)

    # 乐观锁：样本与目标库位版本
    if sample["version"] != expected_sample_version:
        raise StorageError(
            f"样本版本已变化（当前 v{sample['version']}，提交基于 "
            f"v{expected_sample_version}），可能已被他人移动或分装，请刷新后重试"
        )
    if target["version"] != expected_to_version:
        raise StorageError(
            f"目标库位 {target['code']} 版本已变化（当前 v{target['version']}，"
            f"提交基于 v{expected_to_version}），占用情况可能已更新，请刷新后重试"
        )

    # 样本当前库位必须与请求声明一致（陈旧来源防护）
    slot = conn.execute(
        "SELECT location_id FROM sample_storage WHERE sample_id = ?", (sample_id,)
    ).fetchone()
    current_from_id = slot["location_id"] if slot is not None else None
    if current_from_id != from_location_id:
        if current_from_id is None:
            cur_desc = "样本当前不在任何库位"
        else:
            r = conn.execute(
                "SELECT code FROM storage_locations WHERE id = ?", (current_from_id,)
            ).fetchone()
            cur_desc = f"样本当前实际位于 {r['code'] if r else current_from_id}"
        if from_location_id is None:
            req_desc = "不在任何库位（首次放入）"
        else:
            r = conn.execute(
                "SELECT code FROM storage_locations WHERE id = ?", (from_location_id,)
            ).fetchone()
            req_desc = f"声明的源库位 {r['code'] if r else from_location_id}"
        raise StorageError(
            f"来源校验失败：{cur_desc}，而请求声明为「{req_desc}」——来源已过期，"
            "请按样本当前位置刷新后重试"
        )

    source = None
    if from_location_id is not None:
        source = conn.execute(
            "SELECT * FROM storage_locations WHERE id = ?", (from_location_id,)
        ).fetchone()
        if source is None:  # 理论不会（占位行外键存在）
            raise StorageError("源库位不存在", status_code=404)
        if expected_from_version is None:
            raise StorageError("转移请求必须携带源库位版本号 expected_from_version")
        if source["version"] != expected_from_version:
            raise StorageError(
                f"源库位 {source['code']} 版本已变化（当前 v{source['version']}，"
                f"提交基于 v{expected_from_version}），请刷新后重试"
            )
        if from_location_id == to_location_id:
            raise StorageError(
                f"样本已在库位 {source['code']} 中，无需重复放入（目标与源库位相同）"
            )

    # 目标剩余容量（友好预检；最终以条件 UPDATE 兜底）
    if target["occupied_count"] >= target["capacity"]:
        raise StorageError(
            f"目标库位 {target['code']} 已满（容量 {target['capacity']}，"
            f"已占用 {target['occupied_count']}），请选择其他库位或等待空位"
        )

    action = ACTION_STORE if source is None else ACTION_TRANSFER

    # 1) 更新样本占位（每个样本至多一行：sample_id 为主键）
    if slot is None:
        try:
            conn.execute(
                "INSERT INTO sample_storage (sample_id, location_id, placed_at, "
                "placed_by, last_move_id, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
                (sample_id, to_location_id, moved_at, operator, moved_at),
            )
        except IntegrityError:
            # 并发兜底：写锁内理论不会发生
            raise StorageError("样本已在其他事务中被放入库位，请刷新后重试")
    else:
        conn.execute(
            "UPDATE sample_storage SET location_id = ?, placed_at = ?, "
            "placed_by = ?, last_move_id = NULL WHERE sample_id = ?",
            (to_location_id, moved_at, operator, sample_id),
        )

    # 2) 目标库位占用 +1、版本 +1：条件 UPDATE 同时校验版本与剩余容量。
    #    并发争抢最后空位时，落败者匹配 0 行 -> 409 整体回滚。
    upd_target = conn.execute(
        "UPDATE storage_locations SET occupied_count = occupied_count + 1, "
        "version = version + 1 WHERE id = ? AND version = ? "
        "AND occupied_count < capacity",
        (to_location_id, expected_to_version),
    )
    if upd_target.rowcount != 1:
        raise StorageError(
            f"目标库位 {target['code']} 已无剩余容量或版本冲突，转移被拒绝"
        )

    # 3) 源库位占用 -1、版本 +1（仅转移）
    if source is not None:
        upd_source = conn.execute(
            "UPDATE storage_locations SET occupied_count = occupied_count - 1, "
            "version = version + 1 WHERE id = ? AND version = ? "
            "AND occupied_count > 0",
            (from_location_id, expected_from_version),
        )
        if upd_source.rowcount != 1:
            raise StorageError("源库位版本或占用数异常，转移被拒绝")

    # 4) 样本版本 +1（乐观锁条件更新兜底）
    upd_sample = conn.execute(
        "UPDATE samples SET version = version + 1 WHERE id = ? AND version = ?",
        (sample_id, expected_sample_version),
    )
    if upd_sample.rowcount != 1:
        raise StorageError("样本版本冲突，转移被拒绝")

    # 5) 写入不可变移动记录（先建行使摘要与自增 id 互相独立；快照不含 id）
    snapshot = move_snapshot(
        sample_id=sample_id,
        sample_code=sample["code"],
        from_location_id=from_location_id,
        from_location_code=source["code"] if source is not None else None,
        to_location_id=to_location_id,
        to_location_code=target["code"],
        operator=operator,
        moved_at=moved_at,
        note=note,
        sample_version_before=expected_sample_version,
        from_version_before=expected_from_version,
        to_version_before=expected_to_version,
    )
    digest = move_digest(snapshot)
    mcur = conn.execute(
        "INSERT INTO storage_moves (sample_id, sample_code, action, "
        "from_location_id, from_location_code, to_location_id, to_location_code, "
        "operator, moved_at, note, sample_version_before, from_version_before, "
        "to_version_before, snapshot, digest, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sample_id, sample["code"], action,
            from_location_id, snapshot["from_location_code"],
            to_location_id, target["code"],
            operator, moved_at, note,
            expected_sample_version, expected_from_version, expected_to_version,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            digest, moved_at,
        ),
    )
    move_id = mcur.lastrowid
    # 回填占位行指向的最新移动记录（不修改不可变移动表本身）
    conn.execute(
        "UPDATE sample_storage SET last_move_id = ? WHERE sample_id = ?",
        (move_id, sample_id),
    )

    move_row = conn.execute(
        "SELECT * FROM storage_moves WHERE id = ?", (move_id,)
    ).fetchone()

    if source is not None:
        src_row = conn.execute(
            "SELECT * FROM storage_locations WHERE id = ?", (from_location_id,)
        ).fetchone()
        from_view = _location_dict(src_row)
    else:
        from_view = None
    to_row = conn.execute(
        "SELECT * FROM storage_locations WHERE id = ?", (to_location_id,)
    ).fetchone()
    return {
        "move": _move_dict(move_row),
        "sample": {
            "id": sample_id,
            "code": sample["code"],
            "version": expected_sample_version + 1,
            "location_id": to_location_id,
        },
        "from": from_view,
        "to": _location_dict(to_row),
    }


def move_sample(
    conn: Connection,
    sample_id: int,
    to_location_id: int,
    from_location_id: Optional[int],
    operator: str,
    moved_at: Optional[str],
    note: str,
    expected_sample_version: int,
    expected_from_version: Optional[int],
    expected_to_version: int,
) -> dict[str, Any]:
    """在单一 ``BEGIN IMMEDIATE`` 事务内校验并执行一次放入 / 转移。

    任何校验失败整体回滚：不改动占用数、不留下半条移动记录。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _move_locked(
            conn,
            sample_id=sample_id,
            to_location_id=to_location_id,
            from_location_id=from_location_id,
            operator=operator,
            moved_at=moved_at or _now(),
            note=note,
            expected_sample_version=expected_sample_version,
            expected_from_version=expected_from_version,
            expected_to_version=expected_to_version,
        )
        conn.commit()
    except StorageError:
        conn.rollback()
        raise
    except IntegrityError as exc:
        conn.rollback()
        raise StorageError(f"库位移动因约束冲突失败：{exc}")
    except Exception:
        conn.rollback()
        raise
    return result


# ---------- 样本删除联动 ----------

def detach_sample_storage(conn: Connection, sample_id: int) -> None:
    """删除样本前解除占位并同步占用数（移动记录随样本外键级联清除）。

    与删除路由处于同一事务：先扣减库位占用数、再删占位行，随后样本删除。
    """
    slot = conn.execute(
        "SELECT location_id FROM sample_storage WHERE sample_id = ?", (sample_id,)
    ).fetchone()
    if slot is None:
        return
    conn.execute(
        "UPDATE storage_locations SET occupied_count = occupied_count - 1 "
        "WHERE id = ? AND occupied_count > 0",
        (slot["location_id"],),
    )
    conn.execute("DELETE FROM sample_storage WHERE sample_id = ?", (sample_id,))
