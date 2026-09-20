"""SQLite 数据访问层。

启动时调用 ``init_db()`` 建表并写入演示数据，保证开箱即可看到
断档 / 重叠 / 逆序三类审计效果。
"""
from __future__ import annotations

import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Iterator

DB_PATH = os.environ.get("AUDIT_DB_PATH", os.path.join(os.path.dirname(__file__), "audit.db"))


def get_connection() -> sqlite3.Connection:
    # check_same_thread=False：FastAPI 异步路由与其同步依赖可能由线程池中的
    # 不同工作线程执行；连接本身是请求级独占（请求结束即提交关闭），跨线程安全。
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 批量导入确认以 BEGIN IMMEDIATE 抢写锁；并发请求等待而非立即报 database is locked，
    # 排队者持锁后会因事件链指纹不匹配而被业务层拒绝（要求重新预览）
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 请求级依赖：请求结束自动提交并关闭连接。"""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    -- 样本分装谱系：初始数量 / 剩余余量以规范十进制文本精确存储（NULL 表示
    -- 尚未在首次分装前设置）；version 为父样本乐观锁，任何成功分装 +1。
    initial_quantity   TEXT,
    remaining_quantity TEXT,
    unit               TEXT,
    version            INTEGER NOT NULL DEFAULT 0,
    -- 谱系来源：由哪个父样本、经哪一批次产生（根样本均为 NULL）。
    -- 外键不加 ON DELETE CASCADE：批次与明细不可变，已有后代 / 由批次产生
    -- 的样本在应用层禁止删除（RESTRICT 兜底）。
    parent_sample_id   INTEGER REFERENCES samples(id),
    batch_id           INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id  INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    custodian  TEXT    NOT NULL,
    start_time TEXT    NOT NULL,
    end_time   TEXT,
    note       TEXT    NOT NULL DEFAULT '',
    seq        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_sample ON events(sample_id);

CREATE TABLE IF NOT EXISTS signoffs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id     INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    signer        TEXT    NOT NULL,
    signed_at     TEXT    NOT NULL,
    remark        TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'active',
    revoke_reason TEXT,
    revoked_at    TEXT,
    snapshot      TEXT    NOT NULL,
    digest        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_signoffs_sample ON signoffs(sample_id);

-- 双向交接确认：审计排序中每对相邻事件一条确认记录（多轮确认产生多行）。
-- from_* 为交出方、to_* 为接收方；事件快照与 digest 在确认当时绑定，
-- 事件后续被编辑/删除或相邻关系变化时仅置 invalidated，原行保留。
-- 事件外键不加 ON DELETE CASCADE：删除事件后失效记录仍需可查。
CREATE TABLE IF NOT EXISTS handovers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id            INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    from_event_id        INTEGER NOT NULL,
    to_event_id          INTEGER NOT NULL,
    status               TEXT    NOT NULL DEFAULT 'active',
    from_name            TEXT,
    from_confirmed_at    TEXT,
    to_name              TEXT,
    to_confirmed_at      TEXT,
    from_event_snapshot  TEXT    NOT NULL,
    to_event_snapshot    TEXT    NOT NULL,
    digest               TEXT    NOT NULL,
    invalidated_reason   TEXT,
    invalidated_at       TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handovers_sample ON handovers(sample_id);
CREATE INDEX IF NOT EXISTS idx_handovers_pair
    ON handovers(from_event_id, to_event_id);

-- 异常处置队列：跨样本的审计发现处置状态。发现身份为 SHA-256 稳定指纹
-- （样本 + 关联事件 + 异常类型 + 时间边界），与列表顺序无关；
-- 链路变更后重算：仍存在沿用状态、消失转已消除、新出现未处理。
-- version 为乐观锁：处置写入必须携带客户端看到的版本，过期返回 409。
-- claimed_by / claimed_at / claim_expires_at 为限时认领租约：未处理发现可被
-- 处置人认领 15 分钟（CLAIM_TTL_SECONDS），租期内其他人认领返回 409；
-- 到期后任何人可接管（版本递增）。异常被系统消除时租约随状态一并失效。
CREATE TABLE IF NOT EXISTS findings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint       TEXT    NOT NULL UNIQUE,
    sample_id         INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
    code              TEXT    NOT NULL,
    kind              TEXT    NOT NULL,
    message           TEXT    NOT NULL,
    event_ids         TEXT    NOT NULL DEFAULT '[]',
    custodians        TEXT    NOT NULL DEFAULT '[]',
    time_from         TEXT,
    time_to           TEXT,
    status            TEXT    NOT NULL DEFAULT 'open',
    handler           TEXT,
    note              TEXT    NOT NULL DEFAULT '',
    claimed_by        TEXT,
    claimed_at        TEXT,
    claim_expires_at  TEXT,
    version           INTEGER NOT NULL DEFAULT 1,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    resolved_at       TEXT,
    eliminated_at     TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_sample ON findings(sample_id);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);

-- 状态迁移记录：首次出现、人工处置、限时认领（认领 / 释放 / 过期接管）
-- 与系统自动迁移（消除 / 复现）全部留痕。event 标明具体迁移事件；
-- from_status / to_status 为发现处置状态（认领类迁移中两者保持一致）。
CREATE TABLE IF NOT EXISTS finding_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id  INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT    NOT NULL,
    event       TEXT    NOT NULL DEFAULT 'state',
    handler     TEXT,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ftrans_finding ON finding_transitions(finding_id);

-- 事件证据附件：内容寻址实体 + 每次上传独立记录。
-- attachment_blobs 为按 SHA-256 摘要寻址的去重实体（相同内容只存一份）；
-- attachments 是每次上传保留的附件记录（原文件名 / 大小 / 上传人 / 时间 /
-- 所属事件），blob_digest 引用实体；删除记录仅解除引用，引用计数归零后
-- 才由应用层清理实体。event_id 外键默认 RESTRICT：有关联附件的事件
-- 不允许直接删除（应用层先以 409 提示处理附件）。
CREATE TABLE IF NOT EXISTS attachment_blobs (
    digest      TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    media_type  TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id           INTEGER NOT NULL REFERENCES events(id),
    blob_digest        TEXT    NOT NULL REFERENCES attachment_blobs(digest),
    filename           TEXT    NOT NULL,
    size               INTEGER NOT NULL,
    media_type         TEXT    NOT NULL,
    uploaded_by        TEXT    NOT NULL,
    uploaded_at        TEXT    NOT NULL,
    status             TEXT    NOT NULL DEFAULT 'ok',
    corruption_reason  TEXT,
    last_verified_at   TEXT,
    created_at         TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_attachments_event ON attachments(event_id);
CREATE INDEX IF NOT EXISTS idx_attachments_digest ON attachments(blob_digest);

-- 样本分装谱系：一次「分装」产生一个不可变批次（aliquot_batches）与多条
-- 明细（aliquot_items）。批次在单一事务内随子样本创建、父样本余量扣减一起
-- 写入；snapshot 固化父样本当时状态与全部分装量，digest 为其 SHA-256。
-- 批次 / 明细一经写入即不可修改、不可删除（应用层不提供更新接口）。
CREATE TABLE IF NOT EXISTS aliquot_batches (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no               TEXT    NOT NULL UNIQUE,
    parent_sample_id       INTEGER NOT NULL REFERENCES samples(id),
    parent_code            TEXT    NOT NULL,
    parent_version_before  INTEGER NOT NULL,
    unit                   TEXT    NOT NULL,
    total_quantity         TEXT    NOT NULL,
    remaining_before       TEXT    NOT NULL,
    remaining_after        TEXT    NOT NULL,
    operator               TEXT    NOT NULL,
    aliquot_at             TEXT    NOT NULL,
    note                   TEXT    NOT NULL DEFAULT '',
    item_count             INTEGER NOT NULL,
    snapshot               TEXT    NOT NULL DEFAULT '',
    digest                 TEXT    NOT NULL DEFAULT '',
    created_at             TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aliquot_batches_parent ON aliquot_batches(parent_sample_id);

CREATE TABLE IF NOT EXISTS aliquot_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES aliquot_batches(id),
    seq              INTEGER NOT NULL,
    child_sample_id  INTEGER NOT NULL UNIQUE REFERENCES samples(id),
    child_code       TEXT    NOT NULL,
    quantity         TEXT    NOT NULL,
    unit             TEXT    NOT NULL,
    created_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_aliquot_items_batch ON aliquot_items(batch_id);

-- 样本库位管理：库位带全局唯一编号与容量；occupied 为在转移事务内维护的
-- 当前占用数（CHECK 兜底不为负），version 为库位级乐观锁，每次占用变化 +1。
CREATE TABLE IF NOT EXISTS locations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    capacity   INTEGER NOT NULL CHECK (capacity > 0),
    occupied   INTEGER NOT NULL DEFAULT 0 CHECK (occupied >= 0),
    version    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL
);

-- 样本当前所在库位：sample_id 为主键，保证每个样本最多占一个库位。
-- 不加 ON DELETE CASCADE：删除样本前应用层要求先移出库位（409），
-- 外键 RESTRICT 兜底，避免占用计数静默失真。
CREATE TABLE IF NOT EXISTS sample_placements (
    sample_id   INTEGER PRIMARY KEY REFERENCES samples(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    placed_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_placements_location ON sample_placements(location_id);

-- 不可变移动记录：放入（from 为 NULL）、库位间转移与移出（to 为 NULL）
-- 一经写入即不可修改、不可删除（应用层不提供更新 / 删除接口）。
-- 样本 / 库位编号以快照随记录保存；样本 id 不加外键，样本删除后历史仍可查。
CREATE TABLE IF NOT EXISTS location_moves (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id             INTEGER NOT NULL,
    sample_code           TEXT    NOT NULL,
    from_location_id      INTEGER,
    from_code             TEXT,
    to_location_id        INTEGER,
    to_code               TEXT,
    operator              TEXT    NOT NULL,
    moved_at              TEXT    NOT NULL,
    note                  TEXT    NOT NULL DEFAULT '',
    sample_version_before INTEGER NOT NULL,
    from_version_before   INTEGER,
    to_version_before     INTEGER,
    created_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_location_moves_sample ON location_moves(sample_id);
CREATE INDEX IF NOT EXISTS idx_location_moves_to ON location_moves(to_location_id);
CREATE INDEX IF NOT EXISTS idx_location_moves_from ON location_moves(from_location_id);
-- idx_samples_parent 在 _migrate() 补齐谱系列之后创建（旧库该列尚不存在）
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对旧版本库做轻量列补齐（CREATE TABLE IF NOT EXISTS 不会改已有表）。

    * findings 增加限时认领租约三列 claimed_by / claimed_at / claim_expires_at；
    * finding_transitions 增加 event 列（state/claim/release/takeover/lease_void）。
    旧迁移记录没有认领事件概念，统一回填为 state。
    """
    finding_cols = {r["name"] for r in conn.execute("PRAGMA table_info(findings)")}
    for name, ddl in (
        ("claimed_by", "ALTER TABLE findings ADD COLUMN claimed_by TEXT"),
        ("claimed_at", "ALTER TABLE findings ADD COLUMN claimed_at TEXT"),
        ("claim_expires_at", "ALTER TABLE findings ADD COLUMN claim_expires_at TEXT"),
    ):
        if name not in finding_cols:
            conn.execute(ddl)
    trans_cols = {r["name"] for r in conn.execute("PRAGMA table_info(finding_transitions)")}
    if "event" not in trans_cols:
        conn.execute("ALTER TABLE finding_transitions ADD COLUMN event TEXT NOT NULL DEFAULT 'state'")
        conn.execute("UPDATE finding_transitions SET event = 'state' WHERE event IS NULL OR event = ''")

    # samples 增加分装谱系列（初始数量 / 剩余量 / 单位 / 乐观锁版本 / 谱系来源）
    sample_cols = {r["name"] for r in conn.execute("PRAGMA table_info(samples)")}
    for name, ddl in (
        ("initial_quantity", "ALTER TABLE samples ADD COLUMN initial_quantity TEXT"),
        ("remaining_quantity", "ALTER TABLE samples ADD COLUMN remaining_quantity TEXT"),
        ("unit", "ALTER TABLE samples ADD COLUMN unit TEXT"),
        ("version", "ALTER TABLE samples ADD COLUMN version INTEGER NOT NULL DEFAULT 0"),
        ("parent_sample_id", "ALTER TABLE samples ADD COLUMN parent_sample_id INTEGER"),
        ("batch_id", "ALTER TABLE samples ADD COLUMN batch_id INTEGER"),
    ):
        if name not in sample_cols:
            conn.execute(ddl)
    # 谱系列补齐后再建索引（新库 SCHEMA 已建表含该列，此处幂等）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_samples_parent ON samples(parent_sample_id)"
    )


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed_demo_data(conn)
    # 确保证据附件持久化目录存在（内容寻址实体的根目录）
    from .attachments import attachment_dir

    attachment_dir()


