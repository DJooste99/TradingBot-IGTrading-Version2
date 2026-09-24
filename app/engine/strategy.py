"""Technical strategy engine: turns candles + a strategy config into a candidate signal."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.config import AssetConfig, IndicatorConfig, Strategy
from app.engine.conditions import CONDITIONS, EvalContext
from app.engine.indicators import INDICATORS


@dataclass
class Signal:
    epic: str
    asset_class: str
    strategy_name: str
    strategy: Strategy
    direction: Optional[str]            # 'buy' / 'sell' when rules fired, else None
    rules: list[dict] = field(default_factory=list)
    indicator_values: dict = field(default_factory=dict)  # latest value of every indicator output
    price: float = 0.0
    candle_time: str = ""


def watched_instruments(assets: AssetConfig, indicators: IndicatorConfig) -> list[tuple[str, str, str]]:
    """(epic, asset_class, strategy_name) for every epic that should trade right now."""
    out = []
    for cls in assets.enabled_classes:
        for epic in assets.instruments.get(cls, []):
            strat = indicators.instrument_overrides.get(epic) or indicators.asset_class_defaults.get(cls)
            if strat:
                out.append((epic, cls, strat))
    return out


def _round(v):
    return None if v is None else round(v, 6)


def evaluate(epic: str, asset_class: str, strategy_name: str, strategy: Strategy, candles) -> Signal:
    values, types = {}, {}
    for spec in strategy.indicators:
        values[spec.key] = INDICATORS[spec.type](candles, spec.params)
        types[spec.key] = spec.type
    ctx = EvalContext(candles=candles, values=values, types=types)

    rule_results = []
    for rule in strategy.entry_rules:
        ok = bool(CONDITIONS[rule.condition](ctx, rule))
        rule_results.append({"condition": rule.condition, "value": rule.value, "met": ok})

    met = [r["met"] for r in rule_results]
    fired = all(met) if strategy.rule_logic == "all" else any(met)

    snapshot = {
        key: {"type": types[key], "params": next(s.params for s in strategy.indicators if s.key == key),
              **{name: _round(series[-1]) for name, series in outputs.items()},
              **{f"{name}_prev": _round(series[-2]) for name, series in outputs.items() if len(series) > 1}}
        for key, outputs in values.items()
    }
    return Signal(
        epic=epic, asset_class=asset_class, strategy_name=strategy_name, strategy=strategy,
        direction=strategy.direction if fired else None,
        rules=rule_results, indicator_values=snapshot,
        price=candles[-1].close, candle_time=candles[-1].time.isoformat(),
    )
