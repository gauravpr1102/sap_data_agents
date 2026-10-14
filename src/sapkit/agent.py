"""The Claude agent: a tool-use loop over the governed tools (DECISIONS D2-D5).

Claude decides which tools to call and writes the answer. The tools, the
plant scope and the grounding guard are enforced here, in code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import duckdb

from . import audit, guard
from .config import ROOT, ClientConfig
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


class PlanningAgent:
    def __init__(self, con: duckdb.DuckDBPyConnection, cfg: ClientConfig, session: Session,
                 client: Any | None = None, use_fallbacks: bool = True):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.con, self.cfg, self.session = con, cfg, session
        self.toolbox = Toolbox(con, cfg, session)
        self.tools = tool_definitions(cfg)
        self.system = [{"type": "text", "text": system_prompt(cfg), "cache_control": {"type": "ephemeral"}}]
        self.model = cfg.get("replies", "model", default="claude-opus-5-5")
        self.effort = cfg.get("replies", "effort", default="medium")
        self.use_fallbacks = use_fallbacks
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

    def ask(self, question: str) -> AgentResult:
        """One user turn. Conversation history is kept on the agent (append-only)."""
        messages = self.history + [{"role": "user", "content": question}]
        calls: list[dict] = []
        successful = 0
        nudged = False
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        for _ in range(MAX_TURNS):
            response = self._create(messages)
            u = getattr(response, "usage", None)
            for k in usage:
                usage[k] += int(getattr(u, k, 0) or 0) if u is not None else 0
            if response.stop_reason == "refusal":
                self.history = messages + [{"role": "assistant", "content": response.content}]
                return AgentResult("I can't help with that request.", calls, refused=True,
                                   stop_reason="refusal", usage=usage)
            messages = messages + [{"role": "assistant", "content": response.content}]
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "tool_use" and tool_uses:
                results = []
                for b in tool_uses:
                    text, is_error = self.toolbox.run(b.name, dict(b.input))
                    calls.append({"tool": b.name, "input": dict(b.input), "error": is_error})
                    audit.log(self.con, f"agent:{self.session.user}", "tool_call", b.name,
                              {"input": dict(b.input), "error": is_error})
                    if not is_error:
                        successful += 1
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": text,
                                    **({"is_error": True} if is_error else {})})
                messages = messages + [{"role": "user", "content": results}]
                continue
            if response.stop_reason == "pause_turn":
                continue
            answer = "".join(b.text for b in response.content if b.type == "text").strip()
            if guard.should_block(answer, successful):
                if not nudged:
                    nudged = True
                    messages = messages + [{"role": "user", "content": guard.BLOCK_NUDGE}]
                    continue
                audit.log(self.con, f"agent:{self.session.user}", "answer_blocked", "grounding_guard",
                          {"question": question})
                self.history = messages
                return AgentResult(guard.BLOCKED_MESSAGE, calls, blocked=True,
                                   stop_reason=response.stop_reason, usage=usage)
            self.history = messages
            return AgentResult(answer, calls, stop_reason=response.stop_reason, usage=usage)
        self.history = messages
        return AgentResult("I stopped after too many steps without a final answer. Try a narrower question.",
                           calls, stop_reason="max_turns", usage=usage)


def tool_call_summary(result: AgentResult) -> str:
    return json.dumps(result.tool_calls, indent=2)