def _seed_demo_data(conn: sqlite3.Connection) -> None:
    """数据库为空时写入两个演示样本（其中一个故意包含断档与重叠）。"""
    if conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] > 0:
        return

    today = date.today()
    base = datetime.combine(today, datetime.min.time()).replace(hour=9)

    def t(h: float, m: float = 0) -> str:
        return (base + timedelta(hours=h, minutes=m)).strftime("%Y-%m-%d %H:%M:%S")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO samples (code, name, description, created_at) VALUES (?, ?, ?, ?)",
        ("SAM-DEMO-001", "演示样本·异常链路", "用于演示断档、重叠与逆序录入的检测效果", now),
    )
    sid = cur.lastrowid
    rows = [
        ("张三（采集组）", t(0), t(1), "现场采集并密封"),
        ("李四（运输组）", t(1), t(2, 30), "冷链车运输，温度 2-8℃"),
        # 录入顺序逆序（第三条先登记，时间却最早）
        ("赵六（档案室）", t(0, 30), t(0, 45), "事后补录的档案登记——时间早于录入顺序"),
        # 与上一条之间存在 40 分钟无人保管断档
        ("王五（检测组）", t(3, 10), t(4, 30), "实验室接收并检测"),
        # 与王五区间重叠
        ("孙七（留样柜）", t(4), t(5, 15), "检测期间并行留样"),
        # 仍在保管中，未归还
        ("周八（留样柜）", t(5, 15), None, "留样观察中"),
    ]
    event_ids = []
    for seq, (custodian, start, end, note) in enumerate(rows, start=1):
        ins = conn.execute(
            "INSERT INTO events (sample_id, custodian, start_time, end_time, note, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, custodian, start, end, note, seq),
        )
        event_ids.append(ins.lastrowid)

    cur = conn.execute(
        "INSERT INTO samples (code, name, description, created_at) VALUES (?, ?, ?, ?)",
        ("SAM-DEMO-002", "演示样本·完整链路", "各段交接无缝衔接，当前仍在保管中", now),
    )
    sid2 = cur.lastrowid
    rows2 = [
        ("张三（采集组）", t(0), t(1), "现场采集"),
        ("李四（运输组）", t(1), t(2, 30), "冷链运输"),
        ("王五（检测组）", t(2, 30), None, "检测中，尚未归还"),
    ]
    event_ids2 = []
    for seq, (custodian, start, end, note) in enumerate(rows2, start=1):
        ins = conn.execute(
            "INSERT INTO events (sample_id, custodian, start_time, end_time, note, seq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid2, custodian, start, end, note, seq),
        )
        event_ids2.append(ins.lastrowid)

    _seed_handovers(conn, sid, event_ids, rows, sid2, event_ids2, rows2, now)
    _seed_lineage(conn, sid, now)
    _seed_locations(conn, sid2, "SAM-DEMO-002", now)


