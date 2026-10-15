"""Ways to put an eval question to an agent. Each returns the same Observation.

- ``offline``: the rule-based agent (no model, no key). Fast; used in CI.
- ``api``: the Claude agent through the Claude API or Vertex AI (needs credentials).
- ``claude-code``: real Claude through the Claude Code CLI, connected to the
  kit's MCP server as the case's user. No API key: uses the signed-in Claude
  account. Built-in tools are switched off, so Claude can only use our tools.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..config import ClientConfig
from ..rbac import AccessDenied, load_rbac


@dataclass
class Observation:
    answer: str
    tools: list[str] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
    tool_errors: int = 0
    blocked: bool = False
    refused: bool = False
    guardrail: str = ""
    seconds: float = 0.0
    cost_usd: float = 0.0
    error: str = ""
    mcp_status: str = ""


def run_offline(cfg: ClientConfig, user: str, question: str) -> Observation:
    try:
        load_rbac().session_for(user, cfg)
    except AccessDenied as e:  # an unknown or role-less user gets nothing, exactly as in the product
        return Observation(f"Access denied: {e}", refused=True)
    from ..offline_agent import OfflineAgent
    session = load_rbac().session_for(user, cfg)
    con = db.connect(cfg, session=session)
    t0 = time.time()
    try:
        agent = OfflineAgent(con, cfg, session)
        res = agent.ask(question)
        outputs = list(getattr(agent, "outputs", []))
    finally:
        con.close()
    return Observation(res.answer, [c["tool"] for c in res.tool_calls], outputs,
                       sum(1 for c in res.tool_calls if c["error"]), res.blocked, res.refused, res.guardrail,
                       round(time.time() - t0, 2))


def run_api(cfg: ClientConfig, user: str, question: str) -> Observation:
    try:
        load_rbac().session_for(user, cfg)
    except AccessDenied as e:  # an unknown or role-less user gets nothing, exactly as in the product
        return Observation(f"Access denied: {e}", refused=True)
    from ..agent import PlanningAgent
    session = load_rbac().session_for(user, cfg)
    con = db.connect(cfg, session=session)
    t0 = time.time()
    try:
        agent = PlanningAgent(con, cfg, session)
        res = agent.ask(question)
    finally:
        con.close()
    return Observation(res.answer, [c["tool"] for c in res.tool_calls], [],
                       sum(1 for c in res.tool_calls if c["error"]), res.blocked, res.refused, res.guardrail,
                       round(time.time() - t0, 2), res.usage.get("cost_usd", 0.0))


def run_claude_code(cfg: ClientConfig, user: str, question: str, timeout: int = 300) -> Observation:
    exe = shutil.which("claude")
    if not exe:
        return Observation("", error="Claude Code CLI not found")
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        from ..agent import system_prompt
        mcp = {"mcpServers": {"sapkit": {
            "command": sys.executable.replace("\\", "/"),
            "args": ["-m", "sapkit", "--config", str(cfg.path).replace("\\", "/"), "mcp", "--user", user],
            "env": {"SAPKIT_USER": user, **({"SAPKIT_DB": os.environ["SAPKIT_DB"]} if os.environ.get("SAPKIT_DB") else {})},
        }}}
        cfg_path = Path(tmp) / "mcp.json"
        cfg_path.write_text(json.dumps(mcp), encoding="utf-8")
        prompt = system_prompt(cfg) + ("\n\n# Tools\nAnswer only with the sapkit tools available to you. "
                                       "If none of them can answer, say so.")
        # Our planning system prompt, no personal Claude Code settings (output styles, hooks),
        # no built-in tools: only the governed MCP tools, exactly as a deployment would run.
        cmd = [exe, "-p", question, "--system-prompt", prompt, "--setting-sources", "",
               "--mcp-config", str(cfg_path), "--strict-mcp-config", "--tools", "",
               "--allowedTools", "mcp__sapkit__*", "--output-format", "stream-json", "--verbose"]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # Print mode otherwise starts before the MCP server finishes connecting (it takes ~2 s),
        # and Claude then has no tools for the whole run.
        env["MCP_CONNECTION_NONBLOCKING"] = "false"
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                                 cwd=tmp, env=env)
        except subprocess.TimeoutExpired:
            return Observation("", error="timeout", seconds=timeout)
    obs = Observation("", seconds=round(time.time() - t0, 1))
    for line in out.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            status = {s.get("name"): s.get("status") for s in ev.get("mcp_servers", [])}
            obs.mcp_status = status.get("sapkit", "missing")
        elif ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    obs.tools.append(block.get("name", "").replace("mcp__sapkit__", ""))
        elif ev.get("type") == "user":
            for block in ev.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    text = (content if isinstance(content, str) else
                            "".join(c.get("text", "") for c in (content or []) if isinstance(c, dict)))
                    if block.get("is_error"):
                        obs.tool_errors += 1
                    else:
                        obs.tool_outputs.append(text)
        elif ev.get("type") == "result":
            obs.answer = ev.get("result", "") or ""
            obs.cost_usd = float(ev.get("total_cost_usd", 0) or 0)
            if ev.get("is_error"):
                obs.error = ev.get("subtype", "error")
    if not obs.answer and out.returncode != 0:
        obs.error = obs.error or out.stderr[:300]
    if obs.mcp_status and obs.mcp_status != "connected":
        # The tools weren't available, so the answer says nothing about the agent: report it, don't score it.
        obs.error = f"MCP server not connected ({obs.mcp_status})"
    return obs


RUNNERS = {"offline": run_offline, "api": run_api, "claude-code": run_claude_code}
