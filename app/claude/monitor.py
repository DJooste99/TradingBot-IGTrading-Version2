"""Runtime bug monitor: captures exceptions, order rejections and API errors,
stores them as bug reports and sends them to Claude for diagnosis.

Claude only diagnoses and suggests; nothing is ever patched automatically.
"""
from __future__ import annotations

import hashlib
import linecache
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from app import config, db
from app.claude import service

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
WATCH_FOR_TRIGGER = {
    "exception": "exceptions",
    "order_rejection": "unexpected_order_rejections",
    "api_error": "api_errors",
}
DEDUPE_SECONDS = 3600

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bug-diagnosis")
_recent: dict[str, float] = {}


def _source_context(tb) -> str:
    """Source lines around every frame inside this app, so Claude sees the code that failed."""
    chunks, seen = [], set()
    for frame in traceback.extract_tb(tb) if tb else []:
        path = Path(frame.filename)
        try:
            path.relative_to(APP_DIR)
        except ValueError:
            continue
        key = (frame.filename, frame.lineno)
        if key in seen:
            continue
        seen.add(key)
        start = max(1, frame.lineno - 15)
        lines = [f"{n:4d}{'>' if n == frame.lineno else ' '} {linecache.getline(frame.filename, n).rstrip()}"
                 for n in range(start, frame.lineno + 10)]
        chunks.append(f"# {path.relative_to(APP_DIR.parent)}:{frame.lineno} in {frame.name}\n" + "\n".join(lines))
    return "\n\n".join(chunks[-4:])


def report(trigger_type: str, summary: str, exc: Optional[BaseException] = None,
           context: Optional[dict] = None) -> Optional[int]:
    """Record a runtime problem. Returns the bug report id, or None if filtered/deduplicated."""
    try:
        cfg = config.load("claude").code_monitor
    except Exception:  # config itself broken: still record the problem, without Claude
        cfg = None
    if cfg is not None and (not cfg.enabled or WATCH_FOR_TRIGGER[trigger_type] not in cfg.watch):
        log.warning("%s (not watched): %s", trigger_type, summary)
        return None

    stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else None
    signature = hashlib.sha1(f"{trigger_type}|{summary[:200]}".encode()).hexdigest()
    now = time.time()
    if now - _recent.get(signature, 0) < DEDUPE_SECONDS:
        log.warning("duplicate %s suppressed: %s", trigger_type, summary)
        return None
    _recent[signature] = now

    report_id = db.insert_bug_report(trigger_type, summary[:2000], stack)
    log.error("bug report #%s [%s] %s", report_id, trigger_type, summary)
    source = _source_context(exc.__traceback__) if exc else ""
    if cfg is not None:
        _executor.submit(_diagnose, report_id, trigger_type, summary, stack, context or {}, source)
    return report_id


def _diagnose(report_id, trigger_type, summary, stack, context, source) -> None:
    try:
        model = config.load("claude").decision_engine.model
        result = service.diagnose(trigger_type, summary, stack, context, source, model)
        db.update_bug_report(report_id, claude_diagnosis=result["diagnosis"],
                             claude_suggested_fix=result["suggested_fix"])
    except Exception as e:  # never recurse into the monitor from its own failures
        log.exception("Claude diagnosis failed for bug report #%s", report_id)
        db.update_bug_report(report_id, claude_diagnosis=f"(diagnosis unavailable: {e})")