def _seed_locations(conn: sqlite3.Connection, sample_id: int, sample_code: str, now: str) -> None:
    """预置两个演示库位，并把无缝链路样本放入其一（含一条初始入库移动记录）。"""
    loc_a = conn.execute(
        "INSERT INTO locations (code, name, capacity, occupied, version, created_at) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        ("LOC-A-01", "冷藏柜 A-1（2-8℃）", 8, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO locations (code, name, capacity, occupied, version, created_at) "
        "VALUES (?, ?, ?, 0, 0, ?)",
        ("LOC-B-02", "冷冻柜 B-2（-20℃）", 4, now),
    )
    # 初始入库：占用数、样本当前位置、样本版本与移动记录在同一事务内一致写入
    conn.execute(
        "INSERT INTO sample_placements (sample_id, location_id, placed_at) VALUES (?, ?, ?)",
        (sample_id, loc_a, now),
    )
    conn.execute(
        "UPDATE locations SET occupied = 1, version = 1 WHERE id = ?", (loc_a,)
    )
    conn.execute("UPDATE samples SET version = version + 1 WHERE id = ?", (sample_id,))
    conn.execute(
        "INSERT INTO location_moves (sample_id, sample_code, from_location_id, from_code, "
        "to_location_id, to_code, operator, moved_at, note, sample_version_before, "
        "from_version_before, to_version_before, created_at) "
        "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, 0, ?)",
        (sample_id, sample_code, loc_a, "LOC-A-01", "赵六（留样管理员）", now, "初始入库", now),
    )


