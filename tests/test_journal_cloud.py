"""云端存储三件事的自测：分块哈希算法、加密往返、记账表。

不联网：微云上传走的是真接口，这里只验本地能验的部分，避免测试打到线上配额。
"""

import hashlib
import os
import tempfile

import pytest

from src.journal.cloud.crypto import decrypt_bytes, encrypt_bytes
from src.journal.cloud.sha1stream import BLOCK, Sha1Stream, block_info
from src.journal.store import JournalStore


def test_sha1_digest_matches_hashlib():
    data = os.urandom(900)
    s = Sha1Stream()
    s.update(data)
    assert s.digest_hex() == hashlib.sha1(data).hexdigest()


def test_sha1_state_is_40_hex_and_stable():
    data = os.urandom(BLOCK)
    s = Sha1Stream()
    s.update(data)
    st = s.state_hex()
    assert len(st) == 40 and all(c in "0123456789abcdef" for c in st)
    # 同一份数据、同一切点，状态必须一致
    s2 = Sha1Stream()
    s2.update(data)
    assert s2.state_hex() == st


def test_block_info_single_block(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes("摘星".encode("utf-8") * 10)
    info = block_info(str(p))
    assert info["size"] == 60
    assert len(info["block_sha_list"]) == 1
    assert info["block_sha_list"][0] == hashlib.sha1(p.read_bytes()).hexdigest()
    assert len(info["check_sha"]) == 40
    assert info["md5"] == hashlib.md5(p.read_bytes()).hexdigest()


def test_block_info_two_blocks(tmp_path):
    p = tmp_path / "b.bin"
    body = os.urandom(BLOCK + 100)
    p.write_bytes(body)
    info = block_info(str(p))
    assert len(info["block_sha_list"]) == 2
    assert info["block_sha_list"][-1] == hashlib.sha1(body).hexdigest()


def test_crypto_roundtrip():
    data = b"today I fed the birds twice"
    blob = encrypt_bytes(data)
    assert blob.startswith(b"WXJ1")
    assert decrypt_bytes(blob) == data
    # 同一段明文两次加密，密文不同（随机 nonce）
    assert encrypt_bytes(data) != blob


def test_crypto_plain_passthrough():
    assert decrypt_bytes(b"not-encrypted") == b"not-encrypted"


@pytest.fixture()
def store(tmp_path):
    s = JournalStore(db_path=tmp_path / "t.db")
    yield s
    s.close()


def test_cloud_object_record_and_query(store):
    cid = store.add_cloud_object(kind="voice", entry_id=7, object_key="k1", pdir_key="p1",
                                 name="a.ogg.enc", bytes=900, sha256="x", local_path="/tmp/a.wav")
    assert cid > 0
    row = store.cloud_of_entry(7, "voice")
    assert row["name"] == "a.ogg.enc" and row["state"] == "uploaded"


def test_cloud_object_purge_flow(store):
    store.add_cloud_object(kind="voice", entry_id=8, object_key="k2", pdir_key="p2",
                           name="b.ogg.enc", bytes=10, created_at="2026-01-01T00:00:00")
    rows = store.list_cloud_objects(kind="voice", uploaded_before="2026-02-01T00:00:00")
    assert len(rows) == 1
    store.mark_cloud_purged(rows[0]["id"])
    assert store.list_cloud_objects(kind="voice", uploaded_before="2026-02-01T00:00:00") == []
    assert store.cloud_of_entry(8, "voice")["state"] == "purged"


def test_last_snapshot_at(store):
    assert store.last_snapshot_at() == ""
    store.add_cloud_object(kind="db_snapshot", entry_id=None, object_key="k3", pdir_key="p3",
                           name="日记库-20260101.db.enc", bytes=1)
    assert store.last_snapshot_at() != ""
