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
