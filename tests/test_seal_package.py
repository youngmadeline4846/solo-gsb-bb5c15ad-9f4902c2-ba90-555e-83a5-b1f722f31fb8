"""证据封存包测试。

覆盖：导出 ZIP（清单 / 审计报告 / 未损坏附件、清单字段与 SHA-256 核对）、
损坏附件排除并登记清单、生成期间链路或附件记录变化中止（409）、
不落库校验（通过 / 篡改摘要 / 缺失 / 多余 / 路径穿越 / 解压总量限制 /
缺清单 / 非 ZIP）。
"""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="audit-seal-tests-")
os.environ["AUDIT_DB_PATH"] = os.path.join(_tmp, "test_seal.db")
os.environ["ATTACHMENT_DIR"] = os.path.join(_tmp, "attachments")

import hashlib
import io
import itertools
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import seal_package
from app.attachments import attachment_dir
from app.db import get_connection, init_db
from app.main import app


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


_seq = itertools.count(1)


@pytest.fixture()
def sample(client):
    code = f"UT-SEAL-{next(_seq):03d}"
    resp = client.post(
        "/api/samples",
        json={"code": code, "name": "封存包测试样本", "description": ""},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture()
def event(client, sample):
    resp = client.post(
        f"/api/samples/{sample['id']}/events",
        json={"custodian": "张三", "start_time": "2026-09-18 09:00:00",
              "end_time": "2026-09-18 10:00:00"},
    )
    assert resp.status_code == 201
    return resp.json()


def _pdf(seed: bytes = b"") -> bytes:
    return b"%PDF-1.4\n" + seed + os.urandom(8)


def _png(seed: bytes = b"") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + seed + os.urandom(8)


def _upload(client, event_id, content, filename="证据.pdf",
            content_type="application/pdf", uploaded_by="审计员 王敏"):
    return client.post(
        f"/api/events/{event_id}/attachments",
        files={"file": (filename, content, content_type)},
        data={"uploaded_by": uploaded_by},
    )


def _blob_file(digest: str):
    return attachment_dir() / digest[:2] / digest[2:4] / digest


def _export(client, sample_id):
    return client.get(f"/api/samples/{sample_id}/seal-package")


def _read_manifest(zip_bytes: bytes):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    prefix = zf.namelist()[0].split("/")[0]
    return zf, prefix, json.loads(zf.read(f"{prefix}/manifest.json").decode("utf-8"))


def _verify(client, zip_bytes: bytes, filename="pkg.zip"):
    return client.post(
        "/api/seal-packages/verify",
        files={"file": (filename, zip_bytes, "application/zip")},
    )


def _rebuild_zip(original: bytes, drop=(), add=None, replace=None):
    """复制 ZIP，可丢弃 / 替换 / 追加条目（目录条目自动跳过）。"""
    src = zipfile.ZipFile(io.BytesIO(original))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.is_dir() or info.filename in drop:
                continue
            data = src.read(info.filename)
            if replace and info.filename in replace:
                data = replace[info.filename]
            dst.writestr(info.filename, data)
        for name, data in (add or {}).items():
            dst.writestr(name, data)
    return buf.getvalue()


# ---------- 导出 ----------

def test_export_manifest_report_and_attachments(client, sample, event):
    pdf, png = _pdf(), _png()
    a1 = _upload(client, event["id"], pdf, "证据一.pdf").json()
    a2 = _upload(client, event["id"], png, "现场照片.png", "image/png").json()

    resp = _export(client, sample["id"])
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "seal_package_" in resp.headers["content-disposition"]
    assert resp.headers["X-Seal-Attachments"] == "2"
    assert resp.headers["X-Seal-Excluded"] == "0"

    zf, prefix, manifest = _read_manifest(resp.content)
    names = zf.namelist()
    assert f"{prefix}/manifest.json" in names
    assert f"{prefix}/report.md" in names

    # 清单：样本 / 事件 / 附件逻辑关系
    assert manifest["package_type"] == "evidence-seal-package"
    assert manifest["format_version"] == 1
    assert manifest["sample"]["code"] == sample["code"]
    assert len(manifest["events"]) == 1
    ev = manifest["events"][0]
    assert ev["id"] == event["id"]
    assert ev["attachment_ids"] == [a1["id"], a2["id"]]
    assert len(manifest["chain_fingerprint"]) == 64
    assert manifest["counts"] == {"events": 1, "attachments": 2, "excluded_attachments": 0}
    assert manifest["excluded_attachments"] == []

    # 附件条目：路径 / 大小 / SHA-256，且包内内容一致
    by_name = {a["filename"]: a for a in manifest["attachments"]}
    assert set(by_name) == {"证据一.pdf", "现场照片.png"}
    e1 = by_name["证据一.pdf"]
    assert e1["event_id"] == event["id"]
    assert e1["size"] == len(pdf)
    assert e1["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert e1["path"].startswith(f"attachments/{event['id']}/")
    data = zf.read(f"{prefix}/{e1['path']}")
    assert hashlib.sha256(data).hexdigest() == e1["sha256"]

    # 审计报告条目：路径 / 大小 / SHA-256 与包内文件一致
    report = manifest["report"]
    assert report["path"] == "report.md"
    report_bytes = zf.read(f"{prefix}/report.md")
    assert report["size"] == len(report_bytes)
    assert hashlib.sha256(report_bytes).hexdigest() == report["sha256"]
    text = report_bytes.decode("utf-8")
    assert "样本交接链路审计报告" in text
    assert "证据一.pdf" in text


def test_export_sample_without_events(client, sample):
    resp = _export(client, sample["id"])
    assert resp.status_code == 200
    zf, prefix, manifest = _read_manifest(resp.content)
    assert manifest["events"] == []
    assert manifest["attachments"] == []
    assert f"{prefix}/report.md" in zf.namelist()


def test_export_404_for_missing_sample(client):
    assert client.get("/api/samples/999999/seal-package").status_code == 404


def test_export_excludes_corrupt_attachment(client, sample, event):
    ok = _upload(client, event["id"], _pdf(), "正常.pdf").json()
    bad = _upload(client, event["id"], _png(), "丢失.png", "image/png").json()
    _blob_file(bad["digest"]).unlink()

    resp = _export(client, sample["id"])
    assert resp.status_code == 200
    assert resp.headers["X-Seal-Attachments"] == "1"
    assert resp.headers["X-Seal-Excluded"] == "1"

    zf, prefix, manifest = _read_manifest(resp.content)
    assert [a["filename"] for a in manifest["attachments"]] == ["正常.pdf"]
    assert len(manifest["excluded_attachments"]) == 1
    exc = manifest["excluded_attachments"][0]
    assert exc["attachment_id"] == bad["id"]
    assert exc["filename"] == "丢失.png"
    assert exc["reason"] == "missing"
    assert "实体文件缺失" in exc["reason_text"]
    assert not any(n.endswith("丢失.png") for n in zf.namelist())
    # 正常附件仍在包内
    assert any(n.endswith("正常.pdf") for n in zf.namelist())


def test_export_aborts_when_chain_changes(client, sample, event, monkeypatch):
    """生成期间链路变化（另一会话新增事件）-> 中止并返回 409。"""
    original = seal_package._reverify_attachments

    def sneaky(meta):
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO events (sample_id, custodian, start_time, end_time, note, seq) "
                "VALUES (?, '李四', '2026-09-18 10:00:00', NULL, '', 99)",
                (sample["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        return original(meta)

    monkeypatch.setattr(seal_package, "_reverify_attachments", sneaky)
    resp = _export(client, sample["id"])
    assert resp.status_code == 409
    assert "已变化" in resp.json()["detail"]
    assert "混合快照" in resp.json()["detail"]


def test_export_aborts_when_attachment_record_changes(client, sample, event, monkeypatch):
    """生成期间附件记录变化（另一会话删除附件记录）-> 中止并返回 409。"""
    a = _upload(client, event["id"], _pdf(), "会被删.pdf").json()
    original = seal_package._reverify_attachments

    def sneaky(meta):
        conn = get_connection()
        try:
            conn.execute("DELETE FROM attachments WHERE id = ?", (a["id"],))
            conn.commit()
        finally:
            conn.close()
        return original(meta)

    monkeypatch.setattr(seal_package, "_reverify_attachments", sneaky)
    resp = _export(client, sample["id"])
    assert resp.status_code == 409
    assert "已变化" in resp.json()["detail"]


# ---------- 不落库校验 ----------

def test_verify_roundtrip_ok(client, sample, event):
    _upload(client, event["id"], _pdf(), "证据A.pdf")
    _upload(client, event["id"], _png(), "证据B.png", "image/png")
    exported = _export(client, sample["id"])
    assert exported.status_code == 200

    resp = _verify(client, exported.content)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["valid"] is True
    assert result["issues"] == []
    assert result["counts"]["declared_files"] == 3  # 2 附件 + 1 报告
    assert result["counts"]["checked_files"] == 3
    assert result["package"]["sample_code"] == sample["code"]
    assert "校验通过" in result["summary"]


def test_verify_detects_tampered_attachment(client, sample, event):
    a = _upload(client, event["id"], _pdf(), "被篡改.pdf").json()
    exported = _export(client, sample["id"])
    zf, prefix, manifest = _read_manifest(exported.content)
    entry = manifest["attachments"][0]
    full = f"{prefix}/{entry['path']}"
    original = zf.read(full)
    # 等长篡改：只触发摘要异常，不触发大小不一致
    tampered = original[:-1] + bytes([original[-1] ^ 0xFF])
    forged = _rebuild_zip(exported.content, replace={full: tampered})

    result = _verify(client, forged).json()
    assert result["valid"] is False
    digest_issues = [i for i in result["issues"] if i["type"] == "digest_mismatch"]
    assert len(digest_issues) == 1
    assert "摘要" in digest_issues[0]["message"]
    assert digest_issues[0]["path"] == full


def test_verify_detects_missing_and_extra(client, sample, event):
    _upload(client, event["id"], _pdf(), "会缺失.pdf")
    exported = _export(client, sample["id"])
    zf, prefix, manifest = _read_manifest(exported.content)
    full = f"{prefix}/{manifest['attachments'][0]['path']}"
    forged = _rebuild_zip(exported.content, drop={full}, add={f"{prefix}/stray.txt": b"x"})

    result = _verify(client, forged).json()
    assert result["valid"] is False
    types = {i["type"] for i in result["issues"]}
    assert "missing" in types
    assert "extra" in types
    missing = next(i for i in result["issues"] if i["type"] == "missing")
    assert missing["path"] == full
    assert "缺失" in missing["message"]
    extra = next(i for i in result["issues"] if i["type"] == "extra")
    assert extra["path"].endswith("stray.txt")
    assert "多余" in extra["message"]


def test_verify_detects_size_mismatch(client, sample, event):
    _upload(client, event["id"], _pdf(), "变长.pdf")
    exported = _export(client, sample["id"])
    zf, prefix, manifest = _read_manifest(exported.content)
    entry = manifest["attachments"][0]
    full = f"{prefix}/{entry['path']}"
    forged = _rebuild_zip(exported.content, replace={full: zf.read(full) + b"tail"})

    result = _verify(client, forged).json()
    assert result["valid"] is False
    size_issues = [i for i in result["issues"] if i["type"] == "size_mismatch"]
    assert len(size_issues) == 1
    assert "大小不一致" in size_issues[0]["message"]


def test_verify_rejects_path_traversal(client, sample, event):
    _upload(client, event["id"], _pdf(), "正常.pdf")
    exported = _export(client, sample["id"])
    forged = _rebuild_zip(exported.content, add={"../evil.txt": b"x", "a/../../b.txt": b"y"})

    result = _verify(client, forged).json()
    assert result["valid"] is False
    traversal = [i for i in result["issues"] if i["type"] == "path_traversal"]
    assert len(traversal) == 2
    assert any(".." in i["path"] for i in traversal)
    assert all("穿越" in i["message"] for i in traversal)


def test_verify_limits_uncompressed_total(client, sample, event, monkeypatch):
    _upload(client, event["id"], _pdf(), "大文件.pdf")
    exported = _export(client, sample["id"])
    monkeypatch.setattr(seal_package, "MAX_VERIFY_UNCOMPRESSED_BYTES", 10)

    result = _verify(client, exported.content).json()
    assert result["valid"] is False
    limit = [i for i in result["issues"] if i["type"] == "limit"]
    assert limit
    assert "解压总量" in limit[0]["message"]


def test_verify_missing_manifest(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "没有清单")
    result = _verify(client, buf.getvalue()).json()
    assert result["valid"] is False
    assert any(i["type"] == "manifest" and "manifest.json" in i["message"]
               for i in result["issues"])


def test_verify_rejects_non_zip(client):
    resp = _verify(client, b"this is not a zip file")
    assert resp.status_code == 422
    assert "ZIP" in resp.json()["detail"]


def test_verify_does_not_write_db(client, sample, event):
    """校验入口不落库：校验前后数据库行数不变。"""
    _upload(client, event["id"], _pdf(), "只读校验.pdf")
    exported = _export(client, sample["id"])

    def counts():
        conn = get_connection()
        try:
            return tuple(
                conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("samples", "events", "attachments", "attachment_blobs")
            )
        finally:
            conn.close()

    before = counts()
    resp = _verify(client, exported.content)
    assert resp.status_code == 200
    assert counts() == before
