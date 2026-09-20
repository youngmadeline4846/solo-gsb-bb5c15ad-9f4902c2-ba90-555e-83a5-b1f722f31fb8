"""样本库位管理（storage locations）。

库位（``locations``）带全局唯一编号与容量；样本可**放入**库位、在库位间
**转移**或**移出**（``to_location_id`` 为 NULL），每个样本最多占一个库位
（``sample_placements`` 以 sample_id 为主键）。

转移在单一 ``BEGIN IMMEDIATE`` 写事务内完成全部校验：

* 样本当前库位必须与客户端声明的 ``expected_from_location_id`` 一致
  （陈旧来源拒绝）；
* 目标库位剩余容量必须足够（``occupied < capacity``，并发争抢最后空位
  由条件 UPDATE 兜底）；
* 样本与源 / 目标库位的 ``expected_version`` 必须与当前版本一致（乐观锁）。

成功后在**同一事务**内：源库位占用 -1、目标库位占用 +1（均随版本 +1）、
更新样本当前位置与样本版本，并写入一条**不可变移动记录**
（``location_moves``，无更新 / 删除接口）。并发争抢最后空位、陈旧来源或
版本过期均返回 **409**；任何失败整体回滚，绝不产生重复占位、超容量或
半条记录。
"""
from __future__ import annotations

from datetime import datetime
from sqlite3 import Connection, IntegrityError
from typing import Any, Optional


