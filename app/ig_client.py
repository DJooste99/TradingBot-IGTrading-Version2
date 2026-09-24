"""Thin IG REST API client (demo and live share the same code; only credentials/base URL differ)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import requests

from app import config

log = logging.getLogger(__name__)


class IGError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, error_code: Optional[str] = None,
                 path: str = ""):
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.path = path


class IGClient:
    def __init__(self, mode: str):
        creds = config.credentials_for(mode)
        self.mode = mode
        self.base_url = creds["base_url"] + "/gateway/deal"
        self._api_key = creds["api_key"]
        self._username = creds["username"]
        self._password = creds["password"]
        self._session = requests.Session()
        self._tokens: dict[str, str] = {}
        self._lock = threading.Lock()
        self.account_id: Optional[str] = None
        self.account_currency: Optional[str] = None
        self._market_cache: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------ plumbing
    def _headers(self, version: int) -> dict:
        h = {
            "X-IG-API-KEY": self._api_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "Version": str(version),
        }
        h.update(self._tokens)
        return h

    def login(self) -> None:
        resp = self._session.post(
            f"{self.base_url}/session",
            json={"identifier": self._username, "password": self._password},
            headers=self._headers(2), timeout=20)
        if resp.status_code != 200:
            raise IGError(f"IG login failed ({self.mode}): {resp.text[:300]}", resp.status_code,
                          _error_code(resp), "/session")
        self._tokens = {"CST": resp.headers["CST"], "X-SECURITY-TOKEN": resp.headers["X-SECURITY-TOKEN"]}
        body = resp.json()
        self.account_id = body.get("currentAccountId")
        self.account_currency = body.get("currencyIsoCode")
        log.info("Logged in to IG %s account %s", self.mode, self.account_id)

    def _request(self, method: str, path: str, version: int = 1, retry_auth: bool = True,
                 extra_headers: Optional[dict] = None, **kwargs) -> Any:
        with self._lock:
            if not self._tokens:
                self.login()
        headers = self._headers(version)
        if extra_headers:
            headers.update(extra_headers)
        resp = self._session.request(method, f"{self.base_url}{path}", headers=headers, timeout=30, **kwargs)
        code = _error_code(resp)
        if resp.status_code in (401, 403) and retry_auth and code and "token" in code:
            with self._lock:
                self._tokens = {}
                self.login()
            return self._request(method, path, version, retry_auth=False, extra_headers=extra_headers, **kwargs)
        if resp.status_code >= 400:
            raise IGError(f"IG {method} {path} -> {resp.status_code}: {resp.text[:500]}",
                          resp.status_code, code, path)
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------ market data
    def get_prices(self, epic: str, resolution: str, max_bars: int) -> list[dict]:
        data = self._request("GET", f"/prices/{epic}", version=3,
                             params={"resolution": resolution, "max": max_bars, "pageSize": 0})
        return data.get("prices", [])

    def get_market(self, epic: str, max_age: float = 60) -> dict:
        cached = self._market_cache.get(epic)
        if cached and time.time() - cached[0] < max_age:
            return cached[1]
        data = self._request("GET", f"/markets/{epic}", version=3)
        self._market_cache[epic] = (time.time(), data)
        return data

    # ------------------------------------------------------------ account / positions
    def get_accounts(self) -> list[dict]:
        return self._request("GET", "/accounts", version=1).get("accounts", [])

    def get_positions(self) -> list[dict]:
        return self._request("GET", "/positions", version=2).get("positions", [])

    def get_activity(self, from_iso: str, detailed: bool = True) -> list[dict]:
        data = self._request("GET", "/history/activity", version=3,
                             params={"from": from_iso[:19], "detailed": str(detailed).lower(), "pageSize": 200})
        return data.get("activities", [])

    def get_transactions(self, from_iso: str) -> list[dict]:
        data = self._request("GET", "/history/transactions", version=2,
                             params={"type": "ALL_DEAL", "from": from_iso[:19], "pageSize": 200})
        return data.get("transactions", [])

    # ------------------------------------------------------------ dealing
    def open_position(self, epic: str, direction: str, size: float, currency: str,
                      stop_level: Optional[float], limit_level: Optional[float]) -> dict:
        body = {
            "epic": epic,
            "expiry": "-",
            "direction": direction.upper(),
            "size": size,
            "orderType": "MARKET",
            "currencyCode": currency,
            "forceOpen": True,
            "guaranteedStop": False,
            "trailingStop": False,
            "stopLevel": stop_level,
            "limitLevel": limit_level,
        }
        ref = self._request("POST", "/positions/otc", version=2, json=body)["dealReference"]
        return self.confirm(ref)

    def close_position(self, deal_id: str, direction: str, size: float) -> dict:
        body = {"dealId": deal_id, "direction": "SELL" if direction.upper() == "BUY" else "BUY",
                "size": size, "orderType": "MARKET"}
        # IG's close endpoint is a DELETE with a body, sent as POST + _method override.
        ref = self._request("POST", "/positions/otc", version=1, json=body,
                            extra_headers={"_method": "DELETE"})["dealReference"]
        return self.confirm(ref)

    def confirm(self, deal_reference: str, attempts: int = 5) -> dict:
        last: Optional[IGError] = None
        for i in range(attempts):
            try:
                data = self._request("GET", f"/confirms/{deal_reference}", version=1)
                data["dealReference"] = deal_reference
                return data
            except IGError as e:  # confirm can 404 briefly while the deal is processed
                last = e
                time.sleep(0.5 * (i + 1))
        raise last  # type: ignore[misc]


def _error_code(resp: requests.Response) -> Optional[str]:
    try:
        return resp.json().get("errorCode")
    except ValueError:
        return None
