"""The MCP server: the same tools over the protocol Claude Code and Claude Desktop speak."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _call(workdir, plants, calls):
    env = {**os.environ, "SAPKIT_DB": str(workdir / "db.duckdb"), "SAPKIT_PLANTS": plants}

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "sapkit", "mcp"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = await s.list_tools()
                results = [await s.call_tool(name, args) for name, args in calls]
                return init, tools, results

    return asyncio.run(go())


def test_mcp_serves_the_tools_with_planning_instructions(workdir, cfg):
    init, tools, (summary, queue, refused) = _call(workdir, "1000", [
        ("get_shortage_summary", {}),
        ("get_shortage_queue", {"plant": "1000", "within_days": 7, "limit": 3}),
        ("get_shortage_queue", {"plant": "2000"}),
    ])
    assert init.server_info.name == "sapkit"
    assert "Every number you state comes from a tool result" in (init.instructions or "")
    assert len(tools.tools) == 10
    s = json.loads(summary.content[0].text)
    assert {r["plant"] for r in s["rows"]} == {"1000"}  # scope from the environment, not the model
    q = json.loads(queue.content[0].text)
    assert not queue.is_error and q["returned"] <= 3 and q["data_as_of"] == "2026-10-14"
    assert refused.is_error and "do not have access to plant 2000" in refused.content[0].text


def test_mcp_calls_are_audited(workdir, cfg):
    _call(workdir, "1000,2000,3000", [("get_missing_asn", {"plant": "3000", "limit": 2})])
    import duckdb
    con = duckdb.connect(str(workdir / "db.duckdb"), read_only=True)
    n = con.execute("SELECT count(*) FROM ops_audit_log WHERE actor LIKE 'mcp:%' AND subject = 'get_missing_asn'").fetchone()[0]
    con.close()
    assert n == 1
