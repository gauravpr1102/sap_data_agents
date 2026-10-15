"""Tracing, token use and cost (docs/OPERATIONS.md).

Every question gets a trace: who asked, through which channel, how long it
took, which tools ran, whether a guardrail fired, and the tokens and estimated
cost of each model call. Traces always go to the ``ops_trace`` and
``ops_llm_usage`` tables. If an Opik server is configured (``OPIK_URL_OVERRIDE``
or ``OPIK_API_KEY``) and the ``opik`` package is installed, each trace is also
sent there for dashboards and scoring.

Structured log lines (JSON, one per trace) go to stderr when SAPKIT_LOG_JSON=1.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import duckdb

# USD per million tokens. Kept here, not in prompts; update when prices change.
PRICES = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10},
}


def redact(text: str) -> str:
    """Remove card and ID numbers before a question is stored (blocked questions are traced too)."""
    import re
    text = re.sub(r"\b(?:\d[ -]?){13,16}\b", "[card number removed]", text or "")
    return re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[id number removed]", text)


def estimate_cost(model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> float:
    p = PRICES.get(model)
    if p is None:
        return 0.0
    return round((input_tokens * p["input"] + output_tokens * p["output"]
                  + cache_read_tokens * p["cache_read"]) / 1_000_000, 6)


@dataclass
class Trace:
    user: str
    channel: str
    question: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.time)
    tools: list[dict] = field(default_factory=list)
    guardrail: str = ""
    outcome: str = "answered"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    model_calls: int = 0

    def add_usage(self, model: str, usage: Any) -> None:
        self.model = model
        self.model_calls += 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens, self.cache_read_tokens)

    def finish(self, con: duckdb.DuckDBPyConnection | None) -> None:
        latency_ms = int((time.time() - self.started) * 1000)
        row = {
            "trace_id": self.trace_id, "user": self.user, "channel": self.channel,
            "question": redact(self.question)[:500], "latency_ms": latency_ms, "outcome": self.outcome,
            "guardrail": self.guardrail, "tool_calls": len(self.tools),
            "tools": json.dumps([t.get("tool") for t in self.tools]),
            "model": self.model, "model_calls": self.model_calls,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens, "cost_usd": self.cost_usd,
        }
        if con is not None:
            try:
                con.execute(
                    "INSERT INTO ops_trace (trace_id, user_id, channel, question, latency_ms, outcome, guardrail, "
                    "tool_calls, tools, model, model_calls, input_tokens, output_tokens, cache_read_tokens, cost_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [row[k] for k in ("trace_id", "user", "channel", "question", "latency_ms", "outcome",
                                      "guardrail", "tool_calls", "tools", "model", "model_calls",
                                      "input_tokens", "output_tokens", "cache_read_tokens", "cost_usd")])
            except duckdb.Error:
                pass  # read-only connection
        if os.environ.get("SAPKIT_LOG_JSON") == "1":
            print(json.dumps({"event": "trace", **row}), file=sys.stderr)
        _export_opik(self, row)


def _export_opik(trace: Trace, row: dict) -> None:
    if not (os.environ.get("OPIK_URL_OVERRIDE") or os.environ.get("OPIK_API_KEY")):
        return
    try:
        import opik  # optional dependency: pip install opik
    except ImportError:
        return
    try:
        client = opik.Opik(project_name=os.environ.get("OPIK_PROJECT_NAME", "sapkit"))
        client.trace(name=f"{trace.channel}:ask", input={"question": trace.question, "user": trace.user},
                     output={"outcome": trace.outcome, "guardrail": trace.guardrail},
                     metadata={k: v for k, v in row.items() if k not in ("question",)})
        client.flush()
    except Exception as e:  # noqa: BLE001 - tracing must never break an answer
        print(f"opik export failed: {type(e).__name__}: {e}", file=sys.stderr)


def summary(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cur = con.execute("""
        SELECT channel, count(*) AS questions,
               round(avg(latency_ms)) AS avg_latency_ms,
               round(quantile_cont(latency_ms, 0.95)) AS p95_latency_ms,
               count(*) FILTER (WHERE outcome <> 'answered') AS blocked_or_refused,
               sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
               round(sum(cost_usd), 4) AS cost_usd
        FROM ops_trace GROUP BY channel ORDER BY channel""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


__all__ = ["Trace", "estimate_cost", "summary", "PRICES", "asdict"]
