"""A lightweight MCP server over stdio (DECISIONS D27).

Same tools, same RBAC and audit as mcp_server.py, but without importing the
MCP SDK, whose import takes 5-13 seconds. Claude Code in print mode stops
waiting for a server well before that, so with the SDK the tools were missing
on some runs. This server answers ``initialize`` in about a second.

Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (the MCP stdio
transport). Supported methods: initialize, ping, tools/list, tools/call;
notifications are accepted and ignored. Anything else gets "method not found".
Compatibility is tested with the official MCP client (tests/test_mcp_server.py).
"""
from __future__ import annotations

import json
import os
import sys

from . import __version__

SERVER_PROTOCOL = "2025-06-18"


def _session(cfg):
    from .rbac import AccessDenied, Session, load_rbac
    user = os.environ.get("SAPKIT_USER", "").strip()
    try:
        session = load_rbac().session_for(user, cfg)
    except AccessDenied:
        return Session(user=user or "anonymous", allowed_plants=[], tools=frozenset(), actions=frozenset())
    plants = os.environ.get("SAPKIT_PLANTS", "").strip()
    if plants:
        session = session.restricted_to([p.strip() for p in plants.split(",") if p.strip()])
    return session


class Server:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = _session(cfg)
        self._tools = None

    def tools(self) -> list[dict]:
        if self._tools is None:
            from .tools import tool_definitions
            self._tools = [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                           for t in tool_definitions(self.cfg, self.session)]  # only tools this user may use
        return self._tools

    def call(self, name: str, args: dict) -> dict:
        from . import db
        from .tools import Toolbox
        con = db.connect(self.cfg, session=self.session)
        try:
            text, is_error = Toolbox(con, self.cfg, self.session, channel="mcp").run(name, args or {})
        finally:
            con.close()
        if is_error:
            try:
                text = json.loads(text).get("error", text)
            except ValueError:
                pass
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # a notification: never answered
            return None
        try:
            if method == "initialize":
                from .agent import system_prompt
                requested = (msg.get("params") or {}).get("protocolVersion") or SERVER_PROTOCOL
                result = {"protocolVersion": requested,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "sapkit", "title": f"SAP planning tools: {self.cfg.client_name}",
                                         "version": __version__},
                          "instructions": system_prompt(self.cfg)}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                p = msg.get("params") or {}
                result = self.call(p.get("name", ""), p.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except Exception as e:  # noqa: BLE001 - a server error is returned to the client, never crashes the server
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(config_path: str | None = None, stdin=None, stdout=None) -> None:
    from .config import load_config
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(load_config(config_path))
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            batch = msg if isinstance(msg, list) else [msg]
            replies = [r for r in (server.handle(m) for m in batch if isinstance(m, dict)) if r is not None]
            if not replies:
                continue
            reply = replies if isinstance(msg, list) else replies[0]
        stdout.write(json.dumps(reply, default=str) + "\n")
        stdout.flush()
