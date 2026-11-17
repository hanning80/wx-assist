"""收藏补充查询：把收藏的标题、正文和高德链接直接带到回答里。

收藏的向量块只存了展平后的正文，位置链接没有进块。这里按 fav_id
回查 messages.db，避免为了补一个链接重建整个收藏索引。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
MESSAGES_DB = REPO_ROOT / "data" / "messages.db"


def favorite_details(fav_id) -> Optional[dict]:
    """返回一条收藏的标题、类型、正文、链接；查不到返回 None。"""
    try:
        fid = int(fav_id)
    except (TypeError, ValueError):
        return None
    if not MESSAGES_DB.exists():
        return None
    conn = sqlite3.connect(str(MESSAGES_DB))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT fav_id, type, type_name, title, description, link, clean_text"
            " FROM fav_cache WHERE fav_id=?", (fid,)
        ).fetchone()
        if row is None:
            return None
        return {
            "fav_id": row["fav_id"],
            "type": row["type"] or 0,
            "type_name": row["type_name"] or "收藏",
            "title": row["title"] or "",
            "description": row["description"] or "",
            "link": row["link"] or "",
            "clean_text": row["clean_text"] or "",
        }
    finally:
        conn.close()


def favorite_link(fav_id) -> str:
    """只取收藏的原始链接；位置收藏通常是高德标记链接。"""
    detail = favorite_details(fav_id)
    return (detail or {}).get("link", "")
