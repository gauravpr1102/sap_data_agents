"""The Claude agent: a tool-use loop over the governed tools (DECISIONS D2-D5).

Claude decides which tools to call and writes the answer. Everything that
must hold regardless of what the model does is enforced here, in code:

    question -> input screen -> model/tool loop (budgets, RBAC in the tools)
             -> table guard -> figure grounding -> output screen -> answer

Each question produces one trace (telemetry.py) and every tool call one audit
row (tools.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import duckdb

from . import audit, guard, guardrails, llm
from .config import ROOT, ClientConfig
from .telemetry import Trace
from .tools import Session, Toolbox, tool_definitions

DOMAIN_DIR = ROOT / "domain"
MAX_TURNS = 10


def system_prompt(cfg: ClientConfig) -> str:
    values = {
        "client_name": cfg.client_name,
        "horizon_days": str(cfg.horizon_days),
        "hi_pct": f"{float(cfg.get('forecast', 'change_threshold_high_pct')):g}",
        "lo_pct": f"{float(cfg.get('forecast', 'change_threshold_low_pct')):g}",
        "draftable_ranks": ", ".join(str(r) for r in cfg.get("forecast", "draftable_ranks")),
    }
    parts = []
    for path in sorted(DOMAIN_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for k, v in values.items():
            text = text.replace("{" + k + "}", v)
        parts.append(text.strip())
    return "\n\n".join(parts)


@dataclass
class AgentResult:
    answer: str
    tool_calls: list[dict] = field(default_factory=list)
    blocked: bool = False
    refused: bool = False
    stop_reason: str | None = None
    usage: dict = field(default_factory=dict)
    guardrail: str = ""
    trace_id: str = ""


FIGURE_NUDGE = ("Your answer contains figures that no tool returned in this turn: {figs}. Only state numbers "
                "that appear in tool results; don't calculate new ones. Rewrite the answer.")
FIGURE_BLOCKED = ("I couldn't back every figure in that answer with data from the planning tools, so I've held "
                  "it back. Try asking for the specific list or material directly.")


class PlanningAgent:
    def __init__(self, con: duckdb.DuckDBPyConnection, cfg: ClientConfig, session: Session,
                 client: Any | None = None, use_fallbacks: bool | None = None,
                 content_filter: guardrails.ContentFilter | None = None):
        self.settings = llm.settings(cfg)
        self.client = client if client is not None else llm.make_client(self.settings)
        self.con, self.cfg, self.session = con, cfg, session
        self.toolbox = Toolbox(con, cfg, session, channel="agent")
        self.tools = tool_definitions(cfg, session)
        self.system = [{"type": "text", "text": system_prompt(cfg), "cache_control": {"type": "ephemeral"}}]
        self.model = self.settings.model
        self.effort = self.settings.effort
        self.use_fallbacks = self.settings.supports_fallbacks if use_fallbacks is None else use_fallbacks
        self.content_filter = content_filter or guardrails.content_filter_from_env()
        self.history: list[dict] = []

    def _create(self, messages: list[dict]):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=self.system,
            tools=self.tools,
            output_config={"effort": self.effort},
            messages=messages,
        )
        if self.use_fallbacks:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return self.client.beta.messages.create(**kwargs)

    def _finish(self, trace: Trace, result: AgentResult, messages: list[dict] | None) -> AgentResult:
        if messages is not None:
            self.history = messages
        trace.tools = result.tool_calls
        trace.guardrail = result.guardrail
        trace.outcome = "blocked" if result.blocked else ("refused" if result.refused else "answered")
        trace.finish(self.con)
        result.trace_id = trace.trace_id
        result.usage = {"input_tokens": trace.input_tokens, "output_tokens": trace.output_tokens,
                        "cache_read_input_tokens": trace.cache_read_tokens, "cost_usd": trace.cost_usd}
        return result

    def ask(self, question: str) -> AgentResult:
        """One user turn. Conversation history is kept on the agent (append-only)."""
        trace = Trace(user=self.session.user, channel="agent", question=question)

        screened = guardrails.screen_input(question, self.content_filter)
        if not screened.allowed:
            audit.log(self.con, f"agent:{self.session.user}", "input_blocked", screened.category,
                      {"reason": screened.reason})
            return self._finish(trace, AgentResult(guardrails.INPUT_REFUSAL.format(reason=screened.reason),
                                                   blocked=True, guardrail=screened.category,
                                                   stop_reason="input_guard"), None)

        messages = self.history + [{"role": "user", "content": question}]
        calls: list[dict] = []
        outputs: list[str] = []
        successful = 0
        table_nudged = figure_nudged = False
        for _ in range(MAX_TURNS):
            response = self._create(messages)
            trace.add_usage(self.model, getattr(response, "usage", None))
            if response.stop_reason == "refusal":
                return self._finish(trace, AgentResult("I can't help with that request.", calls, refused=True,
                                                       stop_reason="refusal"),
                                    messages + [{"role": "assistant", "content": response.content}])
            messages = messages + [{"role": "assistant", "content": response.content}]
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_uses:
                results = []
                for b in tool_uses:
                    if len(calls) >= guardrails.MAX_TOOL_CALLS_PER_QUESTION:
                        text, is_error = json.dumps({"error": "tool-call budget for this question is used up; "
                                                              "answer with what you have"}), True
                    else:
                        text, is_error = self.toolbox.run(b.name, dict(b.input))
                    calls.append({"tool": b.name, "input": dict(b.input), "error": is_error})
                    if not is_error:
                        successful += 1
                        outputs.append(text)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": text,
                                    **({"is_error": True} if is_error else {})})
                messages = messages + [{"role": "user", "content": results}]
                continue
            if response.stop_reason == "pause_turn":
                continue
            answer = "".join(b.text for b in response.content if b.type == "text").strip()

            if guard.should_block(answer, successful):
                if not table_nudged:
                    table_nudged = True
                    messages = messages + [{"role": "user", "content": guard.BLOCK_NUDGE}]
                    continue
                audit.log(self.con, f"agent:{self.session.user}", "answer_blocked", "grounding_guard",
                          {"question": question})
                return self._finish(trace, AgentResult(guard.BLOCKED_MESSAGE, calls, blocked=True,
                                                       guardrail="ungrounded_table",
                                                       stop_reason=response.stop_reason), messages)

            out = guardrails.screen_output(answer, outputs, question, content_filter=self.content_filter)
            if not out.allowed:
                if out.category == "ungrounded_figure" and not figure_nudged:
                    figure_nudged = True
                    messages = messages + [{"role": "user",
                                            "content": FIGURE_NUDGE.format(figs=", ".join(out.details[:10]))}]
                    continue
                audit.log(self.con, f"agent:{self.session.user}", "answer_blocked", out.category,
                          {"question": question, "reason": out.reason, "details": out.details[:20]})
                message = FIGURE_BLOCKED if out.category == "ungrounded_figure" else (
                    f"I've held that answer back: {out.reason}.")
                return self._finish(trace, AgentResult(message, calls, blocked=True, guardrail=out.category,
                                                       stop_reason=response.stop_reason), messages)

            return self._finish(trace, AgentResult(answer, calls, stop_reason=response.stop_reason), messages)
        return self._finish(trace, AgentResult(
            "I stopped after too many steps without a final answer. Try a narrower question.",
            calls, stop_reason="max_turns"), messages)


def tool_call_summary(result: AgentResult) -> str:
    return json.dumps(result.tool_calls, indent=2)