class LocationError(Exception):
    """业务校验失败，由路由层转换为 HTTPException（默认 409）。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 库位 ----------

def _location_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "capacity": row["capacity"],
        "occupied": row["occupied"],
        "remaining": row["capacity"] - row["occupied"],
        "version": row["version"],
        "created_at": row["created_at"],
    }


def _require_location(conn: Connection, location_id: int) -> Any:
    row = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if row is None:
        raise LocationError("库位不存在", status_code=404)
    return row


def create_location(conn: Connection, code: str, name: str, capacity: int) -> dict[str, Any]:
    """创建库位（编号全局唯一；容量为正整数，由 schema 层校验）。"""
    exists = conn.execute("SELECT 1 FROM locations WHERE code = ?", (code,)).fetchone()
    if exists:
        raise LocationError(f"库位编号 {code} 已存在，编号必须全局唯一")
    now = _now()
    cur = conn.execute(
        "INSERT INTO locations (code, name, capacity, occupied, version, created_at) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        (code, name, capacity, now),
    )
    conn.commit()
    return _location_dict(_require_location(conn, cur.lastrowid))


def list_locations(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM locations ORDER BY code, id").fetchall()
    return [_location_dict(r) for r in rows]


def get_location_detail(conn: Connection, location_id: int) -> dict[str, Any]:
    """库位详情：基本信息 + 当前占用清单 + 涉及该库位的移动历史（近 50 条）。"""
    loc = _location_dict(_require_location(conn, location_id))
    rows = conn.execute(
        "SELECT s.id, s.code, s.name, p.placed_at FROM sample_placements p "
        "JOIN samples s ON s.id = p.sample_id WHERE p.location_id = ? "
        "ORDER BY p.placed_at, s.code",
        (location_id,),
    ).fetchall()
    loc["occupants"] = [
        {"sample_id": r["id"], "code": r["code"], "name": r["name"],
         "placed_at": r["placed_at"]}
        for r in rows
    ]
    loc["moves"] = _moves_for_location(conn, location_id)
    return loc


# ---------- 移动记录 ----------

def _move_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "sample_id": row["sample_id"],
        "sample_code": row["sample_code"],
        "from_location_id": row["from_location_id"],
        "from_code": row["from_code"],
        "to_location_id": row["to_location_id"],
        "to_code": row["to_code"],
        "operator": row["operator"],
        "moved_at": row["moved_at"],
        "note": row["note"],
        "sample_version_before": row["sample_version_before"],
        "from_version_before": row["from_version_before"],
        "to_version_before": row["to_version_before"],
        "created_at": row["created_at"],
    }


def _moves_for_location(conn: Connection, location_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM location_moves WHERE from_location_id = ? OR to_location_id = ? "
        "ORDER BY id DESC LIMIT 50",
        (location_id, location_id),
    ).fetchall()
    return [_move_dict(r) for r in rows]


def list_sample_moves(conn: Connection, sample_id: int) -> list[dict[str, Any]]:
    """样本的移动历史（不可变记录，新的在前）。"""
    rows = conn.execute(
        "SELECT * FROM location_moves WHERE sample_id = ? ORDER BY id DESC",
        (sample_id,),
    ).fetchall()
    return [_move_dict(r) for r in rows]


# ---------- 样本当前位置 ----------

def current_placement(conn: Connection, sample_id: int) -> Optional[dict[str, Any]]:
    """样本当前所在库位（含库位当前版本，供客户端做乐观锁）；未入库返回 None。"""
    row = conn.execute(
        "SELECT p.location_id, p.placed_at, l.code, l.name, l.version AS location_version "
        "FROM sample_placements p JOIN locations l ON l.id = p.location_id "
        "WHERE p.sample_id = ?",
        (sample_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "location_id": row["location_id"],
        "code": row["code"],
        "name": row["name"],
        "placed_at": row["placed_at"],
        "location_version": row["location_version"],
    }


# ---------- 转移（放入 / 转移 / 移出） ----------

def move_sample(
    conn: Connection,
    sample_id: int,
    to_location_id: Optional[int],
    operator: str,
    *,
    expected_sample_version: int,
    expected_from_location_id: Optional[int],
    expected_from_version: Optional[int],
    expected_to_version: Optional[int],
    moved_at: Optional[str] = None,
    note: str = "",
) -> dict[str, Any]:
    """在单一写事务内校验并执行一次库位移动。

    ``to_location_id`` 为 None 表示移出库位（不进入新库位）；源库位由
    ``expected_from_location_id`` 声明（None 表示客户端认为样本未入库）。
    任何校验失败整体回滚，不产生部分记录。
    """
    if to_location_id is not None and expected_to_version is None:
        raise LocationError("转移到目标库位时必须携带目标库位版本 expected_to_version",
                            status_code=422)
    if expected_from_location_id is not None and expected_from_version is None:
        raise LocationError("样本当前在库位中时必须携带源库位版本 expected_from_version",
                            status_code=422)

    conn.execute("BEGIN IMMEDIATE")
    try:
        sample = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
        if sample is None:
            raise LocationError("样本不存在", status_code=404)
        # 乐观锁：样本版本不符即拒绝（重复提交 / 并发变更）
        if sample["version"] != expected_sample_version:
            raise LocationError(
                f"样本版本已变化（当前 v{sample['version']}，提交基于 "
                f"v{expected_sample_version}），可能已被他人转移或修改，请刷新后重试"
            )

        placement = conn.execute(
            "SELECT * FROM sample_placements WHERE sample_id = ?", (sample_id,)
        ).fetchone()
        current_from = placement["location_id"] if placement is not None else None
        # 陈旧来源：客户端声明的当前库位与服务端实际不一致
        if current_from != expected_from_location_id:
            if current_from is None:
                raise LocationError(
                    "样本当前不在任何库位（可能已被他人移出），请刷新后重试"
                )
            raise LocationError(
                "样本当前所在库位与提交时不一致（陈旧来源），请刷新后重试"
            )

        if to_location_id is not None and to_location_id == current_from:
            raise LocationError("样本已在目标库位，无需重复放入（重复占位被拒绝）")

        to_loc = None
        if to_location_id is not None:
            to_loc = conn.execute(
                "SELECT * FROM locations WHERE id = ?", (to_location_id,)
            ).fetchone()
            if to_loc is None:
                raise LocationError("目标库位不存在", status_code=404)
            if to_loc["version"] != expected_to_version:
                raise LocationError(
                    f"目标库位版本已变化（当前 v{to_loc['version']}，提交基于 "
                    f"v{expected_to_version}），请刷新后重试"
                )
            if to_loc["occupied"] >= to_loc["capacity"]:
                raise LocationError(
                    f"目标库位 {to_loc['code']} 已满（{to_loc['occupied']}/"
                    f"{to_loc['capacity']}），没有剩余空位"
                )

        from_loc = None
        if current_from is not None:
            from_loc = conn.execute(
                "SELECT * FROM locations WHERE id = ?", (current_from,)
            ).fetchone()
            if from_loc is None:  # 防御：位置记录引用的库位缺失
                raise LocationError("样本当前所在库位记录异常，请刷新后重试")
            if from_loc["version"] != expected_from_version:
                raise LocationError(
                    f"源库位版本已变化（当前 v{from_loc['version']}，提交基于 "
                    f"v{expected_from_version}），请刷新后重试"
                )

        now = moved_at or _now()

        # 源库位占用 -1（条件更新兜底并发：版本一致且有占用才扣）
        if from_loc is not None:
            cur = conn.execute(
                "UPDATE locations SET occupied = occupied - 1, version = version + 1 "
                "WHERE id = ? AND version = ? AND occupied > 0",
                (from_loc["id"], expected_from_version),
            )
            if cur.rowcount != 1:
                raise LocationError("源库位并发冲突，请刷新后重试")

        # 目标库位占用 +1（条件更新同时锁定版本与剩余容量，杜绝超容量占位）
        if to_loc is not None:
            cur = conn.execute(
                "UPDATE locations SET occupied = occupied + 1, version = version + 1 "
                "WHERE id = ? AND version = ? AND occupied < capacity",
                (to_loc["id"], expected_to_version),
            )
            if cur.rowcount != 1:
                raise LocationError(
                    "目标库位并发冲突（最后空位可能刚被占用），请刷新后重试"
                )

        # 样本当前位置（sample_id 主键保证每样本最多一条，即最多占一个库位）
        if to_loc is not None:
            if placement is None:
                conn.execute(
                    "INSERT INTO sample_placements (sample_id, location_id, placed_at) "
                    "VALUES (?, ?, ?)",
                    (sample_id, to_loc["id"], now),
                )
            else:
                conn.execute(
                    "UPDATE sample_placements SET location_id = ?, placed_at = ? "
                    "WHERE sample_id = ?",
                    (to_loc["id"], now, sample_id),
                )
        else:
            conn.execute(
                "DELETE FROM sample_placements WHERE sample_id = ?", (sample_id,)
            )

        # 样本版本 +1（条件更新兜底）
        cur = conn.execute(
            "UPDATE samples SET version = version + 1 WHERE id = ? AND version = ?",
            (sample_id, expected_sample_version),
        )
        if cur.rowcount != 1:
            raise LocationError("样本版本冲突，请刷新后重试")

        # 不可变移动记录（含提交时各方版本快照）
        cur = conn.execute(
            "INSERT INTO location_moves (sample_id, sample_code, from_location_id, "
            "from_code, to_location_id, to_code, operator, moved_at, note, "
            "sample_version_before, from_version_before, to_version_before, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_id, sample["code"],
                current_from, from_loc["code"] if from_loc is not None else None,
                to_loc["id"] if to_loc is not None else None,
                to_loc["code"] if to_loc is not None else None,
                operator, now, note,
                expected_sample_version,
                expected_from_version if from_loc is not None else None,
                expected_to_version if to_loc is not None else None,
                _now(),
            ),
        )
        move_id = cur.lastrowid
        conn.commit()
    except LocationError:
        conn.rollback()
        raise
    except IntegrityError as exc:
        # 唯一 / 外键约束等：统一映射为 409，事务整体回滚
        conn.rollback()
        raise LocationError(f"库位转移因约束冲突失败：{exc}")
    except Exception:
        conn.rollback()
        raise

    return {
        "move_id": move_id,
        "sample_id": sample_id,
        "sample_version": expected_sample_version + 1,
        "from_location_id": current_from,
        "from_code": from_loc["code"] if from_loc is not None else None,
        "to_location_id": to_loc["id"] if to_loc is not None else None,
        "to_code": to_loc["code"] if to_loc is not None else None,
        "moved_at": now,
    }
