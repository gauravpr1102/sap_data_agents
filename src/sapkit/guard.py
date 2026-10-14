"""Grounding guard: a table may only appear in an answer if a tool produced data this turn.

Instructions alone hold most of the time but not always, so this check runs
in code after every model turn.
"""
from __future__ import annotations

import re

# A markdown table's separator row: dashes between pipes, e.g. |---|:--:|. A line
# must contain a pipe, so a plain "---" horizontal rule is not mistaken for one.
_SEPARATOR_ROW = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*")


def has_table(text: str) -> bool:
    return any("|" in line and _SEPARATOR_ROW.fullmatch(line) for line in (text or "").splitlines())


def should_block(text: str, successful_tool_calls_this_turn: int) -> bool:
    return has_table(text) and successful_tool_calls_this_turn == 0


BLOCK_NUDGE = ("Your last answer contained a table, but no tool returned data in this turn. "
               "Call the tool that provides those figures, or answer without a table.")
BLOCKED_MESSAGE = ("I couldn't back that answer with data from the planning tools, so I've held it back. "
                   "Try asking about a specific plant or material.")
