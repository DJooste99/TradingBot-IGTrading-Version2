"""Offline tests: indicators, rules, risk manager, config validation, engine pipeline and API.

IG and Claude are replaced with fakes, so nothing touches the network.
"""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    from app import config, db
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    for k in ("IG_DEMO_API_KEY", "IG_DEMO_USERNAME", "IG_DEMO_PASSWORD",
              "IG_LIVE_API_KEY", "IG_LIVE_USERNAME", "IG_LIVE_PASSWORD"):
        monkeypatch.setenv(k, "x")
    db.init_db()
    yield


# ---------------------------------------------------------------- indicators
def candles_from(closes, spread=0.5):
    from app.engine.candles import Candle
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Candle(t0 + timedelta(minutes=15 * i), c, c + spread, c - spread, c) for i, c in enumerate(closes)]


def test_ema_and_rsi_basics():
    from app.engine.indicators import ema, rsi
    cs = candles_from([float(i) for i in range(1, 41)])
    out = ema(cs, {"fast": 3, "slow": 5})
    assert out["ema_fast"][2] == pytest.approx(2.0)  # seeded with SMA
    assert out["ema_fast"][-1] > out["ema_slow"][-1]  # uptrend
    assert rsi(cs, {"period": 14})["rsi"][-1] == 100.0  # no losses


def test_bollinger_macd_atr_shapes():
    from app.engine.indicators import atr, bollinger, macd
    cs = candles_from([100 + (i % 7) for i in range(80)])
    bb = bollinger(cs, {"period": 20, "std_dev": 2})
    assert bb["bb_upper"][-1] > bb["bb_middle"][-1] > bb["bb_lower"][-1]
    m = macd(cs, {"fast": 12, "slow": 26, "signal": 9})
    assert m["macd_signal"][-1] is not None
    assert atr(cs, {"period": 14})["atr"][-1] > 0


def test_strategy_fires_on_ema_cross():
    from app import config
    from app.engine.strategy import evaluate
    strat = config.load("indicator").strategies["forex_trend_momentum"]
    closes = [100 - i * 0.2 for i in range(60)] + [88 + i * 0.1 for i in range(40)]
    fired_at = None
    for n in range(40, len(closes) + 1):
        sig = evaluate("EPIC", "forex", "forex_trend_momentum", strat, candles_from(closes[:n]))
        if sig.direction:
            fired_at = n
            break
    assert fired_at is not None
    assert sig.direction == "buy"
    assert all(r["met"] for r in sig.rules)
    assert "atr_stop" in sig.indicator_values


# ---------------------------------------------------------------- config validation
def test_config_rejects_plaintext_and_bad_refs():
    from app import config
    conn = config.load_raw("connection")
    conn["live"]["password"] = "hunter2"
    with pytest.raises(ValueError):
        config.save("connection", conn)
    ind = config.load_raw("indicator")
    ind["asset_class_defaults"]["indices"] = "does_not_exist"
    with pytest.raises(ValueError):
        config.save("indicator", ind)
    risk = config.load_raw("risk")
    risk["max_per_trade"] = 1000
    with pytest.raises(ValueError):
        config.save("risk", risk)
    claude = config.load_raw("claude")
    claude["code_monitor"]["auto_apply_fixes"] = True
    with pytest.raises(ValueError):
        config.save("claude", claude)


# ---------------------------------------------------------------- risk manager
FTSE_MARKET = {
    "instrument": {"name": "FTSE 100", "valueOfOnePip": "1.00", "onePipMeans": "1 Index Point",
                   "marginFactor": 5, "marginFactorUnit": "PERCENTAGE",
                   "currencies": [{"code": "GBP", "isDefault": True, "baseExchangeRate": 1.0}]},
    "snapshot": {"bid": 8000.0, "offer": 8001.0, "marketStatus": "TRADEABLE", "scalingFactor": 1},
    "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 0.1},
                     "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": 8}},
}


def test_risk_sizes_within_caps():
    from app import config
    from app.engine.risk import market_economics, plan_order
    econ = market_economics("IX.D.FTSE.DAILY.IP", FTSE_MARKET)
    strat = config.load("indicator").strategies["shares_breakout"]  # fixed 3%
    plan = plan_order("buy", strat, {}, econ, config.load("risk"), current_exposure=0)
    # 1 unit commits 8001 * 1 * 5% = 400.05 GBP > 100 cap -> 0.24 units
    assert plan.size == 0.24
    assert plan.exposure <= 100
    assert plan.stop_level == pytest.approx(8001 * 0.97)


def test_risk_rejects_when_total_exposure_used():
    from app import config
    from app.engine.risk import RiskRejection, market_economics, plan_order
    econ = market_economics("IX.D.FTSE.DAILY.IP", FTSE_MARKET)
    strat = config.load("indicator").strategies["shares_breakout"]
    with pytest.raises(RiskRejection, match="max_total_exposure"):
        plan_order("buy", strat, {}, econ, config.load("risk"), current_exposure=500)
    with pytest.raises(RiskRejection, match="minimum"):
        plan_order("buy", strat, {}, econ, config.load("risk"), current_exposure=497)


def test_atr_stop_widened_to_broker_minimum():
    from app import config
    from app.engine.risk import market_economics, plan_order
    econ = market_economics("IX.D.FTSE.DAILY.IP", FTSE_MARKET)
    strat = config.load("indicator").strategies["forex_trend_momentum"]
    plan = plan_order("sell", strat, {"atr_stop": {"atr": 2.0}}, econ, config.load("risk"), 0)
    assert plan.stop_loss_used["widened_to_broker_min"]
    assert plan.stop_level == pytest.approx(8000 + 8)


