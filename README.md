# IG Trading Bot

Implementation of [DesignDocument/ig_trading_bot_design.md](DesignDocument/ig_trading_bot_design.md):
Python/FastAPI backend, SQLite log, vanilla-JS dashboard.

## Run

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # fill in IG demo credentials + ANTHROPIC_API_KEY
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and press **Start engine**. Tests: `.venv\Scripts\pip install pytest httpx` then `.venv\Scripts\python -m pytest tests`.

## Layout

| Path | Role |
|---|---|
| `config/*.json` | The five config files from the design. Edited from the dashboard, validated on save, re-read every engine cycle. |
| `app/config.py` | Pydantic validation for every config file; resolves `env:` secrets; rejects plaintext credentials. |
| `app/db.py` | SQLite schema (`trades`, `claude_periodic_reviews`, `claude_bug_reports`) and queries. |
| `app/ig_client.py` | IG REST client: session/token refresh, prices, markets, positions, dealing, history. |
| `app/engine/indicators.py` | EMA, SMA, RSI, ATR, MACD, Bollinger. |
| `app/engine/conditions.py` | Named entry conditions (`ema_fast_crosses_above_ema_slow`, `rsi_below`, …). |
| `app/engine/strategy.py` | Maps instruments to strategies and evaluates entry rules (`all`/`any`). |
| `app/engine/risk.py` | Risk manager: sizing, `max_per_trade` / `max_total_exposure`, fixed-% or ATR stop, take-profit. |
| `app/engine/trader.py` | Engine loop: candle → signal → Claude → risk → order; tracks closes; schedules reviews. |
| `app/claude/service.py` | Claude's four roles: decision (with web search), post-trade review, periodic review, bug diagnosis. |
| `app/claude/monitor.py` | Captures exceptions / order rejections / API errors into bug reports for Claude to diagnose. |
| `app/calibration.py` | Demo → live calibration gate. |
| `static/` | Dashboard. |

## Behaviour notes and additions to the design

- **The risk manager is never bypassed.** Claude returns only `confirm` / `reverse` / `skip` plus confidence. Size, stop and caps come from config. If IG market data is missing what's needed to calculate exposure, the trade is rejected (it fails closed).
- **What "exposure" means:** the margin committed to a position, in account currency: `size × price × value-per-point × marginFactor`. This uses IG's `valueOfOnePip`, `onePipMeans`, `scalingFactor`, `marginFactor` and the currency's `baseExchangeRate`. **Check the sizes it produces on demo for each instrument before going live**, especially shares, where IG's pip conventions vary.
- If a stop is tighter than IG's minimum stop distance, it is widened to that minimum. This is recorded as `widened_to_broker_min` in `stop_loss_used`.
- Only one open bot position per instrument. Signals are evaluated once per *closed* candle, and a signal already logged for that candle isn't acted on again (safe across restarts).
- Positions exit through their IG stop / take-profit, or through **Close** on the dashboard. The engine picks up closes from IG's activity and transaction history. If it can't match a transaction, P/L is estimated from prices and noted in `status_note`.
- `trades` has extra columns: `status`, `status_note`, `deal_reference`, `deal_id`, `closed_at`. Strategies accept an optional `"direction": "buy"|"sell"` field (default `buy`), and entry rules accept an optional `source` indicator id.
- If a Claude decision fails (API error or refusal), no trade is placed. The row is logged with `status='error'` and kept out of the "Claude skipped" analysis.
- `code_monitor.auto_apply_fixes` is locked to `false` by validation. Repeat errors are deduplicated for an hour.
- **IG API limits are enforced client-side** ([app/ig_limits.py](app/ig_limits.py)). IG allows 60 non-trading requests per minute per app, 30 per account, 100 trading requests per minute, and 10,000 historical price points per week.
  - Every request waits for a free slot, capped at 90% of each limit. Price and dashboard reads leave 5 slots a minute free for deal confirms, position sync and closes.
  - If IG returns an `exceeded-*` error, that budget is paused.
  - The weekly price allowance is read from IG's response `metadata`, saved to disk, and spent only down to a 300-point reserve.
  - Candles are stored in SQLite, so a restart doesn't download history again. After warm-up, each new candle costs about 2 points.
  - Prices aren't fetched while a market is closed. The engine checks market status every 10 minutes, which uses one ordinary request and no allowance.
  - Config saves are rejected if the projected weekly use is over 90% of the allowance. For example, one instrument on 15m is ~960 points/week and one on 1m is ~14,400.
  - The Overview tab shows live usage.
- **Prices come from REST polling, not Lightstreamer.** Strategies act on closed candles, so polling is enough, and the candle cache keeps usage inside IG's historical-data allowance. Streaming is a possible later addition.
- The Claude model comes from `decision_engine.model` (`claude-sonnet-4-6` as in the design) and is used for all four roles. Newer options are `claude-sonnet-5` or `claude-opus-5`.
