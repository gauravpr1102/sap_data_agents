"""The nine governed tools served over MCP (DECISIONS D23).

This is the same tool layer the reference build exposes through MCP Toolbox. Any
MCP client can use it: Claude Code or Claude Desktop on the user's own Claude
login (no API key), or another agent framework.

    python -m sapkit mcp                       # stdio server, all plants
    SAPKIT_PLANTS=1000 python -m sapkit mcp    # a user who can only see plant 1000

Each call opens and closes its own DuckDB connection, so the server never
holds a lock that would block the other `sapkit` commands.
"""
from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import audit, db
from .agent import system_prompt
from .config import load_config
from .tools import Session, Toolbox


def _session(cfg) -> Session:
    env = os.environ.get("SAPKIT_PLANTS", "").strip()
    plants = [p.strip() for p in env.split(",") if p.strip()] if env else list(cfg.plants)
    return Session(user=os.environ.get("SAPKIT_USER", "mcp-user"), allowed_plants=plants)


def build_server(config_path: str | None = None) -> MCPServer:
    cfg = load_config(config_path)
    session = _session(cfg)
    server = MCPServer(
        name="sapkit",
        title=f"SAP planning tools: {cfg.client_name}",
        instructions=system_prompt(cfg),
    )

    def run(tool: str, args: dict) -> str:
        con = db.connect(cfg)
        try:
            text, is_error = Toolbox(con, cfg, session).run(tool, args)
            audit.log(con, f"mcp:{session.user}", "tool_call", tool, {"input": args, "error": is_error})
        finally:
            con.close()
        if is_error:
            # ToolError reaches the client with its message (a refusal the model should read);
            # any other exception would be reported as a generic crash.
            raise ToolError(json.loads(text).get("error", text))
        return text

    @server.tool(name="get_shortage_summary",
                 description="Shortage counts per plant: total, within 7 days, today, value at risk, missing "
                             "shipping notices and safety-stock dips. Use for cross-plant questions.")
    def get_shortage_summary() -> str:
        return run("get_shortage_summary", {})

    @server.tool(name="get_shortage_queue",
                 description="Ranked shortage queue for one plant, most urgent first. within_days=0 for the "
                             "whole 105-day horizon. limit 1-50.")
    def get_shortage_queue(plant: str, within_days: int = 0, limit: int = 10) -> str:
        return run("get_shortage_queue", {"plant": plant, "within_days": within_days, "limit": limit})

    @server.tool(name="get_shortage_detail",
                 description="Why one material is or isn't short at one plant: dated stock, supply and "
                             "requirement events with running balance, and open POs with shipping-notice status.")
    def get_shortage_detail(material: str, plant: str) -> str:
        return run("get_shortage_detail", {"material": material, "plant": plant})

    @server.tool(name="get_missing_asn",
                 description="PO lines that require a shipping notice, are past their ship-by date, and have none.")
    def get_missing_asn(plant: str, limit: int = 10) -> str:
        return run("get_missing_asn", {"plant": plant, "limit": limit})

    @server.tool(name="get_safety_stock_dips",
                 description="Materials whose projected balance dips into safety stock without going short.")
    def get_safety_stock_dips(plant: str, limit: int = 10) -> str:
        return run("get_safety_stock_dips", {"plant": plant, "limit": limit})

    @server.tool(name="get_forecast_changes",
                 description="Forecast changes between the last two snapshots, ranked 1-7 (see rank_legend). "
                             "ranks: list of ranks to include, empty for all.")
    def get_forecast_changes(plant: str, ranks: list[int] | None = None, limit: int = 10) -> str:
        return run("get_forecast_changes", {"plant": plant, "ranks": ranks or [], "limit": limit})

    @server.tool(name="get_outreach_status",
                 description="Supplier outreach eligibility for a plant: counts per outcome, vendors that would "
                             "get a draft, and items needing a buyer's attention with the reason.")
    def get_outreach_status(plant: str) -> str:
        return run("get_outreach_status", {"plant": plant})

    @server.tool(name="preview_supplier_email",
                 description="Preview the supplier e-mail that would be drafted for one vendor. Creates and sends nothing.")
    def preview_supplier_email(vendor: str) -> str:
        return run("preview_supplier_email", {"vendor": vendor})

    @server.tool(name="get_supplier_replies",
                 description="Processed supplier replies: category, summary, whether a buyer must review, "
                             "commitments; plus contacted vendors with no reply. Filters are optional.")
    def get_supplier_replies(vendor: str = "", category: str = "", needs_review_only: bool = False) -> str:
        return run("get_supplier_replies", {"vendor": vendor, "category": category,
                                            "needs_review_only": needs_review_only})

    return server


def main(config_path: str | None = None) -> None:
    build_server(config_path).run("stdio")
