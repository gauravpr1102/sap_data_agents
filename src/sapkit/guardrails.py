"""Guardrails around the agent (docs/SECURITY.md).

Layers, in the order a request meets them:

1. Input screen: oversized input, prompt-injection patterns, personal data the
   user shouldn't paste in. Injection attempts are refused before any model
   or tool sees them.
2. Content filter hook: an optional external classifier (Model Armor on
   Google Cloud, or any HTTP service) for inputs and outputs. Off by default;
   the interface is fixed so it can be switched on per client.
3. Budgets: tool calls and model turns per question, so a looping agent
   stops.
4. Output checks:
   - grounding: a table needs a tool call this turn (guard.py), and every
     significant number in the answer must appear in this turn's tool
     results;
   - disclosure: no prices or costs in anything addressed to a supplier, no
     personal data echoed back.

Every block is returned as a ``GuardResult`` with a reason, and the caller
writes it to the audit log.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Protocol

MAX_INPUT_CHARS = 4000
MAX_TOOL_CALLS_PER_QUESTION = 12

_INJECTION_PATTERNS = [
    r"ignore (all |any |your |the )?(previous |prior |above )?(instructions|rules|guidelines)",
    r"disregard (all |your |the )?(previous |prior )?(instructions|rules)",
    r"you are now (in )?(developer|dan|admin|god) mode",
    r"(reveal|print|show|repeat) (me )?(your|the) (system prompt|instructions|hidden prompt)",
    r"pretend (that )?you (are|have) (an? )?(admin|administrator|no restrictions)",
    r"\bjailbreak\b",
    r"act as (if you were )?(an? )?(admin|administrator|root)",
    r"(bypass|override|disable) (the )?(rbac|access control|permissions|guardrails|security)",
    r"i am (the |an )?(admin|administrator|ceo|owner).{0,40}(show|give|grant).{0,30}(all|every)",
    r"(ignore|bypass|override|remove|lift) (my |the |all |these |any )?(\w+ )?(restrictions|limits|scope|permissions)",
]
# Text that looks like an attempt to inject SQL through a question. The tools bind
# every parameter, so this can't reach the database anyway; refusing it early is
# defence in depth and keeps such attempts visible in the audit log.
_SQLI_PATTERNS = [
    r"'\s*or\s*'?\d*'?\s*=\s*'?\d", r";\s*(drop|delete|update|insert|alter|truncate)\s",
    r"\bunion\s+(all\s+)?select\b", r"--\s*$", r"/\*.*\*/", r"\bxp_cmdshell\b",
]
_PII_PATTERNS = {
    "payment card number": r"\b(?:\d[ -]?){13,16}\b",
    "US social security number": r"\b\d{3}-\d{2}-\d{4}\b",
    "IBAN": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
}
_PRICE_PATTERNS = [r"\$\s?\d", r"\bunit (price|cost)\b", r"\bprice list\b", r"\bstandard price\b", r"\bUSD\b"]


@dataclass
class GuardResult:
    allowed: bool
    reason: str = ""
    category: str = ""
    details: list[str] = field(default_factory=list)


ALLOW = GuardResult(True)


# ------------------------------------------------------------------ content filter hook
class ContentFilter(Protocol):
    def screen(self, text: str, direction: str) -> GuardResult: ...


class NoContentFilter:
    def screen(self, text: str, direction: str) -> GuardResult:
        return ALLOW


class HttpContentFilter:
    """Calls an external screening endpoint (e.g. a Model Armor proxy).

    The endpoint receives {"text", "direction"} and returns {"allowed": bool,
    "reason": str}. Enabled by SAPKIT_CONTENT_FILTER_URL. If the endpoint is
    unreachable the request is blocked (fail closed).
    """

    def __init__(self, url: str, timeout: float = 5.0):
        self.url, self.timeout = url, timeout

    def screen(self, text: str, direction: str) -> GuardResult:
        import httpx
        try:
            r = httpx.post(self.url, json={"text": text, "direction": direction}, timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
        except Exception as e:  # noqa: BLE001 - fail closed on any screening failure
            return GuardResult(False, f"content filter unavailable ({type(e).__name__})", "content_filter")
        if body.get("allowed", False):
            return ALLOW
        return GuardResult(False, body.get("reason", "blocked by content filter"), "content_filter")


def content_filter_from_env() -> ContentFilter:
    url = os.environ.get("SAPKIT_CONTENT_FILTER_URL", "").strip()
    return HttpContentFilter(url) if url else NoContentFilter()


# ------------------------------------------------------------------ input
def screen_input(text: str, content_filter: ContentFilter | None = None) -> GuardResult:
    if not text or not text.strip():
        return GuardResult(False, "empty question", "empty")
    if len(text) > MAX_INPUT_CHARS:
        return GuardResult(False, f"question is longer than {MAX_INPUT_CHARS} characters", "too_long")
    low = text.lower()
    hits = [p for p in _INJECTION_PATTERNS if re.search(p, low)]
    if hits:
        return GuardResult(False, "the question tries to change the assistant's rules or access", "injection", hits)
    if any(re.search(p, low) for p in _SQLI_PATTERNS):
        return GuardResult(False, "the question contains what looks like a database command", "sql_injection")
    pii = [name for name, p in _PII_PATTERNS.items() if re.search(p, text)]
    if pii:
        return GuardResult(False, f"the question contains what looks like a {pii[0]}; please remove it",
                           "personal_data", pii)
    if content_filter is not None:
        return content_filter.screen(text, "input")
    return ALLOW


# ------------------------------------------------------------------ output
_NUM = re.compile(r"(?<![\w.])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(?![\w])")
_IGNORED_WORDS_BEFORE = ("plant", "rank", "#", "top", "step", "page")


def _numbers_in(text: str) -> set[str]:
    out = set()
    for m in _NUM.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        out.add(_canon(val))
    return out


def _numbers_in_tool_output(text: str) -> set[str]:
    """Numbers in a tool result: walk the parsed JSON so escapes like \\n don't hide them."""
    import json
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return _numbers_in(text)
    out: set[str] = set()

    def walk(v) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            out.add(_canon(float(v)))
        elif isinstance(v, str):
            out.update(_numbers_in(v))
        elif isinstance(v, dict):
            for k, x in v.items():
                out.update(_numbers_in(str(k)))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(data)
    return out


