"""Client-side enforcement of IG's REST API limits.

IG's published limits (labs.ig.com/faq):
  - per-app (API key) non-trading requests: 60 / minute
  - per-account non-trading requests:       30 / minute
  - per-account trading requests:          100 / minute (create/amend/close positions & orders)
  - historical price data points:       10,000 / week

Every IG request goes through a ``RateGate`` that waits for a free slot rather
than letting IG reject the call. The gate runs at SAFETY_FACTOR of each limit,
and normal-priority calls (price history, dashboard reads) leave a reserve for
high-priority ones (deal confirms, position sync, closes). Historical data
points are tracked from the ``metadata.allowance`` IG returns with every price
request, persisted so restarts don't lose track.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from typing import Optional

from app import db

IG_LIMITS = {
    "app_non_trading": 60,
    "account_non_trading": 30,
    "account_trading": 100,
}
IG_HISTORICAL_POINTS_PER_WEEK = 10_000
SAFETY_FACTOR = 0.9
HIGH_PRIORITY_RESERVE = 5        # slots per minute kept free for confirms / position sync / closes
HISTORICAL_RESERVE = 300         # points never spent by routine polling (kept for warm-ups after restarts)
WINDOW = 60.0

EXCEEDED_CODES = {
    "error.public-api.exceeded-api-key-allowance": "app_non_trading",
    "error.public-api.exceeded-account-allowance": "account_non_trading",
    "error.public-api.exceeded-account-trading-allowance": "account_trading",
    "error.public-api.exceeded-account-historical-data-allowance": "historical",
}


class RateLimited(Exception):
    """Raised instead of sending a request that would break an IG limit."""


class _Window:
    def __init__(self, name: str, limit: int):
        self.name = name
        self.limit = limit
        self.stamps: deque[float] = deque()
        self.blocked_until = 0.0

    def _purge(self, now: float) -> None:
        while self.stamps and now - self.stamps[0] >= WINDOW:
            self.stamps.popleft()

    def wait_time(self, now: float, reserve: int) -> float:
        self._purge(now)
        if now < self.blocked_until:
            return self.blocked_until - now
        if len(self.stamps) < max(1, self.limit - reserve):
            return 0.0
        # wait until enough old stamps age out of the window
        idx = len(self.stamps) - max(1, self.limit - reserve)
        return max(0.0, self.stamps[idx] + WINDOW - now)

    def used(self, now: float) -> int:
        self._purge(now)
        return len(self.stamps)


class RateGate:
    """Shared across IGClient instances: one per API key and one per account."""

    _registry: dict[str, _Window] = {}
    _lock = threading.Lock()

    @classmethod
    def _window(cls, key: str, kind: str) -> _Window:
        full = f"{kind}:{key}"
        if full not in cls._registry:
            cls._registry[full] = _Window(kind, int(IG_LIMITS[kind] * SAFETY_FACTOR))
        return cls._registry[full]

    def __init__(self, api_key: str, account_key: str):
        app = hashlib.sha1(api_key.encode()).hexdigest()[:12]
        acct = hashlib.sha1(account_key.encode()).hexdigest()[:12]
        self.non_trading = [self._window(app, "app_non_trading"), self._window(acct, "account_non_trading")]
        self.trading = [self._window(acct, "account_trading")]

    def acquire(self, trading: bool, high_priority: bool, timeout: float = 90.0) -> None:
        windows = self.trading if trading else self.non_trading
        reserve = 0 if high_priority else HIGH_PRIORITY_RESERVE
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                wait = max(w.wait_time(now, reserve) for w in windows)
                if wait <= 0:
                    for w in windows:
                        w.stamps.append(now)
                    return
            if now + wait > deadline:
                raise RateLimited(f"IG request budget exhausted; next slot in {wait:.0f}s")
            time.sleep(min(wait, 5.0))

    def block(self, kind: str, seconds: float) -> None:
        """IG rejected a call for exceeding a limit: stop using that budget for a while."""
        with self._lock:
            for w in self.non_trading + self.trading:
                if w.name == kind:
                    w.blocked_until = time.monotonic() + seconds

    def usage(self) -> dict:
        now = time.monotonic()
        with self._lock:
            return {w.name: {"used_last_minute": w.used(now), "limit_enforced": w.limit,
                             "ig_limit": IG_LIMITS[w.name],
                             "blocked_for_s": max(0, round(w.blocked_until - now))}
                    for w in self.non_trading + self.trading}


class HistoricalAllowance:
    """Tracks IG's weekly historical price data point allowance for one account."""

    def __init__(self, account_key: str):
        self.key = "hist_allowance:" + hashlib.sha1(account_key.encode()).hexdigest()[:12]
        self._lock = threading.Lock()
        saved = db.kv_get(self.key) or {}
        self.remaining: Optional[int] = saved.get("remaining")
        self.total: int = saved.get("total", IG_HISTORICAL_POINTS_PER_WEEK)
        self.resets_at: Optional[float] = saved.get("resets_at")  # unix time

    def _expired(self) -> bool:
        return self.resets_at is not None and time.time() >= self.resets_at

    def check(self, points: int, high_priority: bool = False) -> None:
        with self._lock:
            if self.remaining is None or self._expired():
                return  # unknown or reset: IG will tell us on the next response
            reserve = 0 if high_priority else HISTORICAL_RESERVE
            if self.remaining - points < reserve:
                wait_h = (self.resets_at - time.time()) / 3600 if self.resets_at else None
                raise RateLimited(
                    f"historical data allowance low: {self.remaining} of {self.total} points left, "
                    f"need {points}" + (f"; resets in {wait_h:.1f}h" if wait_h else ""))

    def update(self, metadata: dict, points_used: int) -> None:
        allowance = (metadata or {}).get("allowance") or {}
        with self._lock:
            if "remainingAllowance" in allowance:
                self.remaining = int(allowance["remainingAllowance"])
                self.total = int(allowance.get("totalAllowance", self.total))
                if allowance.get("allowanceExpiry") is not None:
                    self.resets_at = time.time() + float(allowance["allowanceExpiry"])
            elif self.remaining is not None:
                self.remaining -= points_used
            self._save()

    def exhausted_until(self, seconds: Optional[float]) -> None:
        with self._lock:
            self.remaining = 0
            self.resets_at = time.time() + (seconds or 3600)
            self._save()

    def _save(self) -> None:
        db.kv_set(self.key, {"remaining": self.remaining, "total": self.total, "resets_at": self.resets_at})

    def status(self) -> dict:
        with self._lock:
            return {"remaining": None if self._expired() else self.remaining, "total": self.total,
                    "resets_in_h": round((self.resets_at - time.time()) / 3600, 1)
                    if self.resets_at and not self._expired() else None,
                    "reserve": HISTORICAL_RESERVE}


