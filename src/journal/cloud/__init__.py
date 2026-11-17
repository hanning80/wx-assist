"""日记的云端存储层（阶段4 模块八）。

对外只暴露两个判断 + 存储实现，具体协议在 weiyun / crypto / sha1stream 里。
"""

from __future__ import annotations

import os


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def cloud_enabled() -> bool:
    from .provider import cloud_enabled as _cloud_enabled
    return _cloud_enabled()


def keep_days() -> int:
    try:
        return int(_env("JOURNAL_KEEP_DAYS", "7"))
    except ValueError:
        return 7
