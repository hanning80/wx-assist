"""语音转写队列：新语音先入库占位，后台转写成功后回写文字。

设计原则：
- 队列只存“消息在哪里”，不长期保存音频；
- 同一条消息只转一次，失败最多重试三次；
- 转写成功立刻改 messages.content，并标记云端需要更新；
- 语义索引由 RAG 的重建方法接住，不让旧占位一直留在索引里。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
PROCESSING_TIMEOUT_SEC = 600
VOICE_PLACEHOLDER = "[语音]"
# 这些错误代表原件已经不在微信本地库里，重试多少次都不会有结果。
PERMANENT_ERRORS = ("微信语音数据读取失败", "缺少会话 ID", "微信语音解码失败")


def _now() -> int:
    return int(time.time())


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id  TEXT    NOT NULL UNIQUE,
            chat_id     TEXT    NOT NULL,
            local_id    INTEGER,
            server_id   INTEGER,
            msg_time    INTEGER NOT NULL DEFAULT 0,
            state       TEXT    NOT NULL DEFAULT 'pending',
            attempts    INTEGER NOT NULL DEFAULT 0,
            text        TEXT    NOT NULL DEFAULT '',
            engine      TEXT    NOT NULL DEFAULT '',
            error       TEXT    NOT NULL DEFAULT '',
            cloud_dirty INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER NOT NULL DEFAULT 0,
            updated_at  INTEGER NOT NULL DEFAULT 0
        )
    """)


def enqueue_message(db_path: str | Path, msg: dict) -> bool:
    """把一条语音登记进队列；同一条消息重复登记不会丢已有结果。

    msg 至少要有 message_id / chat_id / timestamp；local_id 与 server_id
    可为空——历史消息的 messages 表没有这两列，只能靠“会话 + 时间”定位。
    """
    if int(msg.get("msg_type") or 0) != 34:
        return False
    message_id = str(msg.get("message_id") or "").strip()
    if not message_id:
        return False
    now = _now()
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        conn.execute("""
            INSERT INTO voice_jobs
                (message_id, chat_id, local_id, server_id, msg_time,
                 state, attempts, text, engine, error, cloud_dirty,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', 0, '', '', '', 0, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                local_id=COALESCE(excluded.local_id, voice_jobs.local_id),
                server_id=COALESCE(excluded.server_id, voice_jobs.server_id),
                msg_time=excluded.msg_time,
                updated_at=excluded.updated_at
        """, (
            message_id, str(msg.get("chat_id") or ""),
            msg.get("local_id"), msg.get("server_id"),
            int(msg.get("timestamp") or 0), now, now,
        ))
        conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("语音任务登记失败 %s: %s", message_id[:12], e)
        return False
    finally:
        conn.close()


def queue_stats(db_path: str | Path) -> dict:
    """返回语音回溯进度：占位总数、任务状态分布、已转写字数。"""
    db_path = Path(db_path)
    out = {"placeholders": 0, "jobs": {}, "done": 0, "pending": 0,
           "failed": 0, "processing": 0, "chars": 0}
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        out["placeholders"] = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE msg_type=34"
            " AND (content='' OR content='[语音]')"
        ).fetchone()[0]
        for row in conn.execute("SELECT state, COUNT(*) FROM voice_jobs GROUP BY state"):
            out["jobs"][row[0]] = row[1]
            if row[0] in out:
                out[row[0]] = row[1]
        out["chars"] = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(text)),0) FROM voice_jobs WHERE state='done'"
        ).fetchone()[0]
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
    finally:
        conn.close()
    return out


def backfill_placeholders(db_path: str | Path, limit: int = 0) -> dict:
    """把历史仍为“[语音]”占位的消息登记进队列。

    历史消息的 messages 表没有 local_id / server_id，只能用 chat_id +
    timestamp 去 VoiceInfo 里对时间取音频；对不上的会在处理阶段明确记
    “原件已失效”，不伪造文字。
    """
    db_path = Path(db_path)
    out = {"scanned": 0, "enqueued": 0, "already": 0, "skipped": 0}
    conn = _connect(db_path)
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT message_id, chat_id, timestamp FROM messages"
            " WHERE msg_type=34"
            "   AND (content='' OR content='[语音]')"
            " ORDER BY timestamp ASC"
        ).fetchall()
        out["scanned"] = len(rows)
        for r in rows:
            mid = str(r["message_id"] or "")
            if not mid:
                out["skipped"] += 1
                continue
            exists = conn.execute(
                "SELECT state, error FROM voice_jobs WHERE message_id=?", (mid,)
            ).fetchone()
            if exists and exists["state"] == "done":
                out["already"] += 1
                continue
            if exists and exists["state"] in ("pending", "processing"):
                out["already"] += 1
                continue
            # 永久失败不再重排：微信本地已经没有这段音频，重试只是白花时间与调用。
            if exists and exists["state"] == "failed" and any(
                    k in str(exists["error"] or "") for k in PERMANENT_ERRORS):
                out["skipped"] += 1
                continue
            conn.execute("""
                INSERT INTO voice_jobs
                    (message_id, chat_id, local_id, server_id, msg_time,
                     state, attempts, text, engine, error, cloud_dirty,
                     created_at, updated_at)
                VALUES (?, ?, NULL, NULL, ?, 'pending', 0, '', '', '', 0, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    state='pending', attempts=0, error='',
                    msg_time=excluded.msg_time, updated_at=excluded.updated_at
            """, (mid, str(r["chat_id"] or ""), int(r["timestamp"] or 0),
                  _now(), _now()))
            out["enqueued"] += 1
            if limit and out["enqueued"] >= int(limit):
                break
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("历史语音登记失败: %s", e)
        out["error"] = str(e)[:200]
    finally:
        conn.close()
    return out



