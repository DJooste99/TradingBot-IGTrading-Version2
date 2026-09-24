"""SQLite log: trades, periodic reviews, bug reports.

The three tables follow the design schema. ``trades`` carries a few extra
operational columns (status, deal ids, close time) the engine needs to track
positions through their lifecycle.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import ROOT

DB_PATH = Path(os.environ.get("BOT_DB_PATH", ROOT / "data" / "trading_bot.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    mode TEXT NOT NULL,
    instrument TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    strategy TEXT NOT NULL,
    indicator_suggested_direction TEXT,
    claude_final_direction TEXT NOT NULL,
    claude_confidence TEXT,
    claude_reasoning TEXT,
    news_summary TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    stop_loss_used TEXT,
    profit_loss REAL,
    indicators_used TEXT NOT NULL,
    claude_post_trade_note TEXT,
    -- operational columns (not in the original design schema)
    status TEXT NOT NULL DEFAULT 'pending', -- skipped, rejected_risk, rejected_broker, open, closed, error
    status_note TEXT,
    deal_reference TEXT,
    deal_id TEXT,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_mode_status ON trades(mode, status);
CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument, strategy);

CREATE TABLE IF NOT EXISTS claude_periodic_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    trades_reviewed INTEGER NOT NULL,
    summary TEXT NOT NULL,
    flagged_issues TEXT,
    mode TEXT
);

CREATE TABLE IF NOT EXISTS claude_bug_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    error_summary TEXT NOT NULL,
    stack_trace TEXT,
    claude_diagnosis TEXT,
    claude_suggested_fix TEXT,
    status TEXT DEFAULT 'unreviewed'
);
"""

TRADE_JSON_FIELDS = ("stop_loss_used", "indicators_used")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for f in TRADE_JSON_FIELDS + ("flagged_issues",):
        if f in d and isinstance(d[f], str):
            try:
                d[f] = json.loads(d[f])
            except json.JSONDecodeError:
                pass
    return d


