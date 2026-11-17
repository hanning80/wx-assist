"""长期回答规则：识别、落库、开关、提示词注入。

只测纯逻辑，不调模型、不写正式库。
"""

from __future__ import annotations

from src.journal.answer_rule import (
    DEFAULT_RULE_TEXT,
    get_rule,
    maybe_handle,
    prompt_block,
    save_rule,
)
from src.journal.store import JournalStore


def _store(tmp_path):
    return JournalStore(tmp_path / "journal.db")


def test_default_rule_is_enabled(tmp_path):
    store = _store(tmp_path)
    try:
        rule = get_rule(store)
        assert rule["enabled"] == 1
        assert rule["rule_text"] == ""
        assert "逐字引用" in prompt_block(store)
    finally:
        store.close()


def test_set_intent_saves_rule_not_just_reply(tmp_path):
    store = _store(tmp_path)
    try:
        reply = maybe_handle(
            "以后回答我问题的时候，引用我的日记原话或者引用我的原话",
            store=store,
        )
        assert reply and "记下了" in reply
        rule = get_rule(store)
        assert rule["enabled"] == 1
        assert "逐字引用" in prompt_block(store)
    finally:
        store.close()


def test_off_and_on(tmp_path):
    store = _store(tmp_path)
    try:
        assert maybe_handle("以后不用引用原话了", store=store)
        assert get_rule(store)["enabled"] == 0
        assert prompt_block(store) == ""

        assert maybe_handle("开启引用原话", store=store)
        assert get_rule(store)["enabled"] == 1
        assert "逐字引用" in prompt_block(store)
    finally:
        store.close()


def test_custom_rule_text_and_disable(tmp_path):
    store = _store(tmp_path)
    try:
        save_rule(True, "引用时用书名号，注明日期，不要编", store=store)
        rule = get_rule(store)
        assert rule["enabled"] == 1
        assert "书名号" in rule["rule_text"]

        save_rule(False, "引用时用书名号", store=store)
        assert get_rule(store)["enabled"] == 0
        assert prompt_block(store) == ""
    finally:
        store.close()


def test_normal_chat_is_not_a_rule(tmp_path):
    store = _store(tmp_path)
    try:
        assert maybe_handle("今天天气怎么样？", store=store) is None
        assert maybe_handle("帮我查一下辣椒红品相", store=store) is None
    finally:
        store.close()


def test_default_text_is_explicit(tmp_path):
    assert "找不到原文" in DEFAULT_RULE_TEXT
    assert "不要编造" in DEFAULT_RULE_TEXT


def test_chat_reply_intercepts_rule_without_calling_model(tmp_path):
    from src.journal.capture import JournalIngest
    from src.journal.remind import JournalService

    store = _store(tmp_path)
    called = {"chat": 0}

    def fake_chat(text, chat):
        called["chat"] += 1
        return "模型回复"

    try:
        ingest = JournalIngest(
            store=store,
            service=JournalService(store=store),
            client=object(),
            asr=object(),
            chat_fn=fake_chat,
        )
        reply = ingest._chat_reply(
            "以后回答我问题的时候，引用我的日记原话或者引用我的原话", "filehelper")
        assert "记下了" in reply
        assert called["chat"] == 0
        assert get_rule(store)["enabled"] == 1
    finally:
        store.close()


def test_rule_instruction_does_not_become_diary(tmp_path):
    from src.journal.capture import JournalIngest

    class FakeService:
        def _say(self, _text):
            return None

    store = _store(tmp_path)
    try:
        ingest = JournalIngest(
            store=store, service=FakeService(), client=object(), asr=object())
        before = store._rows("SELECT COUNT(*) AS c FROM diary_entry")[0]["c"]
        result = ingest._handle_text(
            "以后回答我问题的时候，引用我的日记原话或者引用我的原话", 99)
        after = store._rows("SELECT COUNT(*) AS c FROM diary_entry")[0]["c"]
        assert result == "rule"
        assert after == before
        assert get_rule(store)["enabled"] == 1
    finally:
        store.close()
