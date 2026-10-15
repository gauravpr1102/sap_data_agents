"""Governed tools: the only way the agent reads data.

Each tool runs reviewed SQL with bound parameters, enforces the caller's plant
scope (which comes from the session, never from the model), caps rows, and
returns the data as-of date and the full match count with every result.
"""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import duckdb

from . import audit, outreach
from .config import ClientConfig
from .db import rows
from .rbac import AccessDenied, Session  # noqa: F401  (Session re-exported for callers)

MAX_ROWS = 50
MAX_ARG_CHARS = 200  # no legitimate argument is longer; longer ones are refused


class ToolError(Exception):
    """A refusal or bad input, returned to the model as an error result."""


def _mat(material: str) -> str:
    digits = "".join(ch for ch in material if ch.isdigit())
    if not digits:
        raise ToolError(f"'{material}' is not a material number")
    return digits.zfill(18)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, str) and len(v) == 18 and v.isdigit():
        return v.lstrip("0") or "0"  # show material numbers the way users type them
    return v


class Toolbox:
    def __init__(self, con: duckdb.DuckDBPyConnection, cfg: ClientConfig, session: Session,
                 channel: str = "cli"):
        self.con, self.cfg, self.session, self.channel = con, cfg, session, channel
        self._handlers: dict[str, Callable[..., dict]] = {
            "get_shortage_summary": self.get_shortage_summary,
            "get_shortage_queue": self.get_shortage_queue,
            "get_shortage_detail": self.get_shortage_detail,
            "get_missing_asn": self.get_missing_asn,
            "get_safety_stock_dips": self.get_safety_stock_dips,
            "get_forecast_changes": self.get_forecast_changes,
            "get_outreach_status": self.get_outreach_status,
            "preview_supplier_email": self.preview_supplier_email,
            "get_supplier_replies": self.get_supplier_replies,
            "get_drafts_for_review": self.get_drafts_for_review,
        }

    # -------------------------------------------------------------- plumbing
    def _plant(self, plant: str) -> str:
        if plant not in self.cfg.plants:
            raise ToolError(f"Unknown plant '{plant}'. Plants: {', '.join(self.cfg.plants)}")
        if plant not in self.session.allowed_plants:
            raise ToolError(f"You do not have access to plant {plant}.")
        return plant

    def _envelope(self, data: list[dict], total: int, **extra: Any) -> dict:
        return {
            "data_as_of": self.cfg.as_of.isoformat(),
            "cover_rule": self.cfg.cover_rule,
            "total_matching": total,
            "returned": len(data),
            "truncated": total > len(data),
            **extra,
            "rows": [{k: _jsonable(v) for k, v in r.items()} for r in data],
        }

    def run(self, name: str, args: dict) -> tuple[str, bool]:
        """Execute a tool. Returns (json_text, is_error). Every call is audited."""
        decision, text, is_error = "allowed", "", False
        handler = self._handlers.get(name)
        if handler is None:
            decision, text, is_error = "unknown_tool", json.dumps({"error": f"unknown tool {name}"}), True
        elif not self.session.can_use_tool(name):
            decision = "denied_tool"
            text, is_error = json.dumps({"error": f"Your role does not allow the {name} tool."}), True
        else:
            try:
                self._check_args(args)
                text = json.dumps(handler(**args), default=str)
            except ToolError as e:
                decision = "denied_scope" if "access" in str(e) else "refused"
                text, is_error = json.dumps({"error": str(e)}), True
            except TypeError as e:
                decision, text, is_error = "bad_arguments", json.dumps({"error": f"bad arguments: {e}"}), True
        try:
            audit.log(self.con, f"{self.channel}:{self.session.user}", "tool_call", name,
                      {"input": args, "decision": decision, "error": is_error})
        except duckdb.Error:
            pass  # read-only connections can't write the audit log; callers that need audit use read-write
        return text, is_error

    @staticmethod
    def _check_args(args: dict) -> None:
        for k, v in args.items():
            if isinstance(v, str) and len(v) > MAX_ARG_CHARS:
                raise ToolError(f"Argument '{k}' is too long.")
            if isinstance(v, list) and len(v) > 20:
                raise ToolError(f"Argument '{k}' has too many items.")

    # -------------------------------------------------------------- tools
    def get_shortage_summary(self) -> dict:
        plants = self.session.allowed_plants
        ph = ",".join("?" * len(plants))
        data = rows(self.con, f"""
            SELECT s.werks AS plant,
                   count(*) AS shortages,
                   count(*) FILTER (WHERE days_to_short <= 7) AS short_within_7_days,
                   count(*) FILTER (WHERE days_to_short <= 0) AS short_today,
                   round(sum(coalesce(value_at_risk, 0)), 0) AS value_at_risk,
                   (SELECT count(*) FROM v_missing_asn m WHERE m.werks = s.werks) AS missing_asn_lines,
                   (SELECT count(*) FROM v_safety_stock_dip d WHERE d.werks = s.werks) AS safety_stock_dips
            FROM v_shortage s WHERE s.werks IN ({ph})
            GROUP BY s.werks ORDER BY value_at_risk DESC""", plants)
        names = self.cfg.plants
        for r in data:
            r["plant_name"] = names[r["plant"]]
        return self._envelope(data, len(data))

    def get_shortage_queue(self, plant: str, within_days: int = 0, limit: int = 10) -> dict:
        plant = self._plant(plant)
        limit = max(1, min(int(limit), MAX_ROWS))
        where = "werks = ?"
        params: list = [plant]
        if within_days and within_days > 0:
            where += " AND days_to_short <= ?"
            params.append(int(within_days))
        total = self.con.execute(f"SELECT count(*) FROM v_shortage WHERE {where}", params).fetchone()[0]
        data = rows(self.con, f"""
            SELECT risk_rank, matnr AS material, maktx AS description, first_short_date, days_to_short,
                   shortfall_qty, recovery_date, next_firm_receipt, parent_count AS assemblies_fed,
                   value_at_risk, risk_score, ekgrp AS purchasing_group
            FROM v_shortage WHERE {where} ORDER BY risk_rank LIMIT ?""", params + [limit])
        return self._envelope(data, total, plant=plant)

    def get_shortage_detail(self, material: str, plant: str) -> dict:
        plant = self._plant(plant)
        mat = _mat(material)
        scope = rows(self.con, "SELECT matnr, maktx, plifz, eisbe, ekgrp FROM v_scope WHERE matnr = ? AND werks = ?",
                     [mat, plant])
        if not scope:
            raise ToolError(f"Material {material} is not planned at plant {plant} (out of scope or unknown).")
        short = rows(self.con, """SELECT first_short_date, days_to_short, shortfall_qty, recovery_date,
                                         next_firm_receipt, parent_count AS assemblies_fed, unit_cost,
                                         value_at_risk, risk_rank, risk_score
                                  FROM v_shortage WHERE matnr = ? AND werks = ?""", [mat, plant])
        events = rows(self.con, """SELECT evt_date, kind, qty FROM v_events WHERE matnr = ? AND werks = ?
                                   ORDER BY evt_date, kind""", [mat, plant])
        balance = 0.0
        for e in events:
            balance += e["qty"]
            e["balance_after"] = balance
        pos = rows(self.con, """SELECT ebeln AS po, lifnr AS vendor, eindt AS delivery_date, open_qty,
                                       has_asn, asn_date, bstae AS confirmation_control
                                FROM v_po_lines WHERE matnr = ? AND werks = ? ORDER BY eindt""", [mat, plant])
        return {
            "data_as_of": self.cfg.as_of.isoformat(),
            "cover_rule": self.cfg.cover_rule,
            "material": _jsonable(mat), "plant": plant,
            "master": {k: _jsonable(v) for k, v in scope[0].items()},
            "is_short": bool(short),
            "shortage": {k: _jsonable(v) for k, v in short[0].items()} if short else None,
            "events_total": len(events),
            "events": [{k: _jsonable(v) for k, v in e.items()} for e in events[:MAX_ROWS]],
            "open_purchase_orders": [{k: _jsonable(v) for k, v in p.items()} for p in pos[:MAX_ROWS]],
        }

    def get_missing_asn(self, plant: str, limit: int = 10) -> dict:
        plant = self._plant(plant)
        limit = max(1, min(int(limit), MAX_ROWS))
        total = self.con.execute("SELECT count(*) FROM v_missing_asn WHERE werks = ?", [plant]).fetchone()[0]
        data = rows(self.con, """SELECT ebeln AS po, matnr AS material, maktx AS description, lifnr AS vendor,
                                        vendor_name, eindt AS delivery_date, CAST(ship_by_date AS DATE) AS ship_by_date,
                                        days_past_ship_by, open_qty
                                 FROM v_missing_asn WHERE werks = ?
                                 ORDER BY days_past_ship_by DESC, ebeln LIMIT ?""", [plant, limit])
        return self._envelope(data, total, plant=plant)

    def get_safety_stock_dips(self, plant: str, limit: int = 10) -> dict:
        plant = self._plant(plant)
        limit = max(1, min(int(limit), MAX_ROWS))
        total = self.con.execute("SELECT count(*) FROM v_safety_stock_dip WHERE werks = ?", [plant]).fetchone()[0]
        data = rows(self.con, """SELECT matnr AS material, maktx AS description, safety_stock, lowest_balance,
                                        first_dip_date
                                 FROM v_safety_stock_dip WHERE werks = ?
                                 ORDER BY first_dip_date, matnr LIMIT ?""", [plant, limit])
        return self._envelope(data, total, plant=plant)

    def get_forecast_changes(self, plant: str, ranks: list[int] | None = None, limit: int = 10) -> dict:
        plant = self._plant(plant)
        limit = max(1, min(int(limit), MAX_ROWS))
        ranks = [int(r) for r in (ranks or []) if 1 <= int(r) <= 7] or [1, 2, 3, 4, 5, 6, 7]
        ph = ",".join("?" * len(ranks))
        total = self.con.execute(f"SELECT count(*) FROM v_forecast_change WHERE werks = ? AND change_rank IN ({ph})",
                                 [plant, *ranks]).fetchone()[0]
        data = rows(self.con, f"""SELECT change_rank, matnr AS material, maktx AS description, window_ AS window,
                                         plifz AS lead_time_days, prior_qty, current_qty, change_qty, change_pct,
                                         capture_prev, capture_cur
                                  FROM v_forecast_change WHERE werks = ? AND change_rank IN ({ph})
                                  ORDER BY change_rank, abs(coalesce(change_pct, 1e9)) DESC, matnr LIMIT ?""",
                    [plant, *ranks, limit])
        return self._envelope(data, total, plant=plant,
                              rank_legend={"1": "inside lead time, >= high threshold",
                                           "2": "inside lead time, >= low threshold",
                                           "3": "outside lead time, >= high threshold",
                                           "4": "outside lead time, >= low threshold",
                                           "5": "inside lead time, small change (report only)",
                                           "6": "outside lead time, small change (report only)",
                                           "7": "new demand (no prior quantity)"})

    def get_outreach_status(self, plant: str) -> dict:
        plant = self._plant(plant)
        decisions = outreach.decide_all(self.con, self.cfg, [plant])
        counts: dict[str, int] = {}
        for d in decisions:
            counts[d["disposition"]] = counts.get(d["disposition"], 0) + 1
        vendors: dict[str, int] = {}
        for d in decisions:
            if d["disposition"] == "ELIGIBLE":
                vendors[d["resolved_vendor"]] = vendors.get(d["resolved_vendor"], 0) + 1
        needs_attention = [
            {"material": _jsonable(d["matnr"]), "change_rank": d["change_rank"], "disposition": d["disposition"],
             "reason": d["reason"], "vendor": _jsonable(d["resolved_vendor"]) if d["resolved_vendor"] else None}
            for d in decisions if d["disposition"] not in ("ELIGIBLE", "REPORT_ONLY")][:MAX_ROWS]
        return {
            "data_as_of": self.cfg.as_of.isoformat(), "plant": plant,
            "dispositions": counts,
            "reason_legend": outreach.REASONS,
            "eligible_vendors": [{"vendor": v.lstrip("0"), "items": n} for v, n in sorted(vendors.items())][:MAX_ROWS],
            "eligible_vendor_count": len(vendors),
            "needs_attention_total": sum(1 for d in decisions if d["disposition"] not in ("ELIGIBLE", "REPORT_ONLY")),
            "needs_attention": needs_attention,
        }

    def preview_supplier_email(self, vendor: str) -> dict:
        p = outreach.preview(self.con, self.cfg, "".join(ch for ch in vendor if ch.isdigit()) or vendor,
                             self.session.allowed_plants)
        p["note"] = "Preview only. Nothing is created or sent by this tool; the buyer sends."
        return p

    def get_drafts_for_review(self, status: str = "PENDING_REVIEW", limit: int = 10) -> dict:
        from .workflow import STATUSES, review_queue
        if status not in STATUSES:
            raise ToolError(f"Status must be one of {', '.join(STATUSES)}.")
        total, data = review_queue(self.con, self.session, status, max(1, min(int(limit), MAX_ROWS)))
        for d in data:
            d["vendor"] = d.pop("lifnr").lstrip("0")
        return self._envelope(data, total, status=status,
                              note="Approve, edit or hold drafts with `sapkit drafts`; the buyer sends them.")

    def get_supplier_replies(self, vendor: str = "", category: str = "", needs_review_only: bool = False) -> dict:
        where, params = ["1=1"], []
        if vendor:
            where.append("r.lifnr = ?")
            params.append("".join(ch for ch in vendor if ch.isdigit()).zfill(10))
        if category:
            where.append("r.category = ?")
            params.append(category)
        if needs_review_only:
            where.append("r.needs_buyer_review")
        w = " AND ".join(where)
        by_category = rows(self.con, """SELECT category, count(*) AS replies,
                                               count(*) FILTER (WHERE needs_buyer_review) AS need_review
                                        FROM v_my_reply_extract GROUP BY category ORDER BY replies DESC""")
        total = self.con.execute(f"SELECT count(*) FROM v_my_reply_extract r WHERE {w}", params).fetchone()[0]
        data = rows(self.con, f"""
            SELECT r.lifnr AS vendor, r.message_id, r.category, r.summary, r.needs_buyer_review, r.review_reasons,
                   r.instructions_to_ai_detected,
                   (SELECT list(struct_pack(part := c.matnr, confirmed_qty := c.confirmed_qty, ship_date := c.ship_date))
                    FROM ops_reply_commitment c WHERE c.message_id = r.message_id) AS commitments
            FROM v_my_reply_extract r WHERE {w} ORDER BY r.lifnr, r.message_id LIMIT ?""", params + [MAX_ROWS])
        for r in data:
            r["vendor"] = r["vendor"].lstrip("0")
            r["commitments"] = [{"part": c["part"].lstrip("0"), "confirmed_qty": c["confirmed_qty"],
                                 "ship_date": _jsonable(c["ship_date"])} for c in (r["commitments"] or [])]
        unreplied = rows(self.con, """SELECT DISTINCT o.lifnr FROM v_my_outreach o WHERE NOT EXISTS
                                      (SELECT 1 FROM ops_reply_extract r WHERE r.lifnr = o.lifnr) ORDER BY 1""")
        return self._envelope(data, total, all_replies_by_category=by_category,
                              replies_needing_review_total=sum(c["need_review"] for c in by_category),
                              vendors_without_reply=[u["lifnr"].lstrip("0") for u in unreplied][:MAX_ROWS],
                              vendors_without_reply_total=len(unreplied))


