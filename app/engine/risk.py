"""Risk manager: position sizing, exposure caps and stop-loss placement.

This sits downstream of every decision (technical or Claude) and is never
bypassed. Claude only ever supplies a direction; size, stop and caps are
computed here from ``risk_config.json`` and the strategy's stop-loss block.

Exposure = margin committed to a position, in account currency:
    size * price * value_per_point * margin_factor / fx
It fails closed: if IG's market data lacks what's needed to compute it,
the trade is rejected rather than guessed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from app.config import RiskConfig, StopLoss, Strategy


class RiskRejection(Exception):
    pass


@dataclass
class MarketEconomics:
    epic: str
    bid: float
    offer: float
    status: str
    currency: str
    value_per_point: float        # instrument currency per 1 quoted-price unit per 1 size
    margin_factor: float          # fraction, e.g. 0.0333
    fx_to_account: float          # divide instrument-currency amounts by this
    min_size: float
    min_stop_distance: float      # quoted-price units
    raw: dict = field(default_factory=dict, repr=False)

    def exposure(self, size: float, price: float) -> float:
        return size * price * self.value_per_point * self.margin_factor / self.fx_to_account


@dataclass
class OrderPlan:
    direction: str
    size: float
    entry_price: float
    stop_level: float
    limit_level: Optional[float]
    exposure: float
    stop_loss_used: dict


def _num(text) -> Optional[float]:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else None


def market_economics(epic: str, market: dict) -> MarketEconomics:
    inst = market.get("instrument", {}) or {}
    snap = market.get("snapshot", {}) or {}
    rules = market.get("dealingRules", {}) or {}

    bid, offer = _num(snap.get("bid")), _num(snap.get("offer"))
    if bid is None or offer is None:
        raise RiskRejection(f"{epic}: no live bid/offer")

    pip_value = _num(inst.get("valueOfOnePip"))
    pip_size = _num(inst.get("onePipMeans"))
    scaling = _num(snap.get("scalingFactor")) or 1.0
    if not pip_value or not pip_size:
        raise RiskRejection(f"{epic}: IG market data lacks valueOfOnePip/onePipMeans; cannot size safely")
    pip_in_quote_units = pip_size * scaling if pip_size < 1 else pip_size
    value_per_point = pip_value / pip_in_quote_units

    unit = (inst.get("marginFactorUnit") or "PERCENTAGE").upper()
    mf = _num(inst.get("marginFactor"))
    if mf is None or unit != "PERCENTAGE":
        raise RiskRejection(f"{epic}: unsupported margin factor {mf} {unit}")

    currencies = inst.get("currencies") or []
    cur = next((c for c in currencies if c.get("isDefault")), currencies[0] if currencies else {})
    fx = _num(cur.get("baseExchangeRate")) or 1.0

    min_size = _num((rules.get("minDealSize") or {}).get("value")) or 0.01
    stop_rule = rules.get("minNormalStopOrLimitDistance") or {}
    min_stop = _num(stop_rule.get("value")) or 0.0
    if (stop_rule.get("unit") or "POINTS").upper() == "PERCENTAGE":
        min_stop = (bid + offer) / 2 * min_stop / 100

    return MarketEconomics(
        epic=epic, bid=bid, offer=offer, status=snap.get("marketStatus", "UNKNOWN"),
        currency=cur.get("code", "GBP"), value_per_point=value_per_point, margin_factor=mf / 100,
        fx_to_account=fx, min_size=min_size, min_stop_distance=min_stop, raw=market,
    )


def resolve_stop_loss(strategy: Strategy, risk: RiskConfig) -> StopLoss:
    return strategy.stop_loss or risk.default_stop_loss


def plan_order(direction: str, strategy: Strategy, signal_values: dict, econ: MarketEconomics,
               risk: RiskConfig, current_exposure: float) -> OrderPlan:
    if direction not in ("buy", "sell"):
        raise RiskRejection(f"invalid direction {direction!r}")
    if econ.status != "TRADEABLE":
        raise RiskRejection(f"market status is {econ.status}")

    entry = econ.offer if direction == "buy" else econ.bid
    sl = resolve_stop_loss(strategy, risk)

    if sl.mode == "fixed_pct":
        distance = entry * sl.value / 100
        used = {"mode": "fixed_pct", "pct": sl.value}
    else:
        atr_block = signal_values.get(sl.atr_source) or {}
        atr_value = atr_block.get("atr")
        if not atr_value or atr_value <= 0:
            raise RiskRejection(f"ATR source {sl.atr_source!r} has no value yet")
        distance = atr_value * sl.atr_multiplier
        used = {"mode": "atr_dynamic", "atr_value": atr_value, "multiplier": sl.atr_multiplier}

    if distance < econ.min_stop_distance:
        used["widened_to_broker_min"] = True
        used["requested_distance"] = distance
        distance = econ.min_stop_distance
    stop = entry - distance if direction == "buy" else entry + distance
    used["stop_distance"] = distance
    used["stop_price"] = stop

    limit = None
    if risk.take_profit_pct:
        tp = entry * risk.take_profit_pct / 100
        limit = entry + tp if direction == "buy" else entry - tp
        used["take_profit_price"] = limit

    remaining = risk.max_total_exposure - current_exposure
    budget = min(risk.max_per_trade, remaining)
    if budget <= 0:
        raise RiskRejection(f"max_total_exposure reached ({current_exposure:.2f}/{risk.max_total_exposure:.2f})")

    per_unit = econ.exposure(1.0, entry)
    if per_unit <= 0:
        raise RiskRejection("could not compute exposure per unit")
    size = math.floor(budget / per_unit * 100) / 100
    if size < econ.min_size:
        raise RiskRejection(
            f"budget {budget:.2f} {risk.currency} buys size {size} < IG minimum {econ.min_size} "
            f"(one unit commits {per_unit:.2f})")

    exposure = econ.exposure(size, entry)
    # Final hard checks, independent of the arithmetic above.
    if exposure > risk.max_per_trade + 1e-9:
        raise RiskRejection(f"exposure {exposure:.2f} exceeds max_per_trade {risk.max_per_trade}")
    if current_exposure + exposure > risk.max_total_exposure + 1e-9:
        raise RiskRejection(f"exposure would exceed max_total_exposure {risk.max_total_exposure}")

    return OrderPlan(direction=direction, size=size, entry_price=entry, stop_level=stop, limit_level=limit,
                     exposure=exposure, stop_loss_used=used)


def open_exposure(ig, positions: list[dict], econ_getter) -> float:
    """Sum exposure of every open position on the account (not just the bot's)."""
    total = 0.0
    for p in positions:
        pos, mkt = p.get("position", {}), p.get("market", {})
        epic = mkt.get("epic")
        try:
            econ = econ_getter(epic)
        except RiskRejection:
            # Unknown economics: count the worst case so we never under-count exposure.
            return float("inf")
        total += econ.exposure(float(pos.get("size", 0)), float(pos.get("level", 0)))
    return total
