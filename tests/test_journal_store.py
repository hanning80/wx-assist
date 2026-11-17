"""存储层单元自测（阶段4 模块一）。

跑法：F:\\微信监控\\.venv\\Scripts\\python.exe -m pytest tests/test_journal_store.py -v
"""

from pathlib import Path

import pytest

from src.journal.store import JournalStore


@pytest.fixture()
def store(tmp_path: Path):
    s = JournalStore(tmp_path / "journal.db")
    yield s
    s.close()


def test_duplicate_source_rejected(store):
    """同一条微信消息只能登记一次，重启后不会重复记日记。"""
    first = store.add_source("filehelper", 101, msg_time="2026-11-15T09:00:00", msg_type="voice")
    assert isinstance(first, int)
    assert store.add_source("filehelper", 101, msg_time="2026-11-15T09:00:00") is None
    assert store.source_exists("filehelper", 101)
    assert store.known_sources() == {("filehelper", 101)}


def test_purge_entry_removes_source_and_allows_reimport(store):
    """彻底删除后，数据库行、来源、附件和提醒都清掉，同一消息可重新入库。"""
    src = store.add_source("filehelper", 202, msg_time="2026-11-15T09:00:00", msg_type="text")
    eid = store.add_entry("2026-11-15", "text", "辣椒红品相判断", summary="辣椒红",
                          source_id=src)
    aid = store.add_attachment("filehelper", 202, "image", "pepper.jpg",
                               local_path="F:/tmp/pepper.jpg")
    store.attach_attachments([aid], eid)
    cid = store.create_chain(eid, "看辣椒", "2026-11-15", days=3, daily_time="09:00")
    cloud_id = store.add_cloud_object(
        "attachment", eid, "file-key", "pdir-key", "pepper.jpg",
        local_path="F:/tmp/pepper.jpg")
    # 附件上云后本地暂存会清掉，cloud_object 可能已是 purged；
    # 彻底删除仍必须把云端 key 带出来，不能跳过。
    store.mark_cloud_purged(cloud_id)

    info = store.purge_entry(eid)
    assert info["id"] == eid
    assert info["source_removed"] is True
    assert info["attachment_ids"] == [aid]
    assert info["chain_ids"] == [cid]
    assert info["attachments"][0]["local_path"] == "F:/tmp/pepper.jpg"
    assert info["cloud_objects"][0]["object_key"] == "file-key"
    assert store._rows("SELECT id FROM diary_entry WHERE id=?", (eid,)) == []
    assert store._rows("SELECT id FROM entry_source WHERE id=?", (src,)) == []
    assert store._rows("SELECT id FROM attachment WHERE id=?", (aid,)) == []
    assert store._rows("SELECT id FROM reminder_chain WHERE id=?", (cid,)) == []
    assert store._rows("SELECT id FROM reminder_task WHERE chain_id=?", (cid,)) == []
    assert store._rows(
        "SELECT entry_id FROM cloud_object WHERE id=?", (cloud_id,))[0]["entry_id"] is None

    # 来源表已清空，同一条微信消息可以再次登记并重新入库。
    src2 = store.add_source("filehelper", 202, msg_time="2026-11-15T09:00:00", msg_type="text")
    assert src2 is not None
    eid2 = store.add_entry("2026-11-15", "text", "辣椒红品相判断", summary="辣椒红",
                           source_id=src2)
    assert store._rows("SELECT id FROM diary_entry WHERE id=?", (eid2,)) != []


def test_seven_day_chain_expands_to_eight_tasks(store):
    """喂七天 = 7 条每日提醒 + 1 条收尾提醒，收尾落在第 8 天。"""
    eid = store.add_entry("2026-11-15", "voice", "鸟房今天开始喂白苏子，喂七天", summary="喂白苏子")
    cid = store.create_chain(eid, "喂白苏子", "2026-11-15", days=7, daily_time="08:30",
                             object_name="鸟房", action="喂")
    tasks = store.tasks_of_chain(cid)
    assert len(tasks) == 8
    daily = [t for t in tasks if t["kind"] == "daily"]
    final = [t for t in tasks if t["kind"] == "final"]
    assert [d["seq"] for d in daily] == [1, 2, 3, 4, 5, 6, 7]
    assert daily[0]["fire_date"] == "2026-11-15"
    assert daily[-1]["fire_date"] == "2026-11-21"
    assert len(final) == 1
    assert final[0]["seq"] == 8
    assert final[0]["fire_date"] == "2026-11-22"
    assert daily[0]["fire_at"] == "2026-11-15T08:30:00"


