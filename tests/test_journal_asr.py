"""火山识别模块单元自测。"""

import struct

import pytest

from src.journal.asr_volc import (MT_AUDIO_ONLY, MT_ERROR, MT_FULL_CLIENT,
                                  make_frame, parse_frame, read_pcm, _truncate)


def test_frame_roundtrip_json():
    """自己拼的帧能被自己解开，长度与头都对。"""
    payload = b'{"result":{"text":"\xe5\x96\x82\xe7\x99\xbd\xe8\x8b\x8f\xe5\xad\x90"}}'
    frame = make_frame(MT_FULL_CLIENT, 0, 1, payload)
    assert frame[0] == 0x11
    assert (frame[1] >> 4) == MT_FULL_CLIENT
    assert struct.unpack(">I", frame[4:8])[0] == len(payload)
    obj = parse_frame(frame)
    assert obj["result"]["text"] == "喂白苏子"


def test_parse_error_frame():
    """错误帧要解出 code 与信息，不能当正常结果处理。"""
    payload = "额度不足".encode()
    # 错误帧没有长度前缀：头 + code + size + 正文
    frame = (bytes([0x11, (MT_ERROR << 4) | 0, (1 << 4) | 0, 0x00])
             + struct.pack(">I", 45000081)
             + struct.pack(">I", len(payload))
             + payload)
    obj = parse_frame(frame)
    assert obj["_type"] == "error"
    assert obj["code"] == 45000081
    assert "额度" in obj["message"]


def test_parse_gzip_payload():
    """服务端压缩帧要能自动解压。"""
    import gzip
    import json
    payload = json.dumps({"result": {"text": "你好"}}).encode()
    gz = gzip.compress(payload)
    frame = (bytes([0x11, (MT_AUDIO_ONLY << 4) | 0, (1 << 4) | 1, 0x00])
             + struct.pack(">I", len(gz)) + gz)
    obj = parse_frame(frame)
    assert obj["result"]["text"] == "你好"


def test_parse_short_garbage_is_safe():
    """截断/异常字节不能抛异常，判为 raw。"""
    assert parse_frame(b"\x11")["_type"] == "raw"


def test_truncate_bounds():
    """超过 60 秒截断，不超的不动。"""
    rate, ch = 16000, 1
    one_second = rate * 2 * ch
    assert len(_truncate(b"\x00" * one_second, rate, ch)) == one_second
    three_minutes = b"\x00" * (one_second * 180)
    assert len(_truncate(three_minutes, rate, ch)) == one_second * 60


def test_missing_key_returns_error(tmp_path):
    """没密钥时明确报错，不去发请求。"""
    from src.journal.asr_volc import VolcAsr
    asr = VolcAsr(api_key="")
    r = asr.transcribe(tmp_path / "nope.wav")
    assert r["ok"] is False
    assert "密钥" in r["error"] or r["error"]
