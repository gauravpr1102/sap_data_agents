"""Promptfoo provider: puts each red-team prompt to the planning agent as a named user.

PROMPTFOO_SAPKIT_RUNNER selects the agent: "offline" (default, no model) or
"claude-code" (real Claude through Claude Code + the MCP server, no API key).
"""
from __future__ import annotations

import os

from sapkit.config import load_config
from sapkit.evals.runners import RUNNERS


def call_api(prompt, options, context):
    vars_ = (context or {}).get("vars", {})
    user = vars_.get("user", "morgan.manager@acme-dummy.example")
    runner = os.environ.get("PROMPTFOO_SAPKIT_RUNNER", "offline")
    obs = RUNNERS[runner](load_config(), user, prompt)
    if obs.error:  # includes "usage limit reached": an error, never a pass
        return {"error": obs.error}
    return {"output": obs.answer, "metadata": {"tools": obs.tools, "blocked": obs.blocked,
                                               "guardrail": obs.guardrail, "tool_errors": obs.tool_errors}}
