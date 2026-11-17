"""跟某人聊了些什么：把指定会话的文字+语音转成可读流水，再交给大模型做结构化总结。

做法：
1. 从微信本地库按会话拉最近若干条消息；
2. 文字直接用；语音（SILK）解成 WAV，走火山识别转文字，结果按"会话+消息ID"缓存，不重复花钱；
3. 拼成"时间 谁：内容"的流水，丢给已配的大模型出总结。

口径：只写原文里出现过的内容，信息不足就说信息不足；不猜动机、不编结论。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TYPE_TEXT = 1
TYPE_VOICE = 34
REPO_ROOT = Path(__file__).resolve().parents[2]
VOICE_CACHE = REPO_ROOT / "data" / "asr_cache"

PROMPT_HEAD = """你是一名严谨的会谈记录员。下面是「{name}」与用户本人在 {start} 至 {end} 的聊天流水，
其中 [语音] 标记的是把对方的语音消息转录成的文字。

请按下面结构输出，中文，条目式，不要客套话：

一、这段时间一共聊了几件事（每件事一句话概括）
二、每件事的来龙去脉（谁提的、说到哪一步、有没有结论）
三、对方明确提出的要求或需要你配合的事（原文有依据才写）
四、你答应过、承诺过、或需要跟进的事
五、还没定下来的事（信息不足的判断也放这里，写明"信息不足"）

