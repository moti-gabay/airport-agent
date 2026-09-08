"""The agent loop: Claude plans which tools to call, deterministic code produces the numbers.

A hand-written loop rather than the SDK tool runner, for two reasons. The runner is a beta
surface, and every tool call has to be captured as a trace the UI can show, which means
seeing each round anyway.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import anthropic

from app.agent import memory
from app.agent.prompts import system_prompt
from app.tools import handlers
from app.tools.schemas import TOOL_SCHEMAS

MODEL = os.getenv("AGENT_MODEL", "claude-opus-5")
MAX_TOKENS = 4096
EFFORT = "medium"
MAX_TOOL_ROUNDS = 6

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env.")
        _client = anthropic.Anthropic()
    return _client


@dataclass
class ToolCall:
    name: str
    input: dict
    ok: bool
    summary: str


@dataclass
class ChatResult:
    session_id: str
    answer: str
    trace: list[ToolCall] = field(default_factory=list)
    rounds: int = 0
    stop_reason: str | None = None


def summarize(name: str, out: dict) -> str:
    """One line describing what a tool returned, for the trace panel in the UI."""
    if "error" in out:
        return f"{out['error']}: {out['message']}"
    if name == "rank_airports":
        top = [a["iata"] for a in out.get("ranked", [])]
        u = out.get("universe", {})
        return (f"{len(top)} of {u.get('in_filter', 0)} eligible airports"
                + (f" -> {', '.join(top[:5])}" if top else ""))
    if name == "compare_airports":
        return "compared " + ", ".join(a["iata"] for a in out.get("airports", []))
    if name == "long_haul_share":
        return (f"{out['iata']}: {out['share_pct']}% of {out['total_departures']:,} departures "
                f"at or beyond {out['threshold_mi']} mi")
    if name == "unmet_demand":
        high = out.get("signals_at_high_level") or ["none"]
        return f"{out['iata']}: high signals -> {', '.join(high)}"
    return "ok"


def _text(content: list) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()


def chat(user_text: str, session_id: str | None = None) -> ChatResult:
    """Run one user turn to completion, including any tool rounds it needs."""
    sid = session_id or uuid.uuid4().hex
    messages: list[dict[str, Any]] = memory.get(sid)
    messages.append({"role": "user", "content": user_text})

    result = ChatResult(session_id=sid, answer="")
    system = [{"type": "text", "text": system_prompt(),
               "cache_control": {"type": "ephemeral"}}]

    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        resp = client().messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, tools=TOOL_SCHEMAS,
            messages=messages, output_config={"effort": EFFORT},
        )
        result.rounds = round_no
        result.stop_reason = resp.stop_reason
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":
            result.answer = _text(resp.content) or "I can't help with that request."
            break
        if resp.stop_reason != "tool_use":
            result.answer = _text(resp.content)
            break

        tool_results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            out = handlers.dispatch(block.name, dict(block.input))
            result.trace.append(ToolCall(block.name, dict(block.input),
                                         "error" not in out, summarize(block.name, out)))
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": json.dumps(out, default=str),
                                 "is_error": "error" in out})
        # All results from one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": tool_results})
    else:
        # Ran out of tool rounds: ask for an answer from what has already been gathered.
        resp = client().messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=system, tools=TOOL_SCHEMAS,
            tool_choice={"type": "none"}, messages=messages,
            output_config={"effort": EFFORT},
        )
        messages.append({"role": "assistant", "content": resp.content})
        result.answer = _text(resp.content)
        result.stop_reason = "max_tool_rounds"

    memory.set(sid, messages)
    return result
