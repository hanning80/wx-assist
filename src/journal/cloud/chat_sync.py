"""聊天记录增量上云。

全量包解决“有一天要换机器”，增量包解决“云端别落后太多”。
正常消息只做加法；语音占位后来补成文字时，额外上传“更正增量”，
云端合并按 message_id 用新内容覆盖旧内容。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .storage import CloudStore

logger = logging.getLogger(__name__)

SCHEMA = "wx-assist-chat-increment-v1"
STATE_NAME = "chat_sync_state.json"
DEFAULT_LIMIT = 5000


def _max_rowid(messages_db: str) -> int:
    conn = sqlite3.connect(messages_db)
    try:
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _new_rows(messages_db: str, last_rowid: int, limit: int) -> list[dict]:
    conn = sqlite3.connect(messages_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT rowid AS _rowid, message_id, chat_id, sender_id, sender_name,"
            " content, msg_type, timestamp, created_at"
            " FROM messages WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (last_rowid, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class ChatIncrementSync:
    """按 rowid 游标把新聊天写成增量包并上传微云。"""

    def __init__(self, messages_db: str, journal_db: str,
                 store=None, cloud: Optional[CloudStore] = None):
        self.messages_db = messages_db
        self.journal_db = journal_db
        self.store = store
        self.cloud = cloud or CloudStore(store=store)
        self.state_path = Path(journal_db).parent / "journal" / "export" / STATE_NAME

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_state(self, last_rowid: int, extra: Optional[dict] = None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        data = {
            "last_rowid": int(last_rowid),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if isinstance(extra, dict):
            data.update(extra)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(str(tmp), str(self.state_path))

    def _put_manifest(self, payload: dict) -> bool:
        try:
            self.cloud.put_chat_manifest(payload)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("[聊天增量] 清单上传失败（增量已成功，下次重试）: %s", e)
            return False

    def _correction_rows(self, limit: int = 500) -> list[dict]:
        """取出已转写、等云端覆盖的语音消息。"""
        conn = sqlite3.connect(self.messages_db, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT m.rowid AS _rowid, m.message_id, m.chat_id, m.sender_id,"
                " m.sender_name, m.content, m.msg_type, m.timestamp, m.created_at,"
                " v.id AS _job_id"
                " FROM voice_jobs v JOIN messages m ON m.message_id=v.message_id"
                " WHERE v.state='done' AND v.cloud_dirty=1"
                " ORDER BY v.id LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def _sync_corrections(self, limit: int = 500) -> int:
        """把语音补字作为“更正增量”上传；合并时同 message_id 以后者为准。"""
        rows = self._correction_rows(limit)
        if not rows:
            return 0
        messages = []
        job_ids = []
        for r in rows:
            job_ids.append(r.pop("_job_id"))
            r.pop("_rowid", None)
            messages.append(r)
        payload = {
            "schema": SCHEMA,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "kind": "chat_corrections",
            "from_rowid": 0,
            "to_rowid": max(_max_rowid(self.messages_db), 0),
            "count": len(messages),
            "messages": messages,
        }
        try:
            rec = self.cloud.put_chat_increment(payload, name_suffix="更正")
        except Exception as e:  # noqa: BLE001
            logger.warning("[聊天增量] 语音更正上传失败，下次重试: %s", e)
            return 0
        conn = sqlite3.connect(self.messages_db, timeout=15)
        try:
            conn.executemany(
                "UPDATE voice_jobs SET cloud_dirty=0, updated_at=?"
                " WHERE id=?",
                [(int(datetime.now().timestamp()), int(j)) for j in job_ids],
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("[聊天增量] 已上传语音更正 %d 条（%s）",
                    len(messages), rec.get("name", ""))
        return len(messages)

    def run(self, limit: int = DEFAULT_LIMIT) -> dict:
        """同步一次；没有新消息就静默返回。"""
        if not os.path.exists(self.messages_db):
            return {"new": 0, "reason": "messages_db_missing"}

        maxid = _max_rowid(self.messages_db)
        state = self._load_state()
        last = state.get("last_rowid")
        corrected = self._sync_corrections()

        # 上一轮增量上传成功、清单失败的，先把清单补上；不能因为
        # maxid == last 就直接返回，否则清单永远停在旧行号。
        pending = state.get("pending_manifest")
        pending_failed = False
        if isinstance(pending, dict) and pending:
            if self._put_manifest(pending):
                state.pop("pending_manifest", None)
                self._save_state(int(state.get("last_rowid") or 0), state)
                logger.info("[聊天增量] 补传清单成功: last_rowid=%s",
                            pending.get("last_rowid"))
            else:
                # 清单失败不能挡住新消息上传；继续往下走，末尾会用新的
                # pending_manifest 覆盖它，下一次再补。
                pending_failed = True

        if last is None:
            # 首次运行只建立游标，不重传已有 11051 条；全量包已经覆盖历史。
            self._save_state(maxid)
            return {"new": 0, "initialized": True, "last_rowid": maxid,
                    "corrected": corrected}

        try:
            last = int(last)
        except (TypeError, ValueError):
            self._save_state(maxid)
            return {"new": 0, "reset": True, "last_rowid": maxid,
                    "corrected": corrected}

        if maxid < last:
            # 数据库被替换/压缩，游标不能大于当前最大行号，否则新消息会被跳过。
            self._save_state(maxid)
            return {"new": 0, "reset": True, "last_rowid": maxid,
                    "corrected": corrected}
        if maxid == last:
            return ({"new": 0, "last_rowid": last, "manifest_retry": True,
                     "corrected": corrected}
                    if pending_failed else {"new": 0, "last_rowid": last,
                                            "corrected": corrected})

        rows = _new_rows(self.messages_db, last, limit)
        if not rows:
            return {"new": 0, "last_rowid": last}

        to_rowid = int(rows[-1]["_rowid"])
        messages = []
        for r in rows:
            item = dict(r)
            item.pop("_rowid", None)
            messages.append(item)

        payload = {
            "schema": SCHEMA,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "from_rowid": last,
            "to_rowid": to_rowid,
            "count": len(messages),
            "messages": messages,
        }
        rec = self.cloud.put_chat_increment(payload)
        manifest = {
            "last_rowid": to_rowid,
            "last_increment": rec.get("name", ""),
            "last_count": len(messages),
            "last_exported_at": payload["exported_at"],
        }
        # 增量文件已经上传成功，游标必须先推进，避免重复上传同一段；
        # 清单失败则记在 pending_manifest 里，下一次同步补传。
        self._save_state(to_rowid, {"pending_manifest": manifest})
        if self._put_manifest(manifest):
            self._save_state(to_rowid)
        return {
            "new": len(messages),
            "name": rec.get("name", ""),
            "last_rowid": to_rowid,
            "corrected": corrected,
        }
