"""复盘页单元自测。"""

from datetime import date
from pathlib import Path

from src.journal.store import JournalStore
from src.journal.weekly import generate, month_range, week_range


def test_ranges():
    assert week_range(date(2026, 11, 15)) == ("2026-11-09", "2026-11-15")
    assert week_range(date(2026, 11, 9)) == ("2026-11-09", "2026-11-15")
    assert month_range(date(2026, 11, 15)) == ("2026-11-01", "2026-11-30")
    assert month_range(date(2026, 12, 31)) == ("2026-12-01", "2026-12-31")


def test_empty_week_still_renders(tmp_path: Path):
    """空周也出页面，写清没记录，不留白。"""
    store = JournalStore(tmp_path / "j.db")
    r = generate(store, "week", date(2026, 11, 15), out_dir=tmp_path)
    body = Path(r["path"]).read_text(encoding="utf-8")
    assert "还没有任何记录" in body
    assert "这一周" in body
    store.close()


def test_week_page_contains_entries_and_future(tmp_path: Path):
    store = JournalStore(tmp_path / "j.db")
    eid = store.add_entry("2026-11-15", "voice", "鸟房今天开始喂白苏子，喂七天",
                          summary="开始喂白苏子", full_text="鸟房今天开始喂白苏子，喂七天",
                          topics="喂养、白苏子")
    cid = store.create_chain(eid, "喂白苏子", "2026-11-15", days=7, daily_time="08:00")
    store.mark_task_sent(store.tasks_of_chain(cid)[0]["id"])
    r = generate(store, "week", date(2026, 11, 15), out_dir=tmp_path)
    body = Path(r["path"]).read_text(encoding="utf-8")
    assert "开始喂白苏子" in body
    assert "喂白苏子" in body
    assert "每天 08:00" in body
    assert "已发 1" in body
    assert "白苏子" in body
    store.close()


def test_month_report_upserts(tmp_path: Path):
    store = JournalStore(tmp_path / "j.db")
    store.add_entry("2026-11-03", "voice", "进了两只玉鸟", summary="进鸟")
    generate(store, "month", date(2026, 11, 15), out_dir=tmp_path)
    rows = store._rows("SELECT week_start FROM weekly_review")
    assert len(rows) == 1 and rows[0]["week_start"] == "2026-11-01"
    store.close()
