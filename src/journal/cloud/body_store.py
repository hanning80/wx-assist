"""日记正文的云端存放与按需取回。

用户要求：本地 journal.db 不再长期保存日记正文，微云才是唯一正文仓。
但详情、编辑、删除、索引重建和 Bot 问答不能因此倒退，所以：
- 正文单独存成 JSON 对象（不加密），放“摘星日记/正文/”；
- 本地只留 summary/topics 等轻量元数据和云端对象指针；
- 需要正文时按 entry_id 从微云取回，不落长期本地文件。

受保护的日记（辣椒红 id=9）默认不迁移，仍在本地原样保留。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from .provider import make_client
from .text_export import write_json

logger = logging.getLogger(__name__)

BODY_KIND = "entry_body"
BODY_PREFIX = "日记正文-"
PROTECTED_IDS = {9}


def _safe_id(entry_id) -> int:
    try:
        return int(entry_id)
    except (TypeError, ValueError):
        return 0


def _download_json(client, obj) -> dict:
    link = client.download_url(obj["object_key"], obj["pdir_key"])
    url = link.get("https_download_url") or link.get("download_url")
    if not url:
        raise RuntimeError("微云没有返回正文下载地址")
    headers = {}
    if link.get("cookie"):
        headers["Cookie"] = link["cookie"]
    r = requests.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return json.loads(r.content.decode("utf-8-sig"))


def body_cloud_object(store, entry_id: int):
    rows = store.cloud_objects_of_entry(entry_id)
    for r in rows:
        if (r["kind"] or "") == BODY_KIND and (r["state"] or "") in ("uploaded", "purged"):
            return r
    return None


def put_entry_body(store, entry_id: int, payload: dict,
                   client=None) -> dict:
    """把一条日记的正文上传云端，并在本地记云端对象。"""
    entry_id = _safe_id(entry_id)
    if not entry_id:
        raise ValueError("entry_id 无效")
    client = client or make_client()
    export_dir = Path(store.db_path).parent / "journal" / "export" / "body"
    export_dir.mkdir(parents=True, exist_ok=True)
    name = "%s%d.json" % (BODY_PREFIX, entry_id)
    local = export_dir / name
    payload = dict(payload or {})
    payload.setdefault("entry_id", entry_id)
    payload["offloaded_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(payload, local)
    try:
        pdir = client.ensure_path(["摘星日记", "正文"])
        # 同名旧件先删，微云重名会报 1051
        try:
            out = client.call("weiyun.list", {"limit": 50, "get_type": 2,
                                              "dir_key": pdir, "pdir_key": pdir})
            for f in out.get("file_list", []):
                if (f.get("filename") or "") == name:
                    client.delete(f["file_id"], pdir, complete=True)
        except Exception:
            pass
        res = client.upload(str(local), pdir, filename=name)
        obj = store.add_cloud_object(
            kind=BODY_KIND, entry_id=entry_id,
            object_key=res.get("file_id", ""), pdir_key=pdir, name=name,
            bytes=os.path.getsize(str(local)), sha256="", local_path="",
        )
        logger.info("日记正文上云：id=%s %s", entry_id, name)
        return {"cloud_id": obj, "name": name, "pdir_key": pdir,
                "object_key": res.get("file_id", ""),
                "bytes": os.path.getsize(str(local)) if local.exists() else 0}
    finally:
        try:
            if local.exists():
                local.unlink()
        except OSError:
            pass


def fetch_entry_body(store, entry_id: int,
                     client=None) -> Optional[dict]:
    entry_id = _safe_id(entry_id)
    obj = body_cloud_object(store, entry_id)
    if obj is None:
        return None
    client = client or make_client()
    try:
        return _download_json(client, obj)
    except Exception as e:  # noqa: BLE001
        logger.warning("取回日记正文失败 id=%s: %s", entry_id, e)
        return None


def hydrate_entry(store, row, client=None) -> dict:
    """把 sqlite 行补成带正文的 dict；本地有正文就不动。"""
    if row is None:
        return {}
    d = dict(row)
    body = (d.get("full_text") or d.get("raw_text") or "").strip()
    if body:
        d["_body_source"] = "local"
        return d
    remote = fetch_entry_body(store, d.get("id", 0), client=client)
    if not remote:
        d["_body_source"] = "missing"
        return d
    d["raw_text"] = remote.get("raw_text") or ""
    d["full_text"] = remote.get("full_text") or ""
    d["summary"] = d.get("summary") or remote.get("summary") or ""
    d["topics"] = d.get("topics") or remote.get("topics") or ""
    d["attachments_text"] = remote.get("attachments_text") or []
    d["_body_source"] = "cloud"
    d["_remote_attachments"] = remote.get("attachments") or []
    return d


def clear_local_entry_body(store, entry_id: int) -> None:
    """正文已经上云并校验后，清掉本地长文本，保留摘要和元数据。"""
    entry_id = _safe_id(entry_id)
    with store._lock:
        store._conn.execute(
            "UPDATE diary_entry SET raw_text='', full_text='' WHERE id=?", (entry_id,))
        store._conn.execute(
            "UPDATE entry_source SET asr_raw='' WHERE id IN"
            " (SELECT source_id FROM diary_entry WHERE id=? AND source_id IS NOT NULL)",
            (entry_id,))
        store._conn.execute(
            "UPDATE reminder_chain SET source_text='' WHERE entry_id=?", (entry_id,))
        store._conn.commit()
    store.audit("system", "entry_body_offload", "entry", entry_id, "本地正文已清")


def build_body_payload(store, entry_id: int) -> dict:
    conn = store._conn
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, entry_date, created_at, kind, raw_text, summary, full_text,"
        " topics, entities, mood, confidence, source_id FROM diary_entry"
        " WHERE id=? AND deleted=0", (entry_id,)).fetchone()
    if row is None:
        raise LookupError("日记不存在或已删除")
    attachments = []
    for a in conn.execute(
        "SELECT id, kind, filename, note, extracted_text, cloud_pdir, cloud_name"
        " FROM attachment WHERE entry_id=? ORDER BY id", (entry_id,)).fetchall():
        attachments.append(dict(a))
    sources = []
    if row["source_id"] is not None:
        s = conn.execute(
            "SELECT chat_id, local_id, msg_time, msg_type, asr_raw FROM entry_source"
            " WHERE id=?", (row["source_id"],)).fetchone()
        if s:
            sources.append(dict(s))
    reminders = []
    for c in conn.execute(
        "SELECT title, start_date, days, daily_time, status, source_text"
        " FROM reminder_chain WHERE entry_id=? ORDER BY id", (entry_id,)).fetchall():
        reminders.append(dict(c))
    return {
        "schema": "wx-assist-journal-body-v1",
        "entry": dict(row),
        "raw_text": row["raw_text"] or "",
        "full_text": row["full_text"] or "",
        "summary": row["summary"] or "",
        "topics": row["topics"] or "",
        "attachments": attachments,
        "sources": sources,
        "reminders": reminders,
        "attachments_text": [a.get("extracted_text") or "" for a in attachments
                             if (a.get("extracted_text") or "").strip()],
    }


def migrate_entry(store, entry_id: int, verify: bool = True,
                  allow_protected: bool = False) -> dict:
    """上传并清空一条日记的本地正文；默认跳过 id=9。"""
    entry_id = _safe_id(entry_id)
    if entry_id in PROTECTED_IDS and not allow_protected:
        return {"id": entry_id, "skipped": "protected"}
    # 正文已经上过云、本地也只剩附件文字时，不重复上传；否则 worker 会天天
    # 把同一条附件日记重新传一遍。
    if body_cloud_object(store, entry_id):
        row = store._conn.execute(
            "SELECT raw_text, full_text FROM diary_entry WHERE id=?", (entry_id,)).fetchone()
        if row and not ((row[0] or "").strip() or (row[1] or "").strip()):
            return {"id": entry_id, "skipped": "already"}
    payload = build_body_payload(store, entry_id)
    if not (payload.get("raw_text") or payload.get("full_text")
            or payload.get("attachments_text")):
        return {"id": entry_id, "skipped": "empty"}
    rec = put_entry_body(store, entry_id, payload)
    if verify:
        remote = fetch_entry_body(store, entry_id)
        if not remote or (remote.get("full_text") or "") != (payload.get("full_text") or ""):
            # 上传读回不一致时不清本地，宁可多留一份
            raise RuntimeError("正文上云校验失败 id=%s" % entry_id)
    clear_local_entry_body(store, entry_id)
    return {"id": entry_id, "cloud": rec["name"], "bytes": rec.get("bytes")}
