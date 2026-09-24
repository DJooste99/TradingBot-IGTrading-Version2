"""Candle model and a per-instrument cache that keeps IG price-history usage low.

IG's historical price allowance is small (especially on demo), so the cache
fetches a full warm-up window once and then only the last few bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# design timeframe -> (IG resolution, bar length)
TIMEFRAMES: dict[str, tuple[str, timedelta]] = {
    "1m": ("MINUTE", timedelta(minutes=1)),
    "2m": ("MINUTE_2", timedelta(minutes=2)),
    "3m": ("MINUTE_3", timedelta(minutes=3)),
    "5m": ("MINUTE_5", timedelta(minutes=5)),
    "10m": ("MINUTE_10", timedelta(minutes=10)),
    "15m": ("MINUTE_15", timedelta(minutes=15)),
    "30m": ("MINUTE_30", timedelta(minutes=30)),
    "1h": ("HOUR", timedelta(hours=1)),
    "2h": ("HOUR_2", timedelta(hours=2)),
    "3h": ("HOUR_3", timedelta(hours=3)),
    "4h": ("HOUR_4", timedelta(hours=4)),
    "1d": ("DAY", timedelta(days=1)),
    "1w": ("WEEK", timedelta(weeks=1)),
}


@dataclass
class Candle:
    time: datetime  # bar open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None


def _mid(p: dict) -> Optional[float]:
    bid, ask, last = p.get("bid"), p.get("ask"), p.get("lastTraded")
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return last if last is not None else (bid if bid is not None else ask)


def _parse_time(p: dict) -> datetime:
    raw = p.get("snapshotTimeUTC") or p.get("snapshotTime")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y:%m:%d-%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognised IG snapshot time {raw!r}")


def parse_ig_prices(prices: list[dict]) -> list[Candle]:
    out = []
    for p in prices:
        o, h, l, c = (_mid(p.get(k) or {}) for k in ("openPrice", "highPrice", "lowPrice", "closePrice"))
        if None in (o, h, l, c):
            continue
        out.append(Candle(_parse_time(p), o, h, l, c, p.get("lastTradedVolume")))
    return out


def _to_row(c: Candle) -> dict:
    return {"time": c.time.isoformat(), "open": c.open, "high": c.high, "low": c.low, "close": c.close,
            "volume": c.volume}


def _from_row(r: dict) -> Candle:
    return Candle(datetime.fromisoformat(r["time"]), r["open"], r["high"], r["low"], r["close"], r.get("volume"))


class CandleCache:
    """Keeps candles in memory and SQLite so IG history is only ever fetched once.

    A cold start (no stored candles) fetches the full warm-up window. After that
    each fetch asks IG only for bars from the last stored one onwards, which is
    normally 2 data points: the bar that just closed and the one now forming.
    """

    def __init__(self, ig_client_getter):
        self._get_ig = ig_client_getter
        self._data: dict[tuple[str, str, str], list[Candle]] = {}

    def clear(self) -> None:
        self._data.clear()

    def closed_candles(self, epic: str, timeframe: str, needed: int) -> list[Candle]:
        """Return up to ``needed`` fully closed candles, oldest first."""
        from app import db

        ig = self._get_ig()
        resolution, length = TIMEFRAMES[timeframe]
        key = (ig.mode, epic, resolution)
        have = self._data.get(key)
        if have is None:
            have = [_from_row(r) for r in db.load_candles(ig.mode, epic, resolution, needed + 5)]

        now = datetime.now(timezone.utc)
        if len(have) < needed:
            raw = ig.get_prices(epic, resolution, needed + 2, high_priority=True)
        else:
            elapsed = int((now - have[-1].time) / length)
            estimate = min(needed + 2, max(1, elapsed) + 1)
            raw = ig.get_prices(epic, resolution, estimate, since=have[-1].time)
        fresh = parse_ig_prices(raw)

        merged = {c.time: c for c in have}
        for c in fresh:
            merged[c.time] = c
        candles = sorted(merged.values(), key=lambda c: c.time)[-(needed + 5):]
        self._data[key] = candles
        if fresh:
            db.save_candles(ig.mode, epic, resolution, [_to_row(c) for c in fresh], keep=needed + 5)
        return [c for c in candles if c.time + length <= now][-needed:]
