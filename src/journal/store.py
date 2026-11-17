"""日记与提醒的存储层（阶段4 模块一）。

按 `开发文档/03-数据模型.md` 建库，负责：
  - 建表、版本校验、审计
  - 来源溯源与去重（同一条微信消息只入一次）
  - 日记条目读写
  - 提醒链与提醒实例（按天展开）
  - 待答追问（同会话唯一）
  - 周复盘快照

设计要点：
  - 一个连接、线程安全用锁包住，避免扫描线程与网页请求互相踩
  - 所有时间用本地时间、ISO8601 字符串，字典序即时间序
  - 状态字段存英文枚举，展示层再翻译
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
# 回执与追问统一发到 ClawBot 会话；内容侧仍从文件传输助手读库
DEFAULT_CHAT_ID = "ilink_dm"
DELIVERY_VIA = "ilink_bot"

# 状态枚举
CHAIN_STATUS = ("pending_time", "active", "finished", "cancelled", "expired")
TASK_STATUS = ("pending", "sent", "skipped", "failed")
QUESTION_STATE = ("asked", "answered", "expired", "gaveup", "cancelled")


def _project_root() -> Path:
    try:
        from src.config import PROJECT_ROOT  # type: ignore

        return Path(PROJECT_ROOT)
    except Exception:
        return Path(__file__).resolve().parents[2]


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def today_str() -> str:
    return date.today().isoformat()


def combine(date_str: str, hhmm: str) -> str:
    """日期 + 'HH:MM' → ISO 时刻字符串（本地朴素时间）。"""
    h, m = hhmm.split(":")
    d = date.fromisoformat(date_str)
    return datetime(d.year, d.month, d.day, int(h), int(m)).isoformat()


def split_times(value) -> list[str]:
    """把 "10:00,17:00"、"晚上七点"清成 ['10:00','17:00']；认不出的丢掉。"""
    if not value:
        return []
    parts = value if isinstance(value, (list, tuple)) else re.split(r"[,，、;；\s]+", str(value).strip())
    out: list[str] = []
    for part in parts:
        part = str(part).strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", part):
            continue
        h, m = part.split(":")
        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
            t = "%02d:%02d" % (int(h), int(m))
            if t not in out:
                out.append(t)
    return sorted(out, key=lambda x: (int(x[:2]), int(x[3:])))


def join_times(times) -> str:
    return ",".join(split_times(times))


DDL = [
    """CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        note TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS entry_source (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        local_id INTEGER NOT NULL,
        server_id INTEGER,
        msg_time TEXT,
        msg_type TEXT,
        voice_path TEXT,
        voice_duration_ms INTEGER,
        asr_engine TEXT,
        asr_latency_ms INTEGER,
        asr_raw TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_source_chat_local
        ON entry_source(chat_id, local_id)""",
    """CREATE TABLE IF NOT EXISTS purged_source (
        chat_id TEXT NOT NULL,
        local_id INTEGER NOT NULL,
        purged_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, local_id)
    )""",
    """CREATE TABLE IF NOT EXISTS diary_entry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_date TEXT NOT NULL,
        created_at TEXT NOT NULL,
        kind TEXT NOT NULL,
        raw_text TEXT,
        summary TEXT,
        full_text TEXT,
        topics TEXT,
        entities TEXT,
        mood TEXT,
        confidence REAL,
        needs_review INTEGER DEFAULT 0,
        source_id INTEGER,
        chain_id INTEGER,
        deleted INTEGER DEFAULT 0
    )""",
    """CREATE INDEX IF NOT EXISTS ix_entry_date ON diary_entry(entry_date)""",
    """CREATE INDEX IF NOT EXISTS ix_entry_review ON diary_entry(needs_review)""",
    """CREATE TABLE IF NOT EXISTS reminder_chain (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id INTEGER,
        title TEXT NOT NULL,
        object TEXT,
        action TEXT,
        start_date TEXT NOT NULL,
        days INTEGER NOT NULL DEFAULT 1,
        daily_time TEXT,
        status TEXT NOT NULL,
        ask_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        activated_at TEXT,
        finished_at TEXT,
        end_notice_sent INTEGER DEFAULT 0,
        source_text TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_chain_status ON reminder_chain(status)""",
    """CREATE INDEX IF NOT EXISTS ix_chain_start ON reminder_chain(start_date)""",
    """CREATE TABLE IF NOT EXISTS reminder_task (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chain_id INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        kind TEXT NOT NULL,
        fire_date TEXT NOT NULL,
        fire_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        sent_at TEXT,
        retry_count INTEGER DEFAULT 0,
        last_error TEXT,
        delivery_via TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_task_chain_seq
        ON reminder_task(chain_id, seq, kind)""",
    """CREATE INDEX IF NOT EXISTS ix_task_status_time
        ON reminder_task(status, fire_at)""",
    """CREATE TABLE IF NOT EXISTS pending_question (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        chain_id INTEGER,
        payload TEXT,
        question_text TEXT,
        state TEXT NOT NULL,
        ask_count INTEGER DEFAULT 1,
        asked_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        answered_at TEXT,
        answer_text TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_question_one_asked
        ON pending_question(chat_id) WHERE state = 'asked'""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        detail TEXT,
        result TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts)""",
    """CREATE TABLE IF NOT EXISTS weekly_review (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start TEXT NOT NULL,
        week_end TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        html_path TEXT,
        stats TEXT,
        narrative TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_weekly_start
        ON weekly_review(week_start)""",
    """CREATE TABLE IF NOT EXISTS cloud_object (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        entry_id INTEGER,
        object_key TEXT NOT NULL,
        pdir_key TEXT NOT NULL,
        name TEXT NOT NULL,
        bytes INTEGER,
        sha256 TEXT,
        local_path TEXT,
        created_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'uploaded',
        purged_at TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS ix_cloud_kind ON cloud_object(kind, created_at)""",
    """CREATE TABLE IF NOT EXISTS attachment (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        local_id INTEGER NOT NULL,
        server_id INTEGER,
        msg_type INTEGER,
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        local_path TEXT,
        bytes INTEGER,
        sha256 TEXT,
        extracted_text TEXT,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        entry_id INTEGER,
        cloud_entry_id INTEGER,
        cloud_pdir TEXT,
        cloud_name TEXT,
        upload_error TEXT,
        created_at TEXT NOT NULL,
        attached_at TEXT,
        uploaded_at TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_attachment_chat_local
        ON attachment(chat_id, local_id)""",
    """CREATE INDEX IF NOT EXISTS ix_attachment_status
        ON attachment(status, created_at)""",
    """CREATE TABLE IF NOT EXISTS media_asset (
        asset_id TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL,
        mime TEXT DEFAULT '',
        bytes INTEGER DEFAULT 0,
        width INTEGER DEFAULT 0,
        height INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        original_key TEXT DEFAULT '',
        preview_key TEXT DEFAULT '',
        text_key TEXT DEFAULT '',
        ocr_text TEXT DEFAULT '',
        asr_text TEXT DEFAULT '',
        caption TEXT DEFAULT '',
        state TEXT DEFAULT 'uploaded',
        created_at TEXT NOT NULL,
        last_seen_at TEXT DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS ix_media_kind ON media_asset(kind, created_at)""",
    """CREATE INDEX IF NOT EXISTS ix_media_state ON media_asset(state)""",
    """CREATE TABLE IF NOT EXISTS media_ref (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        source_id TEXT DEFAULT '',
        message_id TEXT DEFAULT '',
        local_id INTEGER DEFAULT 0,
        entry_id INTEGER DEFAULT 0,
        fav_id INTEGER DEFAULT 0,
        role TEXT DEFAULT 'original',
        created_at TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS ix_media_ref_asset ON media_ref(asset_id)""",
    """CREATE INDEX IF NOT EXISTS ix_media_ref_source
        ON media_ref(source_kind, source_id)""",
    """CREATE TABLE IF NOT EXISTS answer_rule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_key TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1,
        rule_text TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )""",
]


class JournalStore:
    """日记库的读写入口。"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _project_root() / "data" / "journal.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.init_db()

    # ── 基础设施 ────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_db(self) -> None:
        with self._lock:
            for sql in DDL:
                self._conn.execute(sql)
            # 老库没有 asset_id 列；只加列，不改旧数据。
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(attachment)")}
            if "asset_id" not in cols:
                self._conn.execute("ALTER TABLE attachment ADD COLUMN asset_id TEXT")
            cur = self._conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cur.fetchone()
            if not row:
                self._conn.execute(
                    "INSERT INTO schema_version(version, applied_at, note) VALUES(?,?,?)",
                    (SCHEMA_VERSION, now_iso(), "首次建库"),
                )
                self._conn.commit()
                self.audit("system", "init", "schema", None, {"version": SCHEMA_VERSION}, "ok")
            elif row["version"] != SCHEMA_VERSION:
                logger.warning("schema 版本 %s，代码期望 %s", row["version"], SCHEMA_VERSION)
            self._conn.commit()

    def audit(self, actor: str, action: str, target_type: str | None,
              target_id: int | None, detail: Any = None, result: str = "ok") -> None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False) if detail is not None else ""
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log(ts, actor, action, target_type, target_id, detail, result) "
                "VALUES(?,?,?,?,?,?,?)",
                (now_iso(), actor, action, target_type, target_id, detail, result),
            )
            self._conn.commit()

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ── 长期回答规则 ────────────────────────────────────────────

    def get_answer_rule(self) -> dict:
        """读取“回答引用原话”规则；没写过时默认开启，规则正文留空由调用方补默认。"""
        rows = self._rows(
            "SELECT enabled, rule_text, updated_at FROM answer_rule"
            " WHERE rule_key = 'quote_sources' LIMIT 1"
        )
        if not rows:
            return {"enabled": 1, "rule_text": "", "updated_at": ""}
        row = rows[0]
        return {
            "enabled": int(row["enabled"] or 0),
            "rule_text": row["rule_text"] or "",
            "updated_at": row["updated_at"] or "",
        }

    def save_answer_rule(self, enabled: bool, rule_text: str = "") -> None:
        """保存规则；只保留一条，重复保存覆盖，不产生多条互相打架的规则。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO answer_rule(rule_key, enabled, rule_text, updated_at)"
                " VALUES('quote_sources', ?, ?, ?)"
                " ON CONFLICT(rule_key) DO UPDATE SET"
                " enabled=excluded.enabled, rule_text=excluded.rule_text,"
                " updated_at=excluded.updated_at",
                (1 if enabled else 0, (rule_text or "").strip(), now_iso()),
            )
            self._conn.commit()
        self.audit("config", "answer_rule", "rule", None,
                   {"enabled": bool(enabled), "rule_text": (rule_text or "")[:120]},
                   "ok")

    # ── 来源溯源 ────────────────────────────────────────────────

    def add_source(self, chat_id: str, local_id: int, msg_time: str | None = None,
                   msg_type: str = "voice", server_id: int | None = None,
                   voice_path: str | None = None, voice_duration_ms: int | None = None,
                   asr_engine: str | None = None, asr_latency_ms: int | None = None,
                   asr_raw: str | None = None) -> Optional[int]:
        """登记一条原始消息。已存在则返回 None（不重复处理）。"""
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO entry_source(chat_id, local_id, server_id, msg_time, msg_type,"
                    " voice_path, voice_duration_ms, asr_engine, asr_latency_ms, asr_raw, created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (chat_id, local_id, server_id, msg_time, msg_type, voice_path,
                     voice_duration_ms, asr_engine, asr_latency_ms, asr_raw, now_iso()),
                )
                self._conn.commit()
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def source_exists(self, chat_id: str, local_id: int) -> bool:
        rows = self._rows(
            "SELECT 1 FROM entry_source WHERE chat_id=? AND local_id=?"
            " UNION ALL SELECT 1 FROM purged_source WHERE chat_id=? AND local_id=?",
            (chat_id, local_id, chat_id, local_id),
        )
        return bool(rows)

    def known_sources(self) -> set[tuple[str, int]]:
        """已处理过的来源 + 彻底删除后的来源墓碑。

        墓碑只用于阻止同一条微信消息被扫描器自动重新入库，不显示为日记；
        用户以后明确要求重新入库时，再单独清墓碑。
        """
        rows = self._rows(
            "SELECT chat_id, local_id FROM entry_source"
            " UNION SELECT chat_id, local_id FROM purged_source"
        )
        return {(r["chat_id"], r["local_id"]) for r in rows}

    def find_duplicate_entry(self, chat_id: str, raw_text: str,
                             days: int = 30) -> Optional[sqlite3.Row]:
        """找同一会话里最近若干天内容完全相同的日记（用于防重复记账）。

        正文可能已经迁到微云，不能只看本地 raw_text；候选行按记录时间筛小范围，
        再按需从云端取回正文比较。
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = self._rows(
            "SELECT d.id, d.summary, d.created_at, d.raw_text, d.full_text"
            " FROM diary_entry d JOIN entry_source s ON d.source_id=s.id"
            " WHERE s.chat_id=? AND d.deleted=0 AND d.created_at>=?"
            " ORDER BY d.id DESC LIMIT 20",
            (chat_id, cutoff),
        )
        target = (raw_text or "").strip()
        if not target:
            return None
        try:
            from .cloud.body_store import hydrate_entry
        except Exception:
            hydrate_entry = None
        for r in rows:
            body = (r["raw_text"] or r["full_text"] or "").strip()
            if not body and hydrate_entry is not None:
                try:
                    full = hydrate_entry(self, r)
                    body = (full.get("raw_text") or full.get("full_text") or "").strip()
                except Exception:
                    body = ""
            if body == target:
                return r
        return None

    # ── 日记 ────────────────────────────────────────────────────

    def add_entry(self, entry_date: str, kind: str, raw_text: str, summary: str = "",
                  full_text: str = "", topics: str = "", entities: str = "",
                  mood: str = "", confidence: float = 0.0, needs_review: bool = False,
                  source_id: int | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO diary_entry(entry_date, created_at, kind, raw_text, summary, full_text,"
                " topics, entities, mood, confidence, needs_review, source_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_date, now_iso(), kind, raw_text, summary, full_text, topics,
                 entities, mood, confidence, 1 if needs_review else 0, source_id),
            )
            self._conn.commit()
            eid = cur.lastrowid
        self.audit("system", "entry_create", "entry", eid,
                   {"date": entry_date, "kind": kind, "confidence": confidence})
        return eid

    def delete_entry(self, entry_id: int) -> Optional[dict]:
        """软删除一条日记，并把它关联的附件从这条日记上摘下来。

        原件不删除：图片/视频仍在微云，附件行保留，只是不再挂在这条日记上。
        返回删除信息；找不到或已删除时返回 None。
        """
        try:
            eid = int(entry_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, entry_date, kind, summary FROM diary_entry"
                " WHERE id=? AND deleted=0", (eid,)
            ).fetchone()
            if row is None:
                return None
            attachments = self._conn.execute(
                "SELECT id FROM attachment WHERE entry_id=? ORDER BY id", (eid,)
            ).fetchall()
            self._conn.execute(
                "UPDATE diary_entry SET deleted=1 WHERE id=?", (eid,)
            )
            if attachments:
                self._conn.execute(
                    "UPDATE attachment SET entry_id=NULL, status='deleted'"
                    " WHERE entry_id=?", (eid,)
                )
            self._conn.commit()
        info = {
            "id": eid,
            "entry_date": row["entry_date"],
            "kind": row["kind"],
            "summary": row["summary"],
            "attachment_ids": [int(a["id"]) for a in attachments],
        }
        self.audit("web", "entry_delete", "entry", eid,
                   {"date": info["entry_date"], "kind": info["kind"],
                    "attachments": len(info["attachment_ids"])})
        return info

    def purge_entry(self, entry_id: int) -> Optional[dict]:
        """彻底删除一条日记的数据库行，并返回原件信息供调用方清理。

        数据库里的日记、来源、附件记录和提醒会物理删除；微云原件与本地
        暂存文件由网页删除流程按返回的 attachment/cloud_object 信息处理。
        之后同一条微信消息可以重新入库。
        """
        try:
            eid = int(entry_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, entry_date, kind, summary, source_id FROM diary_entry"
                " WHERE id=?", (eid,)
            ).fetchone()
            if row is None:
                return None

            attachments = self._conn.execute(
                "SELECT id, kind, filename, local_path, cloud_pdir, cloud_name"
                " FROM attachment WHERE entry_id=? ORDER BY id", (eid,)
            ).fetchall()
            cloud_objects = self._conn.execute(
                "SELECT id, kind, object_key, pdir_key, name, state"
                " FROM cloud_object WHERE entry_id=? ORDER BY id", (eid,)
            ).fetchall()
            chains = self._conn.execute(
                "SELECT id FROM reminder_chain WHERE entry_id=? ORDER BY id", (eid,)
            ).fetchall()

            source_id = row["source_id"]
            source_removed = False
            if source_id is not None:
                other_refs = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM diary_entry"
                    " WHERE source_id=? AND id<>?", (source_id, eid)
                ).fetchone()["n"]
                if not other_refs:
                    src = self._conn.execute(
                        "SELECT chat_id, local_id FROM entry_source WHERE id=?",
                        (source_id,),
                    ).fetchone()
                    if src is not None:
                        # 用户要“彻底删除”：来源行物理删掉，不再写墓碑。
                        # 自动重扫由扫描游标控制；用户之后明确让 AI 重新入库
                        # 时，仍可重新落库。
                        self._conn.execute("DELETE FROM entry_source WHERE id=?", (source_id,))
                    source_removed = True

            # 提醒链和待答问题一起清掉，避免日记删了提醒还在跑。
            if chains:
                chain_ids = [int(c["id"]) for c in chains]
                marks = ",".join("?" for _ in chain_ids)
                self._conn.execute(
                    "DELETE FROM reminder_task WHERE chain_id IN (%s)" % marks, chain_ids
                )
                self._conn.execute(
                    "DELETE FROM pending_question WHERE chain_id IN (%s)" % marks, chain_ids
                )
                self._conn.execute(
                    "DELETE FROM reminder_chain WHERE id IN (%s)" % marks, chain_ids
                )

            # 附件记录也物理删除，避免同一条附件消息因唯一键无法再次入库。
            self._conn.execute(
                "UPDATE cloud_object SET entry_id=NULL WHERE entry_id=?", (eid,)
            )
            self._conn.execute("DELETE FROM attachment WHERE entry_id=?", (eid,))
            self._conn.execute("DELETE FROM diary_entry WHERE id=?", (eid,))
            self._conn.commit()

        info = {
            "id": eid,
            "entry_date": row["entry_date"],
            "kind": row["kind"],
            "summary": row["summary"],
            "source_id": int(source_id) if source_id is not None else None,
            "source_removed": source_removed,
            "attachment_ids": [int(a["id"]) for a in attachments],
            "attachments": [dict(a) for a in attachments],
            "cloud_objects": [dict(c) for c in cloud_objects],
            "chain_ids": [int(c["id"]) for c in chains],
        }
        self.audit("web", "entry_purge", "entry", eid,
                   {"date": info["entry_date"], "kind": info["kind"],
                    "source_removed": source_removed,
                    "attachments": len(info["attachment_ids"]),
                    "chains": len(info["chain_ids"])})
        return info

    # ── 附件（知识库原件） ──────────────────────────────────────

    def add_attachment(self, chat_id: str, local_id: int, kind: str,
                       filename: str, local_path: str = "", size_bytes: int = 0,
                       sha256: str = "", extracted_text: str = "",
                       note: str = "", msg_type: int = 0,
                       server_id: int | None = None,
                       created_at: str | None = None) -> int | None:
        """登记一条暂存附件；同一条消息只登记一次。"""
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO attachment(chat_id, local_id, server_id, msg_type, kind,"
                    " filename, local_path, bytes, sha256, extracted_text, note, status,"
                    " created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                    (chat_id, local_id, server_id, msg_type, kind, filename,
                     local_path, size_bytes, sha256, extracted_text, note,
                     created_at or now_iso()),
                )
                self._conn.commit()
                aid = cur.lastrowid
        except sqlite3.IntegrityError:
            return None
        self.audit("ingest", "attachment_staged", "attachment", aid,
                   {"kind": kind, "name": filename, "bytes": size_bytes})
        return aid

    def pending_attachments(self, chat_id: str = "filehelper",
                            hours: int = 24) -> list[sqlite3.Row]:
        """取还没挂到日记上的附件；默认只看最近 24 小时。"""
        if hours and hours > 0:
            cutoff = (datetime.now() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
            return self._rows(
                "SELECT * FROM attachment WHERE chat_id=? AND status='pending'"
                " AND created_at>=? ORDER BY id", (chat_id, cutoff))
        return self._rows(
            "SELECT * FROM attachment WHERE chat_id=? AND status='pending'"
            " ORDER BY id", (chat_id,))

    def attachment_rows(self, entry_id: int) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM attachment WHERE entry_id=? ORDER BY id", (entry_id,))

    def all_attachments(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM attachment ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))

    def attach_attachments(self, attachment_ids: list[int], entry_id: int,
                           note: str = "") -> int:
        """把暂存附件挂到一条日记上；批量时一次挂完。"""
        ids = [int(x) for x in attachment_ids if x]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self._lock:
            self._conn.execute(
                "UPDATE attachment SET entry_id=?, status='attached', note=?"
                " WHERE id IN (%s) AND status='pending'" % marks,
                (entry_id, note, *ids),
            )
            self._conn.commit()
        self.audit("system", "attachments_attached", "entry", entry_id,
                   {"count": len(ids)})
        return len(ids)

    def mark_attachment_uploaded(self, attachment_id: int, cloud_entry_id: int | None,
                                 pdir: str, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE attachment SET status='uploaded', cloud_entry_id=?,"
                " cloud_pdir=?, cloud_name=?, uploaded_at=?, upload_error=NULL"
                " WHERE id=?",
                (cloud_entry_id, pdir, name, now_iso(), attachment_id),
            )
            self._conn.commit()

    def mark_attachment_local_purged(self, attachment_id: int) -> None:
        """微云上传成功后清掉本地原件；正文和云端路径仍留在库里。"""
        with self._lock:
            self._conn.execute(
                "UPDATE attachment SET local_path='' WHERE id=?",
                (attachment_id,),
            )
            self._conn.commit()
        self.audit("system", "attachment_local_purged", "attachment", attachment_id,
                   {"reason": "uploaded_to_cloud"})

    def mark_attachment_error(self, attachment_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE attachment SET upload_error=? WHERE id=?",
                (str(error)[:300], attachment_id),
            )
            self._conn.commit()

    # ── 媒体资产：图片/视频原件只在 OSS，库里只留指针和文字 ─────────
    def media_asset_by_sha(self, sha256: str) -> Optional[sqlite3.Row]:
        if not sha256:
            return None
        rows = self._rows("SELECT * FROM media_asset WHERE sha256=? LIMIT 1", (sha256,))
        return rows[0] if rows else None

    def media_asset(self, asset_id: str) -> Optional[sqlite3.Row]:
        if not asset_id:
            return None
        rows = self._rows("SELECT * FROM media_asset WHERE asset_id=? LIMIT 1", (asset_id,))
        return rows[0] if rows else None

    def upsert_media_asset(self, *, asset_id: str, sha256: str, kind: str,
                           mime: str = "", bytes_: int = 0, width: int = 0,
                           height: int = 0, duration_ms: int = 0,
                           original_key: str = "", preview_key: str = "",
                           text_key: str = "", ocr_text: str = "",
                           asr_text: str = "", caption: str = "",
                           state: str = "uploaded") -> dict:
        """同一 SHA 只留一行；重复媒体只更新时间，不重复占空间。"""
        now = now_iso()
        existing = self.media_asset_by_sha(sha256)
        if existing:
            with self._lock:
                self._conn.execute(
                    "UPDATE media_asset SET last_seen_at=?,"
                    " original_key=CASE WHEN ?<>'' THEN ? ELSE original_key END,"
                    " preview_key=CASE WHEN ?<>'' THEN ? ELSE preview_key END,"
                    " text_key=CASE WHEN ?<>'' THEN ? ELSE text_key END,"
                    " ocr_text=CASE WHEN ?<>'' THEN ? ELSE ocr_text END,"
                    " asr_text=CASE WHEN ?<>'' THEN ? ELSE asr_text END,"
                    " caption=CASE WHEN ?<>'' THEN ? ELSE caption END,"
                    " state=? WHERE asset_id=?",
                    (now, original_key, original_key, preview_key, preview_key,
                     text_key, text_key, ocr_text, ocr_text, asr_text, asr_text,
                     caption, caption, state, existing["asset_id"]),
                )
                self._conn.commit()
            return dict(self.media_asset(existing["asset_id"]) or existing)
        with self._lock:
            self._conn.execute(
                "INSERT INTO media_asset(asset_id, sha256, kind, mime, bytes, width,"
                " height, duration_ms, original_key, preview_key, text_key,"
                " ocr_text, asr_text, caption, state, created_at, last_seen_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, sha256, kind, mime, int(bytes_), int(width), int(height),
                 int(duration_ms), original_key, preview_key, text_key,
                 ocr_text, asr_text, caption, state, now, now),
            )
            self._conn.commit()
        return dict(self.media_asset(asset_id) or {})

    def add_media_ref(self, *, asset_id: str, source_kind: str,
                      source_id: str = "", message_id: str = "",
                      local_id: int = 0, entry_id: int = 0, fav_id: int = 0,
                      role: str = "original") -> int:
        now = now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO media_ref(asset_id, source_kind, source_id, message_id,"
                " local_id, entry_id, fav_id, role, created_at)"
                " SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS ("
                " SELECT 1 FROM media_ref WHERE asset_id=? AND source_kind=?"
                " AND source_id=? AND message_id=? AND role=?)",
                (asset_id, source_kind, str(source_id or ""), str(message_id or ""),
                 int(local_id or 0), int(entry_id or 0), int(fav_id or 0), role, now,
                 asset_id, source_kind, str(source_id or ""), str(message_id or ""), role),
            )
            self._conn.execute(
                "UPDATE media_asset SET last_seen_at=? WHERE asset_id=?", (now, asset_id))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def set_attachment_asset(self, attachment_id: int, asset_id: str,
                             cloud_key: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE attachment SET asset_id=?,"
                " cloud_pdir=?, cloud_name=?, uploaded_at=?, upload_error=NULL"
                " WHERE id=?",
                (asset_id, "摘星日记/媒体", cloud_key or asset_id, now_iso(), attachment_id),
            )
            self._conn.commit()

    def attachment_asset(self, attachment_id: int) -> Optional[sqlite3.Row]:
        rows = self._rows(
            "SELECT a.*, m.sha256 AS asset_sha256, m.kind AS asset_kind,"
            " m.original_key, m.preview_key, m.text_key, m.ocr_text, m.asr_text,"
            " m.caption, m.state AS asset_state"
            " FROM attachment a LEFT JOIN media_asset m ON m.asset_id=a.asset_id"
            " WHERE a.id=?", (int(attachment_id),))
        return rows[0] if rows else None

    def list_media_assets(self, limit: int = 60, offset: int = 0,
                          kind: str = "", state: str = "") -> list[sqlite3.Row]:
        sql = ("SELECT m.*, COUNT(r.id) AS ref_count,"
               " MAX(r.entry_id) AS entry_id, MAX(r.fav_id) AS fav_id"
               " FROM media_asset m LEFT JOIN media_ref r ON r.asset_id=m.asset_id")
        where, args = [], []
        if kind:
            where.append("m.kind=?")
            args.append(kind)
        if state:
            where.append("m.state=?")
            args.append(state)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY m.asset_id ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
        args.extend([max(1, int(limit)), max(0, int(offset))])
        return self._rows(sql, tuple(args))

    def media_refs(self, asset_id: str) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM media_ref WHERE asset_id=? ORDER BY id", (asset_id,))

    def media_counts(self) -> dict:
        rows = self._rows(
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(bytes),0) AS bytes"
            " FROM media_asset GROUP BY kind")
        out = {"image": 0, "video": 0, "bytes": 0, "total": 0}
        for r in rows:
            k = r["kind"] or "other"
            out[k] = int(r["n"] or 0)
            out["total"] += int(r["n"] or 0)
            out["bytes"] += int(r["bytes"] or 0)
        return out

    def unprocessed_media_attachments(self, limit: int = 8) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM attachment WHERE status!='deleted'"
            " AND (asset_id IS NULL OR asset_id='')"
            " AND local_path IS NOT NULL AND local_path!=''"
            " AND kind IN ('image','video','video_thumb')"
            " ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))

    def update_entry_full_text(self, entry_id: int, extra: str) -> None:
        """把附件名、正文摘录、云端路径补进日记正文，再重建这条的语义索引。"""
        extra = (extra or "").strip()
        if not entry_id or not extra:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT full_text FROM diary_entry WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                return
            old = (row["full_text"] or "").strip()
            if not old:
                # 本地正文已迁到微云时，先把旧正文取回来再追加，避免覆盖成只剩附件名。
                try:
                    from .cloud.body_store import fetch_entry_body
                    remote = fetch_entry_body(self, entry_id) or {}
                    old = (remote.get("full_text") or remote.get("raw_text") or "").strip()
                except Exception:
                    old = ""
            new = (old + "\n" + extra).strip() if old else extra
            self._conn.execute(
                "UPDATE diary_entry SET full_text=? WHERE id=?", (new, entry_id))
            self._conn.commit()
        # 这条日记若已经迁到云端，改完再同步一份并清掉本地长文，保持“本地无正文”。
        try:
            from .cloud.body_store import body_cloud_object, put_entry_body, clear_local_entry_body
            if body_cloud_object(self, int(entry_id)):
                put_entry_body(self, int(entry_id), __import__(
                    "src.journal.cloud.body_store", fromlist=["build_body_payload"]
                ).build_body_payload(self, int(entry_id)))
                clear_local_entry_body(self, int(entry_id))
        except Exception:
            pass

    def find_entry_by_url(self, url: str) -> Optional[sqlite3.Row]:
        """按原文链接找已经入过库的日记，避免同一篇文章重复记两条。

        正文迁云后本地 full_text 可能是空的，所以先看本地，再按需取云端正文。
        """
        url = (url or "").strip()
        if not url:
            return None
        rows = self._rows(
            "SELECT id, kind, summary, full_text, raw_text FROM diary_entry"
            " WHERE deleted=0 ORDER BY id DESC LIMIT 200")
        try:
            from .cloud.body_store import hydrate_entry
        except Exception:
            hydrate_entry = None
        for r in rows:
            body = (r["full_text"] or r["raw_text"] or "").strip()
            if not body and hydrate_entry is not None:
                try:
                    full = hydrate_entry(self, r)
                    body = (full.get("full_text") or full.get("raw_text") or "").strip()
                except Exception:
                    body = ""
            if url in body:
                return r
        return None

    def replace_entry_full_text(self, entry_id: int, text: str) -> None:
        """用补全后的正文替换原有正文，保留条目本身，不重复追加。"""
        text = (text or "").strip()
        if not entry_id or not text:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE diary_entry SET full_text=? WHERE id=?", (text, entry_id))
            self._conn.commit()
        try:
            from .cloud.body_store import body_cloud_object, put_entry_body, clear_local_entry_body, build_body_payload
            if body_cloud_object(self, int(entry_id)):
                put_entry_body(self, int(entry_id), build_body_payload(self, int(entry_id)))
                clear_local_entry_body(self, int(entry_id))
        except Exception:
            pass

    def attach_chain(self, entry_id: int, chain_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE diary_entry SET chain_id=? WHERE id=?", (chain_id, entry_id))
            self._conn.commit()

    def entries_between(self, start: str, end: str) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM diary_entry WHERE deleted=0 AND entry_date BETWEEN ? AND ?"
            " ORDER BY entry_date, id", (start, end),
        )

    # ── 提醒链 ──────────────────────────────────────────────────

    def create_chain(self, entry_id: int | None, title: str, start_date: str,
                     days: int = 1, daily_time: str | None = None,
                     object_name: str = "", action: str = "",
                     source_text: str = "") -> int:
        """建链。时刻缺失则停在 pending_time，不产生任何提醒实例。

        daily_time 支持一天多次，写成 "08:00,17:00" 这种逗号分隔。
        """
        if days < 1:
            raise ValueError("days 必须 ≥ 1")
        times = split_times(daily_time)
        value = join_times(times) if times else None
        status = "active" if value else "pending_time"
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminder_chain(entry_id, title, object, action, start_date, days,"
                " daily_time, status, created_at, activated_at, source_text)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, title, object_name, action, start_date, days, value, status,
                 now_iso(), now_iso() if status == "active" else None, source_text),
            )
            self._conn.commit()
            chain_id = cur.lastrowid
        self.audit("parser", "chain_create", "chain", chain_id,
                   {"title": title, "start": start_date, "days": days, "time": value})
        if status == "active":
            self.build_tasks(chain_id)
        return chain_id

    def build_tasks(self, chain_id: int) -> list[int]:
        """按天展开实例：每天 times 条 daily，最后一天之后再补 1 条 final。"""
        rows = self._rows("SELECT * FROM reminder_chain WHERE id=?", (chain_id,))
        if not rows:
            raise KeyError(f"chain {chain_id} 不存在")
        chain = rows[0]
        times = split_times(chain["daily_time"])
        if not times or chain["status"] != "active":
            raise ValueError("链不是 active 或时刻未定，不能展开提醒")
        start = date.fromisoformat(chain["start_date"])
        per_day = len(times)
        created = []
        with self._lock:
            for d in range(1, chain["days"] + 1):
                fire_date = (start + timedelta(days=d - 1)).isoformat()
                for j, hhmm in enumerate(times, start=1):
                    seq = (d - 1) * per_day + j
                    self._conn.execute(
                        "INSERT OR IGNORE INTO reminder_task(chain_id, seq, kind, fire_date, fire_at, status)"
                        " VALUES(?,?,?,?,?, 'pending')",
                        (chain_id, seq, "daily", fire_date, combine(fire_date, hhmm)),
                    )
                    created.append(seq)
            # 收尾提醒落在"做满 N 天"的次日：起始日 + N 天
            final_date = (start + timedelta(days=chain["days"])).isoformat()
            final_seq = chain["days"] * per_day + 1
            self._conn.execute(
                "INSERT OR IGNORE INTO reminder_task(chain_id, seq, kind, fire_date, fire_at, status)"
                " VALUES(?,?,?,?,?, 'pending')",
                (chain_id, final_seq, "final", final_date, combine(final_date, times[0])),
            )
            self._conn.commit()
        self.audit("system", "tasks_built", "chain", chain_id,
                   {"daily_per_day": per_day, "days": chain["days"], "final_date": final_date})
        return created

    def set_chain_time(self, chain_id: int, hhmm) -> list[str]:
        """时刻确认 → 链转 active 并展开实例。hhmm 可以是 "19:00" 或 ["10:00","17:00"]。"""
        times = hhmm if isinstance(hhmm, (list, tuple)) else split_times(hhmm)
        if not times:
            raise ValueError("时刻格式必须是 HH:MM")
        value = join_times(times)
        with self._lock:
            self._conn.execute(
                "UPDATE reminder_chain SET daily_time=?, status='active', activated_at=? WHERE id=?",
                (value, now_iso(), chain_id),
            )
            self._conn.commit()
        self.audit("user", "chain_activate", "chain", chain_id, {"time": value})
        self.build_tasks(chain_id)
        return times

    def update_chain(self, chain_id: int, title: str | None = None,
                     start_date: str | None = None, days: int | None = None,
                     daily_time: str | None = None) -> dict:
        """手动修改在跑的提醒：改标题、起始日、总天数和每天时刻。

        只重建今天及以后的提醒实例；已经发过的保留，不回头补发。
        这样改“共几天”不会把过去几天又排一遍。
        """
        rows = self._rows("SELECT * FROM reminder_chain WHERE id=?", (chain_id,))
        if not rows:
            raise KeyError("reminder chain %s not found" % chain_id)
        old = rows[0]
        title = (title if title is not None else old["title"] or "").strip()
        if not title:
            raise ValueError("提醒内容不能为空")
        start = (start_date if start_date is not None else old["start_date"] or "").strip()
        try:
            start_d = date.fromisoformat(start)
        except ValueError:
            raise ValueError("起始日期必须是 YYYY-MM-DD")
        try:
            total_days = int(days if days is not None else old["days"])
        except (TypeError, ValueError):
            raise ValueError("天数必须是整数")
        if total_days < 1 or total_days > 366:
            raise ValueError("天数必须在 1 到 366 之间")
        times = split_times(daily_time if daily_time is not None else old["daily_time"])
        value = join_times(times) if times else None
        status = "active" if value else "pending_time"

        with self._lock:
            self._conn.execute(
                "UPDATE reminder_chain SET title=?, start_date=?, days=?, daily_time=?,"
                " status=?, activated_at=CASE WHEN ?='active' THEN COALESCE(activated_at, ?)"
                " ELSE activated_at END WHERE id=?",
                (title, start, total_days, value, status, status, now_iso(), chain_id),
            )
            # 只作废还没发出去的旧实例；已发/已跳过的保留作历史。
            self._conn.execute(
                "UPDATE reminder_task SET status='skipped', last_error='用户修改提醒，旧计划作废'"
                " WHERE chain_id=? AND status='pending'", (chain_id,),
            )
            self._conn.commit()

        if status == "active":
            self._rebuild_future_tasks(chain_id, start_d, total_days, times)
        self.audit("web", "chain_update", "chain", chain_id,
                   {"title": title, "start": start, "days": total_days, "time": value})
        return dict(self.chain(chain_id))

    def _rebuild_future_tasks(self, chain_id: int, start_d: date,
                              total_days: int, times: list[str]) -> None:
        """从今天（或起始日）往后排；过去的日子不再生成实例。"""
        today = date.today()
        first = max(start_d, today)
        last = start_d + timedelta(days=total_days - 1)
        if last < first:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0) AS m FROM reminder_task WHERE chain_id=?",
                (chain_id,),
            ).fetchone()
            seq = int(row["m"] or 0)
            d = first
            while d <= last:
                fire_date = d.isoformat()
                for hhmm in times:
                    seq += 1
                    self._conn.execute(
                        "INSERT OR IGNORE INTO reminder_task(chain_id, seq, kind, fire_date, fire_at, status)"
                        " VALUES(?,?,?,?,?, 'pending')",
                        (chain_id, seq, "daily", fire_date, combine(fire_date, hhmm)),
                    )
                d += timedelta(days=1)
            final_date = (start_d + timedelta(days=total_days)).isoformat()
            seq += 1
            self._conn.execute(
                "INSERT OR IGNORE INTO reminder_task(chain_id, seq, kind, fire_date, fire_at, status)"
                " VALUES(?,?,?,?,?, 'pending')",
                (chain_id, seq, "final", final_date, combine(final_date, times[0])),
            )
            self._conn.commit()
        self.audit("web", "tasks_rebuilt", "chain", chain_id,
                   {"first": first.isoformat(), "last": last.isoformat(),
                    "per_day": len(times)})

    def cancel_chain(self, chain_id: int, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminder_chain SET status='cancelled' WHERE id=?", (chain_id,)
            )
            self._conn.execute(
                "UPDATE reminder_task SET status='skipped' WHERE chain_id=? AND status='pending'",
                (chain_id,),
            )
            self._conn.commit()
        self.audit("user", "cancel", "chain", chain_id, {"reason": reason})

    def finish_chain(self, chain_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminder_chain SET status='finished', finished_at=? WHERE id=?",
                (now_iso(), chain_id),
            )
            self._conn.execute(
                "UPDATE reminder_chain SET end_notice_sent=1 WHERE id=?", (chain_id,)
            )
            self._conn.commit()
        self.audit("system", "chain_finish", "chain", chain_id)

    def chains(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        if status:
            return self._rows("SELECT * FROM reminder_chain WHERE status=? ORDER BY id DESC", (status,))
        return self._rows("SELECT * FROM reminder_chain ORDER BY id DESC")

    def chain(self, chain_id: int) -> Optional[sqlite3.Row]:
        rows = self._rows("SELECT * FROM reminder_chain WHERE id=?", (chain_id,))
        return rows[0] if rows else None

    # ── 提醒实例 ────────────────────────────────────────────────

    def due_tasks(self, at: Optional[str] = None) -> list[sqlite3.Row]:
        at = at or now_iso()
        return self._rows(
            "SELECT t.*, c.title, c.object, c.action, c.days, c.start_date, c.daily_time FROM reminder_task t"
            " JOIN reminder_chain c ON c.id = t.chain_id"
            " WHERE t.status='pending' AND c.status='active' AND t.fire_at <= ?"
            " ORDER BY t.fire_at", (at,),
        )

    def mark_task_sent(self, task_id: int, via: str = DELIVERY_VIA) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminder_task SET status='sent', sent_at=?, delivery_via=? WHERE id=?",
                (now_iso(), via, task_id),
            )
            self._conn.commit()
        self.audit("scan", "task_send", "task", task_id, {"via": via})

    def mark_task_failed(self, task_id: int, error: str, via: str = DELIVERY_VIA) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT retry_count FROM reminder_task WHERE id=?", (task_id,)
            ).fetchone()
            retry = (row["retry_count"] if row else 0) + 1
            status = "failed" if retry >= 3 else "pending"
            self._conn.execute(
                "UPDATE reminder_task SET status=?, retry_count=?, last_error=?, delivery_via=?"
                " WHERE id=?",
                (status, retry, error[:300], via, task_id),
            )
            self._conn.commit()
        self.audit("scan", "task_fail", "task", task_id,
                   {"error": error[:300], "retry": retry}, "fail")
        return retry

    def mark_task_skipped(self, task_id: int, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminder_task SET status='skipped', last_error=? WHERE id=?",
                (reason[:300], task_id),
            )
            self._conn.commit()
        self.audit("system", "task_skip", "task", task_id, {"reason": reason}, "skip")

    def tasks_of_chain(self, chain_id: int) -> list[sqlite3.Row]:
        return self._rows(
            "SELECT * FROM reminder_task WHERE chain_id=? ORDER BY seq, kind", (chain_id,)
        )

    # ── 追问 ────────────────────────────────────────────────────

    def active_question(self, chat_id: str = DEFAULT_CHAT_ID) -> Optional[sqlite3.Row]:
        rows = self._rows(
            "SELECT * FROM pending_question WHERE chat_id=? AND state='asked'", (chat_id,)
        )
        return rows[0] if rows else None

    def ask_question(self, kind: str, question_text: str, chain_id: int | None = None,
                     chat_id: str = DEFAULT_CHAT_ID, payload: Any = None,
                     ttl_hours: int = 24) -> int:
        """挂一条待答。同会话已有 asked 时报错（不静默覆盖）。"""
        if self.active_question(chat_id):
            raise ValueError("该会话已有待回答问题，先答或先过期")
        expires = (datetime.now() + timedelta(hours=ttl_hours)).replace(microsecond=0).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO pending_question(chat_id, kind, chain_id, payload, question_text,"
                " state, ask_count, asked_at, expires_at) VALUES(?,?,?,?,?, 'asked', 1, ?, ?)",
                (chat_id, kind, chain_id, json.dumps(payload, ensure_ascii=False) if payload else None,
                 question_text, now_iso(), expires),
            )
            self._conn.commit()
            qid = cur.lastrowid
        self.audit("system", "ask", "question", qid, {"kind": kind, "chain_id": chain_id})
        return qid

    def answer_question(self, question_id: int, answer_text: str, hhmm: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_question SET state='answered', answered_at=?, answer_text=?"
                " WHERE id=?", (now_iso(), answer_text, question_id),
            )
            self._conn.commit()
        self.audit("user", "answer", "question", question_id,
                   {"answer": answer_text, "time": hhmm})
        if hhmm:
            rows = self._rows("SELECT chain_id FROM pending_question WHERE id=?", (question_id,))
            chain_id = rows[0]["chain_id"] if rows else None
            if chain_id:
                self.set_chain_time(chain_id, hhmm)

    def expire_questions(self, at: Optional[str] = None) -> list[int]:
        at = at or now_iso()
        rows = self._rows(
            "SELECT id, chain_id FROM pending_question WHERE state='asked' AND expires_at <= ?", (at,)
        )
        ids = []
        with self._lock:
            for r in rows:
                self._conn.execute(
                    "UPDATE pending_question SET state='expired' WHERE id=?", (r["id"],)
                )
                if r["chain_id"]:
                    self._conn.execute(
                        "UPDATE reminder_chain SET status='expired' WHERE id=? AND status='pending_time'",
                        (r["chain_id"],),
                    )
                ids.append(r["id"])
            self._conn.commit()
        for qid in ids:
            self.audit("system", "question_expire", "question", qid, None, "skip")
        return ids

    # ── 周复盘 ──────────────────────────────────────────────────

    def upsert_weekly(self, week_start: str, week_end: str, html_path: str,
                      stats: Any, narrative: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM weekly_review WHERE week_start=?", (week_start,)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE weekly_review SET week_end=?, generated_at=?, html_path=?, stats=?,"
                    " narrative=? WHERE id=?",
                    (week_end, now_iso(), html_path,
                     json.dumps(stats, ensure_ascii=False), narrative, row["id"]),
                )
                self._conn.commit()
                wid = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO weekly_review(week_start, week_end, generated_at, html_path,"
                    " stats, narrative) VALUES(?,?,?,?,?,?)",
                    (week_start, week_end, now_iso(), html_path,
                     json.dumps(stats, ensure_ascii=False), narrative),
                )
                self._conn.commit()
                wid = cur.lastrowid
        self.audit("system", "weekly_build", "weekly", wid, {"week_start": week_start})
        return wid

    def weekly_stats(self, start: str, end: str) -> dict:
        """周复盘要用到的数，全部从表里算，不额外存隐藏字段。"""
        entries = self.entries_between(start, end)
        sent = self._rows(
            "SELECT COUNT(*) AS n FROM reminder_task WHERE status='sent' AND fire_date BETWEEN ? AND ?",
            (start, end),
        )[0]["n"]
        skipped = self._rows(
            "SELECT COUNT(*) AS n FROM reminder_task WHERE status='skipped' AND fire_date BETWEEN ? AND ?",
            (start, end),
        )[0]["n"]
        failed = self._rows(
            "SELECT COUNT(*) AS n FROM reminder_task WHERE status='failed' AND fire_date BETWEEN ? AND ?",
            (start, end),
        )[0]["n"]
        return {
            "entries": len(entries),
            "tasks_sent": sent,
            "tasks_skipped": skipped,
            "tasks_failed": failed,
            "chains": len(self._rows(
                "SELECT id FROM reminder_chain WHERE start_date BETWEEN ? AND ?", (start, end)
            )),
        }

    # ── 云端对象记账（v1.4） ─────────────────────────────────────

    def add_cloud_object(self, kind: str, entry_id: int | None, object_key: str,
                         pdir_key: str, name: str, bytes: int | None = None,
                         sha256: str = "", local_path: str = "",
                         created_at: str | None = None, state: str = "uploaded") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cloud_object(kind, entry_id, object_key, pdir_key, name, bytes,"
                " sha256, local_path, created_at, state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (kind, entry_id, object_key, pdir_key, name, bytes, sha256, local_path,
                 created_at or now_iso(), state),
            )
            self._conn.commit()
            cid = cur.lastrowid
        self.audit("system", "cloud_upload", kind, cid,
                   {"name": name, "bytes": bytes, "entry_id": entry_id})
        return cid

    def list_cloud_objects(self, kind: Optional[str] = None,
                           uploaded_before: Optional[str] = None,
                           state: Optional[str] = "uploaded") -> list[sqlite3.Row]:
        sql = "SELECT * FROM cloud_object WHERE 1=1"
        params: list = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if uploaded_before:
            sql += " AND created_at < ?"
            params.append(uploaded_before)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY id DESC"
        return self._rows(sql, tuple(params))

    def cloud_objects_of_entry(self, entry_id: int) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM cloud_object WHERE entry_id=? ORDER BY id DESC",
                          (entry_id,))

    def cloud_of_entry(self, entry_id: int, kind: str = "voice") -> Optional[sqlite3.Row]:
        # purged 只是"本地缓存删了"，云端对象照样在，回听仍要找得到
        rows = self._rows(
            "SELECT * FROM cloud_object WHERE entry_id=? AND kind=?"
            " ORDER BY id DESC LIMIT 1", (entry_id, kind))
        return rows[0] if rows else None

    def mark_cloud_purged(self, cloud_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE cloud_object SET state='purged', purged_at=?, local_path='' WHERE id=?",
                (now_iso(), cloud_id),
            )
            self._conn.commit()

    def last_snapshot_at(self) -> str:
        rows = self._rows(
            "SELECT created_at FROM cloud_object WHERE kind='db_snapshot' ORDER BY id DESC LIMIT 1")
        return rows[0]["created_at"] if rows else ""



