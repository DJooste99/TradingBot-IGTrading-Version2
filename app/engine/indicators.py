"""Technical indicators, pure Python.

Each indicator takes candles plus its params and returns a dict of named
series (lists aligned with the candles; ``None`` during warm-up).
"""
from __future__ import annotations

import math
from typing import Callable, Optional

Series = list[Optional[float]]


def _ema(values: Series, period: int) -> Series:
    out: Series = [None] * len(values)
    k = 2 / (period + 1)
    seed: list[float] = []
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == period:
                prev = sum(seed) / period
                out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def _sma(values: Series, period: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if all(w is not None for w in window):
            out[i] = sum(window) / period
    return out


def _wilder(values: Series, period: int) -> Series:
    """Wilder's smoothing (RMA), used by RSI and ATR."""
    out: Series = [None] * len(values)
    seed: list[float] = []
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == period:
                prev = sum(seed) / period
                out[i] = prev
            continue
        prev = (prev * (period - 1) + v) / period
        out[i] = prev
    return out


def ema(candles, params) -> dict[str, Series]:
    closes = [c.close for c in candles]
    out = {}
    if "fast" in params or "slow" in params:
        out["ema_fast"] = _ema(closes, int(params.get("fast", 12)))
        out["ema_slow"] = _ema(closes, int(params.get("slow", 26)))
    else:
        out["ema"] = _ema(closes, int(params.get("period", 20)))
    return out


def sma(candles, params) -> dict[str, Series]:
    return {"sma": _sma([c.close for c in candles], int(params.get("period", 20)))}


def rsi(candles, params) -> dict[str, Series]:
    period = int(params.get("period", 14))
    closes = [c.close for c in candles]
    gains: Series = [None]
    losses: Series = [None]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = _wilder(gains, period), _wilder(losses, period)
    out: Series = []
    for g, l in zip(ag, al):
        if g is None or l is None:
            out.append(None)
        elif l == 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + g / l))
    return {"rsi": out}


def atr(candles, params) -> dict[str, Series]:
    period = int(params.get("period", 14))
    trs: Series = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return {"atr": _wilder(trs, period)}


def macd(candles, params) -> dict[str, Series]:
    closes = [c.close for c in candles]
    fast = _ema(closes, int(params.get("fast", 12)))
    slow = _ema(closes, int(params.get("slow", 26)))
    line: Series = [f - s if f is not None and s is not None else None for f, s in zip(fast, slow)]
    signal = _ema(line, int(params.get("signal", 9)))
    hist: Series = [m - s if m is not None and s is not None else None for m, s in zip(line, signal)]
    return {"macd": line, "macd_signal": signal, "macd_hist": hist}


def bollinger(candles, params) -> dict[str, Series]:
    period = int(params.get("period", 20))
    k = float(params.get("std_dev", 2))
    closes = [c.close for c in candles]
    mid = _sma(closes, period)
    upper: Series = [None] * len(closes)
    lower: Series = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        if mid[i] is None:
            continue
        window = closes[i - period + 1:i + 1]
        sd = math.sqrt(sum((x - mid[i]) ** 2 for x in window) / period)
        upper[i] = mid[i] + k * sd
        lower[i] = mid[i] - k * sd
    return {"bb_upper": upper, "bb_middle": mid, "bb_lower": lower}


INDICATORS: dict[str, Callable] = {
    "ema": ema,
    "sma": sma,
    "rsi": rsi,
    "atr": atr,
    "macd": macd,
    "bollinger": bollinger,
}


def warmup_bars(indicator_specs) -> int:
    """Rough number of candles needed before every indicator is stable."""
    longest = 1
    for spec in indicator_specs:
        for v in spec.params.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v < 500:
                longest = max(longest, int(v))
    if any(s.type == "macd" for s in indicator_specs):
        p = next(s.params for s in indicator_specs if s.type == "macd")
        longest = max(longest, int(p.get("slow", 26)) + int(p.get("signal", 9)))
    # EMA/Wilder smoothing needs several multiples of the period to converge
    return max(60, longest * 4)
