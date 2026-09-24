"""Named entry conditions evaluated against indicator values.

Each condition gets an ``EvalContext`` and the rule, and checks the last
closed candle (index -1) against the one before it (index -2) where a cross
is involved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class EvalContext:
    candles: list            # closed candles, oldest first
    values: dict             # indicator key -> {series name -> list}
    types: dict              # indicator key -> indicator type

    def series(self, name: str, source: Optional[str] = None) -> list:
        """Find a named output series, optionally from a specific indicator id."""
        if source:
            if source not in self.values or name not in self.values[source]:
                raise KeyError(f"indicator {source!r} has no output {name!r}")
            return self.values[source][name]
        for outputs in self.values.values():
            if name in outputs:
                return outputs[name]
        raise KeyError(f"no indicator in this strategy produces {name!r}")

    def last(self, name: str, source: Optional[str] = None, back: int = 1) -> Optional[float]:
        s = self.series(name, source)
        return s[-back] if len(s) >= back else None

    @property
    def price(self) -> float:
        return self.candles[-1].close


def _cross_above(ctx, a, b, src=None) -> bool:
    a1, a0, b1, b0 = ctx.last(a, src), ctx.last(a, src, 2), ctx.last(b, src), ctx.last(b, src, 2)
    if None in (a1, a0, b1, b0):
        return False
    return a0 <= b0 and a1 > b1


def _gt(x, y) -> bool:
    return x is not None and y is not None and x > y


def _need_value(rule) -> float:
    if rule.value is None:
        raise ValueError(f"condition {rule.condition} needs a 'value'")
    return rule.value


CONDITIONS: dict[str, Callable[[EvalContext, object], bool]] = {
    # EMA
    "ema_fast_crosses_above_ema_slow": lambda c, r: _cross_above(c, "ema_fast", "ema_slow", r.source),
    "ema_fast_crosses_below_ema_slow": lambda c, r: _cross_above(c, "ema_slow", "ema_fast", r.source),
    "ema_fast_above_ema_slow": lambda c, r: _gt(c.last("ema_fast", r.source), c.last("ema_slow", r.source)),
    "ema_fast_below_ema_slow": lambda c, r: _gt(c.last("ema_slow", r.source), c.last("ema_fast", r.source)),
    "price_above_ema": lambda c, r: _gt(c.price, c.last("ema", r.source)),
    "price_below_ema": lambda c, r: _gt(c.last("ema", r.source), c.price),
    "price_above_sma": lambda c, r: _gt(c.price, c.last("sma", r.source)),
    "price_below_sma": lambda c, r: _gt(c.last("sma", r.source), c.price),
    # RSI
    "rsi_below": lambda c, r: _gt(_need_value(r), c.last("rsi", r.source)),
    "rsi_above": lambda c, r: _gt(c.last("rsi", r.source), _need_value(r)),
    # Bollinger
    "price_above_upper_band": lambda c, r: _gt(c.price, c.last("bb_upper", r.source)),
    "price_below_lower_band": lambda c, r: _gt(c.last("bb_lower", r.source), c.price),
    # MACD
    "macd_bullish_cross": lambda c, r: _cross_above(c, "macd", "macd_signal", r.source),
    "macd_bearish_cross": lambda c, r: _cross_above(c, "macd_signal", "macd", r.source),
    "macd_above_signal": lambda c, r: _gt(c.last("macd", r.source), c.last("macd_signal", r.source)),
    "macd_below_signal": lambda c, r: _gt(c.last("macd_signal", r.source), c.last("macd", r.source)),
    # ATR
    "atr_above": lambda c, r: _gt(c.last("atr", r.source), _need_value(r)),
    "atr_below": lambda c, r: _gt(_need_value(r), c.last("atr", r.source)),
}