class VoiceQueue:
    def __init__(self, db_path: str | Path, client=None, asr=None,
                 rag_getter=None, temp_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.client = client
        self.asr = asr
        self._rag_getter = rag_getter
        self.temp_dir = Path(temp_dir or (self.db_path.parent / "journal" / "voice_queue"))

    def _get_rag(self):
        if self._rag_getter:
            return self._rag_getter()
        try:
            from src.web.server import get_rag_engine
            return get_rag_engine()
        except Exception:
            return None

    def _claim_one(self, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
        now = _now()
        # 机器人进程和手动批量脚本可能同时跑；用写锁把“选一条+改成 processing”
        # 做成一个原子动作，避免同一条语音被两边各转一次。
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        # 上次进程在 processing 状态退出时，超过十分钟自动回收，避免永久卡住。
        conn.execute("""
            UPDATE voice_jobs SET state='pending', updated_at=?
            WHERE state='processing' AND updated_at < ?
        """, (now, now - PROCESSING_TIMEOUT_SEC))
        row = conn.execute("""
            SELECT * FROM voice_jobs
            WHERE state IN ('pending','failed') AND attempts < ?
            ORDER BY id ASC LIMIT 1
        """, (MAX_ATTEMPTS,)).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute("""
            UPDATE voice_jobs
            SET state='processing', attempts=attempts+1, updated_at=?
            WHERE id=?
        """, (now, row["id"]))
        conn.commit()
        return conn.execute("SELECT * FROM voice_jobs WHERE id=?", (row["id"],)).fetchone()

    def _finish(self, conn: sqlite3.Connection, job_id: int, *,
                state: str, text: str = "", engine: str = "",
                error: str = "", cloud_dirty: int = 0) -> None:
        conn.execute("""
            UPDATE voice_jobs
            SET state=?, text=?, engine=?, error=?, cloud_dirty=?, updated_at=?
            WHERE id=?
        """, (state, text, engine, error[:240], cloud_dirty, _now(), job_id))
        conn.commit()

    def _process(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
        message_id = row["message_id"]
        chat_id = row["chat_id"]
        local_id = row["local_id"]
        server_id = row["server_id"]
        msg_time = row["msg_time"]

        current = conn.execute(
            "SELECT sender_id, sender_name, content, msg_type FROM messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if current is None:
            self._finish(conn, row["id"], state="failed",
                         error="消息库找不到这条语音，无法回写")
            return False

        content = (current["content"] or "").strip()
        if content and content != VOICE_PLACEHOLDER and not content.startswith(VOICE_PLACEHOLDER + " "):
            # 已经有人工转写或别的通道写过了，不重复花钱。
            self._finish(conn, row["id"], state="done", text=content,
                         engine="already-filled")
            return True

        if not chat_id and not server_id:
            self._finish(conn, row["id"], state="failed",
                         error="缺少会话 ID 与服务端编号，无法定位语音")
            return False
        if self.client is None or self.asr is None:
            self._finish(conn, row["id"], state="failed",
                         error="语音识别器或微信客户端未就绪")
            return False

        try:
            data = self.client.get_voice_data(chat_id, msg_time,
                                              int(local_id or 0), int(server_id or 0))
            if not data or not data.get("success"):
                self._finish(conn, row["id"], state="failed",
                             error="微信语音数据读取失败")
                return False
            from src.wechat.voice_decode import silk_to_wav
            wav = silk_to_wav(data["hex"])
            if not wav:
                self._finish(conn, row["id"], state="failed",
                             error="微信语音解码失败")
                return False

            self.temp_dir.mkdir(parents=True, exist_ok=True)
            wav_path = self.temp_dir / ("%s.wav" % message_id)
            wav_path.write_bytes(wav)
            try:
                result = self.asr.transcribe(wav_path)
            finally:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    pass

            text = (result.get("text") or "").strip()
            if not result.get("ok") or not text:
                self._finish(conn, row["id"], state="failed",
                             engine=str(result.get("engine") or ""),
                             error=str(result.get("error") or "识别结果为空"))
                return False

            new_content = VOICE_PLACEHOLDER + " " + text
            conn.execute(
                "UPDATE messages SET content=? WHERE message_id=?",
                (new_content, message_id),
            )
            self._finish(conn, row["id"], state="done", text=text,
                         engine=str(result.get("engine") or ""),
                         cloud_dirty=1)
            logger.info("语音转写完成 %s（%s，%d字）",
                        message_id[:12], result.get("engine", ""), len(text))

            # 占位先入过索引时，用单条块替换，保证问答能搜到这段文字。
            try:
                rag = self._get_rag()
                if rag is not None and hasattr(rag, "reindex_message"):
                    rag.reindex_message({
                        "message_id": message_id,
                        "chat_id": chat_id,
                        "sender_id": current["sender_id"] or "",
                        "sender_name": current["sender_name"] or "",
                        "content": new_content,
                        "msg_type": 34,
                        "timestamp": msg_time,
                    })
            except Exception as e:  # noqa: BLE001
                logger.warning("语音文字重建索引失败 %s: %s", message_id[:12], e)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("语音转写任务失败 %s: %s", message_id[:12], e)
            self._finish(conn, row["id"], state="failed", error=str(e))
            return False

    def run_once(self, limit: int = 2) -> dict:
        counts = {"claimed": 0, "done": 0, "failed": 0}
        conn = _connect(self.db_path)
        try:
            ensure_schema(conn)
            for _ in range(max(0, int(limit))):
                row = self._claim_one(conn)
                if row is None:
                    break
                counts["claimed"] += 1
                if self._process(conn, row):
                    counts["done"] += 1
                else:
                    counts["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("语音队列本轮失败: %s", e)
        finally:
            conn.close()
        return counts
