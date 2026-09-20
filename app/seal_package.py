"""证据封存包：样本级 ZIP 导出 + 不落库校验。

导出（:func:`build_seal_package`）
================================
为当前样本生成一个 ZIP 封存包，内含：

* ``manifest.json`` —— JSON 清单：样本、交接事件、附件的逻辑关系，
  以及包内每个文件（审计报告 + 全部未损坏附件）的路径、大小与 SHA-256；
* ``report.md`` —— 与在线导出版一致的 Markdown 审计报告；
* ``attachments/<事件id>/<附件id>_<原文件名>`` —— 全部**未损坏**附件实体。

一致性保证（避免混合快照）：

1. 元数据（样本 / 事件 / 附件记录 / 签署 / 交接确认）在**同一数据库读事务**
   中读取并计算链路指纹；
2. 写包前逐附件**复核实体 SHA-256 摘要**（损坏者排除出包、登记进清单）；
3. 复核完成后开启第二个读事务重读元数据并比对指纹：期间链路或附件记录
   发生任何变化即中止（409）；写包在同一读事务内完成，每个附件写入 ZIP
   时再次流式复算摘要，与清单不一致同样中止。

校验（:func:`verify_seal_package`）
=================================
完全不落库：只读取上传的 ZIP，限制条目数 / 单文件 / 解压总量并拦截路径
穿越，逐项核对清单声明、缺失文件、多余文件与 SHA-256 摘要，返回中文差异。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Optional
from sqlite3 import Connection

from .attachments import (
    CHUNK_SIZE,
    REASON_MISMATCH,
    REASON_TEXT,
    STATUS_OK,
    blob_path,
    human_size,
    verify_blob,
)
from .audit import build_audit
from .handovers import build_handover_view
from .report import build_markdown

PACKAGE_TYPE = "evidence-seal-package"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
REPORT_NAME = "report.md"

# 校验入口限制（防 ZIP 炸弹 / 资源耗尽）
MAX_VERIFY_UPLOAD_BYTES = 300 * 1024 * 1024       # 上传 ZIP 上限
MAX_VERIFY_UNCOMPRESSED_BYTES = 512 * 1024 * 1024  # 解压总量上限
MAX_VERIFY_FILE_BYTES = 64 * 1024 * 1024           # 单文件上限（附件本身 ≤10 MiB）
MAX_VERIFY_ENTRIES = 10000                         # 包内条目数上限
MAX_MANIFEST_BYTES = 8 * 1024 * 1024               # 清单文件上限
MAX_VERIFY_ISSUES = 200                            # 返回的差异条数上限

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# 差异类型 -> 中文标签（前端直接展示）
ISSUE_TYPE_TEXT = {
    "manifest": "清单异常",
    "structure": "结构异常",
    "path_traversal": "路径穿越",
    "limit": "超出限制",
    "missing": "缺失",
    "extra": "多余",
    "size_mismatch": "大小不一致",
    "digest_mismatch": "摘要异常",
}


class SealPackageError(Exception):
    """业务校验失败，由路由层转换为 HTTPException。"""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------- 元数据固定（同一读事务内执行） ----------

_SAMPLE_COLS = "id, code, name, description, created_at"
_EVENT_COLS = "id, sample_id, custodian, start_time, end_time, note, seq"
_ATTACHMENT_COLS = (
    "id, event_id, blob_digest, filename, size, media_type, uploaded_by, "
    "uploaded_at, status, corruption_reason, last_verified_at, created_at"
)


def _fingerprint(sample_row: Any, event_rows: list[Any], attachment_rows: list[Any]) -> str:
    """链路 + 附件记录指纹：任一记录变化都会改变，用于发现混合快照。"""
    payload = {
        "sample": tuple(sample_row),
        "events": [tuple(r) for r in event_rows],
        "attachments": [tuple(r) for r in attachment_rows],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _signoff_for_report(row: Any) -> dict[str, Any]:
    snap = json.loads(row["snapshot"])
    return {
        "signer": row["signer"],
        "signed_at": row["signed_at"],
        "status": row["status"],
        "revoke_reason": row["revoke_reason"],
        "revoked_at": row["revoked_at"],
        "remark": row["remark"],
        "audit_status": snap.get("audit", {}).get("status"),
        "digest": row["digest"][:12],
    }


def _attachment_for_report(row: Any) -> dict[str, Any]:
    reason = row["corruption_reason"]
    return {
        "event_id": row["event_id"],
        "filename": row["filename"],
        "media_type": row["media_type"],
        "size": row["size"],
        "size_text": human_size(row["size"]),
        "uploaded_by": row["uploaded_by"],
        "uploaded_at": row["uploaded_at"],
        "digest": row["blob_digest"],
        "status": row["status"],
        "corruption_reason": reason,
        "corruption_reason_text": REASON_TEXT.get(reason) if reason else None,
    }


def _load_metadata(conn: Connection, sample_id: int) -> dict[str, Any]:
    """读取封存所需全部元数据（只读；须在调用方开启的读事务内执行）。"""
    sample_row = conn.execute(
        f"SELECT {_SAMPLE_COLS} FROM samples WHERE id = ?", (sample_id,)
    ).fetchone()
    if sample_row is None:
        raise SealPackageError("样本不存在", status_code=404)
    event_rows = conn.execute(
        f"SELECT {_EVENT_COLS} FROM events WHERE sample_id = ? ORDER BY id",
        (sample_id,),
    ).fetchall()
    attachment_rows = conn.execute(
        f"SELECT {_ATTACHMENT_COLS} FROM attachments WHERE event_id IN "
        "(SELECT id FROM events WHERE sample_id = ?) ORDER BY id",
        (sample_id,),
    ).fetchall()
    signoff_rows = conn.execute(
        "SELECT * FROM signoffs WHERE sample_id = ? ORDER BY id DESC", (sample_id,)
    ).fetchall()

    events = [dict(r) for r in event_rows]
    return {
        "sample": dict(sample_row),
        "events": events,
        "attachment_rows": attachment_rows,
        "audit": build_audit(events),
        "handovers": build_handover_view(conn, sample_id),
        "signoffs": [_signoff_for_report(r) for r in signoff_rows],
        "fingerprint": _fingerprint(sample_row, event_rows, attachment_rows),
    }


# ---------- 附件摘要复核 ----------

def _reverify_attachments(meta: dict[str, Any]) -> tuple[list[Any], list[tuple[Any, str]]]:
    """写包前复核附件实体摘要。

    返回（可封存附件记录, [（被排除附件记录, 损坏原因）]）：数据库中已标记
    损坏的直接排除；其余现场复算 SHA-256，不一致 / 缺失同样排除。
    """
    included: list[Any] = []
    excluded: list[tuple[Any, str]] = []
    for row in meta["attachment_rows"]:
        if row["status"] != STATUS_OK:
            excluded.append((row, row["corruption_reason"] or REASON_MISMATCH))
            continue
        ok, reason = verify_blob(row["blob_digest"], row["size"])
        if ok:
            included.append(row)
        else:
            excluded.append((row, reason or REASON_MISMATCH))
    return included, excluded


# ---------- ZIP 写包 ----------

def _safe_stem(code: str) -> str:
    return re.sub(r"[^\w一-鿿-]+", "_", code).strip("_") or "sample"


def _zipinfo(name: str, date_time: tuple[int, ...]) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    return info


def _write_blob_verified(
    zf: zipfile.ZipFile,
    entry_name: str,
    row: Any,
    date_time: tuple[int, ...],
) -> None:
    """把附件实体流式写入 ZIP，同时复算 SHA-256 并与附件记录比对。"""
    path = blob_path(row["blob_digest"])
    if not path.exists():
        raise SealPackageError(
            f"附件「{row['filename']}」实体文件在写包时缺失，已中止导出", status_code=409
        )
    hasher = hashlib.sha256()
    total = 0
    info = _zipinfo(entry_name, date_time)
    with open(path, "rb") as src, zf.open(info, "w") as dest:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            hasher.update(chunk)
            dest.write(chunk)
    if total != row["size"] or hasher.hexdigest() != row["blob_digest"]:
        raise SealPackageError(
            f"附件「{row['filename']}」写包复核摘要与记录不一致，已中止以避免混合快照",
            status_code=409,
        )


def _write_zip(
    meta: dict[str, Any],
    included: list[Any],
    excluded: list[tuple[Any, str]],
) -> tuple[Path, dict[str, Any]]:
    """根据已固定的元数据写出封存包 ZIP，返回（临时文件路径, 汇总信息）。"""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_time = datetime.now().timetuple()[:6]
    folder = f"seal_package_{_safe_stem(meta['sample']['code'])}_{generated_at.replace('-', '').replace(' ', '_').replace(':', '')}"

    # 审计报告（与在线 Markdown 报告同一模板，内容来自同一批固定元数据）
    report_attachments = [_attachment_for_report(r) for r in meta["attachment_rows"]]
    event_index = {e["id"]: e for e in meta["audit"]["events"]}
    report_md = build_markdown(
        meta["sample"], meta["audit"], meta["signoffs"], meta["handovers"],
        report_attachments, event_index,
    )
    report_bytes = report_md.encode("utf-8")

    event_by_id = {e["id"]: e for e in meta["events"]}
    manifest_attachments: list[dict[str, Any]] = []
    for row in included:
        ev = event_by_id.get(row["event_id"], {})
        manifest_attachments.append(
            {
                "attachment_id": row["id"],
                "event_id": row["event_id"],
                "event_seq": ev.get("seq"),
                "custodian": ev.get("custodian"),
                "filename": row["filename"],
                "path": f"attachments/{row['event_id']}/{row['id']}_{row['filename']}",
                "size": row["size"],
                "sha256": row["blob_digest"],
                "media_type": row["media_type"],
                "uploaded_by": row["uploaded_by"],
                "uploaded_at": row["uploaded_at"],
            }
        )
    manifest_excluded = [
        {
            "attachment_id": row["id"],
            "event_id": row["event_id"],
            "filename": row["filename"],
            "size": row["size"],
            "sha256": row["blob_digest"],
            "reason": reason,
            "reason_text": REASON_TEXT.get(reason, reason),
        }
        for row, reason in excluded
    ]
    included_ids_by_event: dict[int, list[int]] = {}
    for row in included:
        included_ids_by_event.setdefault(row["event_id"], []).append(row["id"])

    manifest = {
        "package_type": PACKAGE_TYPE,
        "format_version": FORMAT_VERSION,
        "generator": "样本交接链路审计 · 证据封存包",
        "generated_at": generated_at,
        "sample": meta["sample"],
        "audit": {
            "status": meta["audit"]["status"],
            "conclusion": meta["audit"]["conclusion"],
            "summary": meta["audit"]["summary"],
        },
        "events": [
            {
                "id": e["id"],
                "seq": e["seq"],
                "custodian": e["custodian"],
                "start_time": e["start_time"],
                "end_time": e["end_time"],
                "note": e["note"],
                "attachment_ids": included_ids_by_event.get(e["id"], []),
            }
            for e in meta["events"]
        ],
        "attachments": manifest_attachments,
        "excluded_attachments": manifest_excluded,
        "report": {
            "path": REPORT_NAME,
            "size": len(report_bytes),
            "sha256": hashlib.sha256(report_bytes).hexdigest(),
        },
        "chain_fingerprint": meta["fingerprint"],
        "counts": {
            "events": len(meta["events"]),
            "attachments": len(manifest_attachments),
            "excluded_attachments": len(manifest_excluded),
        },
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")

    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=".seal-", suffix=".zip")
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr(_zipinfo(f"{folder}/{MANIFEST_NAME}", date_time), manifest_bytes)
            zf.writestr(_zipinfo(f"{folder}/{REPORT_NAME}", date_time), report_bytes)
            for row, entry in zip(included, manifest_attachments):
                _write_blob_verified(zf, f"{folder}/{entry['path']}", row, date_time)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    summary = {
        "filename": f"{folder}.zip",
        "generated_at": generated_at,
        "events": len(meta["events"]),
        "attachments": len(manifest_attachments),
        "excluded": len(manifest_excluded),
    }
    return Path(tmp.name), summary


def build_seal_package(conn: Connection, sample_id: int) -> tuple[Path, dict[str, Any]]:
    """生成证据封存包：两阶段读事务固定元数据，期间变化即中止。

    返回（ZIP 临时文件路径, 汇总信息）；调用方负责在响应发出后删除临时文件。
    """
    # 阶段一：在同一数据库读事务中固定元数据（样本 / 事件 / 附件记录指纹）
    conn.execute("BEGIN")
    try:
        meta1 = _load_metadata(conn, sample_id)
    finally:
        conn.rollback()

    # 写包前复核附件摘要（纯文件 I/O，不占数据库连接）
    included, excluded = _reverify_attachments(meta1)

    # 阶段二：写包前重读元数据并比对指纹；链路或附件记录变化则中止。
    # 写包在同一读事务内完成（读事务阻塞并发写），杜绝混合快照。
    conn.execute("BEGIN")
    try:
        meta2 = _load_metadata(conn, sample_id)
        if meta2["fingerprint"] != meta1["fingerprint"]:
            raise SealPackageError(
                "生成期间链路或附件记录已变化，已中止以避免混合快照，请重试",
                status_code=409,
            )
        return _write_zip(meta2, included, excluded)
    finally:
        conn.rollback()


# ---------- 不落库校验 ----------

def spool_upload(src: BinaryIO) -> Path:
    """把上传内容流式写入临时文件（限制大小），返回临时文件路径。"""
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=".seal-verify-", suffix=".zip")
    size = 0
    try:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_VERIFY_UPLOAD_BYTES:
                raise SealPackageError(
                    f"上传文件超过大小上限（{human_size(MAX_VERIFY_UPLOAD_BYTES)}）",
                    status_code=413,
                )
            tmp.write(chunk)
        tmp.flush()
        return Path(tmp.name)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    finally:
        if not tmp.closed:
            tmp.close()


def _is_unsafe_zip_path(name: str) -> bool:
    """ZIP 条目路径安全检查：绝对路径、盘符、反斜杠、.. / 空段一律拒绝。"""
    if not name or "\x00" in name:
        return True
    if name.startswith(("/", "\\")) or "\\" in name:
        return True
    if _DRIVE_RE.match(name):
        return True
    return any(part in ("", ".", "..") for part in name.split("/"))


def _valid_file_spec(spec: Any) -> bool:
    return (
        isinstance(spec, dict)
        and isinstance(spec.get("path"), str)
        and bool(spec["path"])
        and isinstance(spec.get("size"), int)
        and spec["size"] >= 0
        and isinstance(spec.get("sha256"), str)
        and bool(_SHA256_RE.match(spec["sha256"]))
    )


def _hash_entry(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[Optional[int], Optional[str]]:
    """流式读取条目并计算 SHA-256；超过单文件上限或读取失败返回 (None, None)。"""
    hasher = hashlib.sha256()
    total = 0
    try:
        with zf.open(info, "r") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VERIFY_FILE_BYTES:
                    return None, None
                hasher.update(chunk)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return None, None
    return total, hasher.hexdigest()


def verify_seal_package(zip_path: Path) -> dict[str, Any]:
    """不落库校验封存包 ZIP，返回中文差异报告（不读写数据库）。"""
    issues: list[dict[str, str]] = []

    def add_issue(type_: str, path: str, message: str) -> None:
        if len(issues) >= MAX_VERIFY_ISSUES:
            return
        issues.append({"type": type_, "path": path, "message": message})
        if len(issues) == MAX_VERIFY_ISSUES:
            issues.append(
                {
                    "type": "limit",
                    "path": "",
                    "message": f"差异项过多，仅列出前 {MAX_VERIFY_ISSUES} 条",
                }
            )

    try:
        zf = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile):
        raise SealPackageError("无法读取：文件不是有效的 ZIP 封存包", status_code=422)

    package_info: Optional[dict[str, Any]] = None
    declared: dict[str, dict[str, Any]] = {}
    checked_files = 0

    with zf:
        try:
            file_infos = [i for i in zf.infolist() if not i.is_dir()]
        except (OSError, RuntimeError, zipfile.BadZipFile):
            raise SealPackageError("无法读取：ZIP 目录结构损坏", status_code=422)

        # 1. 路径穿越检查：危险条目只标记、绝不读取内容（本校验也不落盘解压）
        safe_infos: list[zipfile.ZipInfo] = []
        for info in file_infos:
            if _is_unsafe_zip_path(info.filename):
                add_issue(
                    "path_traversal",
                    info.filename,
                    "条目路径存在穿越风险（含 .. / 绝对路径 / 盘符 / 反斜杠），已跳过",
                )
            else:
                safe_infos.append(info)

        # 2. 条目数与解压总量限制（超限则不再逐项读取内容）
        over_limit = False
        if len(file_infos) > MAX_VERIFY_ENTRIES:
            add_issue(
                "limit",
                "",
                f"包内文件条目数（{len(file_infos)}）超过上限 {MAX_VERIFY_ENTRIES}",
            )
            over_limit = True
        total_uncompressed = sum(i.file_size for i in safe_infos)
        if total_uncompressed > MAX_VERIFY_UNCOMPRESSED_BYTES:
            add_issue(
                "limit",
                "",
                f"解压总量（{human_size(total_uncompressed)}）超过上限 "
                f"{human_size(MAX_VERIFY_UNCOMPRESSED_BYTES)}，已中止逐项核对",
            )
            over_limit = True

        # 3. 定位并解析清单（根目录或唯一一级子目录下的 manifest.json）
        manifest_entries = [
            i
            for i in safe_infos
            if i.filename == MANIFEST_NAME
            or (i.filename.endswith("/" + MANIFEST_NAME) and i.filename.count("/") == 1)
        ]
        manifest_entry: Optional[zipfile.ZipInfo] = None
        manifest: Any = None
        if not manifest_entries:
            add_issue("manifest", "", "包内缺少 manifest.json 清单，无法核对内容")
        elif len(manifest_entries) > 1:
            add_issue(
                "manifest",
                "",
                f"包内存在 {len(manifest_entries)} 个 manifest.json，无法确定唯一清单",
            )
        else:
            manifest_entry = manifest_entries[0]
            if manifest_entry.file_size > MAX_MANIFEST_BYTES:
                add_issue(
                    "manifest",
                    manifest_entry.filename,
                    f"清单文件超过大小上限（{human_size(MAX_MANIFEST_BYTES)}），已中止解析",
                )
            else:
                try:
                    manifest = json.loads(zf.read(manifest_entry).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    add_issue("manifest", manifest_entry.filename, "清单不是有效的 UTF-8 JSON")
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    add_issue("manifest", manifest_entry.filename, "清单条目读取失败（压缩数据损坏）")

        if isinstance(manifest, dict):
            prefix = manifest_entry.filename[: -len(MANIFEST_NAME)] if manifest_entry else ""
            package_info = _interpret_manifest(manifest, prefix, declared, add_issue)
        elif manifest is not None:
            add_issue("manifest", MANIFEST_NAME, "清单顶层结构不是 JSON 对象")

        # 4. 逐项核对（未超限时）：清单声明 -> 缺失 / 大小 / 摘要；随后检查多余文件
        if not over_limit and (declared or manifest_entry is not None):
            by_name = {i.filename: i for i in safe_infos}
            for full_path, spec in declared.items():
                info = by_name.get(full_path)
                if info is None:
                    add_issue("missing", full_path, f"清单声明的文件在包内缺失：{spec['label']}")
                    continue
                if info.file_size > MAX_VERIFY_FILE_BYTES:
                    add_issue(
                        "limit",
                        full_path,
                        f"文件超过单文件上限（{human_size(MAX_VERIFY_FILE_BYTES)}），跳过核对",
                    )
                    continue
                total, digest = _hash_entry(zf, info)
                if total is None:
                    add_issue("structure", full_path, "条目读取失败（压缩数据损坏或大小异常）")
                    continue
                checked_files += 1
                if total != spec["size"]:
                    add_issue(
                        "size_mismatch",
                        full_path,
                        f"大小不一致：清单声明 {spec['size']} 字节，包内实际 {total} 字节",
                    )
                    continue
                if digest != spec["sha256"]:
                    add_issue(
                        "digest_mismatch",
                        full_path,
                        f"SHA-256 摘要不一致：清单 {spec['sha256'][:12]}…，实测 {digest[:12]}…",
                    )
            declared_names = set(declared)
            if manifest_entry is not None:
                declared_names.add(manifest_entry.filename)
            for info in safe_infos:
                if info.filename not in declared_names:
                    add_issue("extra", info.filename, "包内存在清单未声明的多余文件")

    issue_count = len(issues)
    if issue_count:
        summary = (
            f"校验未通过：发现 {issue_count} 项差异"
            f"（缺失 {sum(1 for i in issues if i['type'] == 'missing')}、"
            f"多余 {sum(1 for i in issues if i['type'] == 'extra')}、"
            f"摘要异常 {sum(1 for i in issues if i['type'] == 'digest_mismatch')}、"
            f"其他 {sum(1 for i in issues if i['type'] not in ('missing', 'extra', 'digest_mismatch'))}）"
        )
    else:
        summary = (
            f"校验通过：清单结构与 {checked_files} 个声明文件的大小、SHA-256 摘要全部一致，"
            "未发现缺失或多余文件"
        )

    return {
        "valid": not issues,
        "package": package_info,
        "counts": {
            "entries": len(file_infos),
            "declared_files": len(declared),
            "checked_files": checked_files,
            "issues": issue_count,
        },
        "issues": issues,
        "issue_type_text": ISSUE_TYPE_TEXT,
        "summary": summary,
    }


def _interpret_manifest(
    manifest: dict[str, Any],
    prefix: str,
    declared: dict[str, dict[str, Any]],
    add_issue: Any,
) -> dict[str, Any]:
    """解读清单结构，收集声明文件（含前缀）到 declared。"""
    ptype = manifest.get("package_type")
    if ptype != PACKAGE_TYPE:
        add_issue(
            "manifest",
            MANIFEST_NAME,
            f"清单类型无法识别：{ptype!r}（应为 {PACKAGE_TYPE!r}）",
        )
    version = manifest.get("format_version")
    if version != FORMAT_VERSION:
        add_issue(
            "manifest",
            MANIFEST_NAME,
            f"不支持的清单格式版本：{version!r}（当前支持 {FORMAT_VERSION}）",
        )

    sample = manifest.get("sample")
    package_info = {
        "sample_code": sample.get("code") if isinstance(sample, dict) else None,
        "sample_name": sample.get("name") if isinstance(sample, dict) else None,
        "generated_at": manifest.get("generated_at"),
        "format_version": version,
        "chain_fingerprint": manifest.get("chain_fingerprint"),
    }

    def declare(spec: Any, label: str) -> None:
        if not _valid_file_spec(spec):
            add_issue("manifest", MANIFEST_NAME, f"清单中{label}缺少有效的 path/size/sha256 声明")
            return
        rel = spec["path"]
        if _is_unsafe_zip_path(rel):
            add_issue("path_traversal", rel, f"清单声明的路径存在穿越风险：{label}")
            return
        full = prefix + rel
        if full in declared:
            add_issue("structure", full, f"清单中路径重复声明：{label}")
            return
        declared[full] = {
            "size": spec["size"],
            "sha256": spec["sha256"].lower(),
            "label": label,
        }

    report = manifest.get("report")
    if not isinstance(report, dict):
        add_issue("manifest", MANIFEST_NAME, "清单缺少 report（审计报告）声明")
    else:
        declare(report, "审计报告")

    attachments = manifest.get("attachments")
    if not isinstance(attachments, list):
        add_issue("manifest", MANIFEST_NAME, "清单 attachments 字段不是数组")
    else:
        for idx, att in enumerate(attachments):
            if isinstance(att, dict):
                label = f"附件 #{att.get('attachment_id', '?')}（{att.get('filename', '?')}）"
            else:
                label = f"附件第 {idx + 1} 项"
            declare(att, label)

    return package_info