def _encode(values: dict) -> dict:
    out = {}
    for k, v in values.items():
        out[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    return out


# ---------------------------------------------------------------- trades
def insert_trade(**fields: Any) -> int:
    fields.setdefault("timestamp", now_iso())
    fields = _encode(fields)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", list(fields.values()))
        return cur.lastrowid


def update_trade(trade_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields = _encode(fields)
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE trades SET {sets} WHERE id = ?", [*fields.values(), trade_id])


def get_trade(trade_id: int) -> Optional[dict]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return _row(r) if r else None


def list_trades(mode: Optional[str] = None, status: Optional[str] = None, instrument: Optional[str] = None,
                strategy: Optional[str] = None, limit: int = 200, offset: int = 0) -> list[dict]:
    where, args = [], []
    for col, val in (("mode", mode), ("status", status), ("instrument", instrument), ("strategy", strategy)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    sql = "SELECT * FROM trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    with connect() as conn:
        rows = conn.execute(sql, [*args, limit, offset]).fetchall()
    return [_row(r) for r in rows]


def open_trades(mode: str) -> list[dict]:
    return list_trades(mode=mode, status="open", limit=10_000)


def recent_history(instrument: str, strategy: str, limit: int) -> list[dict]:
    """Recent decided trades for an instrument/strategy, used as Claude context."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT timestamp, mode, indicator_suggested_direction, claude_final_direction, claude_confidence,
                      entry_price, exit_price, profit_loss, status, claude_post_trade_note
               FROM trades WHERE instrument = ? AND strategy = ? AND status IN ('open','closed','skipped')
               ORDER BY id DESC LIMIT ?""",
            (instrument, strategy, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def signal_already_logged(instrument: str, strategy: str, candle_time: str, mode: str) -> bool:
    """Guards against acting twice on the same candle (e.g. after a restart)."""
    with connect() as conn:
        r = conn.execute(
            """SELECT 1 FROM trades WHERE instrument = ? AND strategy = ? AND mode = ?
               AND json_extract(indicators_used, '$.candle_time') = ? LIMIT 1""",
            (instrument, strategy, mode, candle_time)).fetchone()
    return r is not None


def closed_trades(mode: Optional[str] = None, limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM trades WHERE status = 'closed'"
    args: list = []
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    sql += " ORDER BY closed_at DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, [*args, limit]).fetchall()
    return [_row(r) for r in rows]


def pnl_summary(mode: str) -> dict:
    with connect() as conn:
        tot = conn.execute(
            """SELECT COUNT(*) n, COALESCE(SUM(profit_loss),0) pnl,
                      SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) wins,
                      MIN(timestamp) first_ts
               FROM trades WHERE mode = ? AND status = 'closed'""", (mode,)).fetchone()
        by_strategy = conn.execute(
            """SELECT strategy, COUNT(*) n, COALESCE(SUM(profit_loss),0) pnl,
                      SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) wins
               FROM trades WHERE mode = ? AND status = 'closed' GROUP BY strategy""", (mode,)).fetchall()
        decisions = conn.execute(
            """SELECT indicator_suggested_direction s, claude_final_direction c, COUNT(*) n
               FROM trades WHERE mode = ? GROUP BY s, c""", (mode,)).fetchall()
        open_n = conn.execute("SELECT COUNT(*) FROM trades WHERE mode = ? AND status = 'open'", (mode,)).fetchone()[0]
    n = tot["n"] or 0
    return {
        "mode": mode,
        "closed_trades": n,
        "open_trades": open_n,
        "total_pnl": round(tot["pnl"] or 0, 2),
        "wins": tot["wins"] or 0,
        "win_rate_pct": round(100 * (tot["wins"] or 0) / n, 1) if n else None,
        "first_trade_at": tot["first_ts"],
        "by_strategy": [
            {**dict(r), "pnl": round(r["pnl"], 2),
             "win_rate_pct": round(100 * (r["wins"] or 0) / r["n"], 1) if r["n"] else None}
            for r in by_strategy
        ],
        "decision_matrix": [dict(r) for r in decisions],
    }


def demo_calibration_stats() -> dict:
    with connect() as conn:
        r = conn.execute(
            """SELECT COUNT(*) n, SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) wins, MIN(timestamp) first_ts
               FROM trades WHERE mode = 'demo' AND status = 'closed'""").fetchone()
        first_any = conn.execute("SELECT MIN(timestamp) FROM trades WHERE mode = 'demo'").fetchone()[0]
    return {"trades": r["n"] or 0, "wins": r["wins"] or 0, "first_trade_at": first_any}


def skipped_vs_suggested(mode: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Every case where the strategy wanted in and Claude said no."""
    sql = ("SELECT * FROM trades WHERE claude_final_direction = 'skipped' AND status = 'skipped' "
           "AND indicator_suggested_direction IN ('buy','sell')")
    args: list = []
    if mode:
        sql += " AND mode = ?"
        args.append(mode)
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, [*args, limit]).fetchall()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- reviews
def insert_periodic_review(period_start: str, period_end: str, trades_reviewed: int, summary: str,
                           flagged_issues: Iterable[str], mode: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO claude_periodic_reviews
               (timestamp, period_start, period_end, trades_reviewed, summary, flagged_issues, mode)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), period_start, period_end, trades_reviewed, summary, json.dumps(list(flagged_issues)), mode))
        return cur.lastrowid


def list_periodic_reviews(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM claude_periodic_reviews ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def last_periodic_review(mode: str) -> Optional[dict]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM claude_periodic_reviews WHERE mode = ? ORDER BY id DESC LIMIT 1",
                         (mode,)).fetchone()
    return _row(r) if r else None


# ---------------------------------------------------------------- bug reports
BUG_STATUSES = ("unreviewed", "acknowledged", "fixed", "dismissed")


def insert_bug_report(trigger_type: str, error_summary: str, stack_trace: Optional[str]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO claude_bug_reports (timestamp, trigger_type, error_summary, stack_trace) VALUES (?, ?, ?, ?)",
            (now_iso(), trigger_type, error_summary, stack_trace))
        return cur.lastrowid


def update_bug_report(report_id: int, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE claude_bug_reports SET {sets} WHERE id = ?", [*fields.values(), report_id])


def list_bug_reports(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    sql, args = "SELECT * FROM claude_bug_reports", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, [*args, limit]).fetchall()
    return [dict(r) for r in rows]


def count_unreviewed_bugs() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM claude_bug_reports WHERE status = 'unreviewed'").fetchone()[0]
