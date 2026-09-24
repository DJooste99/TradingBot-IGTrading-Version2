"""FastAPI backend: hosts the trading engine and the dashboard API."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from app import calibration, config, db  # noqa: E402
from app.claude import monitor  # noqa: E402
from app.engine.trader import TradingEngine  # noqa: E402
from app.ig_client import IGError  # noqa: E402

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

engine = TradingEngine()
STATIC_DIR = config.ROOT / "static"
EDITABLE_CONFIGS = ("asset", "indicator", "risk", "claude")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    for name in config.MODELS:  # fail fast on a broken config file
        config.load(name)
    if os.environ.get("ENGINE_AUTOSTART", "false").lower() == "true":
        engine.start()
    yield
    engine.stop()


app = FastAPI(title="IG Trading Bot", lifespan=lifespan)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    monitor.report("api_error" if isinstance(exc, IGError) else "exception",
                   f"{request.method} {request.url.path}: {exc}", exc)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---------------------------------------------------------------- status / engine
@app.get("/api/status")
def status():
    mode = config.active_mode()
    return {
        "mode": mode,
        "engine": engine.state,
        "unreviewed_bugs": db.count_unreviewed_bugs(),
        "calibration": calibration.gate_status(),
        "currency": config.load("risk").currency,
    }


@app.post("/api/engine/start")
def engine_start():
    engine.start()
    return engine.state


@app.post("/api/engine/stop")
def engine_stop():
    engine.stop()
    return engine.state


# ---------------------------------------------------------------- mode switching
class ModeSwitch(BaseModel):
    mode: str
    confirm: bool = False
    override: bool = False


@app.get("/api/mode/preview")
def mode_preview():
    """Everything the confirmation modal reads back before a switch."""
    return {
        "current_mode": config.active_mode(),
        "risk": config.load_raw("risk"),
        "assets": config.load_raw("asset"),
        "strategies": config.load_raw("indicator"),
        "decision_engine": config.load_raw("claude")["decision_engine"],
        "calibration": calibration.gate_status(),
    }


@app.post("/api/mode")
def switch_mode(body: ModeSwitch):
    if body.mode not in ("demo", "live"):
        raise HTTPException(400, "mode must be 'demo' or 'live'")
    if body.mode == config.active_mode():
        return {"mode": body.mode, "changed": False}
    if body.mode == "live":
        if not body.confirm:
            raise HTTPException(400, "switching to live requires confirming the read-back of risk/strategy config")
        gate = calibration.gate_status()
        if not gate["passed"]:
            if not (body.override and gate["manual_override_allowed"]):
                raise HTTPException(403, {"message": "calibration gate not cleared", "calibration": gate})
            log.warning("LIVE MODE FORCED via manual override; calibration gate not met: %s", gate["checks"])
        try:
            config.credentials_for("live")
        except RuntimeError as e:
            raise HTTPException(400, f"live credentials unavailable: {e}")
    config.set_active_mode(body.mode)
    engine.reset_connection()
    log.warning("active_mode switched to %s", body.mode)
    return {"mode": body.mode, "changed": True}


# ---------------------------------------------------------------- config
@app.get("/api/config/{name}")
def get_config(name: str):
    if name not in config.MODELS:
        raise HTTPException(404)
    return config.load_raw(name)


@app.put("/api/config/{name}")
async def put_config(name: str, request: Request):
    if name not in EDITABLE_CONFIGS:
        raise HTTPException(403 if name == "connection" else 404,
                            "connection config is changed only through the mode switch")
    data = await request.json()
    try:
        config.save(name, data)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e))
    return {"saved": name}


class EnabledClasses(BaseModel):
    enabled_classes: list[str]


@app.put("/api/assets/enabled")
def set_enabled(body: EnabledClasses):
    raw = config.load_raw("asset")
    raw["enabled_classes"] = body.enabled_classes
    try:
        config.save("asset", raw)
    except (ValidationError, ValueError) as e:
        raise HTTPException(422, str(e))
    return raw


# ---------------------------------------------------------------- trades / P&L
@app.get("/api/trades")
def trades(mode: Optional[str] = None, status: Optional[str] = None, instrument: Optional[str] = None,
           strategy: Optional[str] = None, limit: int = 200, offset: int = 0):
    return db.list_trades(mode, status, instrument, strategy, min(limit, 1000), offset)


@app.post("/api/trades/{trade_id}/close")
def close_trade(trade_id: int):
    try:
        return engine.close_trade(trade_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/summary")
def summary(mode: Optional[str] = None):
    return db.pnl_summary(mode or config.active_mode())


@app.get("/api/decisions/skipped")
def skipped(mode: Optional[str] = None):
    return db.skipped_vs_suggested(mode)


@app.get("/api/positions")
def positions():
    return engine.ig().get_positions()


@app.get("/api/account")
def account():
    ig = engine.ig()
    return {"mode": ig.mode, "account_id": ig.account_id,
            "accounts": [a for a in ig.get_accounts() if a.get("accountId") == ig.account_id]}


# ---------------------------------------------------------------- Claude reviews / bugs
@app.get("/api/reviews")
def reviews():
    return db.list_periodic_reviews()


@app.post("/api/reviews/run")
def run_review(mode: Optional[str] = None):
    review_id = engine.run_periodic_review(mode or config.active_mode())
    if review_id is None:
        raise HTTPException(409, "no closed trades to review, or a review is already running")
    return {"id": review_id}


@app.get("/api/bugs")
def bugs(status: Optional[str] = None):
    return db.list_bug_reports(status)


class BugStatus(BaseModel):
    status: str


@app.patch("/api/bugs/{report_id}")
def set_bug_status(report_id: int, body: BugStatus):
    if body.status not in db.BUG_STATUSES:
        raise HTTPException(400, f"status must be one of {db.BUG_STATUSES}")
    db.update_bug_report(report_id, status=body.status)
    return {"id": report_id, "status": body.status}


# ---------------------------------------------------------------- dashboard
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