def _canon(val: float) -> str:
    return str(int(val)) if float(val).is_integer() else f"{val:.2f}".rstrip("0").rstrip(".")


def ungrounded_numbers(answer: str, tool_outputs: list[str], question: str = "") -> list[str]:
    """Significant numbers in the answer that no tool output (or the question) contains.

    Ignores small counts (<= 31: days, list positions, ranks), years, and
    numbers the user typed. Dates are checked as their parts.
    """
    allowed: set[str] = set()
    for t in tool_outputs + [question]:
        found = _numbers_in_tool_output(t)
        allowed |= found
        # Percentages are rounded in prose ("+41%" for 40.5): accept the rounding.
        for n in list(found):
            try:
                v = float(n)
                allowed.add(_canon(round(v)))
                allowed.add(_canon(abs(v)))
                allowed.add(_canon(round(abs(v))))
            except ValueError:
                pass
    missing = []
    for n in sorted(_numbers_in(answer)):
        v = float(n)
        if abs(v) <= 31 or 1990 <= abs(v) <= 2100:
            continue
        if n not in allowed:
            missing.append(n)
    return missing


def screen_output(answer: str, tool_outputs: list[str], question: str = "",
                  addressed_to_supplier: bool = False,
                  content_filter: ContentFilter | None = None) -> GuardResult:
    if addressed_to_supplier and any(re.search(p, answer, re.I) for p in _PRICE_PATTERNS):
        return GuardResult(False, "pricing must never appear in text meant for a supplier", "pricing")
    pii = [name for name, p in _PII_PATTERNS.items() if re.search(p, answer)]
    if pii:
        return GuardResult(False, f"answer contains what looks like a {pii[0]}", "personal_data", pii)
    if tool_outputs:
        missing = ungrounded_numbers(answer, tool_outputs, question)
        if missing:
            return GuardResult(False, "answer contains figures no tool returned", "ungrounded_figure", missing)
    if content_filter is not None:
        return content_filter.screen(answer, "output")
    return ALLOW


INPUT_REFUSAL = ("I can't help with that request: {reason}. I can answer questions about shortages, late "
                 "shipments, forecast changes, supplier outreach and replies for the plants you have access to.")
