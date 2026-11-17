"""日记服务单元自测（发送通道打桩，不发真消息）。"""

from datetime import datetime
from pathlib import Path

import pytest

from src.journal.parse import DeepSeekParser
from src.journal.remind import JournalService
from src.journal.store import JournalStore


class FakePush:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, text):
        if self.fail:
            return {"success": False, "error": "通道故障"}
        self.sent.append(text)
        return {"success": True}


def fake_parser(kind="reminder", days=7, hint=None):
    """替身解析器：不联网，直接给结构化结果；带"不喂"就返回取消。"""

    class P:
        def parse(self, text, today=None, now_hhmm=None):
            from src.journal.parse import normalize
            k = "cancel" if ("不喂" in text or "停掉" in text) else kind
            return normalize({
                "kind": k,
                "summary": text[:12],
                "full_text": text,
                "reminder": {"title": "喂白苏子", "object": "鸟房", "action": "喂",
                             "days": days, "start_date": today,
                             "daily_time_hint": hint or "",
                             "cancel_target": "喂白苏子" if k == "cancel" else ""},
                "confidence": 0.95,
            }, text, today, now_hhmm)

    return P()


@pytest.fixture()
def svc(tmp_path: Path):
    store = JournalStore(tmp_path / "j.db")
    push = FakePush()
    s = JournalService(store=store, parser=fake_parser(), push=push)
    s._test_push = push
    yield s
    store.close()


def _create(svc, text, chat="filehelper"):
    """新建提醒现在先回确认问句；测试里统一替用户回“确认”。"""
    r = svc.handle_text(chat, text)
    if r.get("action") == "confirm_create":
        return svc.confirm_pending("确认", chat_id=chat, say=True)
    return r


def test_reminder_without_time_asks_question(svc):
    """说了七天但没说几点：先确认新建，确认后追问时刻，且不产生实例。"""
    first = svc.handle_text("filehelper", "鸟房今天开始喂白苏子，喂七天")
    assert first["action"] == "confirm_create"
    assert "确认建吗" in svc._test_push.sent[-1]
    r = svc.confirm_pending("确认", chat_id="filehelper", say=True)
    assert r["action"] == "ask_time"
    chain = svc.store.chain(r["chain_id"])
    assert chain["status"] == "pending_time"
    assert svc.store.tasks_of_chain(chain["id"]) == []
    assert "几点提醒你" in svc._test_push.sent[-1]
    assert svc.store.active_question("filehelper") is not None


def test_answer_time_activates_and_confirms(svc):
    """回"晚上七点"：链转 active、展开 8 条、回执确认。"""
    r = _create(svc, "鸟房今天开始喂白苏子，喂七天", chat="ilink_dm")
    a = svc.handle_text("ilink_dm", "晚上七点")
    assert a["action"] == "answer"
    assert a["times"] == ["19:00"]
    chain = svc.store.chain(r["chain_id"])
    assert chain["status"] == "active"
    assert chain["daily_time"] == "19:00"
    assert len(svc.store.tasks_of_chain(chain["id"])) == 8
    assert "19:00" in svc._test_push.sent[-1]


def test_answer_two_times_per_day(svc):
    """回"每天两次，早上10点、下午5点"：一天两次，7 天 = 14 条 + 1 条收尾。"""
    r = _create(svc, "繁殖房玉鸟喂鱼肝油，喂七天，每天两次", chat="ilink_dm")
    a = svc.handle_text("ilink_dm", "每天两次，早上10点，下午5点吧。")
    assert a["action"] == "answer"
    assert a["times"] == ["10:00", "17:00"]
    chain = svc.store.chain(r["chain_id"])
    assert chain["status"] == "active"
    assert chain["daily_time"] == "10:00,17:00"
    tasks = svc.store.tasks_of_chain(chain["id"])
    assert len(tasks) == 7 * 2 + 1
    assert tasks[0]["fire_at"].endswith("10:00:00")
    assert tasks[1]["fire_at"].endswith("17:00:00")
    assert tasks[-1]["kind"] == "final"
    assert "一天 2 次" in svc._test_push.sent[-1]


def test_time_in_sentence_skips_question(tmp_path):
    store = JournalStore(tmp_path / "j2.db")
    push = FakePush()
    s = JournalService(store=store, parser=fake_parser(hint="早上八点"), push=push)
    r = _create(s, "鸟房开始喂白苏子，喂七天，每天早上八点")
    assert r["action"] == "chain_created"
    assert store.tasks_of_chain(r["chain_id"])[0]["fire_at"].endswith("08:00:00")
    assert store.active_question() is None
    store.close()


