"""Claude's four roles: decision engine, post-trade review, periodic review, bug monitor diagnosis.

Claude is reached through the Claude Code CLI in headless mode (``claude -p``), signed in with the user's
Claude subscription, not through the Claude API. Each call is a fresh, tool-less session except the trade
decision, which may use only Claude Code's built-in WebSearch tool.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.config import ROOT, DecisionEngineConfig

log = logging.getLogger(__name__)

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
CLI_TIMEOUT_SECONDS = 300
# Empty working directory for the CLI, so its sessions never read or touch the bot's own files.
CLI_WORKDIR = ROOT / "data" / "claude_cli"
# Env vars that would make the CLI bill the Claude API instead of using the subscription login.
API_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeError(Exception):
    pass


# ---------------------------------------------------------------- Claude Code CLI
def find_cli() -> Optional[str]:
    """CLAUDE_CLI env var, then `claude` on PATH, then the copy bundled with the VS Code extension."""
    configured = os.environ.get("CLAUDE_CLI")
    if configured:
        return configured if Path(configured).is_file() else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundled = glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude*"))
    bundled = [b for b in bundled if Path(b).is_file()]
    return max(bundled, key=os.path.getmtime) if bundled else None


def _run_cli(model: str, system: str, prompt: str, schema: dict, tools: tuple[str, ...] = ()) -> dict:
    """One headless Claude Code session; returns the answer validated against ``schema``."""
    exe = find_cli()
    if not exe:
        raise ClaudeError("Claude Code CLI not found: install Claude Code, or set CLAUDE_CLI to the claude executable")
    tool_list = ",".join(tools)
    args = [exe, "-p", "--output-format", "json", "--model", model, "--system-prompt", system,
            "--json-schema", json.dumps(schema), "--tools", tool_list,
            # no CLAUDE.md, plugins, hooks or MCP servers; nothing saved; anything needing approval is denied
            "--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--permission-prompts", "none"]
    if tools:
        args += ["--allowedTools", tool_list]
    env = {k: v for k, v in os.environ.items() if k not in API_ENV_VARS}
    CLI_WORKDIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=CLI_TIMEOUT_SECONDS, cwd=CLI_WORKDIR, env=env)
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"Claude Code did not answer within {CLI_TIMEOUT_SECONDS}s")
    except OSError as e:
        raise ClaudeError(f"could not start Claude Code ({exe}): {e}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise ClaudeError(f"Claude Code failed (exit {proc.returncode}): {detail or 'no output'}")
    if out.get("is_error") or out.get("subtype") != "success":
        # e.g. not logged in, usage limit reached, or the structured answer could not be produced
        raise ClaudeError(f"Claude Code error ({out.get('subtype')}): {str(out.get('result') or '')[:400]}")
    structured = out.get("structured_output")
    return structured if isinstance(structured, dict) else _extract_json(str(out.get("result") or ""))


def _extract_json(text: str) -> dict:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = fenced or re.findall(r"\{.*\}", text, re.S)
    for raw in reversed(candidates):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    raise ClaudeError(f"could not find a JSON verdict in Claude's reply: {text[-400:]!r}")


# ---------------------------------------------------------------- 1. decision engine
DECISION_SYSTEM = """You are the final decision-maker for an automated trading bot that trades via the IG API.

A technical strategy has produced a candidate signal. Your job is to decide whether to take it, using:
1. Recent news about the instrument and its drivers (use the WebSearch tool; focus on the lookback window given).
2. The bot's own recent trade history on this instrument/strategy, including outcomes and post-trade notes.
3. The indicator values and rules that fired.

You may: confirm the proposed direction, reverse it, or skip the trade entirely. Skipping is a perfectly good
outcome when news risk is high (e.g. an imminent central-bank decision or earnings), when the signal conflicts
with the news backdrop, or when recent history on this setup has been poor.

You do NOT control position size, stop-loss or exposure limits; a separate risk manager enforces those regardless
of your answer, so do not factor sizing into your verdict.

Answer with: decision (confirm, reverse or skip), confidence (low, medium or high), reasoning (2-5 sentences
explaining the verdict) and news_summary (1-3 sentences on the news you weighed, or 'no material news found')."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["confirm", "reverse", "skip"]},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "reasoning": {"type": "string"},
        "news_summary": {"type": "string"},
    },
    "required": ["decision", "confidence", "reasoning", "news_summary"],
    "additionalProperties": False,
}


@dataclass
class Decision:
    final_direction: str        # 'buy' | 'sell' | 'skipped'
    decision: str               # confirm | reverse | skip | passthrough
    confidence: Optional[str]
    reasoning: str
    news_summary: Optional[str]


