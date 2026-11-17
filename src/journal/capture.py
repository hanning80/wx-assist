"""输入侧：从文件传输助手把新东西捞出来，分派给服务层（阶段4 模块六）。

四种东西，四条处理法：
  语音（类型 34）→ 解 SILK → 转 WAV → 火山识别 → 走解析
  文字（类型 1）  → 直接走解析
  视频号 / 图文分享（类型 49）→ zstd 解压 → 读 XML 取标题、描述、来源、话题
  其他（图片、文件等）→ 记一条"收到但暂不处理"，回执说明

去重靠 entry_source 的唯一索引：同一条消息处理两次也只入一次。
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .remind import JournalService, is_create_reminder_request
from .store import JournalStore, now_iso
from . import knowledge as kb
from . import answer_rule

logger = logging.getLogger(__name__)

TYPE_TEXT = 1
TYPE_VOICE = 34
TYPE_APP = 49
# 你在 ClawBot 会话里回话，也从微信库里读（不用等接收器）
BOT_CHAT = "mmo9cq804hSoB6tYB45Plb6MAxoa-o@weclaw"

# 只让"记一下"这类指令不单独记成日记
INSTRUCTION = re.compile(r"^\s*(把?这个|这条|上面|刚才|帮我)?\s*(记录|记下|记一下|存一下|存起来|保存|收藏)")
# 附件专用：只有明确说"存起来/存微云/收藏这个"才把刚发的文件、图片、视频挂上
STORE_ATTACHMENT = re.compile(
    r"(存起来|存到微云|保存到微云|存微云|上传微云|收藏这个|把这个存|"
    r"附件存档|把文件存|把图片存|把视频存|存一下这个附件)")
VOICE_CACHE = Path(__file__).resolve().parents[2] / "data" / "journal" / "voice"
# ClawBot 里这些词说明是在给我派活，不该当日记流水
COMMAND_HINTS = re.compile(
    r"(总结|汇总|统计|分析|删掉|删除|清理|查一下|查查|查看|列出|列表|导出|生成|提醒列表|确认|确定|取消)")


def zstd_decompress(blob: bytes) -> bytes:
    """微信 4.x 的分享消息是 zstd 压缩的，先解压。"""
    try:
        import zstandard as zstd

        return zstd.ZstdDecompressor().decompress(blob)
    except Exception:  # noqa: BLE001
        try:
            import zstandard as zstd

            with zstd.ZstdDecompressor().stream_reader(blob) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            logger.warning("解压失败: %s", e)
            return b""


def _plain_content(content) -> str:
    """微信库里的文字/图片/视频内容可能是 zstd 压缩的十六进制，统一解成原文。"""
    if isinstance(content, bytes):
        text = content.decode("utf-8", "ignore")
    else:
        text = str(content or "")
    raw = text.strip()
    if re.fullmatch(r"[0-9a-fA-F]+", raw) and len(raw) > 40:
        try:
            return zstd_decompress(bytes.fromhex(raw)).decode("utf-8", "ignore")
        except Exception:
            return text
    return text


def _tag(xml: str, name: str) -> str:
    m = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (name, name), xml, re.S)
    return html.unescape((m.group(1) or "").strip()) if m else ""


def parse_share(content_hex_or_text: str) -> dict:
    """把一条 49 类型消息读成结构化记录。

    返回 {ok, kind, title, desc, source, topics, url, object_id, app_type}
    视频号分享走 finderFeed，没有可直接打开的网址，只存来源与描述。
    """
    text = content_hex_or_text or ""
    if re.fullmatch(r"[0-9a-fA-F]+", text.strip()) and len(text.strip()) > 40:
        raw = zstd_decompress(bytes.fromhex(text.strip()))
        text = raw.decode("utf-8", "ignore")

    if "<msg" not in text:
        return {"ok": False, "reason": "不是可读的分享内容"}

    title = _tag(text, "title")
    # 视频号分享的 title 经常是占位提示，不是真标题；不能把它记成标题。
    if title and "当前微信版本不支持" in title:
        title = ""
    desc = _tag(text, "desc")
    nickname = _tag(text, "nickname")
    url = _tag(text, "url")
    # 视频号占位消息里的 url 是"请升级微信"的支持页，不是内容链接。
    if "support.weixin.qq.com" in (url or "") and "update" in url:
        url = ""
    object_id = _tag(text, "objectId")
    app_type = _tag(text, "type")

    kind = "分享"
    if app_type == "6":
        kind = "文件"
    elif "<finderFeed>" in text:
        kind = "视频号"
    elif "mp.weixin.qq.com" in url or ("公众号" in title):
        kind = "公众号文章"

    topics = []
    for t in re.findall(r"#([^\s#\[\]]{1,20})", desc or ""):
        if t not in topics:
            topics.append(t)

    # 视频号没有可读标题时，用账号名当标题，至少知道这条是谁发的。
    if kind == "视频号" and not title:
        title = nickname or "视频号分享"

    return {
        "ok": True,
        "kind": kind,
        # 标题必须优先取文章/视频标题；昵称只是来源，不能顶掉标题
        "title": (title or nickname)[:80],
        "desc": desc[:300],
        "source": nickname,
        "topics": topics[:8],
        "url": url,
        "object_id": object_id,
        "app_type": app_type,
    }


class JournalIngest:
    """一次扫描：把文件传输助手里的新消息处理掉，并回执到 ClawBot。"""

    def __init__(self, store: Optional[JournalStore] = None,
                 service: Optional[JournalService] = None,
                 client=None, asr=None, chat_fn=None):
        self.store = store or JournalStore()
        self.service = service or JournalService(store=self.store)
        if client is None:
            from src.wechat.wcdb_client import get_shared_client

            client = get_shared_client()
        self.client = client
        if asr is None:
            from .asr_provider import get_shared_asr

            asr = get_shared_asr()
        self.asr = asr
        # 注入式对话函数，测试时可以直接给一个假回复；
        # 不注入时走 AgentEngine，拿不到再退回普通摘要模型。
        self._chat_fn = chat_fn
        self._chat_summarizer = None
        self._chat_history: dict[str, list[dict]] = {}

    # ── 扫描 ────────────────────────────────────────────────────

    def scan(self, limit: int = 40, since: Optional[int] = None,
             since_ts: Optional[int] = None) -> dict:
        msgs = self.client.get_messages("filehelper", limit=limit)
        known = self.store.known_sources()
        done = {"voice": 0, "text": 0, "share": 0, "skip": 0, "failed": 0,
                "last_local_id": int(since or 0)}
        max_local_id = int(since or 0)

        for m in msgs:
            local_id = m.get("local_id")
            if local_id is None:
                continue
            try:
                local_id = int(local_id)
            except (TypeError, ValueError):
                continue
            if local_id > max_local_id:
                max_local_id = local_id
            if since is not None and local_id <= since:
                continue
            if since_ts is not None:
                try:
                    if int(m.get("create_time") or 0) < since_ts:
                        continue
                except (TypeError, ValueError):
                    continue
            if ("filehelper", local_id) in known:
                continue

            mtype = (m.get("local_type") or 0) & 0xFFFFFFFF
            content = m.get("content") or ""

            if mtype == TYPE_VOICE:
                r = self._handle_voice(m, local_id)
            elif mtype == TYPE_TEXT:
                r = self._handle_text(content, local_id)
            elif mtype == TYPE_APP:
                r = self._handle_share(content, local_id, m)
            else:
                r = self._handle_other(m, local_id)
            done[r] = done.get(r, 0) + 1

        done["last_local_id"] = max_local_id
        return done

    # ── 各类处理 ────────────────────────────────────────────────

    def _asr_from_msg(self, chat: str, msg: dict, local_id: int) -> dict:
        """解 SILK → 转 WAV → 火山识别。返回 {ok, text, error, path}。"""
        try:
            import re
            import wave

            from src.wechat.voice_decode import silk_to_wav

            data = self.client.get_voice_data(
                chat, msg.get("create_time"), local_id, msg.get("server_id"))
            if not data or not data.get("success"):
                return {"ok": False, "error": "语音数据没取出来"}

            wav = silk_to_wav(data["hex"])
            VOICE_CACHE.mkdir(parents=True, exist_ok=True)
            wav_path = VOICE_CACHE / ("%s_%s.wav" % (local_id, abs(int(msg.get("create_time") or 0))))
            wav_path.write_bytes(wav)

            # 微信消息里的 voicelength 是界面显示的时长；如果解出来的音频
            # 比它长一倍以上，通常说明取到了上一条/另一条语音。此时宁可
            # 让用户重发，也不要把旧语音的答案发出去。
            actual_sec = 0.0
            try:
                with wave.open(str(wav_path), "rb") as wf:
                    actual_sec = wf.getnframes() / float(wf.getframerate() or 1)
            except Exception:
                pass
            raw = msg.get("content") or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
            try:
                from src.wechat.content_codec import decompress_content

                raw = decompress_content(raw) or raw
            except Exception:
                pass
            meta_ms = 0
            match = re.search(r'voicelength\s*=\s*"(\d+)"', str(raw))
            if match:
                meta_ms = int(match.group(1))
            if meta_ms and actual_sec > 1 and actual_sec > meta_ms / 1000.0 * 2.2:
                # 微信的 voicelength 只是界面显示值，实测会和真实音频差很多
                # （例如显示 3.14 秒、实际 16.8 秒）。它不能当拦截门槛，
                # 否则正常语音也会被误杀；这里只记日志，识别仍按音频内容走。
                logger.warning(
                    "语音时长与微信显示不一致（仅记录，不拦截）：local_id=%s 显示=%.2fs 实际=%.2fs",
                    local_id, meta_ms / 1000.0, actual_sec,
                )

            res = self.asr.transcribe(wav_path)
            res["path"] = str(wav_path)
            logger.info(
                "语音转写：local_id=%s 微信显示=%.2fs 实际=%.2fs 文字=%d字",
                local_id, (meta_ms / 1000.0) if meta_ms else 0.0,
                actual_sec, len((res.get("text") or "")),
            )
            return res
        except Exception as e:  # noqa: BLE001
            logger.warning("语音处理失败: %s", e)
            return {"ok": False, "error": str(e)[:60]}

    def _run_command(self, text: str) -> str:
        """认得的口头指令就当场办（看提醒、总结和某人的聊天），返回回执。"""
        try:
            from .commands import try_handle

            reply = try_handle(text, self.service) or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("指令执行失败: %s", e)
            return ""
        if reply:
            self.store.audit("route", "command", "text", None,
                             {"text": text[:40], "reply": reply[:40]}, "ok")
        return reply

    def _chat_reply(self, text: str, chat: str) -> str:
        """普通聊天：优先交给对话助手，助手没接管时退回普通模型。

        语音条在微信里没有通话事件，只能"一条一条"来；这里拿到转写文字后
        走和网页输入框同一条助手链路，回复再由 iLink 发回微信。
        """
        # 长期回答规则：先认“以后回答引用原话”这类要求，落成规则再回执，
        # 不能当成普通聊天或日记。关/开规则也在这里处理。
        rule_reply = answer_rule.maybe_handle(text, store=self.store)
        if rule_reply:
            return rule_reply

        # Bot 语音“存起来”也要接刚暂存的附件：它走 _chat_reply，不经过
        # _handle_text；少了这一步就会反问“存哪一条、存到哪”。
        stored = self._store_pending_attachments(text, None)
        if stored is not None:
            return stored

        if self._chat_fn is not None:
            try:
                return (self._chat_fn(text, chat) or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.warning("注入对话函数失败: %s", e)
                return ""

        # 0) “总结/分析我和某人最近 N 天”：不依赖 AgentEngine，直接走聊天总结。
        #    对方语音会先解 SILK、再走火山转写；转写失败的会标“没转成文字”。
        m = re.search(
            r"(?:总结|分析)(?:一下)?(?:我和|我跟)(?P<name>[^\s，。？！]{1,12}?)"
            r"(?:最近(?P<days>\d+)天|所有聊天|聊天)", text)
        if m:
            name = m.group("name").strip()
            if m.group("days"):
                days = int(m.group("days"))
            elif "所有聊天" in text:
                days = 0
            else:
                days = 7
            try:
                from src.assistant.chat_summary import summarize_chat
                r = summarize_chat(name, days=days, transcribe=True,
                                  max_voices=30, save=True)
                if r.get("ok"):
                    return ("和「%s」最近 %s 天的 %d 条消息（语音 %d 条已转文字，%d 条未转）"
                            "\n\n%s" % (r["chat"], days, r["rows"], r["voices"],
                                        r["skipped_voices"], r["summary"])).strip()
                return (r.get("error") or "总结失败").strip()
            except Exception as e:  # noqa: BLE001
                logger.warning("聊天总结失败: %s", e)
                return "总结失败：%s" % e

        # 1) 完整机器人已注册 AgentEngine：直接走带工具、带记忆的对话
        try:
            from src.web.server import get_agent_engine

            engine = get_agent_engine()
            if engine is not None:
                reply = engine.run(
                    text,
                    source_platform="local",
                    source_target=chat,
                    conversation_key="clawbot:%s" % chat,
                )
                return (reply or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("对话助手执行失败，改用普通模型: %s", e)

        # 2) 面板单独跑（没有 AgentEngine）：用同一套 AI 后端做普通问答
        try:
            if self._chat_summarizer is None:
                from src.config import load_config
                from src.summarize import create_summarizer

                self._chat_summarizer = create_summarizer(load_config())
            history = self._chat_history.setdefault(chat, [])
            # 没有 AgentEngine 时，工具调用不可用；这里在普通模型前面补一层
            # 语义记忆召回，让"我以前记过/收藏过什么"仍能答上来。
            context_messages = history[-6:]
            try:
                from .recall import JOURNAL_QUERY_RE, recall
            except Exception:
                JOURNAL_QUERY_RE = None
            if JOURNAL_QUERY_RE is not None and JOURNAL_QUERY_RE.search(text):
                try:
                    hits = recall(text, top_k=5)
                    if hits:
                        lines = []
                        for h in hits:
                            lines.append("%s %s｜%s｜%s" % (
                                h.get("date", ""), h.get("kind", ""),
                                h.get("title", ""),
                                (h.get("text") or "")[:180]))
                        context_messages = context_messages + [{
                            "sender_name": "你以前记过的内容",
                            "content": "\n".join(lines),
                        }]
                except Exception as e:  # noqa: BLE001
                    logger.debug("普通对话召回记忆失败（不影响回复）: %s", e)
            reply = self._chat_summarizer.chat(
                text,
                context_messages=context_messages,
                requester_name="用户",
                group_name="微信 ClawBot",
            )
            reply = (reply or "").strip()
            if reply:
                history.append({"sender_name": "用户", "content": text})
                history.append({"sender_name": "助手", "content": reply})
                if len(history) > 6:
                    del history[:-6]
            return reply
        except Exception as e:  # noqa: BLE001
            logger.warning("普通模型对话失败: %s", e)
            return ""


    def _handle_voice(self, msg: dict, local_id: int) -> str:
        asr = self._asr_from_msg("filehelper", msg, local_id)
        src_id = self.store.add_source(
            "filehelper", local_id, msg_type="voice",
            msg_time=str(msg.get("create_time")), voice_path=asr.get("path") or "",
            asr_engine="volc" if asr.get("ok") else "",
            asr_latency_ms=asr.get("latency_ms"),
        )
        if not asr.get("ok"):
            self.service._say("语音转文字没成功（%s），文字没拿到，先记了个来源。"
                              % (asr.get("error") or "未知原因")[:40])
            return "failed"
        text = (asr.get("text") or "").strip()
        if not text:
            self.service._say("这条语音是空的，没听到内容。")
            return "failed"
        # 语音里说的话也可能是指令："总结一下我跟大姚最近都说了什么"
        reply = self._run_command(text)
        if reply:
            self.service._say(reply)
            self._discard_voice(asr.get("path"), -1)  # 文字已用完，音频照样删
            return "command"
        res = self.service.handle_text("filehelper", text, source_id=src_id, kind="voice")
        self._discard_voice(asr.get("path"), (res or {}).get("entry_id"))
        return "voice"

    def _discard_voice(self, wav_path, entry_id) -> None:
        """文字已经落地，原始音频就不留了——本地和云上都不存，省空间也不担风险。

        只删"已经成功记进日记"的那条；转写失败或没落库的保留，等重试。
        """
        if not wav_path or not entry_id:
            return
        try:
            p = Path(str(wav_path))
            # 火山转写前会生成一个 16000Hz 单声道副本，两个都要清。
            for target in (p, p.with_name(p.stem + "_16k.wav")):
                try:
                    os.remove(str(target))
                except OSError:
                    pass
            logger.info("原始音频已删除：%s", os.path.basename(str(p)))
        except OSError:
            pass


    def scan_answers(self, chat: str = BOT_CHAT, limit: int = 10,
                     since_ts: Optional[int] = None) -> dict:
        """读 ClawBot 会话里你自己发的话：语音条走对话，文字只做补充。

        ClawBot 是你的对话窗口，不是日记入口。语音条要一来一回；
        文字在完整机器人接管 iLink 时留给 iLink 回调，避免两边都回。
        """
        done = {"answer": 0, "skip": 0, "failed": 0}
        try:
            pending = self.store.active_question(chat)
        except Exception as e:  # noqa: BLE001
            logger.warning("读待答问题失败: %s", e)
            pending = None

        try:
            msgs = self.client.get_messages(chat, limit=limit)
        except Exception as e:  # noqa: BLE001
            logger.warning("读助手号会话失败: %s", e)
            return done

        known = self.store.known_sources()
        for m in msgs:
            local_id = m.get("local_id")
            if local_id is None:
                continue
            sender = str(m.get("sender") or "")
            if sender == chat:  # 机器人自己发的，跳过
                continue
            if since_ts is not None and int(m.get("create_time") or 0) < since_ts:
                continue
            if (chat, local_id) in known:
                continue

            mtype = (m.get("local_type") or 0) & 0xFFFFFFFF
            is_voice = mtype == TYPE_VOICE
            wav_path = ""
            if mtype == TYPE_TEXT:
                text = _plain_content(m.get("content") or "").strip()
            elif is_voice:
                asr = self._asr_from_msg(chat, m, local_id)
                text = (asr.get("text") or "").strip()
                wav_path = asr.get("path") or ""
                if not asr.get("ok") or not text:
                    self.store.add_source(chat, local_id, msg_type="voice",
                                          msg_time=str(m.get("create_time")),
                                          asr_engine="", asr_raw=str(asr.get("error"))[:100])
                    # ClawBot 语音没有重试入口，转写失败也不留音频。
                    self._discard_voice(wav_path, -1)
                    self.service._say("这条语音没听清（%s），再说一遍就行。"
                                      % (asr.get("error") or "没识别出来")[:30])
                    done["failed"] += 1
                    continue
            else:
                self.store.add_source(chat, local_id, msg_type=str(mtype))
                done["skip"] += 1
                continue

            self.store.add_source(chat, local_id, msg_type="text" if mtype == TYPE_TEXT else "voice",
                                  msg_time=str(m.get("create_time")), asr_engine="volc")
            # 语音转成文字后原始 WAV 就没用了，本地和云端都不留。
            self._discard_voice(wav_path, -1)
            if not text:
                done["skip"] += 1
                continue

            # ClawBot 是你的对话窗口，不是日记入口。先说停/回时刻，
            # 再认"提醒我"这种建提醒的话；其余才是普通对话。
            # 文字在完整机器人接管 iLink 时已经由回调处理过，这里只补语音，
            # 避免同一条文字被日记和助手各回一遍。
            agent_bound = self._agent_bound()
            # 新建/删除提醒在等你确认：语音里说“确认/取消”要自己接住。
            if pending and pending["kind"] in ("confirm_create", "confirm_delete") \
                    and (is_voice or not agent_bound):
                self.service.handle_text(chat, text, source_id=None,
                                         kind="voice" if is_voice else "text")
                done["answer"] += 1
                continue
            if self.service.is_stop_command(text) and (is_voice or not agent_bound):
                self.service.handle_text(chat, text, source_id=None,
                                         kind="voice" if is_voice else "text")
                done["answer"] += 1
                continue
            if pending and self.service._looks_like_time_answer(text) and (is_voice or not agent_bound):
                self.service.handle_text(chat, text, source_id=None,
                                         kind="voice" if is_voice else "text")
                done["answer"] += 1
                continue
            if is_create_reminder_request(text) and (is_voice or not agent_bound):
                # 语音里说"提醒我"也要能建提醒，不能只认文字。
                self.service.handle_text(chat, text, source_id=None,
                                         kind="voice" if is_voice else "text")
                done["answer"] += 1
                continue

            # 指令类的话：助手不在的时候我自己接几件能办的。
            # 语音指令必须自己接：iLink 接收器根本没有语音事件，
            # 不能因为助手接管了文字就把它丢掉。
            if COMMAND_HINTS.search(text):
                if agent_bound and not is_voice:
                    done["skip"] += 1
                    continue
                reply = ""
                # 语音且助手在：直接让助手带着工具接，别用本地半吊子指令解析。
                # 助手不在：先试本地能办的总结/分析，办不了再退回普通对话。
                if not (agent_bound and is_voice):
                    reply = self._run_command(text)
                if not reply:
                    reply = self._chat_reply(text, chat)
                if reply:
                    self.service._say(reply)
                    done["answer"] += 1
                    continue
                self.service._say(
                    "这句我还办不了。日记和提醒发文件传输助手；"
                    "看提醒说「提醒列表」，总结聊天说「总结我和某某最近7天」。")
                done["skip"] += 1
                continue

            # 普通聊天：语音条要走一来一回；文字在助手没接管时也由这里兜底。
            if is_voice or not agent_bound:
                reply = self._chat_reply(text, chat)
                if reply:
                    self.service._say(reply)
                    done["answer"] += 1
                    continue
            done["skip"] += 1
        return done


    @staticmethod
    def _agent_bound() -> bool:
        """对话助手是不是真的接管了 iLink。

        只看回调注册不够：单独跑日记线程时也会注册一个日记回调，
        但接收器因为 Bot 没启动并不会跑，这时不能把 ClawBot 的话丢掉。
        """
        try:
            from src.web.server import get_agent_engine, has_ilink_callback

            return get_agent_engine() is not None and has_ilink_callback()
        except Exception:  # noqa: BLE001
            return False


    def _handle_text(self, text: str, local_id: int) -> str:
        # 长文字在微信库里可能是 zstd 压缩后的十六进制（开头 28b52ffd）；
        # 不先解压，模型就会把压缩串当成原文，出现“我听到的是 28b52ffd…”这种回执。
        text = _plain_content(text).strip()
        if not text:
            return "skip"
        src_id = self.store.add_source("filehelper", local_id, msg_type="text")
        # 先看是不是“把刚发的附件存起来”；只有真有暂存附件时才接管，
        # 否则继续走原来的指令/日记逻辑，避免把“把这个记录起来”误判。
        stored = self._store_pending_attachments(text, src_id)
        if stored is not None:
            if stored:
                self.service._say(stored)
            return "attachment"
        # “以后回答要引用原话”是长期回答规则，不是日记；必须先认出来，
        # 否则会被解析成 diary，既污染日记库又不会改变以后的回答。
        rule_reply = answer_rule.maybe_handle(text, store=self.store)
        if rule_reply:
            self.service._say(rule_reply)
            return "rule"
        if INSTRUCTION.match(text) and len(text) <= 20:
            # "把这个记录起来"这类：上面那条分享已经单独记了，这句不再记成日记
            self.store.audit("ingest", "instruction_skipped", "source", local_id,
                             {"text": text}, "skip")
            return "skip"
        # 文字里也可能是指令：文件传输助手是你主要的入口，这里同样要认
        reply = self._run_command(text)
        if reply:
            self.service._say(reply)
            return "command"
        self.service.handle_text("filehelper", text, source_id=src_id, kind="text")
        return "text"

    def _handle_share(self, content: str, local_id: int, msg: dict | None = None) -> str:
        info = parse_share(content)
        create_time = int((msg or {}).get("create_time") or 0)
        # appmsg type=6 是文件，不是文章/视频号：走附件暂存，别当分享记成一条日记
        if info.get("ok") and str(info.get("app_type") or "") == "6":
            return self._handle_file(info, content, local_id, create_time)
        src_id = self.store.add_source("filehelper", local_id, msg_type="share")
        if not info.get("ok"):
            self.service._say("这条分享我读不出来，先记了个来源，内容没存到。")
            return "failed"

        summary = info["title"] or info["desc"] or info["kind"]
        # 公众号文章尽量先抓正文；正文能抓到就存全文，不再只留标题。
        web = {"ok": False}
        body = ""
        if info["url"]:
            web = kb.fetch_web_text(info["url"])
            if web.get("ok"):
                body = (web.get("text") or "").strip()
                if web.get("author") and not info["source"]:
                    info["source"] = web["author"]

        parts = ["%s《%s》" % (info["kind"], info["title"]) if info["title"] else info["kind"]]
        if info["desc"] and info["desc"] != info["title"]:
            parts.append("文案：%s" % info["desc"])
        if info["topics"]:
            parts.append("话题：%s" % " ".join("#" + t for t in info["topics"]))
        if info["source"]:
            parts.append("来自：%s" % info["source"])
        if info["url"]:
            parts.append("链接：%s" % info["url"])
        if web.get("ok") and body:
            parts.append("正文：%s" % body)
        elif info["url"]:
            parts.append("正文未取到：%s" % (web.get("error") or "网页无法读取"))
        full = "；".join(parts)

        # 同一篇文章只记一条：先按原链接找，已经入库就补正文，不再重复记。
        existing = self.store.find_entry_by_url(info["url"]) if info["url"] else None
        if existing:
            entry_id = int(existing["id"])
            old_text = existing["full_text"] or ""
            # 旧条目可能只存了前 3000 字；只看开头 300 字会误判“已经有了”。
            # 开头或结尾对不上，或者新正文明显更长，都应当替换成全文。
            if body and (body[:300] not in old_text
                         or body[-300:] not in old_text
                         or len(body) > len(old_text) + 200):
                # 老条目只存了 3000 字摘录时，用这次的全文直接替换，避免正文重复。
                if existing["kind"] == "share":
                    self.store.replace_entry_full_text(entry_id, full)
                else:
                    self.store.update_entry_full_text(
                        entry_id, "正文：%s" % body)
                try:
                    from .recall import index_entry
                    index_entry(entry_id)
                except Exception as e:  # noqa: BLE001
                    logger.debug("补全文章正文后重建索引失败: %s", e)
                reply = "这篇公众号文章已经存过了，我把正文补全了（%d 字）。" % len(body)
            else:
                reply = "这篇公众号文章已经存过了，没有重复记。"
            if info["url"]:
                reply += "原文链接也还在日记里。"
            self.service._say(reply)
            self.store.audit("ingest", "share_updated", "entry", entry_id,
                             {"kind": info["kind"], "source": info["source"],
                              "url": info["url"]})
            return "share"

        eid = self.store.add_entry(
            today_iso(), "share", raw_text=info["desc"] or info["title"],
            summary=summary[:40], full_text=full,
            topics="、".join(info["topics"]), entities=str({
                "来源": info["source"], "对象编号": info["object_id"],
                "原文链接": info["url"]}),
            confidence=1.0, source_id=src_id,
        )

        # 立刻并进"我记过什么"语义记忆，之后问什么都能被召回
        try:
            from .recall import index_entry
            index_entry(eid)
        except Exception as e:  # noqa: BLE001
            logger.debug("分享入记忆失败（不影响记账）: %s", e)

        reply = "记下了这条%s《%s》" % (info["kind"], summary[:30])
        if info["source"]:
            reply += "，来自「%s」" % info["source"]
        if info["topics"]:
            reply += "，话题 %s" % " ".join("#" + t for t in info["topics"])
        if info["kind"] == "公众号文章":
            if body:
                reply += "。正文已抓取 %d 字，已经放进日记，之后能按原文检索；原文链接也存了。" % len(body)
            else:
                reply += "。原文链接已经存进日记；这次正文没抓到。"
        else:
            reply += "。网页/文章的文字我尽量抓了；视频画面和视频里的语音仍然看不到。"
        self.service._say(reply)
        self.store.audit("ingest", "share_saved", "entry", eid,
                         {"kind": info["kind"], "source": info["source"]})
        return "share"

    def _stage_attachment(self, *, local_id: int, kind: str, filename: str,
                          local_path: str, size_bytes: int = 0, sha256: str = "",
                          extracted_text: str = "", msg_type: int = 0,
                          server_id: int | None = None) -> int | None:
        if kind in ("image", "video", "video_thumb"):
            from .media import media_upload_enabled
            if not media_upload_enabled():
                # 后台关掉“媒体上云”后，不在云端留原件，也不建媒体检索记录；
                # 暂存副本同步删掉，避免本地长期留存。
                try:
                    Path(local_path).unlink(missing_ok=True)
                except Exception as e:  # noqa: BLE001
                    logger.debug("关闭媒体上云后清理暂存失败: %s", e)
                return None
        aid = self.store.add_attachment(
            "filehelper", local_id, kind, filename, local_path=local_path,
            size_bytes=size_bytes, sha256=sha256,
            extracted_text=extracted_text, msg_type=msg_type,
            server_id=server_id,
        )
        if aid:
            try:
                from .recall import index_attachment
                index_attachment(aid)
            except Exception as e:  # noqa: BLE001
                logger.debug("附件入记忆失败（不影响暂存）: %s", e)
            if kind in ("image", "video", "video_thumb"):
                try:
                    from .media import process_attachment
                    process_attachment(self.store, aid, remove_local=True)
                except Exception as e:  # noqa: BLE001
                    logger.warning("媒体自动上云失败（原件仍暂存）: %s", e)
        return aid

    def _handle_file(self, info: dict, content: str, local_id: int,
                     create_time: int) -> str:
        """文件本体：定位 → 复制到本地 → 抽文字 → 暂存。"""
        title = (info.get("title") or "文件").strip()
        self.store.add_source("filehelper", local_id, msg_type="file")
        md5 = ""
        m = re.search(r"(?:md5|filemd5)=\"([0-9a-fA-F]{32})\"", content or "")
        if m:
            md5 = m.group(1).lower()
        account_dir = getattr(self.client, "account_dir", "") or ""
        src = kb.locate_file(account_dir, title, md5)
        if not src:
            self.service._say("收到文件《%s》，但微信目录里没找到本体（可能还没下载完）。"
                              "先记了来源，没入库。" % title[:60])
            return "skip"
        try:
            staged = kb.stage_file(src, title)
        except Exception as e:  # noqa: BLE001
            logger.warning("文件暂存失败 %s: %s", title, e)
            self.service._say("收到文件《%s》，复制失败：%s" % (title[:50], str(e)[:80]))
            return "failed"
        text = kb.extract_text(staged["path"], kind="file")
        self._stage_attachment(
            local_id=local_id, kind="file", filename=staged["filename"],
            local_path=staged["path"], size_bytes=staged["bytes"],
            sha256=staged["sha256"], extracted_text=text, msg_type=49,
        )
        note = "，已抽出 %d 字正文" % len(text) if text else "，正文没抽出来"
        self.service._say("收到文件《%s》%s，已暂存。要存进云端就回我一句「存起来」。"
                          % (staged["filename"][:60], note))
        return "attachment"

    def _handle_image(self, m: dict, local_id: int) -> str:
        content = _plain_content(m.get("content"))
        meta = kb.parse_image_meta(content)
        create_time = int(m.get("create_time") or 0)
        self.store.add_source("filehelper", local_id, msg_type="image")
        if not meta["md5"]:
            self.service._say("收到一张图片，但没读到图片编号，先记了来源。")
            return "skip"
        account_dir = getattr(self.client, "account_dir", "") or ""
        data = kb.decrypt_image(account_dir, meta["md5"], "filehelper", create_time)
        if not data:
            self.service._say("收到一张图片，但微信图片没解开，先记了来源。")
            return "skip"
        ext = kb._image_ext(data)
        staged = kb.stage_bytes(data, (meta["md5"][:10] or "图片") + ext)
        text = kb.extract_text(staged["path"], kind="image")
        self._stage_attachment(
            local_id=local_id, kind="image", filename=staged["filename"],
            local_path=staged["path"], size_bytes=staged["bytes"],
            sha256=staged["sha256"], extracted_text=text, msg_type=3,
        )
        note = "，识别到 %d 字文字" % len(text) if text else "，没识别出文字"
        self.service._say("收到图片%s，已暂存。要存进云端就回我一句「存起来」。"
                          % note)
        return "attachment"

    def _handle_video(self, m: dict, local_id: int) -> str:
        content = _plain_content(m.get("content"))
        meta = kb.parse_video_meta(content)
        create_time = int(m.get("create_time") or 0)
        self.store.add_source("filehelper", local_id, msg_type="video")
        if not meta["md5"]:
            self.service._say("收到视频，但没读到视频编号，先记了来源。")
            return "skip"
        account_dir = getattr(self.client, "account_dir", "") or ""
        found = kb.locate_video(account_dir, meta["md5"], create_time,
                                raw_md5=meta.get("raw_md5", ""))
        src = found.get("video") or found.get("thumb")
        if not src:
            self.service._say("收到视频，但微信只留了消息、没留视频文件，先记了来源。")
            return "skip"
        name = os.path.basename(src)
        staged = kb.stage_file(src, name)
        kind = "video" if found.get("video") else "video_thumb"
        note = "" if found.get("video") else "完整视频没下载，只存了封面"
        text = ""
        self._stage_attachment(
            local_id=local_id, kind=kind, filename=staged["filename"],
            local_path=staged["path"], size_bytes=staged["bytes"],
            sha256=staged["sha256"], extracted_text=text, msg_type=43,
        )
        extra = "（%s）" % note if note else ""
        self.service._say("收到视频《%s》%s，已暂存。要存进云端就回我一句「存起来」。"
                          % (staged["filename"][:60], extra))
        return "attachment"

    def _purge_local_after_upload(self, attachment_id: int, local_path: str,
                                  cloud_id: int | None) -> bool:
        """微云确认上传成功后删本地原件；只允许删附件暂存目录里的文件。"""
        if not local_path or not cloud_id:
            return False
        try:
            root = kb.ATTACH_ROOT.resolve()
            path = Path(local_path).resolve()
            if root not in path.parents:
                logger.warning("拒绝删除附件目录外的路径：%s", local_path)
                return False
            if not path.is_file():
                self.store.mark_attachment_local_purged(attachment_id)
                self.store.mark_cloud_purged(int(cloud_id))
                return True
            size = path.stat().st_size
            try:
                os.chmod(str(path), 0o666)  # 微信缓存常带只读属性，先去掉
            except OSError:
                pass
            os.remove(str(path))
            self.store.mark_attachment_local_purged(attachment_id)
            self.store.mark_cloud_purged(int(cloud_id))
            logger.info("附件已上云，删本地原件：%s（%d 字节）", path.name, size)
            return True
        except OSError as e:
            logger.warning("附件已上云，但本地原件没删掉：%s（%s）", local_path, e)
            return False

    def _store_pending_attachments(self, text: str, source_id: int | None) -> str | None:
        """把刚发的附件挂到一条日记并上云；没有暂存附件时返回 None。"""
        if not STORE_ATTACHMENT.search(text or ""):
            return None
        pending = self.store.pending_attachments("filehelper", hours=24)
        if not pending:
            return None

        names = [r["filename"] or "附件" for r in pending]
        summary = ("存了 %d 个附件：%s" % (len(pending), "、".join(names)))[:80]
        full = ["附件入库（%d 个）：%s" % (len(pending), "、".join(names))]
        for r in pending:
            body = (r["extracted_text"] or "").strip()
            if body:
                full.append("【%s 正文】%s" % (r["filename"] or "附件", body[:1000]))
        if text.strip():
            full.append("说明：%s" % text.strip()[:200])
        eid = self.store.add_entry(
            today_iso(), "attachment", raw_text=text, summary=summary,
            full_text="\n".join(full), confidence=1.0, source_id=source_id,
        )
        ids = [r["id"] for r in pending]
        self.store.attach_attachments(ids, eid, note=text.strip())

        cloud_paths, errors = [], []
        try:
            from .cloud import cloud_enabled
            if cloud_enabled():
                from .cloud.storage import CloudStore
                cs = CloudStore(store=self.store)
                for r in pending:
                    try:
                        if (r["kind"] or "") in ("image", "video", "video_thumb"):
                            asset_id = r["asset_id"] if "asset_id" in r.keys() else ""
                            if not asset_id:
                                from .media import process_attachment
                                mr = process_attachment(self.store, r["id"], remove_local=True)
                                asset_id = mr.get("asset_id") or ""
                            if not asset_id:
                                raise RuntimeError("媒体资产没入云端")
                            when = datetime.now().strftime("%Y-%m")
                            pdir_display = "摘星日记/媒体/%s" % when
                            path = "%s/%s" % (pdir_display, asset_id)
                            self.store.mark_attachment_uploaded(
                                r["id"], None, pdir_display, asset_id)
                            cloud_paths.append(path)
                            continue
                        rec = cs.put_attachment(r["local_path"], entry_id=eid,
                                                filename=r["filename"] or "附件")
                        when = datetime.now().strftime("%Y-%m")
                        pdir_display = "摘星日记/附件/%s" % when
                        path = "%s/%s" % (pdir_display, rec.get("name") or "")
                        self.store.mark_attachment_uploaded(
                            r["id"], None, pdir_display, rec.get("name") or "")
                        self._purge_local_after_upload(
                            r["id"], r["local_path"], rec.get("cloud_id"))
                        cloud_paths.append(path)
                    except Exception as e:  # noqa: BLE001
                        self.store.mark_attachment_error(r["id"], str(e))
                        errors.append("%s：%s" % (r["filename"], str(e)[:60]))
        except Exception as e:  # noqa: BLE001
            logger.warning("附件上云初始化失败: %s", e)
            errors.append(str(e)[:80])

        extra = []
        if cloud_paths:
            extra.append("云端：%s" % "；".join(cloud_paths))
        if errors:
            extra.append("以下附件只存在本地，云端没传上：%s" % "；".join(errors))
        # 正文补附件名/云端路径；抽出来的文字在附件表里，索引时一并进记忆
        self.store.update_entry_full_text(eid, "\n".join(full + extra))
        try:
            from .recall import index_entry, index_attachment
            index_entry(eid)
            for r in pending:
                index_attachment(r["id"])
        except Exception as e:  # noqa: BLE001
            logger.debug("附件日记入记忆失败（不影响回复）: %s", e)

        if cloud_paths:
            reply = "已存到云端：%s" % "；".join(cloud_paths)
        elif errors:
            reply = "已存到本地；云端没传上：%s" % errors[0]
        else:
            reply = "已存到本地（%d 个附件）。" % len(pending)
        if errors and cloud_paths:
            reply += "；另有 %d 个只存本地。" % len(errors)
        return reply

    def bootstrap(self, limit: int = 300) -> int:
        """首次启用时把已有历史全部标记为"已见过"，不回头处理，也不回执。

        只在空库首次启用时执行；工作线程每次重启都调它会把刚收到、还没
        回复的语音也标成 bootstrap，导致用户看到"没反应"。
        """
        if self.store.known_sources():
            logger.info("已有历史记录，跳过 bootstrap（避免吞掉未处理的新消息）")
            return 0
        n = 0
        for chat in ("filehelper", BOT_CHAT):
            try:
                for m in self.client.get_messages(chat, limit=limit):
                    lid = m.get("local_id")
                    if lid is None:
                        continue
                    if str(m.get("sender") or "") == chat:
                        continue
                    if self.store.add_source(chat, lid, msg_type="bootstrap",
                                             msg_time=str(m.get("create_time"))):
                        n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("标记历史失败(%s): %s", chat, e)
        self.store.audit("ingest", "bootstrap", "source", None, {"marked": n}, "ok")
        return n

    def _handle_other(self, m: dict, local_id: int) -> str:
        mtype = int(m.get("local_type") or 0) & 0xFFFFFFFF
        if mtype == 3:
            return self._handle_image(m, local_id)
        if mtype == 43:
            return self._handle_video(m, local_id)
        self.store.add_source("filehelper", local_id, msg_type=str(mtype))
        self.service._say("这条（类型 %d）我暂时不处理，先记了个来源。" % mtype)
        return "skip"


def today_iso() -> str:
    return datetime.now().date().isoformat()