def test_due_task_sends_and_finishes(svc):
    """到点发每日提醒；隔天的不补发；收尾提醒发完链结案。"""
    from datetime import timedelta

    base = datetime.now().replace(hour=8, minute=1, second=0, microsecond=0)
    r = _create(svc, "鸟房今天开始喂白苏子，喂七天", chat="ilink_dm")
    svc.handle_text("ilink_dm", "早上八点")
    cid = r["chain_id"]
    first = svc.tick(base)
    assert first["sent"] == 1
    assert "第 1 天" in svc._test_push.sent[-1]
    # 隔了三天才开机：中间几天不补发，直接跳过
    mid = svc.tick(base + timedelta(days=3))
    assert mid["sent"] == 1
    assert "第 4 天" in svc._test_push.sent[-1]
    skipped = [t for t in svc.store.tasks_of_chain(cid) if t["status"] == "skipped"]
    assert len(skipped) == 2
    final = svc.tick(base + timedelta(days=7))
    assert final["finished"] == 1
    assert "满 7 天" in svc._test_push.sent[-1]
    assert svc.store.chain(cid)["status"] == "finished"


def test_send_failure_retries_then_failed(tmp_path):
    store = JournalStore(tmp_path / "j3.db")
    s = JournalService(store=store, parser=fake_parser(days=1, hint="早上八点"),
                       push=FakePush(fail=True))
    r = _create(s, "喂白苏子一天")
    at = datetime.now().replace(hour=8, minute=30, second=0, microsecond=0)
    for _ in range(3):
        s.tick(at)
    task = store.tasks_of_chain(r["chain_id"])[0]
    assert task["status"] == "failed"
    store.close()


def test_cancel_by_name(svc):
    r = _create(svc, "鸟房今天开始喂白苏子，喂七天")
    svc.handle_text("ilink_dm", "晚上七点")
    n = svc.handle_text("filehelper", "不喂白苏子了")
    assert n["action"] == "cancel"
    assert svc.store.chain(r["chain_id"])["status"] == "cancelled"


def test_pending_time_sweep_activates(tmp_path):
    """追问超 6 小时没人答：按整点先跑，并留审计。"""
    store = JournalStore(tmp_path / "j4.db")
    push = FakePush()
    s = JournalService(store=store, parser=fake_parser(), push=push)
    r = _create(s, "鸟房今天开始喂白苏子，喂七天")
    from datetime import datetime as dt
    later = dt.fromisoformat(store.chain(r["chain_id"])["created_at"]) + __import__("datetime").timedelta(hours=7)
    n = s.sweep_pending_time(later)
    assert n == 1
    chain = store.chain(r["chain_id"])
    assert chain["status"] == "active"
    assert chain["daily_time"].endswith(":00")
    store.close()


def test_reminder_request_detected_for_wechat_routing():
    """微信里这些话必须由日记链接手，不能再交给对话助手。"""
    from src.journal.remind import is_create_reminder_request
    assert is_create_reminder_request("每天早上九点提醒我喂鱼肝油")
    assert is_create_reminder_request("提醒一下，下午五点叫我收衣服")
    assert is_create_reminder_request("每天两次，早上十点和下午五点提醒我")
    assert not is_create_reminder_request("今天鸟房温度多少")
    assert not is_create_reminder_request("停掉喂鱼肝油的提醒")


def test_ilink_reminder_is_consumed_not_handed_to_router(monkeypatch):
    """同一句提醒只能由日记回执，返回 True 后上层不再交对话助手。"""
    from src.journal import worker

    seen = []

    class S:
        def __init__(self):
            # handle_ilink_message 还会读 store、查待确认；桩要跟真实服务接口一致。
            self.store = type("Store", (), {
                "active_question": staticmethod(lambda chat_id=None: None),
            })()

        def is_stop_command(self, text):
            return False

        def has_pending_confirmation(self, chat_id="ilink_dm"):
            return False

        def handle_text(self, chat, text, source_id=None):
            seen.append((chat, text))
            return {"action": "chain_created"}

    monkeypatch.setattr(worker, "get_service", lambda: S())
    assert worker.handle_ilink_message({"content": "每天早上九点提醒我喂鱼肝油"}) is True
    assert seen == [("ilink_dm", "每天早上九点提醒我喂鱼肝油")]


def test_duplicate_diary_not_recorded_twice(tmp_path):
    """同一段日记发两次：只落一条，第二次明确说已经记过。"""
    store = JournalStore(tmp_path / "j.db")
    push = FakePush()
    svc = JournalService(store=store, parser=fake_parser(kind="diary"), push=push)
    text = "给我记住判断辣椒红品相好坏的关键点：肚皮底下不能有白毛。"
    src1 = store.add_source("filehelper", 101, msg_type="text")
    r1 = svc.handle_text("filehelper", text, source_id=src1)
    assert r1["action"] == "diary"
    assert len(store._rows("select id from diary_entry where deleted=0")) == 1
    src2 = store.add_source("filehelper", 102, msg_type="text")
    r2 = svc.handle_text("filehelper", text, source_id=src2)
    assert r2["action"] == "duplicate"
    assert r2["entry_id"] == r1["entry_id"]
    assert len(store._rows("select id from diary_entry where deleted=0")) == 1
    assert "已经记过" in push.sent[-1]
    store.close()