def _drafts_doc() -> str:
    return ("Supplier e-mail drafts in a given status that you may act on (your purchasing groups and plants): "
            "PENDING_REVIEW, APPROVED, ON_HOLD or SENT.")


def tool_definitions(cfg: ClientConfig, session: Session | None = None) -> list[dict]:
    """Strict JSON schemas; every field required (DECISIONS D5).

    With a session, only the tools that session may use are offered to the model
    (least privilege: the model never sees a tool it would be refused).
    """
    plant = {"type": "string", "enum": list(cfg.plants), "description": "SAP plant code (WERKS)."}
    limit = {"type": "integer", "description": f"Maximum rows to return (1-{MAX_ROWS})."}

    def tool(name: str, description: str, props: dict) -> dict:
        return {"name": name, "description": description, "strict": True,
                "input_schema": {"type": "object", "properties": props, "required": list(props),
                                 "additionalProperties": False}}

    defs = [
        tool("get_shortage_summary",
             "Shortage counts per plant the user can see: total shortages, short within 7 days, short today, "
             "value at risk, missing shipping notices and safety-stock dips. Use for any cross-plant or "
             "'where should I focus' question.", {}),
        tool("get_shortage_queue",
             "The ranked shortage queue for one plant, most urgent first. Each row: first short date, days to "
             "short, shortfall quantity, recovery date, next counted receipt, assemblies fed, value at risk, risk score.",
             {"plant": plant,
              "within_days": {"type": "integer", "description": "Only shortages starting within this many days; 0 for the whole horizon."},
              "limit": limit}),
        tool("get_shortage_detail",
             "Why one material is (or is not) short at one plant: master data, the dated stock/supply/requirement "
             "events with the running balance, and its open purchase orders with shipping-notice status.",
             {"material": {"type": "string", "description": "Material number, with or without leading zeros."},
              "plant": plant}),
        tool("get_missing_asn",
             "Purchase-order lines that require a shipping notice, whose ship-by date has passed, and that have "
             "no notice: 'should have shipped and hasn't'.", {"plant": plant, "limit": limit}),
        tool("get_safety_stock_dips",
             "Materials whose projected balance dips into safety stock without going short.",
             {"plant": plant, "limit": limit}),
        tool("get_forecast_changes",
             "Forecast changes between the last two planning snapshots, ranked 1-7 by lead-time window and size "
             "of change (see rank_legend in the result). Every row names the two snapshots compared "
             "(capture_prev, capture_cur); the pair is the same for every plant, so any plant answers "
             "'which snapshots are compared'.",
             {"plant": plant,
              "ranks": {"type": "array", "items": {"type": "integer"}, "description": "Ranks to include; empty for all."},
              "limit": limit}),
        tool("get_outreach_status",
             "Supplier outreach eligibility for a plant's forecast changes: counts per disposition, the vendors "
             "that would get a draft, and the items that need a buyer's attention with the reason.",
             {"plant": plant}),
        tool("preview_supplier_email",
             "Preview the supplier e-mail that would be drafted for one vendor. Creates nothing and sends nothing.",
             {"vendor": {"type": "string", "description": "Vendor number (LIFNR), with or without leading zeros."}}),
        tool("get_supplier_replies",
             "Processed supplier replies: category, one-line summary, whether a buyer must review, and the "
             "quantities/dates the supplier committed to; also which contacted vendors have not replied.",
             {"vendor": {"type": "string", "description": "Vendor number, or empty string for all vendors."},
              "category": {"type": "string", "description": "Reply category to filter on, or empty string for all."},
              "needs_review_only": {"type": "boolean", "description": "True to return only replies a buyer must review."}}),
        tool("get_drafts_for_review", _drafts_doc(),
             {"status": {"type": "string", "enum": ["PENDING_REVIEW", "APPROVED", "ON_HOLD", "SENT"],
                         "description": "Draft status to list."},
              "limit": limit}),
    ]
    if session is None:
        return defs
    return [d for d in defs if session.can_use_tool(d["name"])]
