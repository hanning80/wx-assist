"""解析模块单元自测（不联网，只验规则）。"""

import json

import pytest

from src.journal import parse as P


def test_time_hint_variants():
    assert P.parse_time_hint("晚上七点") == "19:00"
    assert P.parse_time_hint("早上八点半") == "08:30"
    assert P.parse_time_hint("8:05") == "08:05"
    assert P.parse_time_hint("中午十二点") == "12:00"
    assert P.parse_time_hint("凌晨十二点") == "00:00"
    assert P.parse_time_hint("喂七天") is None
    assert P.parse_time_hint("下午三点") == "15:00"


def test_seven_day_reminder_shape():
    """标准那句：鸟房今天开始喂白苏子，喂七天。"""
    r = P.normalize({
        "kind": "reminder",
        "summary": "鸟房开始喂白苏子，连喂七天",
        "full_text": "鸟房今天开始喂白苏子，喂七天",
        "topics": ["喂养", "白苏子"],
        "entities": {"对象": "鸟房", "饲料": "白苏子"},
        "reminder": {"title": "喂白苏子", "object": "鸟房", "action": "喂",
                     "days": 7, "start_date": "2026-11-15", "daily_time_hint": ""},
        "confidence": 0.92,
    }, "鸟房今天开始喂白苏子，喂七天", today="2026-11-15")
    assert r["kind"] == "reminder"
    assert r["needs_review"] is False
    assert r["reminder"]["days"] == 7
    assert r["reminder"]["start_date"] == "2026-11-15"
    assert r["reminder"]["daily_time_hint"] is None
    assert P.entry_date_of(r) == "2026-11-15"


def test_reminder_without_days_goes_to_review():
    r = P.normalize({"kind": "reminder", "summary": "要喂东西",
                     "reminder": {"title": "喂白苏子"}, "confidence": 0.9},
                    "要喂白苏子", today="2026-11-15")
    assert r["needs_review"] is True
    assert r["reminder"] is None
    assert "天数" in r["reason"]


def test_days_out_of_range_rejected():
    for bad in (0, -3, 999, "七天", None):
        r = P.normalize({"kind": "reminder", "reminder": {"title": "喂", "days": bad},
                         "confidence": 0.9}, "喂", today="2026-11-15")
        assert r["needs_review"] is True


def test_low_confidence_flags_review():
    r = P.normalize({"kind": "diary", "summary": "含糊的一句", "confidence": 0.4},
                    "嗯那个啥", today="2026-11-15")
    assert r["needs_review"] is True
    assert r["kind"] == "diary"


def test_garbage_output_keeps_raw_text():
    r = P.normalize({"kind": "???"}, "今天买了两只猫头鹰", today="2026-11-15")
    assert r["kind"] == "diary"
    assert r["full_text"] == "今天买了两只猫头鹰"


def test_bad_json_from_model(monkeypatch):
    """模型返回不是 JSON，也要把原文记下来并标待复核。"""
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "我觉得这是一段日记。"}}]}

    monkeypatch.setattr(P.requests, "post", lambda *a, **k: Resp())
    p = P.DeepSeekParser(api_key="x")
    r = p.parse("今天鸟房下了四个蛋", today="2026-11-15")
    assert r["needs_review"] is True
    assert r["full_text"] == "今天鸟房下了四个蛋"


def test_no_key_degrades_gracefully():
    """没密钥不报错，只记日记，不建提醒。"""
    p = P.DeepSeekParser(api_key="")
    r = p.parse("鸟房今天开始喂白苏子，喂七天", today="2026-11-15")
    assert r["kind"] == "diary"
    assert r["reminder"] is None
    assert "密钥" in r["reason"]


def test_cancel_without_target_flagged():
    r = P.normalize({"kind": "cancel", "summary": "不喂了", "confidence": 0.9},
                    "不喂了", today="2026-11-15")
    assert r["needs_review"] is True