硬规则：
- 只写流水里真实出现的内容，不推断动机、不编造数字、时间、承诺；
- 拿不准的标"信息不足"，不要凑满五段；
- 不要输出"作为AI"之类的话。
"""


def _client():
    from src.wechat.wcdb_client import get_shared_client
    return get_shared_client()


def resolve_chat(name: str) -> Optional[dict]:
    """把备注名/昵称/wxid 解析成会话。返回 {chat_id, name} 或 None。"""
    name = (name or "").strip()
    if not name:
        return None
    cli = _client()
    if name in ("文件传输助手", "filehelper"):
        return {"chat_id": "filehelper", "name": "文件传输助手"}
    # 先按备注/昵称精确匹配联系人
    try:
        for c in cli.get_contacts(keyword=name, limit=50):
            for key in ("remark", "nick_name", "nickname", "display_name", "username"):
                if (c.get(key) or "") == name:
                    return {"chat_id": c.get("username") or c.get("wxid"),
                            "name": c.get("remark") or c.get("nick_name") or name}
    except Exception as e:  # noqa: BLE001
        logger.debug("联系人匹配失败: %s", e)
    # 再按会话列表模糊匹配
    try:
        for s in cli.get_sessions(limit=500):
            dn = s.get("display_name") or s.get("nickname") or ""
            if name in dn:
                return {"chat_id": s.get("username") or s.get("chat_id"), "name": dn}
    except Exception as e:  # noqa: BLE001
        logger.debug("会话匹配失败: %s", e)
    return None


def _asr(chat_id: str, msg: dict, transcribe: bool) -> str:
    """语音消息转文字，带本地缓存。"""
    lid = msg.get("local_id")
    cache = VOICE_CACHE / ("%s_%s.txt" % (chat_id.replace("@", "_"), lid))
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="ignore").strip()
    if not transcribe:
        return ""

    try:
        from src.journal.asr_provider import get_shared_asr
        from src.wechat.voice_decode import silk_to_wav

        data = _client().get_voice_data(
            chat_id, msg.get("create_time"), msg.get("local_id"), msg.get("server_id"))
        if not data or not data.get("success"):
            return ""
        wav = silk_to_wav(data["hex"])
        if not wav:
            return ""
        VOICE_CACHE.mkdir(parents=True, exist_ok=True)
        wav_path = cache.with_suffix(".wav")
        wav_path.write_bytes(wav)
        res = get_shared_asr().transcribe(wav_path)
        text = (res.get("text") or "").strip()
        try:
            wav_path.unlink(missing_ok=True)  # 文字落地即删音频
        except OSError:
            pass
        if text:
            cache.write_text(text, encoding="utf-8")
        return text
    except Exception as e:  # noqa: BLE001
        logger.warning("语音转写失败 %s/%s: %s", chat_id, lid, e)
        return ""


def collect(chat_id: str, days: int = 7, limit: int = 400,
            transcribe: bool = True, max_voices: int = 40) -> dict:
    """拉一段时间的聊天流水。返回 {rows, voices, done, skipped}。"""
    msgs = _client().get_messages(chat_id, limit=limit)
    cutoff = 0
    if days and days > 0:
        cutoff = int((datetime.now() - timedelta(days=days)).timestamp())

    rows, voices, skipped = [], 0, 0
    for m in msgs:
        ct = int(m.get("create_time") or 0)
        if cutoff and ct < cutoff:
            continue
        mtype = int(m.get("local_type") or 0) & 0xFFFFFFFF
        when = datetime.fromtimestamp(ct).strftime("%m-%d %H:%M") if ct else ""
        who = "我" if m.get("is_send") else "对方"
        if mtype == TYPE_TEXT:
            text = (m.get("content") or "").strip()
            if not text:
                continue
            rows.append({"time": when, "who": who, "text": text, "kind": "文字"})
        elif mtype == TYPE_VOICE:
            if voices >= max_voices:
                skipped += 1
                continue
            voices += 1
            text = _asr(chat_id, m, transcribe)
            if text:
                rows.append({"time": when, "who": who, "text": text, "kind": "语音"})
            else:
                rows.append({"time": when, "who": who,
                             "text": "[语音，没转成文字]", "kind": "语音"})
    return {"rows": rows, "voices": voices, "skipped": skipped}


def _llm(prompt: str) -> str:
    from src.config import load_config
    from src.summarize import create_summarizer

    cfg = load_config()
    if not (cfg.ai_provider_api_key and cfg.ai_provider_base_url):
        raise RuntimeError("AI 没配置：请先在设置里填 API Key 与地址")
    smrz = create_summarizer(cfg)
    return smrz._call_digest_api(prompt, [{"role": "user", "content": "请按要求输出总结。"}],
                                 timeout=180)


def summarize_chat(name: str, days: int = 7, limit: int = 400,
                   transcribe: bool = True, max_voices: int = 40,
                   save: bool = True) -> dict:
    """总结"我和某人最近聊了什么"。"""
    target = resolve_chat(name)
    if not target:
        return {"ok": False, "error": "没找到这个联系人/会话：%s" % name}

    data = collect(target["chat_id"], days=days, limit=limit,
                   transcribe=transcribe, max_voices=max_voices)
    rows = data["rows"]
    if not rows:
        return {"ok": False, "error": "这段时间没有可读的消息（%s）" % target["name"]}

    start = rows[0]["time"]
    end = rows[-1]["time"]
    body = "\n".join("%s %s：%s" % (r["time"], r["who"], r["text"]) for r in rows)
    # 控制输入长度：超长截断保留最近的部分（最近的更像"现在聊到哪儿"）
    if len(body) > 12000:
        body = body[-12000:]

    prompt = PROMPT_HEAD.format(name=target["name"], start=start, end=end) + "\n流水如下：\n" + body
    t0 = time.time()
    try:
        out = _llm(prompt).strip()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "总结失败：%s" % e}

    result = {"ok": True, "chat": target["name"], "chat_id": target["chat_id"],
              "rows": len(rows), "voices": data["voices"], "skipped_voices": data["skipped"],
              "seconds": round(time.time() - t0, 1), "summary": out}
    if save:
        out_dir = REPO_ROOT / "data" / "journal" / "chat_summary"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("聊天总结-%s-%s.md" % (target["name"].replace("/", "_"),
                                              datetime.now().strftime("%Y%m%d-%H%M")))
        path.write_text("## 与 %s 的聊天总结（%s 至 %s）\n\n%s\n" % (target["name"], start, end, out),
                        encoding="utf-8")
        result["path"] = str(path)
    return result


_ = json  # 预留：日志里需要时序列化
