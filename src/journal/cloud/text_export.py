"""知识库全文导出：把日记、附件文字、收藏文字拼成一份可迁移的 JSON。

为什么要这份文件：
- 微云上的数据库快照只有 journal.db，换机器能恢复日记库，但不直观；
- 这份 JSON 把“所有可检索的文字”放在一个文件里，换机、备份、人工查看都方便；
- 语义索引不单独上传，换机后按这份全文重新分块建索引即可，避免搬巨大的向量库。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

SCHEMA = "wx-assist-knowledge-export-v1"


def _strip_binary_sections(text: str, names: list[str]) -> str:
    """Drop OCR text blocks that came from images/videos.

    The attachment metadata (filename, cloud path) can stay; the image/video
    derived text must not enter the knowledge bundle or semantic index.
    """
    if not text:
        return text
    for name in names:
        text = re.sub(r"【%s 正文】.*?(?=\n【|\Z)" % re.escape(name), "", text,
                      flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_text(path: str) -> str:
    """Read a generated review HTML and keep its visible text."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = (raw.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"[ \t\r\f\v]+", " ", raw).strip()


def _rows(db_path: str, sql: str) -> list[dict]:
    if not db_path or not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def build_export(journal_db: str, messages_db: Optional[str] = None,
                 when: Optional[datetime] = None,
                 include_cloud_objects: bool = True,
                 cloud_prefix: Optional[str] = None) -> dict:
    """Build one portable text bundle.

    ``include_cloud_objects`` defaults to True so the existing restore path keeps
    working. A full cloud replacement must pass False: old file ids and parent
    directory keys become invalid as soon as the old cloud tree is deleted.
    """
    when = when or datetime.now()
    diary = _rows(journal_db,
                  "SELECT * FROM diary_entry WHERE deleted=0 ORDER BY id")
    all_attachments = _rows(journal_db,
                            "SELECT * FROM attachment WHERE status!='deleted' ORDER BY id")
    # Images/videos are not knowledge. Keep their names/paths only in diary
    # metadata if already present, but do not export their records or OCR text.
    binary_names = [a.get("filename") or "" for a in all_attachments
                    if a.get("kind") in ("image", "video")]
    attachments = [a for a in all_attachments if a.get("kind") not in ("image", "video")]
    for entry in diary:
        entry["full_text"] = _strip_binary_sections(entry.get("full_text") or "", binary_names)
    cloud_objects = (_rows(journal_db, "SELECT * FROM cloud_object ORDER BY id")
                     if include_cloud_objects else [])
    if cloud_objects and cloud_prefix:
        # 换到新对象存储后，只带新桶里真实存在的指针；旧微云 file_id 全部丢掉。
        cloud_objects = [o for o in cloud_objects
                         if (o.get("object_key") or "").startswith(cloud_prefix)
                         or (o.get("pdir_key") or "").startswith(cloud_prefix)]
    sources = _rows(journal_db,
                    "SELECT * FROM entry_source ORDER BY id")
    purged = _rows(journal_db,
                   "SELECT * FROM purged_source ORDER BY chat_id, local_id")
    reminders = _rows(journal_db,
                      "SELECT * FROM reminder_chain ORDER BY id")
    reminder_tasks = _rows(journal_db,
                           "SELECT * FROM reminder_task ORDER BY chain_id, seq")
    answer_rules = _rows(journal_db,
                         "SELECT * FROM answer_rule ORDER BY id")
    reviews = _rows(journal_db,
                    "SELECT id, week_start, week_end, generated_at, html_path,"
                    " stats, narrative FROM weekly_review ORDER BY id")
    for review in reviews:
        review["html_text"] = _html_text(review.pop("html_path", "") or "")
    favorites = []
    messages = []
    contacts = []
    oa_articles = []
    oa_accounts = []
    voice_jobs = []
    if messages_db:
        # Video-channel / video favorites (type 4 and 20) never enter the
        # knowledge bundle. Moments (sns_cache) are deliberately not exported.
        favorites = _rows(messages_db,
                          "SELECT * FROM fav_cache"
                          " WHERE type NOT IN (4, 20)"
                          " ORDER BY fav_id")
        messages = _rows(messages_db,
                         "SELECT message_id, chat_id, sender_id, sender_name, content,"
                         " msg_type, timestamp, created_at FROM messages"
                         " ORDER BY timestamp, message_id")
        contacts = _rows(messages_db,
                         "SELECT username, alias, nick_name, remark, display_name,"
                         " kind, updated_at FROM contacts_cache ORDER BY username")
        oa_articles = _rows(messages_db,
                            "SELECT url, gh_id, title, digest, source_name, pub_time,"
                            " full_content, content_status, llm_summary, cached_at"
                            " FROM oa_cache ORDER BY pub_time, url")
        oa_accounts = _rows(messages_db,
                            "SELECT gh_id, display_name, avatar_url, last_updated"
                            " FROM oa_accounts ORDER BY gh_id")
        voice_jobs = _rows(messages_db,
                           "SELECT message_id, chat_id, local_id, server_id, msg_time,"
                           " state, attempts, text, engine, error, cloud_dirty"
                           " FROM voice_jobs ORDER BY id")
    return {
        "schema": SCHEMA,
        "exported_at": when.isoformat(timespec="seconds"),
        "mode": "full-replacement-bundle",
        "index_plan": {
            "chunk_chars": 400,
            "overlap": 80,
            "note": "换机后按日记/聊天正文重新分块建语义索引；本文件不搬向量库。",
        },
        "excludes": {
            "image": True,
            "video": True,
            "moments": True,
            "reason": "图片、视频、朋友圈不进入知识库与检索。",
        },
        "counts": {
            "diary": len(diary),
            "attachments": len(attachments),
            "cloud_objects": len(cloud_objects),
            "sources": len(sources),
            "reminders": len(reminders),
            "reminder_tasks": len(reminder_tasks),
            "answer_rules": len(answer_rules),
            "reviews": len(reviews),
            "favorites": len(favorites),
            "oa_articles": len(oa_articles),
            "oa_accounts": len(oa_accounts),
            "messages": len(messages),
            "contacts": len(contacts),
            "voice_jobs": len(voice_jobs),
        },
        "diary": diary,
        "attachments": attachments,
        "cloud_objects": cloud_objects,
        "sources": sources,
        "purged": purged,
        "reminders": reminders,
        "reminder_tasks": reminder_tasks,
        "answer_rules": answer_rules,
        "reviews": reviews,
        "favorites": favorites,
        "oa_articles": oa_articles,
        "oa_accounts": oa_accounts,
        "messages": messages,
        "contacts": contacts,
        "voice_jobs": voice_jobs,
    }


def write_json(payload: dict, path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(str(tmp), str(path))
    return str(path)
