"""Loading, validating and saving the JSON config files.

Every file lives in ``config/``. Demo and live share all files except
``connection_config.json``; credentials are only ever ``env:VAR`` references.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("BOT_CONFIG_DIR", ROOT / "config"))

ASSET_CLASSES = ("shares", "indices", "forex", "commodities")
Mode = Literal["demo", "live"]
ExecutionMode = Literal["auto", "manual"]  # auto places orders; manual only shows buy/sell/do-nothing advice
Confidence = Literal["low", "medium", "high"]

_lock = threading.Lock()


# ---------------------------------------------------------------- models
class Credentials(BaseModel):
    api_key: str
    username: str
    password: str
    base_url: str


class ConnectionConfig(BaseModel):
    active_mode: Mode
    execution_mode: ExecutionMode = "auto"
    demo: Credentials
    live: Credentials

    @model_validator(mode="after")
    def _no_plaintext_secrets(self):
        for mode in ("demo", "live"):
            creds: Credentials = getattr(self, mode)
            for field in ("api_key", "username", "password"):
                value = getattr(creds, field)
                if value and not value.startswith("env:"):
                    raise ValueError(f"{mode}.{field} must be an env: reference, not a plaintext value")
        return self


class AssetConfig(BaseModel):
    enabled_classes: list[str]
    instruments: dict[str, list[str]]

    @field_validator("enabled_classes")
    @classmethod
    def _known_classes(cls, v):
        bad = [c for c in v if c not in ASSET_CLASSES]
        if bad:
            raise ValueError(f"unknown asset classes: {bad}")
        return v


class StopLoss(BaseModel):
    mode: Literal["fixed_pct", "atr_dynamic"]
    value: Optional[float] = None
    atr_source: Optional[str] = None
    atr_multiplier: Optional[float] = None

    @model_validator(mode="after")
    def _fields_for_mode(self):
        if self.mode == "fixed_pct" and (self.value is None or self.value <= 0):
            raise ValueError("fixed_pct stop_loss needs a positive 'value'")
        if self.mode == "atr_dynamic" and (not self.atr_source or not self.atr_multiplier or self.atr_multiplier <= 0):
            raise ValueError("atr_dynamic stop_loss needs 'atr_source' and a positive 'atr_multiplier'")
        return self


class IndicatorSpec(BaseModel):
    type: str
    id: Optional[str] = None
    role: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.id or self.type


class EntryRule(BaseModel):
    condition: str
    value: Optional[float] = None
    source: Optional[str] = None  # optional indicator id when a strategy has several of one type


class Strategy(BaseModel):
    timeframe: str
    indicators: list[IndicatorSpec]
    entry_rules: list[EntryRule]
    rule_logic: Literal["all", "any"] = "all"
    stop_loss: Optional[StopLoss] = None
    # Direction the entry rules imply. The design's rules are all bullish, so default is buy.
    direction: Literal["buy", "sell"] = "buy"


class IndicatorConfig(BaseModel):
    strategies: dict[str, Strategy]
    asset_class_defaults: dict[str, Optional[str]]
    instrument_overrides: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references_exist(self):
        from app.engine.indicators import INDICATORS
        from app.engine.conditions import CONDITIONS
        from app.engine.candles import TIMEFRAMES

        for name, strat in self.strategies.items():
            if strat.timeframe not in TIMEFRAMES:
                raise ValueError(f"strategy {name}: unknown timeframe {strat.timeframe!r}; use one of {list(TIMEFRAMES)}")
            keys = [i.key for i in strat.indicators]
            for ind in strat.indicators:
                if ind.type not in INDICATORS:
                    raise ValueError(f"strategy {name}: unknown indicator type {ind.type!r}")
            for rule in strat.entry_rules:
                if rule.condition not in CONDITIONS:
                    raise ValueError(f"strategy {name}: unknown condition {rule.condition!r}")
            if strat.stop_loss and strat.stop_loss.mode == "atr_dynamic":
                if strat.stop_loss.atr_source not in keys:
                    raise ValueError(f"strategy {name}: atr_source {strat.stop_loss.atr_source!r} is not an indicator id")
        for cls, strat_name in self.asset_class_defaults.items():
            if strat_name is not None and strat_name not in self.strategies:
                raise ValueError(f"asset_class_defaults.{cls} refers to unknown strategy {strat_name!r}")
        for epic, strat_name in self.instrument_overrides.items():
            if strat_name not in self.strategies:
                raise ValueError(f"instrument_overrides.{epic} refers to unknown strategy {strat_name!r}")
        return self


class RiskConfig(BaseModel):
    max_total_exposure: float = Field(gt=0)
    max_per_trade: float = Field(gt=0)
    default_stop_loss: StopLoss
    take_profit_pct: Optional[float] = Field(default=None, gt=0)
    currency: str = "GBP"

    @model_validator(mode="after")
    def _per_trade_within_total(self):
        if self.max_per_trade > self.max_total_exposure:
            raise ValueError("max_per_trade cannot exceed max_total_exposure")
        if self.default_stop_loss.mode != "fixed_pct":
            # atr_dynamic needs an indicator id, which only strategies have.
            raise ValueError("default_stop_loss must be fixed_pct")
        return self


class DecisionEngineConfig(BaseModel):
    enabled: bool = True
    run_on: list[Mode] = ["demo", "live"]
    news_source: Literal["web_search"] = "web_search"
    news_lookback_hours: int = 24
    trade_history_lookback: int = 20
    min_confidence_to_trade: Confidence = "medium"
    model: str = "claude-sonnet-4-6"


class LivePromotionConfig(BaseModel):
    require_calibration: bool = True
    min_demo_trades: int = 30
    min_demo_win_rate_pct: float = 55
    min_demo_days: int = 14
    manual_override_allowed: bool = True


class PostTradeReviewConfig(BaseModel):
    enabled: bool = True
    run_on: list[Mode] = ["demo", "live"]


class PeriodicReviewConfig(BaseModel):
    enabled: bool = True
    frequency: Literal["hourly", "daily", "weekly"] = "daily"
    lookback_trades: int = 50
    run_on: list[Mode] = ["live"]


class CodeMonitorConfig(BaseModel):
    enabled: bool = True
    watch: list[Literal["exceptions", "unexpected_order_rejections", "api_errors"]] = [
        "exceptions", "unexpected_order_rejections", "api_errors"]
    trigger: Literal["on_error"] = "on_error"
    notify: Literal["dashboard_alert"] = "dashboard_alert"
    auto_apply_fixes: Literal[False] = False  # deliberately cannot be enabled


class ClaudeConfig(BaseModel):
    decision_engine: DecisionEngineConfig
    live_promotion: LivePromotionConfig
    post_trade_review: PostTradeReviewConfig
    periodic_review: PeriodicReviewConfig
    code_monitor: CodeMonitorConfig


MODELS: dict[str, type[BaseModel]] = {
    "connection": ConnectionConfig,
    "asset": AssetConfig,
    "indicator": IndicatorConfig,
    "risk": RiskConfig,
    "claude": ClaudeConfig,
}


# ---------------------------------------------------------------- io
def _path(name: str) -> Path:
    if name not in MODELS:
        raise KeyError(f"unknown config {name!r}")
    return CONFIG_DIR / f"{name}_config.json"


def load_raw(name: str) -> dict:
    with _lock, open(_path(name), encoding="utf-8") as f:
        return json.load(f)


def load(name: str):
    """Load and validate a config file, returning its pydantic model."""
    return MODELS[name].model_validate(load_raw(name))


def save(name: str, data: dict) -> BaseModel:
    """Validate then atomically write a config file. Raises ValueError on invalid data."""
    model = MODELS[name].model_validate(data)
    path = _path(name)
    tmp = path.with_suffix(".json.tmp")
    with _lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    return model


def resolve_secret(ref: str) -> str:
    """Resolve an ``env:VAR`` reference to its value."""
    if ref.startswith("env:"):
        var = ref[4:]
        value = os.environ.get(var)
        if not value:
            raise RuntimeError(f"environment variable {var} is not set")
        return value
    return ref


def active_mode() -> str:
    return load("connection").active_mode


def execution_mode() -> str:
    return load("connection").execution_mode


def credentials_for(mode: str) -> dict:
    conn: ConnectionConfig = load("connection")
    creds: Credentials = getattr(conn, mode)
    return {
        "api_key": resolve_secret(creds.api_key),
        "username": resolve_secret(creds.username),
        "password": resolve_secret(creds.password),
        "base_url": creds.base_url.rstrip("/"),
    }


def set_active_mode(mode: str) -> None:
    raw = load_raw("connection")
    raw["active_mode"] = mode
    save("connection", raw)


def set_execution_mode(execution: str) -> None:
    raw = load_raw("connection")
    raw["execution_mode"] = execution
    save("connection", raw)