# ---------------------------------------------------------------- engine pipeline
class FakeIG:
    mode = "demo"

    def __init__(self, status="ACCEPTED"):
        self.status = status
        self.orders = []
        self.positions = []

    def get_market(self, epic, max_age=60):
        return FTSE_MARKET

    def get_positions(self):
        return self.positions

    def open_position(self, epic, direction, size, currency, stop_level, limit_level):
        self.orders.append((epic, direction, size, stop_level))
        if self.status != "ACCEPTED":
            return {"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS", "dealReference": "R1"}
        self.positions.append({"position": {"dealId": "D1", "size": size, "level": 8001.0, "direction": "BUY"},
                               "market": {"epic": epic}})
        return {"dealStatus": "ACCEPTED", "dealId": "D1", "dealReference": "R1", "level": 8001.0, "size": size}

    def get_activity(self, since):
        return [{"date": "2026-01-02T10:00:00", "details": {"level": 8050.0,
                 "actions": [{"actionType": "POSITION_CLOSED", "affectedDealId": "D1"}]}}]

    def get_transactions(self, since):
        return [{"openLevel": "8001", "closeLevel": "8050", "profitAndLoss": "£11.76"}]


def make_signal(direction="buy"):
    from app import config
    from app.engine.strategy import Signal
    strat = config.load("indicator").strategies["shares_breakout"]
    return Signal("IX.D.FTSE.DAILY.IP", "indices", "shares_breakout", strat, direction,
                  rules=[{"condition": "x", "met": True}], indicator_values={}, price=8000.5,
                  candle_time="2026-01-01T10:00:00+00:00")


def test_pipeline_claude_confirm_opens_trade_then_closes(monkeypatch):
    from app import db
    from app.claude import service
    from app.engine.trader import TradingEngine
    monkeypatch.setattr(service, "decide", lambda *a, **k: service.Decision("buy", "confirm", "high", "ok", "calm"))
    reviewed = []
    eng = TradingEngine()
    monkeypatch.setattr(eng, "schedule_post_trade_review", lambda tid, mode: reviewed.append(tid))
    ig = FakeIG()
    tid = eng.handle_signal(ig, "demo", make_signal(), {})
    t = db.get_trade(tid)
    assert t["status"] == "open" and t["deal_id"] == "D1" and t["size"] == 0.24
    assert t["indicators_used"]["candle_time"] == "2026-01-01T10:00:00+00:00"
    assert db.signal_already_logged("IX.D.FTSE.DAILY.IP", "shares_breakout", "2026-01-01T10:00:00+00:00", "demo")

    ig.positions = []  # position closed at IG
    monkeypatch.setattr(eng, "ig", lambda: ig)
    eng.sync_positions("demo")
    t = db.get_trade(tid)
    assert t["status"] == "closed" and t["exit_price"] == 8050.0 and t["profit_loss"] == 11.76
    assert reviewed == [tid]


def test_pipeline_low_confidence_is_skip(monkeypatch):
    from app import db
    from app.claude import service
    from app.engine.trader import TradingEngine

    class FakeResp:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text":
                   '```json\n{"decision":"confirm","confidence":"low","reasoning":"meh","news_summary":"none"}\n```'})()]

    class FakeMessages:
        def create(self, **kw):
            assert kw["tools"][0]["type"] == "web_search_20260209"
            return FakeResp()

    monkeypatch.setattr(service, "client", lambda: type("C", (), {"messages": FakeMessages()})())
    ig = FakeIG()
    tid = TradingEngine().handle_signal(ig, "demo", make_signal(), {})
    t = db.get_trade(tid)
    assert t["claude_final_direction"] == "skipped" and t["status"] == "skipped"
    assert ig.orders == []
    assert db.skipped_vs_suggested("demo")[0]["id"] == tid


def test_pipeline_broker_rejection_is_reported(monkeypatch):
    from app import db
    from app.claude import monitor, service
    from app.engine.trader import TradingEngine
    monkeypatch.setattr(service, "decide", lambda *a, **k: service.Decision("sell", "reverse", "high", "r", None))
    reports = []
    monkeypatch.setattr(monitor, "report", lambda *a, **k: reports.append(a))
    tid = TradingEngine().handle_signal(FakeIG(status="REJECTED"), "demo", make_signal(), {})
    assert db.get_trade(tid)["status"] == "rejected_broker"
    assert reports and reports[0][0] == "order_rejection"


# ---------------------------------------------------------------- API
def test_api_mode_switch_gate():
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/status").json()["mode"] == "demo"
        assert c.post("/api/mode", json={"mode": "live"}).status_code == 400  # needs confirm
        r = c.post("/api/mode", json={"mode": "live", "confirm": True})
        assert r.status_code == 403  # gate not cleared
        r = c.post("/api/mode", json={"mode": "live", "confirm": True, "override": True})
        assert r.status_code == 200 and config.active_mode() == "live"
        assert c.post("/api/mode", json={"mode": "demo"}).json()["changed"]
        assert c.put("/api/config/connection", json={}).status_code == 403
        bad = config.load_raw("risk") | {"max_per_trade": -1}
        assert c.put("/api/config/risk", json=bad).status_code == 422
        assert c.put("/api/assets/enabled", json={"enabled_classes": ["forex"]}).status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/api/summary").json()["closed_trades"] == 0
