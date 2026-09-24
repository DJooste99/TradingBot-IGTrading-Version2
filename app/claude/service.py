"""Claude's four roles: decision engine, post-trade review, periodic review, bug monitor diagnosis."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import anthropic

from app.config import DecisionEngineConfig

log = logging.getLogger(__name__)

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

_client: Optional[anthropic.Anthropic] = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=3, timeout=300)
    return _client


class ClaudeError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def _web_search_tool(model: str, max_uses: int = 5) -> dict:
    # Dynamic-filtering web search needs Opus/Sonnet 4.6+; older models use the basic variant.
    legacy = ("haiku" in model or "claude-3" in model or re.search(r"-4-(5|1)\b|-4-\d{8}", model) is not None)
    return {"type": "web_search_20250305" if legacy else "web_search_20260209",
            "name": "web_search", "max_uses": max_uses}


def _thinking(model: str) -> dict:
    if "haiku" in model:
        return {}
    return {"thinking": {"type": "adaptive"}}


def _text(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


def _check(response) -> None:
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise ClaudeError(f"Claude declined the request: {getattr(details, 'explanation', None) or 'refusal'}")
    if response.stop_reason == "max_tokens":
        raise ClaudeError("Claude response hit max_tokens before finishing")


def _structured(model: str, system: str, prompt: str, schema: dict, max_tokens: int = 16000) -> dict:
    """Single call with a JSON-schema constrained answer (no tools)."""
    response = client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
        **_thinking(model),
    )
    _check(response)
    return json.loads(_text(response))


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
1. Recent news about the instrument and its drivers (use the web_search tool; focus on the lookback window given).
2. The bot's own recent trade history on this instrument/strategy, including outcomes and post-trade notes.
3. The indicator values and rules that fired.

You may: confirm the proposed direction, reverse it, or skip the trade entirely. Skipping is a perfectly good
outcome when news risk is high (e.g. an imminent central-bank decision or earnings), when the signal conflicts
with the news backdrop, or when recent history on this setup has been poor.

You do NOT control position size, stop-loss or exposure limits; a separate risk manager enforces those regardless
of your answer, so do not factor sizing into your verdict.

Finish your reply with a single JSON object in a ```json fenced block, with exactly these keys:
{"decision": "confirm" | "reverse" | "skip",
 "confidence": "low" | "medium" | "high",
 "reasoning": "<2-5 sentences explaining the verdict>",
 "news_summary": "<1-3 sentences on the news you weighed, or 'no material news found'>"}"""


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

    messages = [{"role": "user", "content": (
        f"Candidate trade signal follows. Search for news on {instrument_name} from the last "
        f"{cfg.news_lookback_hours} hours, then give your verdict.\n\n{prompt}")}]
    tools = [_web_search_tool(cfg.model)]

    response = None
    for _ in range(5):  # resume server-side tool loops that pause
        response = client().messages.create(
            model=cfg.model, max_tokens=16000, system=DECISION_SYSTEM,
            messages=messages, tools=tools, **_thinking(cfg.model))
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": response.content}]
    _check(response)

    verdict = _extract_json(_text(response))
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
    out = _structured(model, POST_TRADE_SYSTEM,
                      "Review this closed trade:\n" + json.dumps(fields, indent=1, default=str),
                      POST_TRADE_SCHEMA, max_tokens=4000)
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
    return _structured(model, PERIODIC_SYSTEM,
                       "Review this batch:\n" + json.dumps(payload, indent=1, default=str),
                       PERIODIC_SCHEMA)


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
    return _structured(model, BUG_SYSTEM, prompt, BUG_SCHEMA)
