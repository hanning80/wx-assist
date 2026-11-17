"""输入侧单元自测：分享解析、指令识别、去重、ClawBot 语音对话路由。"""

from src.journal.capture import INSTRUCTION, JournalIngest, parse_share
from src.journal.remind import JournalService
from src.journal.store import JournalStore

SAMPLE = """<?xml version="1.0"?>
<msg><appmsg appid="" sdkver="0">
<title>当前微信版本不支持展示该内容，请升级至最新版本。</title>
<type>51</type>
<url>https://support.weixin.qq.com/update/</url>
<finderFeed>
<objectId><![CDATA[15055101895475927100]]></objectId>
<feedType><![CDATA[4]]></feedType>
<nickname><![CDATA[科技阁的视频号]]></nickname>
<desc><![CDATA[我进化了Siri，让它帮我操控手机。 #vibecoding  #iPhone  #ai]]></desc>
</finderFeed>
</appmsg></msg>"""

BOT_CHAT = "mmo9cq804hSoB6tYB45Plb6MAxoa-o@weclaw"


class FakeClient:
    def __init__(self, msgs):
        self.msgs = msgs

    def get_messages(self, chat, limit=10):
        return self.msgs


def _voice_msg(local_id=5, text_time=1794786907):
    return {
        "local_id": local_id,
        "sender": "wxid_user",
        "local_type": 34,
        "create_time": text_time,
        "content": "",
    }


def _make_ingest(tmp_path, text="今天天气怎么样", reply="答复", handle_text=None):
    store = JournalStore(tmp_path / "journal.db")
    sent = []
    service = JournalService(
        store=store,
        push=lambda message: sent.append(message) or {"ok": True},
    )
    if handle_text is not None:
        service.handle_text = handle_text
    ingest = JournalIngest(
        store=store,
        service=service,
        client=FakeClient([_voice_msg()]),
        asr=object(),
        chat_fn=lambda message, chat: reply,
    )
    ingest._asr_from_msg = lambda chat, msg, local_id: {
        "ok": True, "text": text, "path": "", "latency_ms": 10,
    }
    return ingest, store, sent


def test_parse_finder_feed():
    info = parse_share(SAMPLE)
    assert info["ok"] is True
    assert info["kind"] == "视频号"
    assert info["source"] == "科技阁的视频号"
    assert "操控手机" in info["desc"]
    assert info["topics"] == ["vibecoding", "iPhone", "ai"]
    assert info["object_id"] == "15055101895475927100"


def test_parse_plain_text_is_rejected():
    info = parse_share("随便一句普通话")
    assert info["ok"] is False


def test_instruction_detected():
    for text in ("把这个记录起来", "记一下", "帮我存起来", "这条记录"):
        assert INSTRUCTION.match(text)
    for text in ("鸟房今天开始喂白苏子", "今天买了两只鸟"):
        assert not INSTRUCTION.match(text)


def test_clawbot_voice_general_chat_gets_reply(tmp_path):
    ingest, store, sent = _make_ingest(tmp_path, text="今天天气怎么样", reply="今天晴")
    result = ingest.scan_answers()
    assert result["answer"] == 1
    assert sent == ["今天晴"]
    assert (BOT_CHAT, 5) in store.known_sources()
    # 第二次扫描不会重复回一遍
    assert ingest.scan_answers()["answer"] == 0


def test_clawbot_voice_reminder_routes_to_journal(tmp_path):
    calls = []

    def handle_text(chat, text, source_id=None, **kwargs):
        calls.append((chat, text))
        return {"ok": True}

    ingest, _, _ = _make_ingest(
        tmp_path, text="提醒我每天9点喂鱼肝油", reply="不该走普通对话",
        handle_text=handle_text,
    )
    result = ingest.scan_answers()
    assert result["answer"] == 1
    assert calls and calls[0][0] == BOT_CHAT
    assert "提醒我" in calls[0][1]


def test_clawbot_voice_store_uses_just_staged_video(tmp_path, monkeypatch):
    """Bot 说“存起来”要接刚暂存的视频，不能反问存哪一条。"""
    monkeypatch.setattr("src.journal.cloud.cloud_enabled", lambda: False)
    ingest, store, sent = _make_ingest(
        tmp_path, text="存起来", reply="要存什么内容？")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake-video")
    store.add_attachment(
        "filehelper", 101, "video", "22e9f5ab.mp4",
        local_path=str(video), size_bytes=video.stat().st_size,
    )

    reply = ingest._chat_reply("存起来", BOT_CHAT)
    assert "已存到本地" in reply
    assert "要存什么内容" not in reply
    assert store.pending_attachments("filehelper", hours=24) == []
    assert store._rows("SELECT entry_id FROM attachment WHERE id=1")[0]["entry_id"] is not None