def projected_weekly_points(watched: list[tuple[str, str]]) -> int:
    """Upper-bound estimate of weekly historical points for (epic, timeframe) pairs.

    Each closed candle costs 2 points (the new bar plus the in-progress one IG may
    return). Assumes a market open 24x5; closed-market hours cost nothing because
    the engine checks market status before fetching prices.
    """
    from app.engine.candles import TIMEFRAMES

    total = 0
    for _, timeframe in watched:
        _, length = TIMEFRAMES[timeframe]
        bars = (5 * 24 * 3600) / length.total_seconds()
        total += int(bars * 2)
    return total


MAX_PROJECTED_SHARE = 0.9  # refuse configs projected to use more than this share of the weekly allowance


def check_config_projection(assets, indicators) -> int:
    """Raise ValueError if these configs would plausibly exhaust IG's weekly historical allowance."""
    from app.engine.strategy import watched_instruments

    watched = [(e, indicators.strategies[s].timeframe) for e, _, s in watched_instruments(assets, indicators)]
    projected = projected_weekly_points(watched)
    budget = int(IG_HISTORICAL_POINTS_PER_WEEK * MAX_PROJECTED_SHARE) - HISTORICAL_RESERVE
    if projected > budget:
        raise ValueError(
            f"these settings would use ~{projected} IG historical data points per week, over the safe budget of "
            f"{budget} (IG allows {IG_HISTORICAL_POINTS_PER_WEEK}). Trade fewer instruments or use longer timeframes.")
    return projected