def decide(signal, history: list[dict], cfg: DecisionEngineConfig, instrument_name: str) -> Decision:
    prompt = json.dumps({
        "instrument": {"epic": signal.epic, "name": instrument_name, "asset_class": signal.asset_class},
        "news_lookback_hours": cfg.news_lookback_hours,
        "candidate_signal": {
            "strategy": signal.strategy_name,
            "timeframe": signal.strategy.timeframe,
            "proposed_direction": signal.direction,
            "last_close": signal.price,
            "candle_time_utc": signal.candle_time,
            "rules": signal.rules,
            "rule_logic": signal.strategy.rule_logic,
            "indicators": signal.indicator_values,
        },
        "recent_trade_history": history,
    }, indent=1, default=str)

    verdict = _run_cli(cfg.model, DECISION_SYSTEM,
                       f"Candidate trade signal follows. Search for news on {instrument_name} from the last "
                       f"{cfg.news_lookback_hours} hours, then give your verdict.\n\n{prompt}",
                       DECISION_SCHEMA, tools=("WebSearch",))
    decision = str(verdict.get("decision", "skip")).lower()
    confidence = str(verdict.get("confidence", "low")).lower()
    if confidence not in CONFIDENCE_RANK:
        confidence = "low"
    reasoning = str(verdict.get("reasoning", ""))
    news = verdict.get("news_summary")

    if decision == "confirm":
        direction = signal.direction
    elif decision == "reverse":
        direction = "sell" if signal.direction == "buy" else "buy"
    else:
        decision, direction = "skip", "skipped"

    if direction != "skipped" and CONFIDENCE_RANK[confidence] < CONFIDENCE_RANK[cfg.min_confidence_to_trade]:
        reasoning = f"[below min_confidence_to_trade={cfg.min_confidence_to_trade}, treated as skip] {reasoning}"
        direction = "skipped"

    return Decision(direction, decision, confidence, reasoning, news)


def passthrough(signal, why: str) -> Decision:
    return Decision(signal.direction, "passthrough", None, why, None)


# ---------------------------------------------------------------- 2. post-trade review
POST_TRADE_SYSTEM = """You review individual closed trades for an automated trading bot.
Be concrete and brief: say whether the entry logic, Claude's decision, and the stop-loss placement looked sound in
hindsight, and name one thing worth watching. 2-4 sentences."""

POST_TRADE_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}


def post_trade_review(trade: dict, model: str) -> str:
    fields = {k: trade.get(k) for k in (
        "timestamp", "closed_at", "mode", "instrument", "asset_class", "strategy",
        "indicator_suggested_direction", "claude_final_direction", "claude_confidence", "claude_reasoning",
        "news_summary", "entry_price", "exit_price", "size", "stop_loss_used", "profit_loss", "indicators_used")}
    out = _run_cli(model, POST_TRADE_SYSTEM,
                   "Review this closed trade:\n" + json.dumps(fields, indent=1, default=str), POST_TRADE_SCHEMA)
    return out["note"]


# ---------------------------------------------------------------- 3. periodic review
PERIODIC_SYSTEM = """You perform periodic performance reviews of an automated trading bot.
Look across the batch for patterns: which strategies and instruments are winning or losing, whether stop-loss
settings look too tight or too loose (e.g. many trades stopped out just before reversing), whether Claude's
confirm/reverse/skip calls are adding value versus the raw technical signal, and any data or execution anomalies.
Write a concise summary and a list of specific, actionable flagged issues (empty if none). Recommend config changes
by naming the exact config field; never suggest raising risk limits as a way to improve results."""

PERIODIC_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "flagged_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "flagged_issues"],
    "additionalProperties": False,
}


def periodic_review(trades: list[dict], skipped: list[dict], configs: dict, model: str) -> dict:
    payload = {
        "closed_trades": trades,
        "signals_claude_skipped": skipped,
        "current_config": configs,
    }
    return _run_cli(model, PERIODIC_SYSTEM,
                    "Review this batch:\n" + json.dumps(payload, indent=1, default=str), PERIODIC_SCHEMA)


# ---------------------------------------------------------------- 4. code/bug monitor
BUG_SYSTEM = """You diagnose runtime problems in a Python (FastAPI) trading bot that trades via the IG REST API.
You receive the error, stack trace, surrounding context and relevant source code.
Explain the most likely root cause and propose a concrete fix (a code change or a config change). A human reviews
and applies every fix; do not assume anything will be patched automatically. If the error is an external/transient
condition (market closed, rate limit, network), say so and suggest how the bot should handle it."""

BUG_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "suggested_fix": {"type": "string"},
    },
    "required": ["diagnosis", "suggested_fix"],
    "additionalProperties": False,
}


def diagnose(trigger_type: str, summary: str, stack: Optional[str], context: dict, source: str, model: str) -> dict:
    prompt = (f"Trigger: {trigger_type}\nError: {summary}\n\nContext:\n{json.dumps(context, indent=1, default=str)}"
              f"\n\nStack trace:\n{stack or '(none)'}\n\nRelevant source:\n{source or '(none)'}")
    return _run_cli(model, BUG_SYSTEM, prompt, BUG_SCHEMA)
