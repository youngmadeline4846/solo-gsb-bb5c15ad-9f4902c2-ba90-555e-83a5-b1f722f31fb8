"""FastAPI 入口：样本 / 交接事件 CRUD、审计、结论签署与报告导出、样本库位管理。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from sqlite3 import Connection
from starlette.background import BackgroundTask

from .attachments import (
    AttachmentError,
    attachment_count_for_event,
    attachment_count_for_sample,
    delete_attachment,
    list_event_attachments,
    list_sample_attachments,
    refresh_attachment,
    refresh_event_attachments,
    save_attachment,
    verify_and_prepare_download,
)

from .aliquots import (
    AliquotError,
    build_lineage_tree,
    create_aliquot,
    deletion_blocked_reason,
    get_batch_detail,
    quantity_dict,
    set_initial_quantity,
)

from .audit import build_audit
from .bulk_import import (
    CSV_HEADERS,
    BulkImportError,
    confirm_bulk_import,
    preview_bulk_import,
)
from .db import get_conn, init_db
from .findings import (
    FindingError,
    build_queue,
    claim_finding,
    dispose_finding,
    get_finding_detail,
    normalize_bound,
    queue_summary,
    release_finding,
    sync_all_findings,
    sync_sample_findings,
)
from .handovers import (
    HandoverError,
    build_handover_view,
    confirm_handover,
    get_handover_record,
    refresh_handover_validity,
)
from .locations import (
    LocationError,
    create_location,
    current_placement,
    get_location_detail,
    list_locations,
    list_sample_moves,
    move_sample,
)
from .report import build_html, build_json, build_markdown
from .seal_package import (
    SealPackageError,
    build_seal_package,
    spool_upload,
    verify_seal_package,
)
from .schemas import (
    AliquotCreate,
    BulkImportConfirm,
    EventCreate,
    FindingClaim,
    FindingDispose,
    FindingRelease,
    HandoverConfirm,
    LocationCreate,
    LocationMoveCreate,
    SampleCreate,
    SampleQuantityInit,
    SignoffCreate,
    SignoffRevoke,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时建表并写入演示数据（已初始化则跳过）
    init_db()
    yield


app = FastAPI(title="样本交接链路审计", version="1.0.0", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- 样本 ----------

def _sample_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        **quantity_dict(row),
    }


def _fetch_sample(conn: Connection, sample_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="样本不存在")
    return _sample_dict(row)


def _fetch_events(conn: Connection, sample_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM events WHERE sample_id = ? ORDER BY id", (sample_id,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "custodian": r["custodian"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "note": r["note"],
            "seq": r["seq"],
        }
        for r in rows
    ]


@app.get("/api/samples")
def list_samples(conn: Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM samples ORDER BY created_at DESC, id DESC").fetchall()
    samples = []
    for r in rows:
        count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE sample_id = ?", (r["id"],)
        ).fetchone()[0]
        d = _sample_dict(r)
        d["event_count"] = count
        samples.append(d)
    return samples


@app.post("/api/samples", status_code=201)
def create_sample(payload: SampleCreate, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    exists = conn.execute("SELECT 1 FROM samples WHERE code = ?", (payload.code,)).fetchone()
    if exists:
        raise HTTPException(status_code=409, detail=f"样本编号 {payload.code} 已存在")
    cur = conn.execute(
        "INSERT INTO samples (code, name, description, created_at) VALUES (?, ?, ?, ?)",
        (payload.code, payload.name, payload.description,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return _fetch_sample(conn, cur.lastrowid)


@app.get("/api/samples/{sample_id}")
def get_sample(sample_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    sample = _fetch_sample(conn, sample_id)
    events = _fetch_events(conn, sample_id)
    audit = build_audit(events)
    return {
        "sample": sample,
        "events": events,
        "audit": audit,
        "signoffs": _list_signoffs(conn, sample_id),
        "handovers": build_handover_view(conn, sample_id),
        "attachments": list_sample_attachments(conn, sample_id),
        "lineage": build_lineage_tree(conn, sample_id),
        "location": current_placement(conn, sample_id),
        "location_moves": list_sample_moves(conn, sample_id),
    }


@app.delete("/api/samples/{sample_id}", status_code=204)
def delete_sample(sample_id: int, conn: Connection = Depends(get_conn)) -> Response:
    _fetch_sample(conn, sample_id)
    # 证据附件必须先逐事件处理（下载留档后删除附件记录），禁止连带删除
    attached = attachment_count_for_sample(conn, sample_id)
    if attached:
        raise HTTPException(
            status_code=409,
            detail=f"该样本下还有 {attached} 个事件证据附件，请先处理（删除）附件后再删除样本",
        )
    # 已有分装后代（或本身由批次产生）的样本禁止删除：批次记录不可变
    blocked = deletion_blocked_reason(conn, sample_id)
    if blocked:
        raise HTTPException(status_code=409, detail=blocked)
    # 样本当前位于库位中时禁止删除：请先移出库位（库位占用计数须保持一致）
    placement = current_placement(conn, sample_id)
    if placement:
        raise HTTPException(
            status_code=409,
            detail=f"样本当前位于库位 {placement['code']}（{placement['name']}），"
                   "请先移出库位后再删除样本",
        )
    conn.execute("DELETE FROM samples WHERE id = ?", (sample_id,))
    conn.commit()
    return Response(status_code=204)


# ---------- 样本分装谱系 ----------

@app.put("/api/samples/{sample_id}/quantity")
def init_sample_quantity(
    sample_id: int, payload: SampleQuantityInit, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """首次分装前设置初始数量与单位（仅允许一次），须携带样本当前版本。"""
    _fetch_sample(conn, sample_id)
    try:
        return set_initial_quantity(
            conn,
            sample_id,
            payload.initial_quantity,
            payload.unit,
            payload.expected_version,
        )
    except AliquotError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/samples/{sample_id}/aliquots", status_code=201)
def create_sample_aliquot(
    sample_id: int, payload: AliquotCreate, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """从当前样本发起一次分装：单事务内校验编号唯一 / 单位一致 / 分装量为正 /
    合计不超余量，成功后创建子样本、扣减余量并保存不可变批次。

    必须携带父样本版本 ``expected_version``；重复提交或并发变更返回 409，
    任何失败均整体回滚，不产生部分记录。
    """
    _fetch_sample(conn, sample_id)
    items = [i.model_dump() for i in payload.items]
    try:
        result = create_aliquot(
            conn,
            sample_id,
            payload.operator,
            items,
            aliquot_at=payload.aliquot_at,
            note=payload.note,
            expected_version=payload.expected_version,
        )
    except AliquotError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    # 回传刷新后的父样本与谱系树，便于前端直接渲染
    result["parent"] = _fetch_sample(conn, sample_id)
    result["lineage"] = build_lineage_tree(conn, sample_id)
    return result


@app.get("/api/samples/{sample_id}/lineage")
def get_sample_lineage(sample_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """父子谱系树：根样本到全部后代，含来源批次、分装量、初始量与剩余量。"""
    _fetch_sample(conn, sample_id)
    return build_lineage_tree(conn, sample_id)


@app.get("/api/aliquot-batches/{batch_id}")
def get_aliquot_batch(batch_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """批次详情：批次头 + 子样本明细 + 不可变快照与现场 SHA-256 完整性校验。"""
    try:
        return get_batch_detail(conn, batch_id)
    except AliquotError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ---------- 样本库位管理 ----------

@app.get("/api/locations")
def list_locations_route(conn: Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    """库位列表：编号 / 名称 / 容量 / 当前占用 / 剩余 / 乐观锁版本。"""
    return list_locations(conn)


@app.post("/api/locations", status_code=201)
def create_location_route(
    payload: LocationCreate, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """创建库位（编号全局唯一，容量为正整数）。"""
    try:
        return create_location(conn, payload.code, payload.name, payload.capacity)
    except LocationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/locations/{location_id}")
def get_location_route(location_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """库位详情：基本信息 + 当前占用清单 + 涉及该库位的移动历史。"""
    try:
        return get_location_detail(conn, location_id)
    except LocationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/samples/{sample_id}/location-move", status_code=201)
def move_sample_location(
    sample_id: int, payload: LocationMoveCreate, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """样本库位移动：放入（源为 null）、库位间转移或移出（目标为 null）。

    在单一 ``BEGIN IMMEDIATE`` 事务内校验样本当前库位、目标剩余容量，以及
    样本和源 / 目标库位的 ``expected_version``；成功后同步更新两侧占用数、
    样本当前位置与版本，并写入不可变移动记录。并发争抢最后空位、陈旧来源
    或版本过期均返回 409，失败整体回滚，不产生重复占位、超容量或半条记录。
    """
    try:
        result = move_sample(
            conn,
            sample_id,
            payload.to_location_id,
            payload.operator,
            expected_sample_version=payload.expected_sample_version,
            expected_from_location_id=payload.expected_from_location_id,
            expected_from_version=payload.expected_from_version,
            expected_to_version=payload.expected_to_version,
            moved_at=payload.moved_at,
            note=payload.note,
        )
    except LocationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    # 回传刷新后的样本 / 当前位置 / 移动历史 / 库位列表，便于前端直接渲染
    result["sample"] = _fetch_sample(conn, sample_id)
    result["location"] = current_placement(conn, sample_id)
    result["location_moves"] = list_sample_moves(conn, sample_id)
    result["locations"] = list_locations(conn)
    return result


@app.get("/api/samples/{sample_id}/location-moves")
def list_sample_location_moves(
    sample_id: int, conn: Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    """样本的移动历史（不可变记录，新的在前）。"""
    _fetch_sample(conn, sample_id)
    return list_sample_moves(conn, sample_id)


# ---------- 交接事件 ----------

@app.post("/api/samples/{sample_id}/events", status_code=201)
def add_event(
    sample_id: int, payload: EventCreate, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    _fetch_sample(conn, sample_id)
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE sample_id = ?", (sample_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO events (sample_id, custodian, start_time, end_time, note, seq) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sample_id, payload.custodian, payload.start_time, payload.end_time, payload.note, seq),
    )
    # 新事件可能插入到某对相邻事件之间，原确认随之失效（记录保留）
    refresh_handover_validity(conn, sample_id)
    # 重算审计发现指纹：新异常以未处理入库，消失的转已消除历史
    sync_sample_findings(conn, sample_id)
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"id": row["id"], "seq": row["seq"]}


@app.put("/api/events/{event_id}")
def update_event(event_id: int, payload: EventCreate, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="交接事件不存在")
    conn.execute(
        "UPDATE events SET custodian = ?, start_time = ?, end_time = ?, note = ? WHERE id = ?",
        (payload.custodian, payload.start_time, payload.end_time, payload.note, event_id),
    )
    # 绑定该事件关键字段的原确认保留为失效记录，需两侧重新确认
    refresh_handover_validity(conn, row["sample_id"])
    # 重算审计发现指纹：仍存在的沿用处置状态，新出现的保持未处理
    sync_sample_findings(conn, row["sample_id"])
    conn.commit()
    return {"id": event_id, "updated": True}


@app.delete("/api/events/{event_id}", status_code=204)
def delete_event(event_id: int, conn: Connection = Depends(get_conn)) -> Response:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="交接事件不存在")
    sample_id = row["sample_id"]
    # 有关联证据附件的事件禁止删除：请先下载留档并删除附件记录
    attached = attachment_count_for_event(conn, event_id)
    if attached:
        raise HTTPException(
            status_code=409,
            detail=f"该事件还有 {attached} 个证据附件，请先处理（删除）附件后再删除事件",
        )
    # 删除事件后，绑定它的原确认保留为失效记录（行与快照不删）
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    refresh_handover_validity(conn, sample_id)
    # 重算审计发现指纹：消失的异常转为已消除历史
    sync_sample_findings(conn, sample_id)
    conn.commit()
    return Response(status_code=204)


# ---------- 事件证据附件（内容寻址存储） ----------

def _fetch_event(conn: Connection, event_id: int) -> Any:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="交接事件不存在")
    return row


def _attachment_headers(filename: str) -> dict[str, str]:
    """兼容各浏览器的 Content-Disposition（ASCII 回退 + RFC 5987 编码）。"""
    fallback = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or "attachment"
    return {"Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"}


@app.post("/api/events/{event_id}/attachments", status_code=201)
async def upload_event_attachment(
    event_id: int,
    file: UploadFile = File(..., description="PDF / JPEG / PNG，单文件不超过 10 MiB"),
    uploaded_by: str = Form(..., description="上传人姓名"),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """流式落盘证据附件，按 SHA-256 内容寻址；相同内容只存一份实体。"""
    _fetch_event(conn, event_id)
    try:
        return save_attachment(conn, event_id, file, uploaded_by)
    except AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/events/{event_id}/attachments")
def list_event_attachment_route(
    event_id: int, conn: Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    """事件附件列表（含完整性状态；实体缺失自动标为损坏）。"""
    _fetch_event(conn, event_id)
    return list_event_attachments(conn, event_id)


@app.get("/api/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: int, conn: Connection = Depends(get_conn)
) -> FileResponse:
    """下载前现场复算 SHA-256；实体缺失 / 摘要不一致返回 409 并标记损坏。"""
    try:
        item = verify_and_prepare_download(conn, attachment_id)
    except AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return FileResponse(
        item["path"],
        media_type=item["media_type"],
        headers=_attachment_headers(item["filename"]),
    )


@app.delete("/api/attachments/{attachment_id}", status_code=204)
def delete_attachment_route(
    attachment_id: int, conn: Connection = Depends(get_conn)
) -> Response:
    """删除附件记录（仅解除引用）；最后一个引用移除后才清理实体。"""
    try:
        delete_attachment(conn, attachment_id)
    except AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return Response(status_code=204)


@app.post("/api/attachments/{attachment_id}/refresh")
def refresh_attachment_route(
    attachment_id: int, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """完整性刷新：复算单个附件 SHA-256 并更新损坏标记。"""
    try:
        return refresh_attachment(conn, attachment_id)
    except AttachmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/events/{event_id}/attachments/refresh")
def refresh_event_attachments_route(
    event_id: int, conn: Connection = Depends(get_conn)
) -> list[dict[str, Any]]:
    """一键复算该事件下全部附件的完整性。"""
    _fetch_event(conn, event_id)
    return refresh_event_attachments(conn, event_id)


# ---------- 批量导入交接事件（CSV 预览 + 确认） ----------

@app.get("/api/samples/{sample_id}/events/import-template")
def download_import_template(sample_id: int, conn: Connection = Depends(get_conn)) -> Response:
    """下载 UTF-8 CSV 导入模板（含表头与示例行；示例行不会被自动导入）。"""
    _fetch_sample(conn, sample_id)
    lines = [
        ",".join(CSV_HEADERS),
        "张三（采集组）,2026-09-18 09:00:00,2026-09-18 10:00:00,现场采集并密封",
        "李四（运输组）,2026-09-18 10:00:00,2026-09-18 11:30:00,冷链运输 2-8℃",
        "王五（检测组）,2026-09-18 11:30:00,,接手后仍在保管，结束时间留空",
    ]
    # 写入 UTF-8 BOM，保证 Excel 直接打开中文表头不乱码
    body = "\ufeff" + "\r\n".join(lines) + "\r\n"
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=handover_events_template.csv; "
            "filename*=UTF-8''%E6%89%8B%E4%BA%A4%E4%BA%8B%E4%BB%B6%E5%AF%BC%E5%85%A5%E6%A8%A1%E6%9D%BF.csv"
        },
    )


@app.post("/api/samples/{sample_id}/events/import-preview")
async def preview_events_import(
    sample_id: int, request: Request, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """上传 CSV -> 逐行校验与导入后审计投影；只读不写库，返回绑定事件链的摘要令牌。"""
    _fetch_sample(conn, sample_id)
    content = await request.body()
    try:
        return preview_bulk_import(conn, sample_id, content)
    except BulkImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/samples/{sample_id}/events/import-confirm", status_code=201)
def confirm_events_import(
    sample_id: int,
    payload: BulkImportConfirm,
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """凭预览令牌在单一事务内再次校验并按文件顺序一次性写入。

    任何校验失败都回滚整个事务，绝不部分落库。
    """
    try:
        # BEGIN IMMEDIATE 立即获取写锁，杜绝「重算指纹 -> 写入」之间的并发写入
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = confirm_bulk_import(conn, sample_id, payload.token)
            # 导入后同事务重算审计发现指纹：新异常未处理、消失的转已消除
            sync_sample_findings(conn, sample_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    except BulkImportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    result["sample"] = _fetch_sample(conn, sample_id)
    result["events"] = _fetch_events(conn, sample_id)
    result["signoffs"] = _list_signoffs(conn, sample_id)
    return result


# ---------- 审计 ----------

@app.get("/api/samples/{sample_id}/audit")
def get_audit(sample_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    _fetch_sample(conn, sample_id)
    return build_audit(_fetch_events(conn, sample_id))


# ---------- 双向交接确认 ----------

@app.get("/api/samples/{sample_id}/handovers")
def list_handovers(sample_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """交接项（审计排序相邻事件）当前状态 + 失效历史记录。"""
    _fetch_sample(conn, sample_id)
    return build_handover_view(conn, sample_id)


@app.post("/api/samples/{sample_id}/handovers/confirm", status_code=200)
def confirm_pair(
    sample_id: int,
    from_event_id: int,
    to_event_id: int,
    payload: HandoverConfirm,
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """交出方（side=from）或接收方（side=to）填写姓名与确认时间。"""
    _fetch_sample(conn, sample_id)
    try:
        result = confirm_handover(
            conn,
            sample_id,
            from_event_id,
            to_event_id,
            payload.side,
            payload.confirmer_name,
            payload.confirmed_at,
        )
    except HandoverError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    conn.commit()
    return result


@app.get("/api/handovers/{handover_id}")
def get_handover(handover_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """确认记录详情：双方确认信息、绑定的事件字段摘要、失效原因。"""
    try:
        return get_handover_record(conn, handover_id)
    except HandoverError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ---------- 异常处置队列（跨样本） ----------

@app.get("/api/findings/summary")
def findings_summary(conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """未处理 / 已处理 / 已消除数量（先按当前链路重算指纹）。"""
    sync_all_findings(conn)
    return queue_summary(conn)


@app.get("/api/findings/queue")
def findings_queue(
    status: str = Query("open", pattern="^(open|resolved|eliminated|all)$"),
    code: Optional[str] = Query(None),
    custodian: Optional[str] = Query(None),
    time_from: Optional[str] = Query(None),
    time_to: Optional[str] = Query(None),
    claim: Optional[str] = Query(None, pattern="^(unclaimed|claimed|expired)$"),
    conn: Connection = Depends(get_conn),
) -> dict[str, Any]:
    """跨样本异常处置队列：按状态 / 异常类型 / 经手人 / 发生时间 / 认领状态筛选。"""
    try:
        bound_from = normalize_bound(time_from, "发生时间从")
        bound_to = normalize_bound(time_to, "发生时间至")
        return build_queue(
            conn,
            status=status,
            code=(code or "").strip() or None,
            custodian=custodian,
            time_from=bound_from,
            time_to=bound_to,
            claim=claim,
        )
    except FindingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.get("/api/findings/{fingerprint}")
def finding_detail(fingerprint: str, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """发现详情：当前状态 + 完整状态迁移记录（含认领 / 释放 / 过期接管）。"""
    try:
        return get_finding_detail(conn, fingerprint)
    except FindingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/findings/{fingerprint}/claim")
def finding_claim(
    fingerprint: str, payload: FindingClaim, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """限时认领未处理发现（15 分钟租约）；租约到期后同一接口即接管。

    服务端在单一事务内先按当前链路重算发现，再同时校验 expected_version
    与租约状态：租约未到期且属于他人返回 409；到期 / 未认领 / 已释放则
    认领或接管成功并递增版本。
    """
    try:
        return claim_finding(conn, fingerprint, payload.handler, payload.expected_version)
    except FindingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/findings/{fingerprint}/release")
def finding_release(
    fingerprint: str, payload: FindingRelease, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """主动释放认领：只有当前未到期租约的认领人可以释放（版本递增）。"""
    try:
        return release_finding(
            conn,
            fingerprint,
            payload.handler,
            payload.expected_version,
            payload.note,
        )
    except FindingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@app.post("/api/findings/{fingerprint}/dispose")
def finding_dispose(
    fingerprint: str, payload: FindingDispose, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """处置写入：标记已处理 / 重新打开。

    服务端先在单一事务内按当前链路重算指纹，再同时校验客户端看到的指纹版本
    （expected_version）与认领租约：只有当前认领人可标记已处理；版本过期、
    租约冲突均返回 409，客户端应刷新队列后重试。
    """
    try:
        return dispose_finding(
            conn,
            fingerprint,
            payload.action,
            payload.handler,
            payload.note,
            payload.expected_version,
        )
    except FindingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


# ---------- 结论签署（不可变快照） ----------

def _signoff_summary(row: Any) -> dict[str, Any]:
    """签署记录的列表/摘要视图：状态 + 从快照中提取的审计摘要。"""
    snap = json.loads(row["snapshot"])
    audit = snap.get("audit", {})
    return {
        "id": row["id"],
        "sample_id": row["sample_id"],
        "signer": row["signer"],
        "signed_at": row["signed_at"],
        "remark": row["remark"],
        "status": row["status"],
        "revoke_reason": row["revoke_reason"],
        "revoked_at": row["revoked_at"],
        "created_at": row["created_at"],
        "digest": row["digest"][:12],
        "audit_status": audit.get("status"),
        "conclusion": audit.get("conclusion"),
        "summary": audit.get("summary"),
        "event_count": len(snap.get("events", [])),
    }


def _list_signoffs(conn: Connection, sample_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM signoffs WHERE sample_id = ? ORDER BY id DESC", (sample_id,)
    ).fetchall()
    return [_signoff_summary(r) for r in rows]


def _fetch_signoff(conn: Connection, signoff_id: int) -> Any:
    row = conn.execute("SELECT * FROM signoffs WHERE id = ?", (signoff_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="签署记录不存在")
    return row


@app.post("/api/samples/{sample_id}/signoffs", status_code=201)
def create_signoff(
    sample_id: int, payload: SignoffCreate, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """在当前审计结果上签署：保存样本 + 事件 + 审计发现的不可变快照。"""
    sample = _fetch_sample(conn, sample_id)
    events = _fetch_events(conn, sample_id)
    audit = build_audit(events)
    snapshot = {"sample": sample, "events": events, "audit": audit}
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO signoffs (sample_id, signer, signed_at, remark, status, "
        "snapshot, digest, created_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
        (sample_id, payload.signer, payload.signed_at, payload.remark, raw, digest, now),
    )
    conn.commit()
    return _signoff_summary(_fetch_signoff(conn, cur.lastrowid))


@app.get("/api/samples/{sample_id}/signoffs")
def list_signoffs(sample_id: int, conn: Connection = Depends(get_conn)) -> list[dict[str, Any]]:
    _fetch_sample(conn, sample_id)
    return _list_signoffs(conn, sample_id)


@app.get("/api/signoffs/{signoff_id}")
def get_signoff(signoff_id: int, conn: Connection = Depends(get_conn)) -> dict[str, Any]:
    """快照详情：返回完整快照内容并现场校验完整性。"""
    row = _fetch_signoff(conn, signoff_id)
    detail = _signoff_summary(row)
    detail["digest"] = row["digest"]
    detail["snapshot"] = json.loads(row["snapshot"])
    detail["integrity_ok"] = (
        hashlib.sha256(row["snapshot"].encode("utf-8")).hexdigest() == row["digest"]
    )
    return detail


@app.post("/api/signoffs/{signoff_id}/revoke")
def revoke_signoff(
    signoff_id: int, payload: SignoffRevoke, conn: Connection = Depends(get_conn)
) -> dict[str, Any]:
    """撤销签署：记录原因与时间，原快照保留不变。"""
    row = _fetch_signoff(conn, signoff_id)
    if row["status"] == "revoked":
        raise HTTPException(status_code=409, detail="该签署已撤销，请勿重复操作")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE signoffs SET status = 'revoked', revoke_reason = ?, revoked_at = ? WHERE id = ?",
        (payload.reason, now, signoff_id),
    )
    conn.commit()
    return _signoff_summary(_fetch_signoff(conn, signoff_id))


# ---------- 报告导出 ----------

def _safe_filename(code: str, ext: str) -> str:
    stem = re.sub(r"[^\w一-鿿-]+", "_", code).strip("_") or "sample"
    return f"audit_report_{stem}.{ext}"


@app.get("/api/samples/{sample_id}/report")
def export_report(
    sample_id: int,
    format: str = Query("markdown", pattern="^(markdown|html|json)$"),
    conn: Connection = Depends(get_conn),
):
    sample = _fetch_sample(conn, sample_id)
    audit = build_audit(_fetch_events(conn, sample_id))
    signoffs = _list_signoffs(conn, sample_id)
    handovers = build_handover_view(conn, sample_id)
    attachments = list_sample_attachments(conn, sample_id)
    event_index = {e["id"]: e for e in audit["events"]}

    if format == "markdown":
        body = build_markdown(sample, audit, signoffs, handovers, attachments, event_index)
        headers = {"Content-Disposition": f'attachment; filename="{_safe_filename(sample["code"], "md")}"'}
        return PlainTextResponse(content=body, media_type="text/markdown; charset=utf-8", headers=headers)
    if format == "html":
        body = build_html(sample, audit, signoffs, handovers, attachments, event_index)
        headers = {"Content-Disposition": f'attachment; filename="{_safe_filename(sample["code"], "html")}"'}
        return HTMLResponse(content=body, media_type="text/html; charset=utf-8", headers=headers)

    body = build_json(sample, audit, signoffs, handovers, attachments)
    headers = {"Content-Disposition": f'attachment; filename="{_safe_filename(sample["code"], "json")}"'}
    return Response(content=body, media_type="application/json; charset=utf-8", headers=headers)


# ---------- 证据封存包（导出 + 不落库校验） ----------

@app.get("/api/samples/{sample_id}/seal-package")
def export_seal_package(sample_id: int, conn: Connection = Depends(get_conn)):
    """导出证据封存包 ZIP：JSON 清单 + 审计报告 + 全部未损坏附件。

    元数据在同一数据库读事务中固定，写包前复核附件摘要；期间链路或附件
    记录变化则中止（409），避免混合快照。
    """
    try:
        zip_path, summary = build_seal_package(conn, sample_id)
    except SealPackageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    headers = _attachment_headers(summary["filename"])
    headers["X-Seal-Attachments"] = str(summary["attachments"])
    headers["X-Seal-Excluded"] = str(summary["excluded"])
    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers=headers,
        background=BackgroundTask(os.unlink, zip_path),
    )


@app.post("/api/seal-packages/verify")
async def verify_seal_package_route(
    file: UploadFile = File(..., description="待校验的封存包 ZIP"),
) -> dict[str, Any]:
    """不落库校验封存包：限制解压总量与路径穿越，逐项核对清单、缺失、
    多余文件与 SHA-256 摘要，返回中文差异。"""
    tmp = None
    try:
        tmp = spool_upload(file.file)
        return verify_seal_package(tmp)
    except SealPackageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    finally:
        if tmp is not None:
            os.unlink(tmp)


# ---------- 前端静态页（挂在最后，避免吞掉 API 路由） ----------

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
