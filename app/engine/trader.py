"""Trading engine: candles -> strategy signal -> Claude decision -> risk manager -> IG order.

Runs in a background thread. Config files are re-read every cycle so dashboard
edits take effect without a restart; a change of ``active_mode`` rebuilds the
IG client with the other environment's credentials.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic

from app import config, db
from app.claude import monitor, service
from app.engine.candles import TIMEFRAMES, CandleCache
from app.engine.indicators import warmup_bars
from app.engine.risk import RiskRejection, market_economics, open_exposure, plan_order
from app.engine.strategy import evaluate, watched_instruments
from app.ig_client import IGClient, IGError

log = logging.getLogger(__name__)

EXPECTED_REJECTIONS = {"MARKET_CLOSED", "MARKET_CLOSED_WITH_EDITS", "MARKET_OFFLINE"}
REVIEW_INTERVALS = {"hourly": timedelta(hours=1), "daily": timedelta(days=1), "weekly": timedelta(weeks=1)}


def _money(text) -> Optional[float]:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).replace(",", "")
    neg = "-" in s
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    return -float(m.group()) if neg else float(m.group())


class TradingEngine:
    def __init__(self, poll_seconds: Optional[float] = None):
        self.poll_seconds = poll_seconds or float(os.environ.get("ENGINE_POLL_SECONDS", 30))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ig: Optional[IGClient] = None
        self._ig_lock = threading.Lock()
        self._candles = CandleCache(self.ig)
        self._last_eval: dict[tuple[str, str], datetime] = {}
        self._background = ThreadPoolExecutor(max_workers=2, thread_name_prefix="claude-review")
        self._review_running = threading.Lock()
        self.state: dict = {"running": False, "mode": None, "last_cycle": None, "last_error": None,
                            "instruments": {}}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()
        self.state["running"] = True

    def stop(self) -> None:
        self._stop.set()
        self.state["running"] = False

    def ig(self) -> IGClient:
        mode = config.active_mode()
        with self._ig_lock:
            if self._ig is None or self._ig.mode != mode:
                log.info("Connecting to IG %s", mode)
                self._ig = IGClient(mode)
                self._ig.login()
                self._candles.clear()
                self._last_eval.clear()
            return self._ig

    def reset_connection(self) -> None:
        with self._ig_lock:
            self._ig = None

    def _run(self) -> None:
        log.info("Trading engine started (poll every %ss)", self.poll_seconds)
        while not self._stop.is_set():
            started = time.time()
            try:
                self.cycle()
                self.state["last_error"] = None
            except Exception as e:
                self.state["last_error"] = str(e)
                monitor.report(_trigger_for(e), f"engine cycle failed: {e}", e,
                               {"mode": self.state.get("mode")})
            self.state["last_cycle"] = db.now_iso()
            self._stop.wait(max(1.0, self.poll_seconds - (time.time() - started)))
        log.info("Trading engine stopped")

    # ------------------------------------------------------------ one cycle
    def cycle(self) -> None:
        mode = config.active_mode()
        self.state["mode"] = mode
        ig = self.ig()
        assets, indicators = config.load("asset"), config.load("indicator")

        self.sync_positions(mode)
        self.maybe_periodic_review(mode)

        for epic, asset_class, strat_name in watched_instruments(assets, indicators):
            if self._stop.is_set():
                return
            info = self.state["instruments"].setdefault(epic, {})
            info.update({"asset_class": asset_class, "strategy": strat_name})
            try:
                self._process_instrument(ig, mode, epic, asset_class, strat_name, indicators.strategies[strat_name],
                                         info)
            except Exception as e:
                info["note"] = f"error: {e}"
                monitor.report(_trigger_for(e), f"{epic}/{strat_name}: {e}", e,
                               {"epic": epic, "strategy": strat_name, "mode": mode})

    def _process_instrument(self, ig, mode, epic, asset_class, strat_name, strategy, info) -> None:
        _, length = TIMEFRAMES[strategy.timeframe]
        key = (epic, strat_name)
        last = self._last_eval.get(key)
        now = datetime.now(timezone.utc)
        if last and now < last + 2 * length:
            return  # next bar hasn't closed yet; don't spend IG price allowance

        candles = self._candles.closed_candles(epic, strategy.timeframe, warmup_bars(strategy.indicators))
        if len(candles) < 3:
            info["note"] = "not enough candle history yet"
            return
        latest = candles[-1].time
        if last == latest:
            return
        self._last_eval[key] = latest
        info["last_candle"] = latest.isoformat()

        signal = evaluate(epic, asset_class, strat_name, strategy, candles)
        info["last_rules"] = signal.rules
        if not signal.direction:
            info["note"] = "no signal"
            return
        if db.signal_already_logged(epic, strat_name, signal.candle_time, mode):
            return
        if any(t["instrument"] == epic for t in db.open_trades(mode)):
            info["note"] = f"{signal.direction} signal ignored: position already open"
            return
        self.handle_signal(ig, mode, signal, info)

    # ------------------------------------------------------------ decision -> risk -> order
    def handle_signal(self, ig: IGClient, mode: str, signal, info: dict) -> int:
        market = ig.get_market(signal.epic, max_age=0)
        econ = None
        try:
            econ = market_economics(signal.epic, market)
        except RiskRejection:
            pass  # recorded below if we get as far as sizing
        if econ is not None and econ.status != "TRADEABLE":
            info["note"] = f"{signal.direction} signal ignored: market {econ.status}"
            return 0

        claude_cfg = config.load("claude").decision_engine
        indicators_used = {"candle_time": signal.candle_time, "last_close": signal.price,
                           "rules": signal.rules, "values": signal.indicator_values}
        base = dict(mode=mode, instrument=signal.epic, asset_class=signal.asset_class,
                    strategy=signal.strategy_name, indicator_suggested_direction=signal.direction,
                    indicators_used=indicators_used)

        # 1. Claude decision (final say on direction / whether to trade)
        if claude_cfg.enabled and mode in claude_cfg.run_on:
            try:
                history = db.recent_history(signal.epic, signal.strategy_name, claude_cfg.trade_history_lookback)
                name = (market.get("instrument") or {}).get("name", signal.epic)
                decision = service.decide(signal, history, claude_cfg, name)
            except (anthropic.APIError, service.ClaudeError) as e:
                monitor.report("api_error", f"Claude decision failed for {signal.epic}: {e}", e)
                info["note"] = "Claude unavailable; trade not taken"
                return db.insert_trade(**base, claude_final_direction="skipped", status="error",
                                       status_note=f"Claude decision failed: {e}")
        else:
            decision = service.passthrough(signal, "decision engine disabled for this mode")

        trade_id = db.insert_trade(
            **base, claude_final_direction=decision.final_direction, claude_confidence=decision.confidence,
            claude_reasoning=decision.reasoning, news_summary=decision.news_summary,
            status="skipped" if decision.final_direction == "skipped" else "pending")
        if decision.final_direction == "skipped":
            info["note"] = f"{signal.direction} signal skipped by Claude ({decision.confidence})"
            return trade_id

        # 2. Risk manager (never bypassed)
        risk = config.load("risk")
        try:
            if econ is None:
                econ = market_economics(signal.epic, market)
            current = open_exposure(ig, ig.get_positions(),
                                    lambda e: market_economics(e, ig.get_market(e)))
            plan = plan_order(decision.final_direction, signal.strategy, signal.indicator_values, econ, risk,
                              current)
        except RiskRejection as e:
            db.update_trade(trade_id, status="rejected_risk", status_note=str(e))
            info["note"] = f"risk manager rejected: {e}"
            return trade_id

        db.update_trade(trade_id, size=plan.size, stop_loss_used=plan.stop_loss_used,
                        status_note=f"exposure {plan.exposure:.2f} {risk.currency}")

        # 3. Execute
        try:
            confirm = ig.open_position(signal.epic, plan.direction, plan.size, econ.currency,
                                       plan.stop_level, plan.limit_level)
        except IGError as e:
            db.update_trade(trade_id, status="error", status_note=str(e)[:500])
            monitor.report("api_error", f"order submission failed for {signal.epic}: {e}", e,
                           {"plan": plan.__dict__})
            return trade_id

        if confirm.get("dealStatus") != "ACCEPTED":
            reason = confirm.get("reason", "UNKNOWN")
            db.update_trade(trade_id, status="rejected_broker", status_note=f"IG rejected: {reason}",
                            deal_reference=confirm.get("dealReference"))
            info["note"] = f"IG rejected order: {reason}"
            if reason not in EXPECTED_REJECTIONS:
                monitor.report("order_rejection", f"IG rejected {plan.direction} {signal.epic}: {reason}",
                               context={"plan": plan.__dict__, "confirm": confirm})
            return trade_id

        used = dict(plan.stop_loss_used)
        if confirm.get("stopLevel") is not None:
            used["stop_price"] = confirm["stopLevel"]
        db.update_trade(trade_id, status="open", deal_id=confirm.get("dealId"),
                        deal_reference=confirm.get("dealReference"),
                        entry_price=confirm.get("level", plan.entry_price),
                        size=confirm.get("size", plan.size), stop_loss_used=used)
        info["note"] = f"opened {plan.direction} {plan.size} @ {confirm.get('level')}"
        log.info("Opened trade #%s %s %s %s", trade_id, plan.direction, plan.size, signal.epic)
        return trade_id

    # ------------------------------------------------------------ position lifecycle
    def sync_positions(self, mode: str) -> None:
        open_rows = db.open_trades(mode)
        if not open_rows:
            return
        ig = self.ig()
        live_ids = {p["position"]["dealId"] for p in ig.get_positions()}
        closed = [t for t in open_rows if t["deal_id"] and t["deal_id"] not in live_ids]
        if not closed:
            return
        since = min(t["timestamp"] for t in closed)
        try:
            activities = ig.get_activity(since)
            transactions = ig.get_transactions(since)
        except IGError as e:
            monitor.report("api_error", f"could not fetch IG history for closed trades: {e}", e)
            activities, transactions = [], []
        for trade in closed:
            self._record_close(ig, trade, activities, transactions)

    def _record_close(self, ig, trade: dict, activities: list, transactions: list) -> None:
        exit_price, closed_at = None, None
        for a in activities:
            actions = (a.get("details") or {}).get("actions") or []
            if any(x.get("affectedDealId") == trade["deal_id"] and "CLOSED" in (x.get("actionType") or "")
                   for x in actions):
                exit_price = (a.get("details") or {}).get("level") or a.get("level")
                closed_at = a.get("date")
                break

        pnl = None
        if exit_price is not None:
            for t in transactions:
                if (_money(t.get("closeLevel")) == float(exit_price)
                        and abs((_money(t.get("openLevel")) or 0) - (trade["entry_price"] or 0)) < 1e-6):
                    pnl = _money(t.get("profitAndLoss"))
                    break
        note = None
        if pnl is None and exit_price is not None and trade["entry_price"] is not None:
            try:
                econ = market_economics(trade["instrument"], ig.get_market(trade["instrument"]))
                sign = 1 if trade["claude_final_direction"] == "buy" else -1
                pnl = round(sign * (float(exit_price) - trade["entry_price"]) * trade["size"]
                            * econ.value_per_point / econ.fx_to_account, 2)
                note = "P/L estimated from prices; IG transaction not matched"
            except RiskRejection:
                pass

        db.update_trade(trade["id"], status="closed", exit_price=exit_price, profit_loss=pnl,
                        closed_at=closed_at or db.now_iso(),
                        **({"status_note": note} if note else {}))
        log.info("Trade #%s closed: exit %s P/L %s", trade["id"], exit_price, pnl)
        self.schedule_post_trade_review(trade["id"], trade["mode"])

    def close_trade(self, trade_id: int) -> dict:
        trade = db.get_trade(trade_id)
        if not trade or trade["status"] != "open":
            raise ValueError("trade is not open")
        ig = self.ig()
        if trade["mode"] != ig.mode:
            raise ValueError(f"trade belongs to {trade['mode']} but active mode is {ig.mode}")
        confirm = ig.close_position(trade["deal_id"], trade["claude_final_direction"], trade["size"])
        if confirm.get("dealStatus") != "ACCEPTED":
            raise ValueError(f"IG rejected close: {confirm.get('reason')}")
        self.sync_positions(trade["mode"])
        return confirm

    # ------------------------------------------------------------ reviews
    def schedule_post_trade_review(self, trade_id: int, mode: str) -> None:
        cfg = config.load("claude")
        if not (cfg.post_trade_review.enabled and mode in cfg.post_trade_review.run_on):
            return
        self._background.submit(self._post_trade_review, trade_id, cfg.decision_engine.model)

    def _post_trade_review(self, trade_id: int, model: str) -> None:
        try:
            note = service.post_trade_review(db.get_trade(trade_id), model)
            db.update_trade(trade_id, claude_post_trade_note=note)
        except Exception as e:
            monitor.report(_trigger_for(e), f"post-trade review failed for trade #{trade_id}: {e}", e)

    def maybe_periodic_review(self, mode: str) -> None:
        cfg = config.load("claude").periodic_review
        if not (cfg.enabled and mode in cfg.run_on):
            return
        last = db.last_periodic_review(mode)
        if last and datetime.fromisoformat(last["timestamp"]) + REVIEW_INTERVALS[cfg.frequency] > datetime.now(timezone.utc):
            return
        if not db.closed_trades(mode, limit=1):
            return
        self._background.submit(self.run_periodic_review, mode)

    def run_periodic_review(self, mode: str) -> Optional[int]:
        if not self._review_running.acquire(blocking=False):
            return None
        try:
            cfg = config.load("claude")
            trades = db.closed_trades(mode, limit=cfg.periodic_review.lookback_trades)
            if not trades:
                return None
            skipped = db.skipped_vs_suggested(mode, limit=cfg.periodic_review.lookback_trades)
            configs = {n: config.load_raw(n) for n in ("indicator", "risk", "asset")}
            result = service.periodic_review(trades, skipped, configs, cfg.decision_engine.model)
            return db.insert_periodic_review(
                period_start=min(t["timestamp"] for t in trades), period_end=max(t["closed_at"] for t in trades),
                trades_reviewed=len(trades), summary=result["summary"],
                flagged_issues=result["flagged_issues"], mode=mode)
        except Exception as e:
            monitor.report(_trigger_for(e), f"periodic review failed: {e}", e)
            raise
        finally:
            self._review_running.release()


def _trigger_for(e: BaseException) -> str:
    return "api_error" if isinstance(e, (IGError, anthropic.APIError, service.ClaudeError)) else "exception"
