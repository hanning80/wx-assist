import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from src.journal.store import JournalStore
from src.journal import media as media_mod
from src.journal.cloud import provider
from src.journal import knowledge


class FakeClient:
    def __init__(self):
        self.objects = {}

    def ensure_path(self, path):
        return "/".join(path) if isinstance(path, (list, tuple)) else path

    def upload(self, local_path, key, filename=""):
        self.objects[key] = Path(local_path).read_bytes()
        return {"file_id": key}

    def put_bytes(self, key, data, content_type=""):
        self.objects[key] = data
        return key

    def sign_url(self, key, expire=3600):
        return "https://example.invalid/" + key

    def delete(self, key):
        self.objects.pop(key, None)


def test_media_upload_dedup_and_ref(tmp_path, monkeypatch):
    store = JournalStore(tmp_path / "journal.db")
    fake = FakeClient()
    monkeypatch.setattr(provider, "make_client", lambda: fake)
    monkeypatch.setattr(knowledge, "ocr_image", lambda path: "测试文字")

    p = tmp_path / "a.png"
    Image.new("RGB", (40, 30), (20, 80, 50)).save(p)

    r = media_mod.ingest_media(store, p, source_kind="test", source_id="s1")
    assert r["ok"] is True
    assert r["asset_id"]
    assert store.media_counts()["image"] == 1
    assert any(k.startswith("摘星日记/媒体/原始/图片/") for k in fake.objects)

    # 同一张图再来一次：只加引用，不重复存原件
    r2 = media_mod.ingest_media(store, p, source_kind="test", source_id="s2")
    assert r2["ok"] is True
    assert r2["deduplicated"] is True
    assert store.media_counts()["image"] == 1
    assert len(store.media_refs(r["asset_id"])) == 2

    store.close()


def _make_account(base: Path, name: str) -> Path:
    d = base / name
    (d / "db_storage" / "session").mkdir(parents=True, exist_ok=True)
    (d / "db_storage" / "session" / "session.db").write_text("", encoding="utf-8")
    return d


def test_only_current_wechat_account_is_scanned(tmp_path, monkeypatch):
    """一台机器同一时刻只有一个号在线，别的号目录不属于监控范围。"""
    from types import SimpleNamespace

    import src.config as config_mod

    base = tmp_path / "xwechat_files"
    base.mkdir()
    _make_account(base, "wxid_abh6zf6773ta22_c8de")
    _make_account(base, "wxid_cy3xykvv4vsx12_36f7")

    monkeypatch.setenv("WXID", "wxid_abh6zf6773ta22")
    monkeypatch.setenv("WECHAT_DATA_DIR", str(base))
    monkeypatch.delenv("JOURNAL_SCAN_OTHER_ACCOUNTS", raising=False)
    monkeypatch.setattr(
        config_mod, "load_config",
        lambda: SimpleNamespace(wechat_data_dir=str(base), wxid="wxid_abh6zf6773ta22"))

    assert media_mod._scan_other_accounts_enabled() is False
    assert media_mod._current_account_names() == {"wxid_abh6zf6773ta22_c8de"}

    monkeypatch.setenv("JOURNAL_SCAN_OTHER_ACCOUNTS", "1")
    assert media_mod._scan_other_accounts_enabled() is True


def test_favorite_reason_names_other_accounts(tmp_path, monkeypatch):
    """解不开的收藏要说清是"不在当前号目录"，而不是笼统的密钥问题。"""
    store = JournalStore(tmp_path / "journal.db")
    monkeypatch.setattr(media_mod, "media_upload_enabled", lambda: True)
    monkeypatch.setattr(media_mod, "_decrypt_fav_any", lambda *a, **k: None)
    monkeypatch.delenv("JOURNAL_SCAN_OTHER_ACCOUNTS", raising=False)

    import sqlite3
    db = Path(store.db_path).parent / "messages.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE IF NOT EXISTS fav_cache("
                "fav_id INTEGER, type INTEGER, media_json TEXT)")
    con.execute("INSERT INTO fav_cache VALUES (1, 1, ?)",
                ('[{"fullmd5":"abc","fullsize":10}]',))
    con.commit()
    con.close()

    import src.web.api_handlers as api_mod
    monkeypatch.setattr(api_mod, "_decrypt_fav_v2_media", lambda *a, **k: None)

    r = media_mod.sync_favorite_images(store, limit=5)
    assert r["skipped"] >= 1
    assert "原件不在当前登录微信号目录（其它微信号不纳入监控）" in r["reasons"]
    store.close()
