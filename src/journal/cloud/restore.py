"""换机恢复：从微云取回最近一份全文包 + 当天/历史聊天增量，合并后落到新目录。

设计原则：
- 不覆盖当前机器上的 messages.db / journal.db，先恢复到新目录；
- 先读“聊天记录清单.json”，再下载增量，最后按 message_id 去重合并；
- 需要导入时，在恢复目录里生成新的 messages.db / journal.db，确认后再人工切换；
- 需要重建语义索引时，用恢复后的日记库调用 index_all。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from . import crypto
from .chat_merge import merge_package, merge_from_dir
from .provider import make_client
from .text_export import write_json

logger = logging.getLogger(__name__)

PREFIX = "知识库全文-"
INCREMENT_PREFIX = "聊天增量-"
MANIFEST_NAME = "聊天记录清单.json"
LIST_LIMIT = 50


def _list_all(client, pdir: str) -> list[dict]:
    """微云列表单页最多 50；offset 实测有效，按 finish_flag 翻到底。"""
    files: list[dict] = []
    offset = 0
    for _ in range(200):
        out = client.call("weiyun.list", {"limit": LIST_LIMIT, "get_type": 2,
                                          "dir_key": pdir, "pdir_key": pdir,
                                          "offset": offset})
        page = out.get("file_list") or []
        files.extend(page)
        offset += len(page)
        if not page or out.get("finish_flag") or len(page) < LIST_LIMIT:
            break
    return files


def _download_json_file(client, f: dict, pdir: str) -> dict:
    link = client.download_url(f["file_id"], pdir)
    url = link.get("https_download_url") or link.get("download_url")
    if not url:
        raise RuntimeError("微云没有返回下载地址：%s" % link)
    headers = {}
    if link.get("cookie"):
        headers["Cookie"] = link["cookie"]
    r = requests.get(url, headers=headers, timeout=120)
    r.raise_for_status()
    raw = r.content
    name = f.get("filename") or ""
    if name.endswith(".enc"):
        return json.loads(crypto.decrypt_bytes(raw).decode("utf-8-sig"))
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return json.loads(crypto.decrypt_bytes(raw).decode("utf-8-sig"))


def latest_export(client=None) -> dict:
    client = client or make_client()
    pdir = client.ensure_path(["摘星日记", "知识库全文"])
    files = [f for f in _list_all(client, pdir)
             if (f.get("filename") or "").startswith(PREFIX)]
    if not files:
        raise RuntimeError("云端“知识库全文”目录里没有找到备份")

    def _key(f: dict):
        name = f.get("filename") or ""
        m = re.search(r"(\d{8})", name)
        day = m.group(1) if m else ""
        # 同一天里优先取最新写入的文件；合并包和全量包谁后写谁生效，
        # 避免旧的合并包压住刚补过字段的新全量包。
        try:
            mtime = int(f.get("file_mtime") or 0)
        except (TypeError, ValueError):
            mtime = 0
        merged = 1 if "合并" in name else 0
        return (day, mtime, merged, 1 if name.endswith(".json") else 0, name)
    files.sort(key=_key)
    return {"pdir_key": pdir, "file": files[-1], "all": files}


def read_manifest(client=None) -> dict:
    client = client or make_client()
    pdir = client.ensure_path(["摘星日记", "聊天记录"])
    files = [f for f in _list_all(client, pdir)
             if (f.get("filename") or "") == MANIFEST_NAME]
    if not files:
        return {}
    try:
        return _download_json_file(client, files[-1], pdir)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取聊天清单失败: %s", e)
        return {}


def download_increments(out_dir: str | Path, client=None,
                        keep: int = 0) -> list[str]:
    client = client or make_client()
    pdir = client.ensure_path(["摘星日记", "聊天记录", "增量"])
    files = [f for f in _list_all(client, pdir)
             if (f.get("filename") or "").startswith(INCREMENT_PREFIX)]
    files.sort(key=lambda f: (f.get("filename") or "", f.get("file_size") or 0))
    inc_dir = Path(out_dir) / "增量"
    inc_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for f in files:
        name = Path((f.get("filename") or "增量.json").replace("\\", "/")).name
        try:
            payload = _download_json_file(client, f, pdir)
            p = inc_dir / name
            write_json(payload, p)
            paths.append(str(p))
        except Exception as e:  # noqa: BLE001
            logger.warning("下载增量失败 %s: %s", name, e)
    logger.info("已下载聊天增量 %d 个（云端共 %d 个）", len(paths), len(files))
    return paths


def restore_bundle(out_dir: Optional[str] = None,
                   client=None) -> dict:
    """下载全量 + 全部增量并合并，返回本地恢复目录和合并包路径。"""
    client = client or make_client()
    out = Path(out_dir or (Path(r"F:\微信监控\wx-assist\data\journal\export") /
                           ("恢复-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))))
    out.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(client)
    info = latest_export(client)
    f = info["file"]
    payload = _download_json_file(client, f, info["pdir_key"])
    full_name = Path((f.get("filename") or "知识库全文.json").replace("\\", "/")).name
    if full_name.endswith(".enc"):
        full_name = full_name[:-4]
    full_path = out / full_name
    write_json(payload, full_path)
    increments = download_increments(out, client=client)
    merged = merge_package(payload, [
        json.loads(Path(p).read_text(encoding="utf-8")) for p in increments
    ], when=datetime.now())
    merged_path = out / ("知识库全文-恢复合并-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    write_json(merged, merged_path)
    notes = {
        "schema": "wx-assist-restore-bundle-v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_full": full_name,
        "manifest": manifest,
        "increments": [Path(p).name for p in increments],
        "counts": merged.get("counts", {}),
        "merged_path": str(merged_path),
    }
    write_json(notes, out / "恢复说明.json")
    logger.info("恢复包已生成：%s（聊天 %s 条）", merged_path,
                merged.get("counts", {}).get("messages"))
    return {"out_dir": str(out), "full": str(full_path),
            "merged": str(merged_path), "increments": len(increments),
            "manifest": manifest, "counts": merged.get("counts", {})}


def import_bundle(merged_path: str | Path, db_dir: str | Path,
                  rebuild_index: bool = False) -> dict:
    """把合并包写成新的 messages.db / journal.db；不碰当前正在用的库。"""
    payload = json.loads(Path(merged_path).read_text(encoding="utf-8"))
    db_dir = Path(db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)

    # ── messages.db ──
    msg_db = db_dir / "messages.db"
    if msg_db.exists():
        raise RuntimeError("目标 messages.db 已存在，拒绝覆盖：%s" % msg_db)
    conn = sqlite3.connect(str(msg_db))
    conn.execute(
        "CREATE TABLE messages (message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
        " sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',"
        " msg_type INTEGER NOT NULL DEFAULT 1, timestamp INTEGER NOT NULL,"
        " created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')))")
    conn.execute(
        "CREATE TABLE fav_cache (fav_id INTEGER PRIMARY KEY, type INTEGER DEFAULT 0,"
        " type_name TEXT DEFAULT '', title TEXT DEFAULT '', description TEXT DEFAULT '',"
        " link TEXT DEFAULT '', from_user TEXT DEFAULT '', update_time INTEGER DEFAULT 0,"
        " chat_records_json TEXT DEFAULT '', media_json TEXT DEFAULT '',"
        " clean_text TEXT DEFAULT '', cached_at INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE contacts_cache (username TEXT PRIMARY KEY, alias TEXT NOT NULL DEFAULT '',"
        " nick_name TEXT NOT NULL DEFAULT '', remark TEXT NOT NULL DEFAULT '',"
        " display_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'user',"
        " updated_at INTEGER NOT NULL DEFAULT 0)")
    msgs = 0
    for m in payload.get("messages") or []:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO messages(message_id, chat_id, sender_id, sender_name,"
                " content, msg_type, timestamp, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (m.get("message_id"), m.get("chat_id") or "", m.get("sender_id") or "",
                 m.get("sender_name") or "", m.get("content") or "",
                 int(m.get("msg_type") or 1), int(m.get("timestamp") or 0),
                 int(m.get("created_at") or 0)))
            msgs += 1
        except Exception:
            pass
    favs = 0
    for f in payload.get("favorites") or []:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO fav_cache(fav_id, type, type_name, title,"
                " description, link, from_user, update_time, chat_records_json,"
                " media_json, clean_text, cached_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (int(f.get("fav_id") or 0), int(f.get("type") or 0),
                 f.get("type_name") or "", f.get("title") or "",
                 f.get("description") or "", f.get("link") or "",
                 f.get("from_user") or "", int(f.get("update_time") or 0),
                 f.get("chat_records_json") or "", f.get("media_json") or "",
                 f.get("clean_text") or "", int(f.get("cached_at") or 0)))
            favs += 1
        except Exception as ex:  # noqa: BLE001
            logger.warning("导入收藏失败 id=%s: %s", f.get("fav_id"), ex)
    contacts = 0
    for c in payload.get("contacts") or []:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO contacts_cache(username, alias, nick_name,"
                " remark, display_name, kind, updated_at) VALUES(?,?,?,?,?,?,?)",
                (c.get("username") or "", c.get("alias") or "",
                 c.get("nick_name") or "", c.get("remark") or "",
                 c.get("display_name") or "", c.get("kind") or "user",
                 int(c.get("updated_at") or 0)))
            contacts += 1
        except Exception as ex:  # noqa: BLE001
            logger.warning("导入联系人失败 username=%s: %s", c.get("username"), ex)
    conn.commit()
    conn.close()

    # ── journal.db ──
    from src.journal.store import JournalStore
    journal_db = db_dir / "journal.db"
    if journal_db.exists():
        raise RuntimeError("目标 journal.db 已存在，拒绝覆盖：%s" % journal_db)
    store = JournalStore(journal_db)
    diary = 0
    for e in payload.get("diary") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO diary_entry(id, entry_date, created_at, kind, raw_text,"
                " summary, full_text, topics, entities, mood, confidence, needs_review,"
                " source_id, chain_id, deleted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.get("id"), e.get("entry_date"), e.get("created_at"), e.get("kind"),
                 e.get("raw_text") or "", e.get("summary") or "", e.get("full_text") or "",
                 e.get("topics") or "", e.get("entities") or "", e.get("mood") or "",
                 e.get("confidence") or 0, int(e.get("needs_review") or 0),
                 e.get("source_id"), e.get("chain_id"), int(e.get("deleted") or 0)))
            diary += 1
        except Exception as ex:
            logger.warning("导入日记失败 id=%s: %s", e.get("id"), ex)
    for a in payload.get("attachments") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO attachment(id, chat_id, local_id, server_id, msg_type,"
                " kind, filename, local_path, bytes, sha256, extracted_text, note, status,"
                " entry_id, cloud_entry_id, cloud_pdir, cloud_name, upload_error, created_at,"
                " attached_at, uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.get("id"), a.get("chat_id") or "", a.get("local_id") or 0,
                 a.get("server_id"), a.get("msg_type"), a.get("kind") or "file",
                 a.get("filename") or "", a.get("local_path") or "", a.get("bytes") or 0,
                 a.get("sha256") or "", a.get("extracted_text") or "", a.get("note") or "",
                 a.get("status") or "pending", a.get("entry_id"), a.get("cloud_entry_id"),
                 a.get("cloud_pdir") or "", a.get("cloud_name") or "",
                 a.get("upload_error") or "", a.get("created_at") or "",
                 a.get("attached_at"), a.get("uploaded_at")))
        except Exception as ex:
            logger.warning("导入附件失败 id=%s: %s", a.get("id"), ex)
    # 正文云端指针：没有它，换机后日记正文为空、详情和问答取不回云正文。
    for o in payload.get("cloud_objects") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO cloud_object(id, kind, entry_id, object_key,"
                " pdir_key, name, bytes, sha256, local_path, created_at, state, purged_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (o.get("id"), o.get("kind") or "", o.get("entry_id"),
                 o.get("object_key") or "", o.get("pdir_key") or "",
                 o.get("name") or "", o.get("bytes") or 0, o.get("sha256") or "",
                 o.get("local_path") or "", o.get("created_at") or "",
                 o.get("state") or "uploaded", o.get("purged_at")))
        except Exception as ex:
            logger.warning("导入云端对象失败 id=%s: %s", o.get("id"), ex)
    for s in payload.get("sources") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO entry_source(id, chat_id, local_id, server_id,"
                " msg_time, msg_type, voice_path, voice_duration_ms, asr_engine,"
                " asr_latency_ms, asr_raw, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.get("id"), s.get("chat_id") or "", s.get("local_id") or 0,
                 s.get("server_id"), s.get("msg_time") or "", s.get("msg_type") or "",
                 s.get("voice_path") or "", s.get("voice_duration_ms") or 0,
                 s.get("asr_engine") or "", s.get("asr_latency_ms") or 0,
                 s.get("asr_raw") or "", s.get("created_at") or ""))
        except Exception as ex:
            logger.warning("导入消息来源失败 id=%s: %s", s.get("id"), ex)
    for p in payload.get("purged") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO purged_source(chat_id, local_id, purged_at)"
                " VALUES(?,?,?)",
                (p.get("chat_id") or "", p.get("local_id") or 0,
                 p.get("purged_at") or ""))
        except Exception as ex:
            logger.warning("导入已清理来源失败: %s", ex)
    for r in payload.get("reminders") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO reminder_chain(id, entry_id, title, object,"
                " action, start_date, days, daily_time, status, ask_count, created_at,"
                " activated_at, finished_at, end_notice_sent, source_text)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.get("id"), r.get("entry_id"), r.get("title") or "",
                 r.get("object") or "", r.get("action") or "",
                 r.get("start_date") or "", int(r.get("days") or 1),
                 r.get("daily_time") or "", r.get("status") or "active",
                 int(r.get("ask_count") or 0), r.get("created_at") or "",
                 r.get("activated_at"), r.get("finished_at"),
                 int(r.get("end_notice_sent") or 0), r.get("source_text") or ""))
        except Exception as ex:
            logger.warning("导入生活提醒失败 id=%s: %s", r.get("id"), ex)
    for t in payload.get("reminder_tasks") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO reminder_task(id, chain_id, seq, kind,"
                " fire_date, fire_at, status, sent_at, retry_count, last_error,"
                " delivery_via) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (t.get("id"), t.get("chain_id"), t.get("seq"), t.get("kind") or "",
                 t.get("fire_date") or "", t.get("fire_at") or "",
                 t.get("status") or "pending", t.get("sent_at"),
                 int(t.get("retry_count") or 0), t.get("last_error") or "",
                 t.get("delivery_via") or ""))
        except Exception as ex:
            logger.warning("导入提醒任务失败 id=%s: %s", t.get("id"), ex)
    for a in payload.get("answer_rules") or []:
        try:
            store._conn.execute(
                "INSERT OR REPLACE INTO answer_rule(id, rule_key, enabled, rule_text,"
                " updated_at) VALUES(?,?,?,?,?)",
                (a.get("id"), a.get("rule_key") or "", int(a.get("enabled") or 1),
                 a.get("rule_text") or "", a.get("updated_at") or ""))
        except Exception as ex:
            logger.warning("导入回答规则失败 key=%s: %s", a.get("rule_key"), ex)
    store._conn.commit()
    store.close()

    rebuilt = 0
    if rebuild_index:
        import src.journal.recall as recall
        recall.JOURNAL_DB = journal_db
        recall.STORE_PATH = db_dir / "chroma-journal"
        rebuilt = recall.index_all()
    return {"db_dir": str(db_dir), "messages": msgs, "favorites": favs,
            "contacts": contacts, "diary": diary, "indexed": rebuilt}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="从微云恢复日记与聊天")
    ap.add_argument("--out", default="", help="恢复目录；默认在 data/journal/export 下新建")
    ap.add_argument("--apply", action="store_true", help="同时生成新的数据库到恢复目录")
    ap.add_argument("--rebuild-index", action="store_true", help="恢复后重建日记语义索引")
    args = ap.parse_args()
    bundle = restore_bundle(args.out or None)
    print("恢复包：%s" % bundle["merged"])
    print("清单：last_rowid=%s，增量 %d 个" % (
        (bundle.get("manifest") or {}).get("last_rowid", "?"), bundle["increments"]))
    print("内容：%s" % bundle["counts"])
    if args.apply:
        imported = import_bundle(bundle["merged"], Path(bundle["out_dir"]) / "数据库",
                                 rebuild_index=args.rebuild_index)
        print("已生成新库：%s" % imported)
    else:
        print("未写入数据库；确认恢复包后再加 --apply。当前库不会被动。")


if __name__ == "__main__":
    main()
