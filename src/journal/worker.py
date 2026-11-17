"""常驻工作线程：把入口、提醒、复盘串成一直在跑的一根线（阶段4 模块七）。

三件事按不同节拍：
  每 8 秒   扫一次文件传输助手和 ClawBot，有新语音/文字/分享就处理
  每 60 秒  扫一次到点的提醒，发出去；顺带清理过期追问
  每天 00:05 生成前一周的复盘页；每月 1 号再生成上个月的

另外提供助手号回话的接管入口：你在 ClawBot 里回"晚上七点"，也认。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .capture import JournalIngest
from .cloud import cloud_enabled
from .remind import JournalService
from .store import JournalStore
from .voice_queue import VoiceQueue
from .weekly import generate

logger = logging.getLogger(__name__)

SCAN_SEC = 8
TICK_SEC = 60
REVIEW_HOUR = 0
REVIEW_MINUTE = 5
CHAT_CLOUD_SEC = 300
VOICE_QUEUE_SEC = 5
CHAT_MEDIA_SEC = 30
MEDIA_SEC = 10
CLOUD_RETRY_SEC = 600      # 云任务失败后 10 分钟再试，避免主循环每 5 秒重刷
REVIEW_RETRY_SEC = 600

_service: Optional[JournalService] = None
_store: Optional[JournalStore] = None
_ingest: Optional[JournalIngest] = None


def get_store() -> JournalStore:
    global _store
    if _store is None:
        _store = JournalStore()
    return _store


def get_service() -> JournalService:
    global _service
    if _service is None:
        _service = JournalService(store=get_store())
    return _service


def get_ingest() -> JournalIngest:
    """全局共用一套入库器，保留真实微信客户端和语音识别器。

    以前 handle_ilink_message 每条消息新建 JournalIngest(client=object(),
    asr=object())，一碰到附件就会调用假 client 下载，必然报错。
    """
    global _ingest
    if _ingest is None:
        _ingest = JournalIngest(store=get_store(), service=get_service())
    return _ingest


def handle_ilink_message(msg: dict) -> bool:
    """助手号收到的消息先给日记看一眼。

    返回 True 表示这句话已经被日记消费（回时刻 / 说停），上层不应再交给
    对话路由，否则你会收到两条回复。返回 False 表示照常交给助手。
    """
    try:
        text = (msg.get("content") or msg.get("text") or "").strip()
        if not text:
            return False
        svc = get_service()
        # “以后回答引用原话”是长期规则，先落库并回执；不能再交给对话助手
        # 让它只答一句“好的”，那样规则其实没生效。
        try:
            from . import answer_rule
            rule_reply = answer_rule.maybe_handle(text, store=svc.store)
            if rule_reply:
                svc._say(rule_reply)
                return True
        except Exception as e:  # noqa: BLE001
            logger.warning("回答规则处理失败: %s", e)

        # Bot 文字“存起来”同样要接最近暂存的附件；不要让它落到对话助手
        # 再反问“存哪一条、存到哪”。
        try:
            ingest = get_ingest()
            stored = ingest._store_pending_attachments(text, None)
            if stored is not None:
                svc._say(stored)
                return True
        except Exception as e:  # noqa: BLE001
            logger.warning("暂存附件入库失败: %s", e)

        if svc.is_stop_command(text):
            svc.handle_text("ilink_dm", text, source_id=None)
            return True
        # 新建/删除提醒在等你确认：先接住“确认/取消”，不要落到对话助手。
        if svc.has_pending_confirmation("ilink_dm"):
            from .remind import is_confirmation_reply
            if is_confirmation_reply(text):
                svc.handle_text("ilink_dm", text, source_id=None)
                return True
        # 微信里说"提醒我/每天提醒"：一律由日记链接手并回执。
        # 不能再交给对话助手，否则它会另建一套定时任务，两边各发一遍。
        from .remind import is_create_reminder_request
        if is_create_reminder_request(text):
            svc.handle_text("ilink_dm", text, source_id=None)
            return True
        # “现在有什么定时任务”默认指生活提醒，直接走文字兜底，
        # 不交给模型去猜 list_crons 还是 list_journal_reminders。
        # 明确说“采集与维护/群摘要/榜单/自动推送”时不拦，交给对话助手查采集任务。
        try:
            if not any(k in text for k in ("采集与维护", "群摘要", "榜单", "自动推送")):
                from . import commands
                answer = commands.try_handle(text, svc)
                if answer:
                    svc._say(answer)
                    return True
        except Exception as e:  # noqa: BLE001
            logger.warning("文字命令兜底失败: %s", e)

        # “把某某的位置发给我/某某在哪/导航到某某”这类话，直接查收藏标签
        # 和原始聊天位置，不交给模型猜；模型看不到原始位置消息。
        try:
            if any(k in text for k in ("位置", "定位", "地址", "导航")):
                from .places import guess_person_name, find_locations, format_locations
                person = guess_person_name(text)
                result = find_locations(person) if person else {}
                if result.get("diary") or result.get("favorites") or result.get("chats"):
                    svc._say(format_locations(result))
                    return True
        except Exception as e:  # noqa: BLE001
            logger.warning("位置查询兜底失败: %s", e)

        pending = svc.store.active_question("ilink_dm")
        if not pending:
            return False
        if svc.looks_like_answer(text):
            svc.handle_text("ilink_dm", text, source_id=None)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("助手号回话处理失败: %s", e)
        return False


class JournalWorker:
    """一个后台线程，活到进程结束。"""

    def __init__(self, store: Optional[JournalStore] = None,
                 service: Optional[JournalService] = None,
                 ingest: Optional[JournalIngest] = None):
        self.store = store or get_store()
        self.service = service or get_service()
        self.ingest = ingest or get_ingest()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock_handle = None
        self._last_scan = 0.0
        self._last_tick = 0.0
        self._scan_cursor = self._load_scan_cursor()
        self._last_review_day = ""
        self._last_review_attempt = 0.0
        self._last_cloud_day = ""
        self._last_cloud_attempt = 0.0
        self._last_merge_day = ""
        self._last_merge_attempt = 0.0
        self._last_body_offload = 0.0
        self._last_chat_cloud = 0.0
        self._last_voice_queue = 0.0
        self._last_media = 0.0
        self._last_chat_media = 0.0
        self._voice_backfilled = False
        messages_db = Path(self.store.db_path).parent / "messages.db"
        self.voice_queue = VoiceQueue(
            messages_db,
            client=self.ingest.client,
            asr=self.ingest.asr,
        )

    # ── 单实例保护 ──────────────────────────────────────────────

    def _scan_state_path(self) -> Path:
        # journal.db 在 data/journal.db，游标跟全文包放在 data/journal/export；
        # 不写成 data/export，避免把状态文件散到别的目录。
        return Path(self.store.db_path).with_suffix("") / "export" / "scan_state.json"

    def _load_scan_cursor(self) -> int:
        """只存“扫到哪条”的游标，不存任何正文。

        彻底删除后不再写来源墓碑；靠这个游标避免扫描器把刚删掉的
        文件传输助手消息又自动扫回来。用户之后重新发一条新消息，
        因为 local_id 更大，仍会正常入库。
        """
        try:
            data = json.loads(self._scan_state_path().read_text(encoding="utf-8"))
            return int(data.get("last_local_id") or 0)
        except Exception:
            return 0

    def _save_scan_cursor(self, local_id: int) -> None:
        try:
            p = self._scan_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "last_local_id": int(local_id),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("保存扫描游标失败: %s", e)

    def _acquire_instance_lock(self) -> bool:
        """同一份 journal.db 只允许一个日记线程在跑。

        主程序启动时会开日记线程；联调时又可能单独再起一个
        run_journal_worker.py。两个线程各扫各的，会重复回执、重复发提醒。
        这里用系统级文件锁兜住：第二个进程拿不到锁就不启动。
        """
        try:
            lock_path = Path(self.store.db_path).parent / "journal_worker.lock"
            handle = open(lock_path, "a+")
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_handle = handle
            return True
        except OSError:
            try:
                if "handle" in locals():
                    handle.close()
            except Exception:  # noqa: BLE001
                pass
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("日记线程单实例锁初始化失败: %s", e)
            return True  # 锁机制本身出问题时不阻断功能

    def _release_instance_lock(self) -> None:
        handle, self._lock_handle = self._lock_handle, None
        if not handle:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass

    # ── 生命周期 ────────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return False
        if not self._acquire_instance_lock():
            logger.warning("已有另一个日记线程在跑（锁被占用），本进程不再启动")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="journal-worker", daemon=True)
        self._thread.start()
        logger.info("日记工作线程已启动（每 %d 秒扫一次消息）", SCAN_SEC)
        return True

    def stop(self) -> bool:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._release_instance_lock()
            logger.info("日记工作线程已停止")
            return True
        self._release_instance_lock()
        return False

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── 主循环 ──────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now - self._last_scan >= SCAN_SEC:
                    self._last_scan = now
                    self.safe_scan()
                if now - self._last_tick >= TICK_SEC:
                    self._last_tick = now
                    self.safe_tick()
                self.safe_review()
                self.safe_cloud()
                self.safe_daily_merge()
                self.safe_body_offload()
                if now - self._last_voice_queue >= VOICE_QUEUE_SEC:
                    self._last_voice_queue = now
                    if not self._voice_backfilled:
                        self.safe_voice_backfill()
                    self.safe_voice_queue()
                if now - self._last_media >= MEDIA_SEC:
                    self._last_media = now
                    self.safe_media()
                if now - self._last_chat_media >= CHAT_MEDIA_SEC:
                    self._last_chat_media = now
                    self.safe_chat_media()
                if now - self._last_chat_cloud >= CHAT_CLOUD_SEC:
                    self._last_chat_cloud = now
                    self.safe_chat_cloud()
            except Exception as e:  # noqa: BLE001
                logger.warning("日记工作线程异常: %s", e)
            self._stop.wait(5)

    def safe_scan(self) -> dict:
        try:
            r = self.ingest.scan(limit=20, since=self._scan_cursor)
            last = int(r.get("last_local_id") or 0)
            if last > self._scan_cursor:
                self._scan_cursor = last
                self._save_scan_cursor(last)
            if any(r.get(k) for k in ("voice", "text", "share", "failed")):
                logger.info("新消息处理：%s", r)
            ra = self.ingest.scan_answers(limit=10)
            if ra.get("answer") or ra.get("failed"):
                logger.info("助手号回话处理：%s", ra)
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("扫描文件传输助手失败: %s", e)
            return {}

    def safe_voice_backfill(self) -> dict:
        """启动后一次性把历史“[语音]”占位登记进队列，只跑一次。"""
        self._voice_backfilled = True
        try:
            from .voice_queue import backfill_placeholders
            messages_db = Path(self.store.db_path).parent / "messages.db"
            r = backfill_placeholders(messages_db)
            logger.info("历史语音登记：扫描 %s 条，新登记 %s 条，已有 %s 条",
                        r.get("scanned"), r.get("enqueued"), r.get("already"))
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("历史语音登记失败: %s", e)
            return {}

    def safe_voice_queue(self) -> dict:
        """每 5 秒推进一两条新语音；转写失败自动留在队列重试。"""
        try:
            r = self.voice_queue.run_once(limit=2)
            if r.get("claimed"):
                logger.info("语音队列：%s", r)
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("语音队列推进失败: %s", e)
            return {}

    def safe_media(self) -> dict:
        """每 10 秒补一轮图片/视频：上传原件、缩略图、OCR/转写。"""
        try:
            from .media import scan_attachments
            r = scan_attachments(self.store, limit=2)
            if r.get("ok") or r.get("failed"):
                logger.info("媒体资产：%s", r)
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("媒体扫描失败: %s", e)
            return {}

    def safe_chat_media(self) -> dict:
        """每 30 秒取一两张聊天图片/视频；原始 XML 只在入库时旁路留过才处理。"""
        try:
            from .media import scan_chat_media
            r = scan_chat_media(self.store, limit=2)
            if r.get("ok") or r.get("failed"):
                logger.info("聊天媒体：%s", r)
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("聊天媒体扫描失败: %s", e)
            return {}

    def safe_tick(self) -> dict:
        try:
            r = self.service.tick()
            if r.get("sent") or r.get("failed") or r.get("expired"):
                logger.info("提醒扫描：%s", r)
            self.service.sweep_pending_time()
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("提醒扫描失败: %s", e)
            return {}

    def safe_review(self) -> Optional[str]:
        """每天零点过后生成前一周复盘；每月 1 号再补一份上月的。"""
        today = date.today().isoformat()
        now = datetime.now()
        if self._last_review_day == today:
            return None
        if now.hour < REVIEW_HOUR or (now.hour == REVIEW_HOUR and now.minute < REVIEW_MINUTE):
            return None
        if time.monotonic() - self._last_review_attempt < REVIEW_RETRY_SEC:
            return None
        self._last_review_attempt = time.monotonic()
        try:
            r1 = generate(self.store, "week", date.today() - timedelta(days=7))
            logger.info("已生成周复盘：%s", r1["path"])
            path = r1["path"]
            if date.today().day <= 3:
                r2 = generate(self.store, "month", date.today() - timedelta(days=7))
                logger.info("已生成月复盘：%s", r2["path"])
                path = r2["path"]
            if cloud_enabled():
                try:
                    from .cloud.storage import CloudStore
                    CloudStore(store=self.store).put_review(path)
                    logger.info("复盘页已上云：%s", os.path.basename(path))
                except Exception as e:  # noqa: BLE001
                    logger.warning("复盘页上云失败（本地已生成）: %s", e)
            # 只有真正生成成功才记“今天已做”；失败后 10 分钟还会重试，
            # 不会像以前那样当天直接放弃。
            self._last_review_day = today
            return path
        except Exception as e:  # noqa: BLE001
            logger.warning("生成复盘失败（%s 分钟后重试）: %s",
                           REVIEW_RETRY_SEC // 60, e)
            return None

    # ── 云端存储（数据库快照 + 本地清场） ───────────────────────

    def safe_cloud(self) -> dict:
        """每天一次：日记库快照 + 知识库全文上云；顺手清掉超期的本地语音缓存。"""
        today = date.today().isoformat()
        out = {"snapshot": "", "text_export": "", "purged": 0}
        if self._last_cloud_day == today:
            return out
        if not cloud_enabled():
            return out
        # 失败也记一次尝试时间；否则主循环每 5 秒就会重刷云端和日志。
        if time.monotonic() - self._last_cloud_attempt < CLOUD_RETRY_SEC:
            return out
        self._last_cloud_attempt = time.monotonic()
        text_ok = True
        try:
            from .cloud.storage import CloudStore
            cs = CloudStore(store=self.store)
            # 当天已经有过数据库快照就不再重传，但仍要补一份全文导出。
            if (self.store.last_snapshot_at() or "")[:10] != today:
                rec = cs.put_snapshot(self.store.db_path)
                if rec:
                    out["snapshot"] = rec["name"]
                    logger.info("日记库快照上云：%s", rec["name"])
            try:
                messages_db = str(Path(__file__).resolve().parents[2] / "data" / "messages.db")
                rec = cs.put_knowledge_export(self.store.db_path, messages_db)
                if rec:
                    out["text_export"] = rec["name"]
            except Exception as e:  # noqa: BLE001
                text_ok = False
                logger.warning("知识库全文上云失败（不影响日记库快照）: %s", e)
            p = cs.clean_voice_cache(str(Path(__file__).resolve().parents[2] / "data" / "journal" / "voice"))
            out["purged"] = p.get("removed", 0)
            if p.get("removed"):
                logger.info("清理本地语音缓存 %d 个，释放 %d 字节", p["removed"], p["freed"])
            if text_ok:
                self._last_cloud_day = today
            else:
                logger.warning("全文包今天未成功，10 分钟后再试")
        except Exception as e:  # noqa: BLE001
            logger.warning("云端存储任务失败: %s", e)
        return out

    def safe_daily_merge(self) -> dict:
        """每天一次：全量包之外再合一份“全量 + 增量”兜底包。

        全量包每天成功时已经覆盖历史；这里主要防止全量失败、全量落后或
        增量攒了多天的情况。合并包上传后本地临时明文立即删除。
        """
        today = date.today().isoformat()
        if self._last_merge_day == today:
            return {}
        if not cloud_enabled():
            return {}
        if time.monotonic() - self._last_merge_attempt < CLOUD_RETRY_SEC:
            return {}
        self._last_merge_attempt = time.monotonic()
        try:
            from .cloud.chat_merge import merge_cloud_daily
            r = merge_cloud_daily(self.store)
            self._last_merge_day = today
            logger.info("每日增量合并上云：%s（聊天 %s 条）",
                        r.get("cloud_name"),
                        (r.get("counts") or {}).get("messages"))
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("每日增量合并失败（%s 分钟后重试）: %s",
                           CLOUD_RETRY_SEC // 60, e)
            return {}

    def safe_body_offload(self) -> dict:
        """新日记入库后自动把正文搬去微云，本地只留摘要和指针。

        每 60 秒扫一次；一次最多迁 3 条，避免大量附件正文同时上传把同步堵住。
        辣椒红 id=9 在保护名单里，永远不动。
        """
        if not cloud_enabled():
            return {}
        if time.monotonic() - self._last_body_offload < 60:
            return {}
        self._last_body_offload = time.monotonic()
        try:
            from .cloud.body_store import migrate_entry
            rows = self.store._rows(
                "SELECT id FROM diary_entry WHERE deleted=0"
                " AND (COALESCE(raw_text,'')<>'' OR COALESCE(full_text,'')<>'')"
                " ORDER BY id DESC LIMIT 3")
            done = []
            for r in rows:
                try:
                    info = migrate_entry(self.store, int(r["id"]))
                    if info.get("cloud"):
                        done.append(info)
                except Exception as e:  # noqa: BLE001
                    logger.warning("日记正文上云失败 id=%s: %s", r["id"], e)
            return {"done": done}
        except Exception as e:  # noqa: BLE001
            logger.warning("扫描待上云正文失败: %s", e)
            return {}

    def safe_chat_cloud(self) -> dict:
        """每 5 分钟把新聊天记录写成增量包上传微云。

        本地 messages.db 保留不动；上传的是增量临时包，成功后本地立即删掉。
        """
        if not cloud_enabled():
            return {}
        try:
            from .cloud.chat_sync import ChatIncrementSync
            messages_db = str(Path(__file__).resolve().parents[2] / "data" / "messages.db")
            r = ChatIncrementSync(
                messages_db=messages_db,
                journal_db=self.store.db_path,
                store=self.store,
            ).run()
            if r.get("new"):
                logger.info("聊天增量已上云：%d 条", r["new"])
            if r.get("corrected"):
                logger.info("语音补字已上云：%d 条", r["corrected"])
            return r
        except Exception as e:  # noqa: BLE001
            logger.warning("聊天增量上云失败（本地消息不受影响）: %s", e)
            return {}


_worker: Optional[JournalWorker] = None


def start_worker() -> bool:
    global _worker
    if _worker and _worker.is_running():
        return True
    _worker = JournalWorker()
    return _worker.start()


def stop_worker() -> bool:
    global _worker
    if not _worker:
        return True
    return _worker.stop()


def worker_running() -> bool:
    return bool(_worker and _worker.is_running())
