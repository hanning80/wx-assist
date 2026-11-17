"""云存储客户端工厂：唯一厂商切换点。

业务层只调用 make_client() 拿到 CloudBackend；厂商判断、凭据读取、
缺配置时的报错都收在这里。支持：

    weiyun  腾讯微云（历史保留，不再作为默认）
    oss     阿里云 OSS
    cos     腾讯云 COS

.env 示例：
    CLOUD_PROVIDER=oss            # or cos / weiyun
    OSS_BUCKET=... OSS_ENDPOINT=... OSS_ACCESS_KEY_ID=... OSS_ACCESS_KEY_SECRET=...
    COS_BUCKET=... COS_REGION=... COS_SECRET_ID=... COS_SECRET_KEY=...
"""

from __future__ import annotations

import os
from pathlib import Path

ALIASES = {
    "oss": "oss", "aliyun": "oss", "aliyun-oss": "oss",
    "cos": "cos", "qcloud": "cos", "tencent-cos": "cos",
    "weiyun": "weiyun", "tencent": "weiyun",
}

PROVIDER_LABELS = {
    "oss": "阿里云 OSS",
    "cos": "腾讯云 COS",
    "weiyun": "腾讯微云",
}

PROVIDER_ENV = {
    "oss": ("OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"),
    "cos": ("COS_BUCKET", "COS_SECRET_ID", "COS_SECRET_KEY"),
    "weiyun": ("WEIYUN_MCP_TOKEN",),
}


def env(key: str, default: str = "") -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    path = Path(__file__).resolve().parents[3] / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return default


def _normalize(name: str) -> str:
    return ALIASES.get((name or "").strip().lower(), "")


def provider_name() -> str:
    """当前生效的厂商；显式配置优先，其次按已配凭据自动选。"""
    explicit = _normalize(env("CLOUD_PROVIDER", ""))
    if explicit:
        return explicit
    if env("OSS_BUCKET", "") and env("OSS_ACCESS_KEY_ID", "") and env("OSS_ACCESS_KEY_SECRET", ""):
        return "oss"
    if env("COS_BUCKET", "") and env("COS_SECRET_ID", "") and env("COS_SECRET_KEY", ""):
        return "cos"
    if env("WEIYUN_MCP_TOKEN", ""):
        return "weiyun"
    return "oss" if env("OSS_BUCKET", "") else "weiyun"


def configured_providers() -> list[dict]:
    """给后台下拉框用：每个厂商是否已配齐凭据。"""
    out = []
    for name in ("oss", "cos", "weiyun"):
        keys = PROVIDER_ENV[name]
        ready = all(bool(env(k)) for k in keys)
        out.append({"value": name, "label": PROVIDER_LABELS[name], "ready": ready})
    return out


def make_client(provider: str = ""):
    """返回可用的 CloudBackend；显式指定但缺配置时直接报错，不静默回退。"""
    name = _normalize(provider) or provider_name()
    if name == "oss":
        from .oss import OssClient
        return OssClient()
    if name == "cos":
        from .cos import CosClient
        return CosClient()
    if name == "weiyun":
        from .weiyun import Weiyun
        return Weiyun()
    raise RuntimeError("不支持的云存储厂商：%s" % provider)


def cloud_enabled() -> bool:
    flag = env("JOURNAL_CLOUD_ENABLED", "1").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    name = provider_name()
    return all(bool(env(k)) for k in PROVIDER_ENV.get(name, ()))


def display_name() -> str:
    return PROVIDER_LABELS.get(provider_name(), provider_name())