def _seed_lineage(conn: sqlite3.Connection, sid: int, now: str) -> None:
    """预置一个两级分装谱系演示：父样本 100 mL -> 两个子样本 -> 一个孙样本。

    与真实分装走同一批校验后的结果一致（批次行 + 明细 + SHA-256 快照），
    但不经 API，直接在初始化事务内写入。
    """
    import json

    from .aliquots import batch_digest

    parent = conn.execute("SELECT code FROM samples WHERE id = ?", (sid,)).fetchone()
    code = parent["code"]

    def insert_batch(parent_id, parent_code, version_before, remaining_before,
                     children, aliquot_at, operator="赵六（留样管理员）", note="演示分装"):
        from decimal import Decimal

        unit = "mL"
        total = sum((Decimal(q) for _, _, q, _ in children), Decimal("0"))
        remaining_after = format(Decimal(remaining_before) - total, "f")
        # 父样本若已有批次，批次数顺延
        existed = conn.execute(
            "SELECT COUNT(*) FROM aliquot_batches WHERE parent_sample_id = ?", (parent_id,)
        ).fetchone()[0]
        batch_no = f"ALQ-{parent_code}-{existed + 1:03d}"

        bcur = conn.execute(
            "INSERT INTO aliquot_batches (batch_no, parent_sample_id, parent_code, "
            "parent_version_before, unit, total_quantity, remaining_before, "
            "remaining_after, operator, aliquot_at, note, item_count, snapshot, "
            "digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_no, parent_id, parent_code, version_before, unit,
                format(total, "f"), remaining_before, remaining_after,
                operator, aliquot_at, note, len(children), "", "", now,
            ),
        )
        batch_id = bcur.lastrowid
        snapshot_items = []
        for seq, (ccode, cname, qty, desc) in enumerate(children, start=1):
            cc = conn.execute(
                "INSERT INTO samples (code, name, description, created_at, "
                "initial_quantity, remaining_quantity, unit, version, "
                "parent_sample_id, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (ccode, cname, desc, aliquot_at, qty, qty, unit, parent_id, batch_id),
            )
            child_id = cc.lastrowid
            conn.execute(
                "INSERT INTO aliquot_items (batch_id, seq, child_sample_id, "
                "child_code, quantity, unit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, seq, child_id, ccode, qty, unit, now),
            )
            snapshot_items.append(
                {"seq": seq, "child_sample_id": child_id, "child_code": ccode,
                 "child_name": cname, "quantity": qty}
            )

        conn.execute(
            "UPDATE samples SET remaining_quantity = ?, version = version + 1 WHERE id = ?",
            (remaining_after, parent_id),
        )
        snapshot = {
            "batch_no": batch_no, "parent_sample_id": parent_id,
            "parent_code": parent_code, "parent_version_before": version_before,
            "unit": unit, "total_quantity": format(total, "f"),
            "remaining_before": remaining_before, "remaining_after": remaining_after,
            "operator": operator, "aliquot_at": aliquot_at, "note": note,
            "items": snapshot_items,
        }
        digest = batch_digest(snapshot)
        conn.execute(
            "UPDATE aliquot_batches SET snapshot = ?, digest = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), digest, batch_id),
        )
        return batch_id

    # 父样本先设置初始数量 100 mL（version 0 -> 1）
    conn.execute(
        "UPDATE samples SET initial_quantity = '100', remaining_quantity = '100', "
        "unit = 'mL', version = 1 WHERE id = ?",
        (sid,),
    )
    t1 = "2026-01-01 15:30:00"
    batch1 = insert_batch(
        sid, code, 1, "100",
        [
            ("SAM-DEMO-001-A", "演示子样本·留样 A", "60", "由父样本 SAM-DEMO-001 分装留样 60 mL"),
            ("SAM-DEMO-001-B", "演示子样本·复检 B", "30", "由父样本 SAM-DEMO-001 分装复检 30 mL"),
        ],
        t1,
    )
    # 子样本 A（初始 60 mL）继续分装出一个孙样本，演示「从子样本继续分装」
    child_a = conn.execute(
        "SELECT id FROM samples WHERE code = 'SAM-DEMO-001-A'"
    ).fetchone()["id"]
    insert_batch(
        child_a, "SAM-DEMO-001-A", 1, "60",
        [
            ("SAM-DEMO-001-A-1", "演示孙样本·上机检测", "20",
             "由子样本 SAM-DEMO-001-A 分装上机检测 20 mL"),
        ],
        "2026-01-01 16:00:00",
        operator="王五（检测组）",
        note="从子样本继续分装",
    )


