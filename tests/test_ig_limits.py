"""IG API limit guard: request windows, historical allowance, candle reuse, closed-market behaviour."""
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_bot import FTSE_MARKET, isolated  # noqa: F401  (autouse fixture)


def test_rate_gate_caps_requests_and_reserves_priority(monkeypatch):
    from app import ig_limits
    monkeypatch.setattr(ig_limits.RateGate, "_registry", {})
    gate = ig_limits.RateGate("key-a", "acct-a")
    cap = int(30 * ig_limits.SAFETY_FACTOR)  # account non-trading is the tightest
    normal = cap - ig_limits.HIGH_PRIORITY_RESERVE
    for _ in range(normal):
        gate.acquire(trading=False, high_priority=False)
    with pytest.raises(ig_limits.RateLimited):
        gate.acquire(trading=False, high_priority=False, timeout=0.1)
    for _ in range(ig_limits.HIGH_PRIORITY_RESERVE):  # reserve still available for confirms etc.
        gate.acquire(trading=False, high_priority=True)
    with pytest.raises(ig_limits.RateLimited):
        gate.acquire(trading=False, high_priority=True, timeout=0.1)
    assert gate.usage()["account_non_trading"]["used_last_minute"] == cap
    gate.acquire(trading=True, high_priority=True)  # trading budget is separate


def test_rate_gate_shared_across_clients(monkeypatch):
    from app import ig_limits
    monkeypatch.setattr(ig_limits.RateGate, "_registry", {})
    a, b = ig_limits.RateGate("same-key", "acct-1"), ig_limits.RateGate("same-key", "acct-2")
    assert a.non_trading[0] is b.non_trading[0]      # same API key -> same app window
    assert a.non_trading[1] is not b.non_trading[1]  # different accounts


def test_historical_allowance_tracks_ig_metadata_and_persists():
    from app import ig_limits
    al = ig_limits.HistoricalAllowance("acct")
    al.check(10_000)  # unknown: allowed, IG will report
    al.update({"allowance": {"remainingAllowance": 400, "totalAllowance": 10000, "allowanceExpiry": 3600}}, 5)
    al.check(90)  # 400 - 90 >= reserve 300
    with pytest.raises(ig_limits.RateLimited, match="allowance low"):
        al.check(150)
    al.check(150, high_priority=True)  # warm-ups may dip into the reserve
    again = ig_limits.HistoricalAllowance("acct")  # survives restart
    assert again.remaining == 400 and again.status()["resets_in_h"] == 1.0


def test_exceeded_error_code_blocks_budget(monkeypatch):
    from app import ig_client, ig_limits
    monkeypatch.setattr(ig_limits.RateGate, "_registry", {})
    client = ig_client.IGClient("demo")

    class Resp:
        status_code = 403
        content = b"x"
        text = "exceeded"
        headers = {}

        def json(self):
            return {"errorCode": "error.public-api.exceeded-account-historical-data-allowance"}

    with pytest.raises(ig_limits.RateLimited):
        client._check_exceeded(Resp())
    assert client.allowance.remaining == 0
    with pytest.raises(ig_limits.RateLimited):
        client.allowance.check(1)


class PriceIG:
    mode = "demo"

    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def get_prices(self, epic, resolution, max_bars, high_priority=False, since=None):
        self.calls.append({"max": max_bars, "since": since, "high": high_priority})
        rows = [b for b in self.bars if since is None or b[0] >= since][-max_bars:]
        return [{"snapshotTimeUTC": t.strftime("%Y-%m-%dT%H:%M:%S"),
                 **{k: {"bid": v, "ask": v} for k in ("openPrice", "highPrice", "lowPrice", "closePrice")}}
                for t, v in rows]


def test_candle_cache_warms_once_then_fetches_only_new_bars():
    from app.engine.candles import CandleCache
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % 15)
    bars = [(now - timedelta(minutes=15 * i), 100.0 + i) for i in range(200, -1, -1)]
    ig = PriceIG(bars)
    cache = CandleCache(lambda: ig)
    first = cache.closed_candles("EPIC", "15m", 60)
    assert len(first) == 60 and ig.calls[0] == {"max": 62, "since": None, "high": True}

    cache.closed_candles("EPIC", "15m", 60)
    assert ig.calls[1]["since"] is not None and ig.calls[1]["max"] <= 2  # incremental, tiny

    restarted = CandleCache(lambda: ig)  # new process: reads SQLite instead of re-downloading
    restarted.closed_candles("EPIC", "15m", 60)
    assert ig.calls[2]["since"] is not None and not ig.calls[2]["high"]


def test_closed_market_costs_no_price_history(monkeypatch):
    from app import config
    from app.engine.trader import TradingEngine
    closed = {**FTSE_MARKET, "snapshot": {**FTSE_MARKET["snapshot"], "marketStatus": "CLOSED"}}

    class ClosedIG:
        mode = "demo"
        market_calls = 0

        def get_market(self, epic, max_age=60):
            ClosedIG.market_calls += 1
            return closed

        def get_prices(self, *a, **k):
            raise AssertionError("must not fetch prices while the market is closed")

    eng = TradingEngine()
    strat = config.load("indicator").strategies["forex_trend_momentum"]
    info = {}
    for _ in range(5):  # five engine cycles over a weekend
        eng._process_instrument(ClosedIG(), "demo", "EPIC", "forex", "forex_trend_momentum", strat, info)
    assert ClosedIG.market_calls == 1  # then waits MARKET_CLOSED_RECHECK before asking again
    assert "CLOSED" in info["note"]


def test_config_rejected_when_projection_exceeds_allowance():
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app
    ind = config.load_raw("indicator")
    ind["strategies"]["forex_trend_momentum"]["timeframe"] = "1m"  # 14,400 points/week for one epic
    with TestClient(app) as c:
        r = c.put("/api/config/indicator", json=ind)
        assert r.status_code == 422 and "historical data points" in r.json()["detail"]
        ind["strategies"]["forex_trend_momentum"]["timeframe"] = "15m"
        assert c.put("/api/config/indicator", json=ind).status_code == 200


def test_warmup_ignores_thresholds():
    from app import config
    from app.engine.indicators import warmup_bars
    strat = config.load("indicator").strategies["forex_trend_momentum"]
    assert warmup_bars(strat.indicators) == 78  # 26 (slow EMA) * 3, not RSI overbought=70 * 4
