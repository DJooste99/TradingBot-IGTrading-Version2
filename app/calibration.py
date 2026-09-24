"""Calibration gate: demo track record required before switching to live."""
from __future__ import annotations

from datetime import datetime, timezone

from app import config, db


def gate_status() -> dict:
    gate = config.load("claude").live_promotion
    stats = db.demo_calibration_stats()
    days = 0.0
    if stats["first_trade_at"]:
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(stats["first_trade_at"])).total_seconds() / 86400
    win_rate = 100 * stats["wins"] / stats["trades"] if stats["trades"] else 0.0
    checks = [
        {"name": "min_demo_trades", "required": gate.min_demo_trades, "actual": stats["trades"],
         "met": stats["trades"] >= gate.min_demo_trades},
        {"name": "min_demo_win_rate_pct", "required": gate.min_demo_win_rate_pct, "actual": round(win_rate, 1),
         "met": win_rate >= gate.min_demo_win_rate_pct},
        {"name": "min_demo_days", "required": gate.min_demo_days, "actual": round(days, 1),
         "met": days >= gate.min_demo_days},
    ]
    passed = all(c["met"] for c in checks) or not gate.require_calibration
    return {
        "require_calibration": gate.require_calibration,
        "manual_override_allowed": gate.manual_override_allowed,
        "checks": checks,
        "passed": passed,
    }
