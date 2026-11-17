"""日记服务：把"收到一句话"到"到点提醒"的流程串起来（阶段4 模块四）。

分工（最终口径）：
  收：文件传输助手（读微信本地库，或 ClawBot 里你回的话）
  发：ClawBot 会话（iLink 接口，纯 HTTP，不碰界面）

对外就三个动作：
  handle_text(chat_id, text, source_id) —— 来一句话，记日记、必要时建提醒并追问时刻
  handle_answer(text)                   —— 回了一句时刻，接上待答追问
  tick(now)                             —— 定时扫描：到点发提醒、过期清理、收尾
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .parse import DeepSeekParser, entry_date_of, parse_time_hint, parse_times_hint
from .store import DEFAULT_CHAT_ID, DELIVERY_VIA, JournalStore, now_iso, today_str
from src.config import load_config

logger = logging.getLogger(__name__)

MAX_ASK = 2          # 问两次时刻还不给，就按当下整点先跑起来并标注
ASK_TTL_HOURS = 6    # 追问多久没人答就过期
LATE_GRACE_MIN = 120  # 到点后超过两小时就不再补发，免得深夜翻旧账

# 明确的"停"指令。短语命中即按取消处理，不依赖模型判意图——
# 模型把"删掉那个小模型"误判成 cancel 的坑已经踩过，这里只认"停/取消"类词。
STOP_HINTS = re.compile(
    r"(停掉|停下来|停一下|停了|停了?吧|停止|别再提醒|别提醒|不用再提醒|不用提醒了|"
    r"不要再提醒|取消提醒|取消这项|取消这件|取消掉|撤销提醒)")
# 出现这些词就说明是在"建提醒"而不是"停提醒"，不能误判
CREATE_HINTS = re.compile(r"(每天|每周|连续|提醒我|提醒一下|到时候)")
# 用户明确在"新建一条提醒"的口吻。微信里的提醒只走日记链，
# 不能再交给对话助手，否则同一条会建两套、各发一遍。
REMIND_REQUEST_HINTS = re.compile(
    r"(提醒我|提醒一下|提醒下|到点提醒|到时候提醒|通知我|通知一下|叫我|喊我|记得提醒|"
    r"每天.{0,8}提醒|每周.{0,8}提醒|连续.{0,8}提醒)"
)
ALL_HINTS = re.compile(r"(全部|所有|都停|全停)")

# 新建/删除提醒先挂一条确认，避免语音听错就直接建或删。
CONFIRM_KINDS = ("confirm_create", "confirm_delete")
_CONFIRM_YES = re.compile(
    r"(确认|确定|同意|可以|好的|行|是的|对|建吧|删吧|就建|就删|就这么办)")
_CONFIRM_NO = re.compile(
    r"(取消|不建|不删|不要|别|算了|先不|不了|不用|不确认)")


def is_create_reminder_request(text: str) -> bool:
    """这句话是不是要让系统新建一条提醒（不是停）。"""
    return bool(text and REMIND_REQUEST_HINTS.search(text))


def is_confirmation_reply(text: str) -> bool:
    """是不是在回“确认/取消”，用于接住提醒确认。"""
    t = (text or "").strip()
    if not t or len(t) > 20:
        return False
    return bool(_CONFIRM_YES.search(t) or _CONFIRM_NO.search(t))


def _mmdd(d: str) -> str:
    try:
        return "%d月%d日" % (int(d[5:7]), int(d[8:10]))
    except (ValueError, IndexError):
        return d


def _parse_key() -> str:
    """解析模型的密钥：专供日记的优先，其次沿用系统已配的 AI 通道密钥。"""
    key = os.environ.get("JOURNAL_PARSE_KEY", "").strip()
    if key:
        return key
    try:
        return load_config().ai_provider_api_key
    except Exception:  # noqa: BLE001
        return ""


class JournalService:
    """一条流水线的门面。发送函数可注入，方便测试。"""

    def __init__(self, store: Optional[JournalStore] = None,
                 parser: Optional[DeepSeekParser] = None,
                 push: Optional[Callable[[str], dict]] = None,
                 chat_id: str = DEFAULT_CHAT_ID):
        self.store = store or JournalStore()
        self.parser = parser or DeepSeekParser(api_key=_parse_key())
        self.push = push or self._default_push()
        self.chat_id = chat_id

    # ── 发送 ────────────────────────────────────────────────────

    @staticmethod
    def _default_push() -> Callable[[str], dict]:
        from src.wechat.ilink_push import get_ilink_push

        # 真正复用进程级单例：限速器在 ILinkPush 实例里，每次新建实例
        # 会让 2.5 秒限速失效，和助手回复撞在一起时容易被判成刷屏。
        client = get_ilink_push()

        def _send(text: str) -> dict:
            return client.send_message(text)

        return _send

    @staticmethod
    def _record_attempt(ok: bool, text: str, error: str = "") -> None:
        """把这次发出去的提醒记进推送记录表，免得后台「推送记录」永远是 0。"""
        try:
            import sqlite3
            import time
            import uuid
            from pathlib import Path
            from src.im.delivery import DeliveryService
            db = Path(__file__).resolve().parents[2] / "data" / "im_delivery.db"
            # 先让正式投递服务建表/补列；否则单独跑日记线程时表可能还不存在，
            # 插入会静默失败，页面又变回 0。
            DeliveryService(db_path=db)
            with sqlite3.connect(str(db), timeout=5) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO im_delivery_attempts"
                    "(id, platform, channel, target, source_type, status, created_at, finished_at, provider_error)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "ilink", "ilink", "clawbot", "journal_reminder",
                     "success" if ok else "failed", time.time(), time.time(),
                     "" if ok else (error or "发送失败")),
                )
        except Exception:  # noqa: BLE001
            pass

    def _say(self, text: str) -> bool:
        try:
            r = self.push(text)
            if isinstance(r, dict) and not r.get("success", True):
                logger.warning("回执发送失败: %s", r.get("error"))
                self._record_attempt(False, text, str(r.get("error") or ""))
                return False
            self._record_attempt(True, text)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("回执发送异常: %s", e)
            return False

    # ── 确认后执行：新建/删除提醒先问一句 ─────────────────────

    def has_pending_confirmation(self, chat_id: str = DEFAULT_CHAT_ID) -> bool:
        q = self.store.active_question(chat_id)
        return bool(q and q["kind"] in CONFIRM_KINDS)

    def propose_create(self, text: str, chat_id: str = DEFAULT_CHAT_ID,
                       parsed: Optional[dict] = None, entry_kind: str = "text",
                       source_id: Optional[int] = None, at: Optional[datetime] = None,
                       say: bool = False) -> dict:
        """把新建提醒先挂成待确认，不落日记、不建实例。"""
        at = at or datetime.now()
        text = (text or "").strip()
        parsed = parsed or self.parser.parse(text, today=today_str(),
                                             now_hhmm=at.strftime("%H:%M"))
        rem = parsed.get("reminder") or {}
        if parsed.get("kind") != "reminder" or not rem:
            reply = "没听出要建什么提醒。可以说“提醒我每天九点给红袍鸟房喂食”。"
            if say:
                self._say(reply)
            return {"ok": False, "action": "no_reminder", "reply": reply}

        times = parse_times_hint(rem.get("daily_time_hint") or "")
        when = "、".join(times) if times else "时刻待定"
        prompt = ("要新建生活提醒：%s，从 %s 起 %d 天，每天 %s。确认建吗？"
                  "回「确认」我就建；不想建就回「取消」。" % (
                      rem["title"], _mmdd(rem["start_date"]), rem["days"], when))
        payload = {
            "text": text, "parsed": parsed, "entry_kind": entry_kind,
            "source_id": source_id,
            "entry_date": entry_date_of(parsed),
        }
        try:
            self.store.ask_question("confirm_create", prompt, payload=payload,
                                    chat_id=chat_id, ttl_hours=2)
        except ValueError:
            reply = "现在还有一条待回答的事，先把那条答完，再说新建提醒。"
            if say:
                self._say(reply)
            return {"ok": False, "action": "busy", "reply": reply}
        self._audit_route("propose_create", rem["title"])
        if say:
            self._say(prompt)
        return {"ok": True, "action": "confirm_create", "reply": prompt,
                "chain_id": None}

    def propose_delete(self, target: str = "", chat_id: str = DEFAULT_CHAT_ID,
                       say: bool = False) -> dict:
        """把删除提醒先挂成待确认，不直接删。"""
        active = self._running_rows()
        if not active:
            reply = "现在没有在跑的提醒，没什么可删的。"
            if say:
                self._say(reply)
            return {"ok": False, "action": "no_reminder", "reply": reply}
        hit = self._match_chain(target)
        if hit is None and len(active) == 1:
            hit = active[0]
        if hit is None:
            names = "、".join(r["title"] for r in active)
            reply = "要删哪一条？在跑的是：%s。回我名字再说一次。" % names
            if say:
                self._say(reply)
            return {"ok": False, "action": "need_target", "reply": reply}
        prompt = ("要删除生活提醒「%s」（%s 起共 %d 天，每天 %s）。确认删吗？"
                  "回「确认」我就删；不删就回「取消」。" % (
                      hit["title"], hit["start_date"], hit["days"],
                      (hit["daily_time"] or "时刻待定").replace(",", "、")))
        try:
            self.store.ask_question("confirm_delete", prompt,
                                    payload={"chain_id": int(hit["id"]),
                                             "title": hit["title"]},
                                    chat_id=chat_id, ttl_hours=2)
        except ValueError:
            reply = "现在还有一条待回答的事，先把那条答完，再说删提醒。"
            if say:
                self._say(reply)
            return {"ok": False, "action": "busy", "reply": reply}
        self._audit_route("propose_delete", hit["title"], int(hit["id"]))
        if say:
            self._say(prompt)
        return {"ok": True, "action": "confirm_delete", "reply": prompt,
                "chain_id": int(hit["id"])}

    def confirm_pending(self, text: str, chat_id: str = DEFAULT_CHAT_ID,
                        say: bool = False) -> Optional[dict]:
        """接住“确认/取消”，确认后才真正新建或删除。"""
        pending = self.store.active_question(chat_id)
        if not pending or pending["kind"] not in CONFIRM_KINDS:
            return None
        t = (text or "").strip()
        if _CONFIRM_NO.search(t):
            self.store.answer_question(pending["id"], t)
            reply = "已取消，没有新建，也没有删除。"
            if pending["kind"] == "confirm_delete":
                reply = "已取消，这条提醒还在，没有删。"
            if say:
                self._say(reply)
            return {"ok": True, "action": "confirm_cancelled", "reply": reply}

        if not _CONFIRM_YES.search(t):
            reply = "还等你确认：%s" % (pending["question_text"] or "")
            if say:
                self._say(reply)
            return {"ok": False, "action": "still_pending", "reply": reply}

        self.store.answer_question(pending["id"], t)
        try:
            payload = json.loads(pending["payload"] or "{}")
        except Exception:  # noqa: BLE001
            payload = {}

        if pending["kind"] == "confirm_create":
            return self._create_from_payload(payload, say=say, chat_id=chat_id)

        chain_id = int(payload.get("chain_id") or 0)
        title = payload.get("title") or ""
        if not chain_id:
            reply = "待确认的提醒没有编号，没删成。"
            if say:
                self._say(reply)
            return {"ok": False, "action": "delete_failed", "reply": reply}
        self.store.cancel_chain(chain_id, reason="用户确认删除")
        self._audit_route("confirm_delete", title, chain_id)
        reply = "已删除生活提醒「%s」，以后不会再发。" % title
        if say:
            self._say(reply)
        return {"ok": True, "action": "confirm_delete", "chain_id": chain_id,
                "reply": reply}

    def _create_from_payload(self, payload: dict, say: bool = False,
                             chat_id: str = DEFAULT_CHAT_ID) -> dict:
        parsed = payload.get("parsed") or {}
        rem = parsed.get("reminder") or {}
        text = payload.get("text") or parsed.get("full_text") or rem.get("title") or ""
        entry_kind = payload.get("entry_kind") or "text"
        source_id = payload.get("source_id")
        if not rem:
            reply = "待确认的提醒内容丢了，没建。请再说一遍。"
            if say:
                self._say(reply)
            return {"ok": False, "action": "create_failed", "reply": reply}
        entry_date = payload.get("entry_date") or today_str()
        eid = self.store.add_entry(
            entry_date, entry_kind, raw_text=text,
            summary=parsed.get("summary") or rem.get("title") or text[:20],
            full_text=parsed.get("full_text") or text,
            topics="、".join(parsed.get("topics") or []),
            entities=str(parsed.get("entities") or {}),
            mood=parsed.get("mood") or "", confidence=parsed.get("confidence") or 0.9,
            needs_review=parsed.get("needs_review") or 0, source_id=source_id,
        )
        try:
            from .recall import index_entry
            index_entry(eid)
        except Exception as e:  # noqa: BLE001
            logger.debug("提醒条目入语义记忆失败（不影响提醒）: %s", e)
        times = parse_times_hint(rem.get("daily_time_hint") or "")
        chain_id = self.store.create_chain(
            eid, rem["title"], rem["start_date"], days=rem["days"],
            daily_time=",".join(times) or None, object_name=rem.get("object") or "",
            action=rem.get("action") or "", source_text=text,
        )
        self.store.attach_chain(eid, chain_id)
        if times:
            reply = "已建好：%s，从 %s 起 %d 天，每天 %s 提醒你。" % (
                rem["title"], _mmdd(rem["start_date"]), rem["days"],
                "、".join(times))
            if say:
                self._say(reply)
            return {"ok": True, "action": "chain_created", "chain_id": chain_id,
                    "entry_id": eid, "reply": reply}

        question = ("已记下：%s，从 %s 起 %d 天。每天几点提醒你？回一句，"
                    "比如「晚上七点」。" % (
                        rem["title"], _mmdd(rem["start_date"]), rem["days"]))
        self.store.ask_question("ask_time", question, chain_id=chain_id,
                                chat_id=chat_id, ttl_hours=ASK_TTL_HOURS)
        if say:
            self._say(question)
        return {"ok": True, "action": "ask_time", "chain_id": chain_id,
                "entry_id": eid, "reply": question}

    # ── 收：一句话 ──────────────────────────────────────────────

    def handle_text(self, chat_id: str, text: str, source_id: Optional[int] = None,
                    at: Optional[datetime] = None, kind: Optional[str] = None) -> dict:
        at = at or datetime.now()
        text = (text or "").strip()
        if not text:
            return {"action": "ignore"}
        # 入口显式告诉这里是文字还是语音；filehelper 的文字也带 source_id，
        # 不能再靠 source_id 猜类型，否则文字日记会被记成“语音”。
        entry_kind = kind or ("voice" if source_id else "text")

        # 先看是不是在回答追问
        pending = self.store.active_question(chat_id)
        if pending and pending["kind"] in CONFIRM_KINDS:
            result = self.confirm_pending(text, chat_id=chat_id, say=True)
            if result:
                return result
        if pending and self._looks_like_time_answer(text):
            times = parse_times_hint(text)
            hhmm = ",".join(times)
            self.store.answer_question(pending["id"], text, hhmm)
            chain = self.store.chain(pending["chain_id"]) if pending["chain_id"] else None
            if chain:
                self.store.set_chain_time(chain["id"], times)
                self._say(self._activate_text(chain, times))
                return {"action": "answer", "times": times, "chain_id": chain["id"]}
            return {"action": "answer", "times": times, "chain_id": None}

        # 明确的"停"指令不走模型：模型容易把别的事也读成 cancel
        if self.is_stop_command(text):
            eid = self.store.add_entry(
                today_str(), entry_kind, raw_text=text,
                summary=text[:20], full_text=text, topics="", entities="{}",
                mood="", confidence=0.9, needs_review=0, source_id=source_id)
            return self._do_cancel(text, eid)

        parsed = self.parser.parse(text, today=today_str(),
                                   now_hhmm=at.strftime("%H:%M"))
        # 新建提醒不直接落地：先回一条确认问句，用户回“确认”后再建。
        if parsed["kind"] == "reminder" and parsed.get("reminder"):
            return self.propose_create(
                text, chat_id=chat_id, parsed=parsed, entry_kind=entry_kind,
                source_id=source_id, at=at, say=True)
        # 同一会话里内容完全相同的日记，30 天内不重复落库；提醒类不拦，
        # 避免用户重复说“提醒我”时把新提醒也挡掉。
        if parsed["kind"] != "reminder":
            dupe = self.store.find_duplicate_entry(chat_id, text)
            if dupe:
                self._say("这条之前已经记过了（%s），没有重复记。"
                          % ((dupe["summary"] or "同一句")[:40]))
                return {"action": "duplicate", "entry_id": dupe["id"]}
        entry_date = entry_date_of(parsed)
        eid = self.store.add_entry(
            entry_date, entry_kind, raw_text=text,
            summary=parsed["summary"], full_text=parsed["full_text"],
            topics="、".join(parsed["topics"]), entities=str(parsed["entities"]),
            mood=parsed["mood"], confidence=parsed["confidence"],
            needs_review=parsed["needs_review"], source_id=source_id,
        )
        # 每一条落库的日记都并进"我记过什么"语义记忆，Bot 以后问旧事才查得到。
        # 测试里不建向量库，避免 pytest 把 data/chroma-journal 写脏、拖慢套件。
        try:
            if not os.environ.get("PYTEST_CURRENT_TEST"):
                from .recall import index_entry
                index_entry(eid)
        except Exception as e:  # noqa: BLE001
            logger.debug("日记入语义记忆失败（不影响记账）: %s", e)
        # 每句话怎么被判的，都留一条记录，事后能查能改
        self.store.audit("route", "parsed", "entry", eid, {
            "text": text[:40], "kind": parsed["kind"],
            "confidence": parsed["confidence"],
            "why": parsed.get("reason") or "",
        }, "ok")

        if parsed["kind"] == "cancel":
            return self._do_cancel((parsed.get("reminder") or {}).get("cancel_target")
                                   or parsed["summary"], eid, parsed=parsed)

        if parsed["kind"] != "reminder" or not parsed["reminder"]:
            why = parsed.get("reason") or ""
            if parsed["needs_review"]:
                heard = "听到" if entry_kind == "voice" else "读到"
                self._say("我%s的是「%s」，先按这句记进日记了（%s）。"
                          "要改就到网页上改，不用重新说一遍。"
                          % (heard, text[:40], why or "模型自评不确定"))
                return {"action": "diary_review", "entry_id": eid}
            self._say("记好了：%s" % (parsed["summary"] or text[:12]))
            return {"action": "diary", "entry_id": eid}

        rem = parsed["reminder"]
        times_hint = parse_times_hint(rem["daily_time_hint"] or "")
        chain_id = self.store.create_chain(
            eid, rem["title"], rem["start_date"], days=rem["days"],
            daily_time=",".join(times_hint) or None, object_name=rem["object"],
            action=rem["action"], source_text=text,
        )
        self.store.attach_chain(eid, chain_id)

        if times_hint:
            when = "、".join(times_hint)
            self._say("%s记好了：%s，从 %s 起 %d 天，每天 %s 提醒你。" % (
                "", rem["title"], _mmdd(rem["start_date"]), rem["days"], when))
            return {"action": "chain_created", "chain_id": chain_id, "entry_id": eid}

        question = ("记好了：%s，从 %s 起 %d 天。每天几点提醒你？回我一句就行，"
                    "比如「晚上七点」。" % (rem["title"], _mmdd(rem["start_date"]), rem["days"]))
        self.store.ask_question("ask_time", question, chain_id=chain_id,
                                chat_id=self.chat_id, ttl_hours=ASK_TTL_HOURS)
        self._say(question)
        return {"action": "ask_time", "chain_id": chain_id, "entry_id": eid}

    def _looks_like_time_answer(self, text: str) -> bool:
        return bool(parse_times_hint(text)) and len(text) <= 30

    def looks_like_answer(self, text: str) -> bool:
        """这句话像不像在回我的追问：报时刻，或者让停。其余留给对话助手。"""
        if self._looks_like_time_answer(text):
            return True
        return self.is_stop_command(text)

    @staticmethod
    def is_stop_command(text: str) -> bool:
        """是不是明确的"停提醒"指令（不含建提醒的语境）。"""
        t = text or ""
        return bool(STOP_HINTS.search(t)) and not CREATE_HINTS.search(t)

    def _activate_text(self, chain, times: list[str]) -> str:
        """确认时刻时回的话：说清每天几个点、共几天、哪天收尾。"""
        n = len(times)
        when = "、".join(times)
        start = date.fromisoformat(chain["start_date"])
        final_date = (start + timedelta(days=chain["days"])).isoformat()
        return "好，「%s」每天 %s 提醒你%s，共 %d 天。%s 会给你一条收尾提醒。" % (
            chain["title"], when, "（一天 %d 次）" % n if n > 1 else "",
            chain["days"], _mmdd(final_date))

    def _running_titles(self) -> str:
        rows = self._running_rows()
        if not rows:
            return "（当前没有在跑的提醒）"
        return "、".join(r["title"] for r in rows[:6])

    def _running_rows(self) -> list:
        """还在跑的事：已激活的，和等你说时刻的。"""
        return [r for r in self.store.chains()
                if r["status"] in ("active", "pending_time")]

    def _audit_route(self, action: str, target: str, chain_id=None, note: str = "") -> None:
        try:
            self.store.audit("route", action, "chain", chain_id,
                             {"target": target[:40], "note": note}, "ok")
        except Exception:  # noqa: BLE001
            pass

    def _do_cancel(self, target: str, eid: int, parsed: Optional[dict] = None) -> dict:
        """统一的"停提醒"入口：能匹配就停，只有一件就直接停，匹配不上才追问。"""
        active = self._running_rows()
        target = (target or "").strip()

        if ALL_HINTS.search(target) and active:
            for row in active:
                self.store.cancel_chain(row["id"], reason="用户说全停")
            names = "、".join(r["title"] for r in active)
            self._audit_route("cancel_all", target, note=names)
            self._say("已全部取消：%s。" % names)
            return {"action": "cancel_all", "entry_id": eid}

        hit = self._match_chain(target)
        if hit is not None:
            self.store.cancel_chain(hit["id"], reason="用户说停")
            self._audit_route("cancel", target, hit["id"], note="按名字匹配到「%s」" % hit["title"])
            self._say("已取消：%s。" % hit["title"])
            return {"action": "cancel", "chain_id": hit["id"], "entry_id": eid}

        # 匹配不上但只有一件在跑：不用再让用户报名字
        if len(active) == 1:
            self.store.cancel_chain(active[0]["id"], reason="用户说停（唯一在跑）")
            self._audit_route("cancel", target, active[0]["id"],
                              note="没匹配上，但只有「%s」在跑" % active[0]["title"])
            self._say("已取消：%s。" % active[0]["title"])
            return {"action": "cancel", "chain_id": active[0]["id"], "entry_id": eid}

        if not active:
            self._audit_route("cancel_none", target, note="没有在跑的提醒")
            self._say("现在没有在跑的提醒，不用停了。")
            return {"action": "cancel_none", "entry_id": eid}

        self._audit_route("cancel_ambiguous", target, note=self._running_titles())
        self._say("你说要停，现在在跑的有：%s。回我名字，或者回「全部」。" % self._running_titles())
        return {"action": "cancel_ambiguous", "entry_id": eid}

    @staticmethod
    def _norm_title(text: str) -> str:
        """洗掉口语壳子，留下事本体：喂鱼肝油提醒 → 鱼肝油。"""
        t = text or ""
        for w in ("提醒", "取消", "停止", "停掉", "停了", "别", "不用", "不要", "再",
                  "把", "那个", "这个", "这件", "这项", "一下", "吧", "了", "的"):
            t = t.replace(w, "")
        return t.strip()

    def _match_chain(self, target: str):
        """按名字找在跑的提醒：先原串互含，再比关键词重合。"""
        if not target:
            return None
        rows = [r for r in self.store.chains() if r["status"] in ("active", "pending_time")]
        # 1) 原串互含
        for row in rows:
            if target in row["title"] or row["title"] in target:
                return row
        # 2) 洗掉口语壳子后再互含
        nt = self._norm_title(target)
        for row in rows:
            ntitle = self._norm_title(row["title"])
            if nt and (nt in ntitle or ntitle in nt):
                return row
        # 3) 关键词重合：任意两字片段相同就算命中（"鱼肝油" vs "喂鱼肝油"）
        if nt:
            for row in rows:
                ntitle = self._norm_title(row["title"])
                for i in range(len(nt) - 1):
                    frag = nt[i:i + 2]
                    if len(frag) == 2 and frag in ntitle:
                        return row
        return None

    def _cancel_by_name(self, parsed: dict) -> Optional[object]:
        target = (parsed.get("reminder") or {}).get("cancel_target") or parsed["summary"]
        hit = self._match_chain(target)
        if hit is not None:
            self.store.cancel_chain(hit["id"], reason="用户说停")
        return hit

    # ── 定时扫描 ────────────────────────────────────────────────

    def tick(self, at: Optional[datetime] = None) -> dict:
        at = at or datetime.now()
        expired = self.store.expire_questions(at.replace(microsecond=0).isoformat())
        sent, failed, finished = 0, 0, 0

        for task in self.store.due_tasks(at.replace(microsecond=0).isoformat()):
            # 隔天的每日提醒不补发：早上八点的事拖到深夜再说没有意义，也招人烦
            if task["kind"] == "daily":
                if task["fire_date"] != at.date().isoformat():
                    self.store.mark_task_skipped(task["id"], reason="错过当日时段，不补发")
                    continue
                # 当天但已经过去两小时以上的，也不再补发
                try:
                    late = (at - datetime.fromisoformat(task["fire_at"])).total_seconds() / 60
                except ValueError:
                    late = 0
                if late > LATE_GRACE_MIN:
                    self.store.mark_task_skipped(
                        task["id"], reason="错过时段 %d 分钟，不补发" % int(late))
                    continue
            text = self._task_text(task)
            ok = self._say(text)
            if ok:
                self.store.mark_task_sent(task["id"], via=DELIVERY_VIA)
                sent += 1
                if task["kind"] == "final":
                    self.store.finish_chain(task["chain_id"])
                    finished += 1
            else:
                retry = self.store.mark_task_failed(task["id"], "发送失败", via=DELIVERY_VIA)
                failed += 1
                if retry >= 3:
                    logger.warning("提醒 %s 连续失败三次，置 failed", task["id"])
        return {"expired": len(expired), "sent": sent, "failed": failed, "finished": finished}

    def _task_text(self, task) -> str:
        days = task["days"]
        date_cn = _mmdd(task["fire_date"])
        if task["kind"] == "final":
            start = task["start_date"]
            end = (datetime.fromisoformat(start) + timedelta(days=days - 1)).date().isoformat()
            return "收尾提醒：%s 已经满 %d 天了（%s 到 %s）。这件事到此结束。" % (
                task["title"], days, _mmdd(start), _mmdd(end))
        per_day = max(1, len([x for x in str(task["daily_time"] or "").split(",") if x])) \
            if "daily_time" in task.keys() else 1
        day_index = (task["seq"] - 1) // per_day + 1
        return "今日提醒（第 %d 天／共 %d 天，%s）：%s —— %s。" % (
            day_index, days, (task["fire_at"] or "")[11:16], task["title"], date_cn)

    # ── 追问超时兜底 ────────────────────────────────────────────

    def sweep_pending_time(self, at: Optional[datetime] = None) -> int:
        """待定时刻的链超过 6 小时没人答，按整点先跑，并在网页标注。"""
        at = at or datetime.now()
        n = 0
        for row in self.store.chains("pending_time"):
            created = row["created_at"]
            try:
                age = (at - datetime.fromisoformat(created)).total_seconds()
            except ValueError:
                continue
            if age < ASK_TTL_HOURS * 3600:
                continue
            hh = "%02d:00" % at.hour
            self.store.set_chain_time(row["id"], hh)
            self.store.audit("system", "auto_timeout_activate", "chain", row["id"],
                             {"time": hh, "reason": "追问超时"}, "skip")
            self._say("等不到你回时间，先按每天 %s 提醒「%s」，共 %d 天。想改时间随时说。" % (
                hh, row["title"], row["days"]))
            n += 1
        return n