def _seed_handovers(
    conn: sqlite3.Connection,
    sid: int,
    ids: list[int],
    rows: list[tuple[str, str, str | None, str]],
    sid2: int,
    ids2: list[int],
    rows2: list[tuple[str, str, str | None, str]],
    now: str,
) -> None:
    """预置双向交接确认演示记录（含一条已失效的历史记录）。"""
    from .handovers import (
        ACTIVE,
        INVALIDATED,
        REASON_ADJACENCY_CHANGED,
        event_snapshot,
        pair_digest,
    )

    def snapshot(sample_id: int, event_id: int) -> dict:
        r = conn.execute(
            "SELECT custodian, start_time, end_time, note FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        return event_snapshot(dict(r))

    def insert(sample_id, a, b, from_name, from_at, to_name, to_at,
               status, invalidated_reason=None, invalidated_at=None):
        sa, sb = snapshot(sample_id, a), snapshot(sample_id, b)
        conn.execute(
            "INSERT INTO handovers (sample_id, from_event_id, to_event_id, status, "
            "from_name, from_confirmed_at, to_name, to_confirmed_at, "
            "from_event_snapshot, to_event_snapshot, digest, "
            "invalidated_reason, invalidated_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_id, a, b, status,
                from_name, from_at, to_name, to_at,
                json.dumps(sa, ensure_ascii=False), json.dumps(sb, ensure_ascii=False),
                pair_digest(sa, sb),
                invalidated_reason, invalidated_at, now, now,
            ),
        )

    # 异常链路：按起始时间排序后，孙七（ids[5-1]? 见下）14:15 归还、
    # 周八 14:15 接手，是唯一时间一致的相邻交接 —— 两侧均已确认
    e_sun, e_zhou = ids[4], ids[5]
    insert(
        sid, e_sun, e_zhou,
        "孙七", "2026-01-01 14:16:00",
        "周八", "2026-01-01 14:16:30",
        ACTIVE,
    )
    # 历史失效记录：张三→李四曾在 10:00 完成双向确认，
    # 赵六的补录事件（ids[2]）插入到两人之间后不再相邻，原确认保留为失效
    e_zhang, e_li = ids[0], ids[1]
    insert(
        sid, e_zhang, e_li,
        "张三", "2026-01-01 10:01:00",
        "李四", "2026-01-01 10:01:00",
        INVALIDATED,
        invalidated_reason=REASON_ADJACENCY_CHANGED,
        invalidated_at=now,
    )
    # 完整链路：第一段（张三→李四）两侧均已确认；第二段（李四→王五）待确认
    insert(
        sid2, ids2[0], ids2[1],
        "张三", "2026-01-01 10:02:00",
        "李四", "2026-01-01 10:02:00",
        ACTIVE,
    )
