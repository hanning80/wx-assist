"""模型用量账本：把 data/ai_usage.csv 算成钱。

口径写在代码里，随时可改：
- 单位：元 / 百万 tokens，deepseek-flash 官方价
- 闲时：输入 1，输出 4；高峰：输入 2，输出 8
- 高峰 = 工作日（周一至周五）9:00-12:00、14:00-18:00，其余算闲时
（缓存命中更便宜，账本没记缓存命中，这里按未命中保守算，只会多算不会少算。）
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

USAGE_CSV = Path(__file__).resolve().parents[1] / "data" / "ai_usage.csv"

PRICE = {
    "idle": {"in": 1.0, "out": 4.0},
    "peak": {"in": 2.0, "out": 8.0},
}


def _is_peak(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    return (9 <= dt.hour < 12) or (14 <= dt.hour < 18)


def _read() -> list[dict]:
    if not USAGE_CSV.exists():
        return []
    rows = []
    with USAGE_CSV.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.fromisoformat(r["时间"])
                pin = int(r["输入tokens"] or 0)
                pout = int(r["输出tokens"] or 0)
            except Exception:  # noqa: BLE001
                continue
            rows.append({"at": dt, "model": r.get("模型", ""), "use": r.get("用途", ""),
                         "in": pin, "out": pout})
    return rows


def _cost(rows: list[dict]) -> float:
    total = 0.0
    for r in rows:
        p = PRICE["peak" if _is_peak(r["at"]) else "idle"]
        total += r["in"] / 1_000_000 * p["in"] + r["out"] / 1_000_000 * p["out"]
    return round(total, 4)


def usage_summary(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    rows = _read()
    today = date.today()
    d7 = [r for r in rows if (today - r["at"].date()).days < 7]
    d30 = [r for r in rows if (today - r["at"].date()).days < 30]
    t0 = [r for r in rows if r["at"].date() == today]

    by_use: dict[str, dict] = {}
    for r in d30:
        k = r["use"].split("-")[0] or "未标注"
        v = by_use.setdefault(k, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
        v["calls"] += 1
        v["in"] += r["in"]
        v["out"] += r["out"]
        p = PRICE["peak" if _is_peak(r["at"]) else "idle"]
        v["cost"] = round(v["cost"] + r["in"] / 1e6 * p["in"] + r["out"] / 1e6 * p["out"], 4)

    recent = [{
        "time": r["at"].strftime("%m-%d %H:%M"),
        "use": r["use"],
        "in": r["in"],
        "out": r["out"],
        "cost": round(r["in"] / 1e6 * PRICE["peak" if _is_peak(r["at"]) else "idle"]["in"]
                      + r["out"] / 1e6 * PRICE["peak" if _is_peak(r["at"]) else "idle"]["out"], 4),
    } for r in rows[-8:]][::-1]

    return {
        "currency": "元",
        "price": PRICE,
        "today": {"calls": len(t0), "in": sum(r["in"] for r in t0),
                  "out": sum(r["out"] for r in t0), "cost": _cost(t0)},
        "days7": {"calls": len(d7), "in": sum(r["in"] for r in d7),
                  "out": sum(r["out"] for r in d7), "cost": _cost(d7)},
        "days30": {"calls": len(d30), "in": sum(r["in"] for r in d30),
                   "out": sum(r["out"] for r in d30), "cost": _cost(d30)},
        "total": {"calls": len(rows), "cost": _cost(rows),
                  "since": rows[0]["at"].strftime("%Y-%m-%d %H:%M") if rows else ""},
        "by_use": sorted(by_use.items(), key=lambda kv: -kv[1]["cost"]),
        "recent": recent,
    }