def test_pending_time_chain_has_no_tasks(store):
    """时刻没定，链停在待定，绝不生成任何提醒实例。"""
    cid = store.create_chain(None, "喂白苏子", "2026-11-15", days=7)
    chain = store.chain(cid)
    assert chain["status"] == "pending_time"
    assert store.tasks_of_chain(cid) == []
    assert store.due_tasks("2026-11-16T00:00:00") == []
    store.set_chain_time(cid, "19:00")
    assert store.chain(cid)["status"] == "active"
    assert len(store.tasks_of_chain(cid)) == 8


def test_one_asked_question_per_chat(store):
    """同一会话只挂一条待答，第二条必须报错而不是覆盖。"""
    cid = store.create_chain(None, "喂白苏子", "2026-11-15", days=7)
    q1 = store.ask_question("ask_time", "这件几点提醒你？", chain_id=cid)
    assert store.active_question()["id"] == q1
    with pytest.raises(ValueError):
        store.ask_question("ask_time", "再问一次？", chain_id=cid)


def test_answer_activates_chain(store):
    """回话给定时刻后，链转 active 并展开提醒。"""
    cid = store.create_chain(None, "喂白苏子", "2026-11-15", days=3)
    qid = store.ask_question("ask_time", "几点提醒？", chain_id=cid)
    store.answer_question(qid, "晚上七点", hhmm="19:00")
    assert store.active_question() is None
    assert store.chain(cid)["status"] == "active"
    assert len(store.tasks_of_chain(cid)) == 4


def test_cancel_skips_pending_tasks(store):
    """取消后所有待发实例必须变 skipped。"""
    cid = store.create_chain(None, "喂白苏子", "2026-11-15", days=5, daily_time="09:00")
    store.mark_task_sent(store.tasks_of_chain(cid)[0]["id"])
    store.cancel_chain(cid, reason="不喂了")
    tasks = store.tasks_of_chain(cid)
    assert store.chain(cid)["status"] == "cancelled"
    assert [t["status"] for t in tasks].count("skipped") == 5
    assert [t["status"] for t in tasks].count("sent") == 1


def test_failure_is_audited_and_retried_three_times(store):
    """发送失败留审计，三次后置 failed，不到三次仍待发。"""
    cid = store.create_chain(None, "喂白苏子", "2026-11-15", days=1, daily_time="09:00")
    tid = store.tasks_of_chain(cid)[0]["id"]
    r1 = store.mark_task_failed(tid, "窗口找不到")
    assert r1 == 1
    rows = store._rows("SELECT result, action FROM audit_log WHERE target_id=? AND action='task_fail'", (tid,))
    assert len(rows) == 1 and rows[0]["result"] == "fail"
    store.mark_task_failed(tid, "窗口找不到")
    r3 = store.mark_task_failed(tid, "窗口找不到")
    assert r3 == 3
    task = store._rows("SELECT status FROM reminder_task WHERE id=?", (tid,))[0]
    assert task["status"] == "failed"


def test_weekly_upsert_keeps_one_row(store):
    """同一周只留一行，重建覆盖不新增。"""
    stats = {"entries": 3}
    store.upsert_weekly("2026-11-09", "2026-11-15", "a.html", stats, "第一版")
    store.upsert_weekly("2026-11-09", "2026-11-15", "b.html", stats, "第二版")
    rows = store._rows("SELECT html_path, narrative FROM weekly_review WHERE week_start='2026-11-09'")
    assert len(rows) == 1
    assert rows[0]["html_path"] == "b.html"
    assert rows[0]["narrative"] == "第二版"


def test_weekly_stats_counts(store):
    """周复盘的数能从表里算出来。"""
    eid = store.add_entry("2026-11-11", "voice", "今天进了两只玉鸟")
    cid = store.create_chain(eid, "喂白苏子", "2026-11-11", days=2, daily_time="08:00")
    tasks = store.tasks_of_chain(cid)
    store.mark_task_sent(tasks[0]["id"])
    stats = store.weekly_stats("2026-11-09", "2026-11-15")
    assert stats["entries"] == 1
    assert stats["tasks_sent"] == 1
    assert stats["chains"] == 1
