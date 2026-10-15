"""Offline planning agent: the same tools and safeguards, no language model.

Used when no Anthropic credentials are available, so the agent can be tried
and tested without a key. Questions are routed to a tool by simple rules
(plant, material, vendor and keyword matching), and answers are written from
the tool result with fixed templates. It understands far less than Claude does
and says so when a question doesn't match a rule (DECISIONS D21).
"""
from __future__ import annotations

import datetime as dt
import json
import re

import duckdb

from . import audit, guard, guardrails
from .agent import AgentResult
from .config import ClientConfig
from .telemetry import Trace
from .tools import Session, Toolbox

OFFLINE_NOTE = "(offline mode: answered by rules over the same tools, not by Claude)"
HELP = ("I can answer questions about shortages, late shipping notices, safety-stock dips, forecast "
        "changes, supplier outreach and supplier replies. For example: \"What's short at plant 1000 "
        "this week?\", \"Why is material 10000004 short at plant 1000?\", \"Which suppliers haven't "
        "replied?\"")


def _date(v) -> str:
    if not v:
        return "-"
    d = dt.date.fromisoformat(str(v)[:10])
    return d.strftime("%d %b %Y")


def _num(v) -> str:
    if v is None:
        return "-"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


class OfflineAgent:
    def __init__(self, con: duckdb.DuckDBPyConnection, cfg: ClientConfig, session: Session):
        self.con, self.cfg, self.session = con, cfg, session
        self.toolbox = Toolbox(con, cfg, session, channel="agent-offline")
        self.calls: list[dict] = []

    # ---------------------------------------------------------------- parsing
    def _plants(self, q: str) -> list[str]:
        return [p for p in self.cfg.plants if re.search(rf"\b{p}\b", q)]

    @staticmethod
    def _material(q: str) -> str | None:
        m = re.search(r"\b(?:material|part)\s*#?\s*(\d{5,18})\b", q, re.I)
        return m.group(1) if m else None

    @staticmethod
    def _vendor(q: str) -> str | None:
        m = re.search(r"\b(?:vendor|supplier)\s*#?\s*(\d{4,10})\b", q, re.I)
        return m.group(1) if m else None

    @staticmethod
    def _days(q: str) -> int:
        low = q.lower()
        m = re.search(r"(?:next|within|in)\s+(\d+)\s+days?", low)
        if m:
            return int(m.group(1))
        if "this week" in low or "next week" in low or "7 days" in low:
            return 7
        if "today" in low:
            return 1
        if "this month" in low:
            return 30
        return 0

    # ---------------------------------------------------------------- tool call
    def _call(self, name: str, args: dict) -> tuple[dict, bool]:
        text, is_error = self.toolbox.run(name, args)
        self.calls.append({"tool": name, "input": args, "error": is_error})
        if not is_error:
            self.outputs.append(text)
        return json.loads(text), is_error

    # ---------------------------------------------------------------- routing
    def ask(self, question: str) -> AgentResult:
        trace = Trace(user=self.session.user, channel="agent-offline", question=question)
        result = self._ask(question)
        trace.tools, trace.guardrail = result.tool_calls, result.guardrail
        trace.outcome = "blocked" if result.blocked else "answered"
        trace.finish(self.con)
        result.trace_id = trace.trace_id
        return result

    def _ask(self, question: str) -> AgentResult:
        self.calls = []
        self.outputs: list[str] = []
        screened = guardrails.screen_input(question)
        if not screened.allowed:
            audit.log(self.con, f"agent-offline:{self.session.user}", "input_blocked", screened.category,
                      {"reason": screened.reason})
            return AgentResult(guardrails.INPUT_REFUSAL.format(reason=screened.reason), [], blocked=True,
                               stop_reason="input_guard", guardrail=screened.category)
        q = question.strip()
        low = q.lower()
        plants = self._plants(q)
        plant = plants[0] if plants else None
        material, vendor = self._material(q), self._vendor(q)

        if re.search(r"next year|in 20\d\d|predict|will (the )?(shortage|count).{0,30}be", low):
            answer = (f"I can't predict beyond the data. I can only report shortages within the "
                      f"{self.cfg.horizon_days}-day planning horizon, as of {_date(self.cfg.as_of.isoformat())}.")
        elif re.search(r"\bas of\b|how (fresh|old|recent)|data date|last updated|when was the data", low):
            data, err = self._call("get_shortage_summary", {})
            answer = self._error(data) if err else f"The planning data is as of {_date(data['data_as_of'])}."
        elif re.search(r"\bprices?\b|\bpricing\b|\bcost\b", low) and re.search(r"supplier|vendor|e-?mail|draft", low):
            answer = ("I can't put prices into anything meant for a supplier. Supplier e-mails only carry "
                      "part numbers, quantities and dates, and a buyer reviews and sends every one.")
        elif re.search(r"repl(y|ies|ied)|respon|answered|heard back|can(not|'t)? meet|partially meet", low):
            answer = self._replies(vendor, needs_review="review" in low,
                                   no_reply=bool(re.search(r"(haven't|have not|hasn't|not yet|no) repl", low)))
        elif re.search(r"approv|awaiting|waiting for (my )?(review|approval)|on hold|review queue|drafts? (to|for) review|pending", low):
            status = "ON_HOLD" if "on hold" in low else ("APPROVED" if re.search(r"\bapproved\b", low) else "PENDING_REVIEW")
            answer = self._drafts(status)
        elif vendor and re.search(r"e-?mail|draft|preview|note", low):
            answer = self._preview(vendor)
        elif re.search(r"outreach|eligib|can't go|cannot go|contact|get a draft|"
                       r"drafts?\b|supplier e-?mails?|e-?mails? (to|for) suppliers?|which suppliers would", low):
            answer = self._need_plant(plant, lambda p: self._outreach(p))
        elif re.search(r"forecast|increase|decrease|volume change", low):
            ranks = ([7] if "new demand" in low else [1] if re.search(r"(inside|within) lead time", low)
                     else [3] if "beyond lead time" in low or "outside lead time" in low else [])
            if not plant and "snapshot" in low:  # the snapshot pair is the same for every plant
                plant = self.session.allowed_plants[0]
            answer = self._need_plant(plant, lambda p: self._forecast(p, ranks))
        elif material:
            answer = self._detail(material, plant)
        elif re.search(r"should have shipped|shipping notice|\basn\b|haven't shipped|not shipped|late shipment", low):
            answer = self._need_plant(plant, lambda p: self._missing_asn(p))
        elif "safety stock" in low or "safety-stock" in low:
            answer = self._need_plant(plant, lambda p: self._dips(p))
        elif re.search(r"short|shortage|urgent|at risk|run out|value at risk|overview", low):
            if plant and not re.search(r"which plant|across|overview|compare|all plants", low):
                answer = self._queue(plant, self._days(q))
            else:
                answer = self._summary(by_value="value" in low)
        else:
            answer = "I can only help with SAP planning questions. " + HELP

        successful = sum(1 for c in self.calls if not c["error"])
        if guard.should_block(answer, successful):
            return AgentResult(guard.BLOCKED_MESSAGE, self.calls, blocked=True, stop_reason="offline",
                               guardrail="ungrounded_table")
        out = guardrails.screen_output(answer, self.outputs, question)
        if not out.allowed:  # the templates should never trip this; if they do, it's a bug worth surfacing
            audit.log(self.con, f"agent-offline:{self.session.user}", "answer_blocked", out.category,
                      {"reason": out.reason, "details": out.details[:20]})
            return AgentResult(f"I've held that answer back: {out.reason}.", self.calls, blocked=True,
                               stop_reason="offline", guardrail=out.category)
        return AgentResult(answer, self.calls, stop_reason="offline")

    def _need_plant(self, plant, fn) -> str:
        if plant:
            return fn(plant)
        if len(self.session.allowed_plants) == 1:
            return fn(self.session.allowed_plants[0])
        return f"Which plant? You can see {', '.join(self.session.allowed_plants)}."

    def _error(self, data: dict) -> str:
        return data.get("error", "That request was refused.")

    # ---------------------------------------------------------------- answers
    def _summary(self, by_value: bool) -> str:
        data, err = self._call("get_shortage_summary", {})
        if err:
            return self._error(data)
        rows = sorted(data["rows"], key=lambda r: r["value_at_risk" if by_value else "shortages"], reverse=True)
        if not rows:
            return f"There are no shortages at your plants (data as of {_date(data['data_as_of'])})."
        top = rows[0]
        lead = (f"Plant {top['plant']} ({top['plant_name']}) has the highest value at risk, {_num(top['value_at_risk'])}."
                if by_value else
                f"Plant {top['plant']} ({top['plant_name']}) has the most shortages: {_num(top['shortages'])}.")
        table = _table(["Plant", "Shortages", "Short within 7 days", "Value at risk", "Missing ASN lines", "Safety-stock dips"],
                       [[f"{r['plant']} {r['plant_name']}", _num(r["shortages"]), _num(r["short_within_7_days"]),
                         _num(r["value_at_risk"]), _num(r["missing_asn_lines"]), _num(r["safety_stock_dips"])] for r in rows])
        return f"{lead} Data as of {_date(data['data_as_of'])}.\n\n{table}"

    def _queue(self, plant: str, days: int) -> str:
        data, err = self._call("get_shortage_queue", {"plant": plant, "within_days": days, "limit": 10})
        if err:
            return self._error(data)
        when = f"within {days} days" if days else "in the planning horizon"
        if not data["rows"]:
            return f"Nothing goes short at plant {plant} {when} (data as of {_date(data['data_as_of'])})."
        top = data["rows"][0]
        lead = (f"{_num(data['total_matching'])} materials go short at plant {plant} {when}. The most urgent is "
                f"{top['material']} ({top['description']}), short from {_date(top['first_short_date'])} by "
                f"{_num(top['shortfall_qty'])} units.")
        table = _table(["#", "Material", "Description", "Short from", "Shortfall", "Value at risk", "Plant risk rank"],
                       [[i, r["material"], r["description"], _date(r["first_short_date"]),
                         _num(r["shortfall_qty"]), _num(r["value_at_risk"]), r["risk_rank"]]
                        for i, r in enumerate(data["rows"], 1)])
        more = f"\n\nShowing the top {data['returned']} of {_num(data['total_matching'])}." if data["truncated"] else ""
        return f"{lead} Data as of {_date(data['data_as_of'])}.\n\n{table}{more}"

    def _detail(self, material: str, plant: str | None) -> str:
        candidates = [plant] if plant else list(self.session.allowed_plants)
        data, err = {}, True
        for p in candidates:
            data, err = self._call("get_shortage_detail", {"material": material, "plant": p})
            if not err:
                plant = p
                break
        if err:
            return self._error(data)
        asof = _date(data["data_as_of"])
        unconfirmed = [po for po in data["open_purchase_orders"] if not po["has_asn"]]
        if not data["is_short"]:
            text = f"No. Material {material} is not short at plant {plant} within the planning horizon (data as of {asof})."
            if unconfirmed:
                text += f" It has {len(unconfirmed)} open PO line(s) without a shipping notice, which aren't counted as supply."
            return text
        s = data["shortage"]
        text = (f"Material {material} ({data['master']['maktx']}) goes short at plant {plant} on "
                f"{_date(s['first_short_date'])}, {s['days_to_short']} days from now, by {_num(s['shortfall_qty'])} units. ")
        text += (f"It recovers on {_date(s['recovery_date'])}. " if s["recovery_date"]
                 else "It doesn't recover within the horizon. ")
        if unconfirmed:
            po = unconfirmed[0]
            text += (f"There is an open PO ({po['po']}, {_num(po['open_qty'])} units due {_date(po['delivery_date'])}), "
                     "but the supplier hasn't sent a shipping notice (ASN), so it isn't counted as supply. ")
        elif s["next_firm_receipt"]:
            text += f"The next receipt with a shipping notice arrives {_date(s['next_firm_receipt'])}. "
        text += f"It feeds {s['assemblies_fed']} assembl{'y' if s['assemblies_fed'] == 1 else 'ies'}. Data as of {asof}."
        events = _table(["Date", "Event", "Quantity", "Balance after"],
                        [[_date(e["evt_date"]), e["kind"], _num(e["qty"]), _num(e["balance_after"])] for e in data["events"][:12]])
        return f"{text}\n\n{events}"

    def _missing_asn(self, plant: str) -> str:
        data, err = self._call("get_missing_asn", {"plant": plant, "limit": 10})
        if err:
            return self._error(data)
        if not data["rows"]:
            return f"No PO lines at plant {plant} are overdue for a shipping notice (data as of {_date(data['data_as_of'])})."
        lead = (f"{_num(data['total_matching'])} PO lines at plant {plant} should have shipped by now but have no "
                f"shipping notice. Data as of {_date(data['data_as_of'])}.")
        table = _table(["PO", "Material", "Vendor", "Due", "Ship-by", "Days past", "Open qty"],
                       [[r["po"], r["material"], r["vendor_name"], _date(r["delivery_date"]), _date(r["ship_by_date"]),
                         r["days_past_ship_by"], _num(r["open_qty"])] for r in data["rows"]])
        return f"{lead}\n\n{table}"

    def _dips(self, plant: str) -> str:
        data, err = self._call("get_safety_stock_dips", {"plant": plant, "limit": 10})
        if err:
            return self._error(data)
        lead = (f"{_num(data['total_matching'])} materials at plant {plant} dip into safety stock without going short. "
                f"Data as of {_date(data['data_as_of'])}.")
        if not data["rows"]:
            return lead
        table = _table(["Material", "Description", "Safety stock", "Lowest balance", "First dip"],
                       [[r["material"], r["description"], _num(r["safety_stock"]), _num(r["lowest_balance"]),
                         _date(r["first_dip_date"])] for r in data["rows"]])
        return f"{lead}\n\n{table}"

    def _forecast(self, plant: str, ranks: list[int]) -> str:
        data, err = self._call("get_forecast_changes", {"plant": plant, "ranks": ranks, "limit": 10})
        if err:
            return self._error(data)
        if not data["rows"]:
            return f"No forecast changes match at plant {plant}."
        top = data["rows"][0]
        pct = "new demand" if top["change_pct"] is None else f"{top['change_pct']:+.0f}%"
        lead = (f"{_num(data['total_matching'])} forecast changes at plant {plant} match. The biggest is "
                f"{top['material']} ({top['description']}): {_num(top['prior_qty'])} to {_num(top['current_qty'])} ({pct}), "
                f"{'within' if top['window'] == 'INSIDE' else 'beyond'} its {top['lead_time_days']}-day lead time.")
        table = _table(["Rank", "Material", "Window", "Previous", "New", "Change"],
                       [[r["change_rank"], r["material"], "within LT" if r["window"] == "INSIDE" else "beyond LT",
                         _num(r["prior_qty"]), _num(r["current_qty"]),
                         "new" if r["change_pct"] is None else f"{r['change_pct']:+.0f}%"] for r in data["rows"]])
        return f"{lead} Compared snapshots {top['capture_prev']} and {top['capture_cur']}.\n\n{table}"

    def _outreach(self, plant: str) -> str:
        data, err = self._call("get_outreach_status", {"plant": plant})
        if err:
            return self._error(data)
        d = data["dispositions"]
        lead = (f"{data['eligible_vendor_count']} suppliers would get a draft for plant {plant}'s forecast changes, "
                f"covering {_num(d.get('ELIGIBLE', 0))} items. {_num(data['needs_attention_total'])} items can't go to a "
                f"supplier and need a buyer's attention.")
        reasons = sorted(((k, v) for k, v in d.items() if k not in ("ELIGIBLE", "REPORT_ONLY")), key=lambda kv: -kv[1])
        table = _table(["Outcome", "Items", "Why"],
                       [[k, _num(v), data["reason_legend"][k]] for k, v in reasons])
        return f"{lead} Data as of {_date(data['data_as_of'])}.\n\n{table}"

    def _preview(self, vendor: str) -> str:
        data, err = self._call("preview_supplier_email", {"vendor": vendor})
        if err:
            return self._error(data)
        if not data.get("body"):
            return f"Supplier {vendor} has no eligible items, so there's no e-mail to preview."
        return (f"Here is the draft for supplier {vendor} ({data['eligible_items']} items). "
                f"Nothing is created or sent; the buyer sends it.\n\nTo: {data['to']}\nSubject: {data['subject']}\n\n{data['body']}")

    def _drafts(self, status: str) -> str:
        data, err = self._call("get_drafts_for_review", {"status": status, "limit": 10})
        if err:
            return self._error(data)
        label = status.replace("_", " ").lower()
        if not data["rows"]:
            return f"You have no drafts that are {label}."
        lead = f"You have {_num(data['total_matching'])} supplier drafts that are {label}."
        table = _table(["Draft", "Vendor", "Purchasing group", "Items"],
                       [[r["outreach_id"], r["vendor"], r["ekgrp"], r["item_count"]] for r in data["rows"]])
        return f"{lead} Approve, edit or hold them with `sapkit drafts`.\n\n{table}"

    def _replies(self, vendor: str | None, needs_review: bool, no_reply: bool) -> str:
        data, err = self._call("get_supplier_replies", {"vendor": vendor or "", "category": "",
                                                        "needs_review_only": needs_review})
        if err:
            return self._error(data)
        if no_reply:
            names = ", ".join(data["vendors_without_reply"][:15])
            more = ", ..." if data["vendors_without_reply_total"] > 15 else ""
            return (f"{_num(data['vendors_without_reply_total'])} suppliers we contacted haven't replied yet: "
                    f"{names}{more}.")
        if vendor:
            if not data["rows"]:
                return f"Supplier {vendor} hasn't replied yet."
            parts = []
            for r in data["rows"]:
                s = f"{r['category'].replace('_', ' ')}: {r['summary']}"
                if r["instructions_to_ai_detected"]:
                    s += (" The message also contained instructions aimed at an AI system. They were flagged "
                          "and ignored, and the reply was sent to a buyer.")
                parts.append(s)
            return f"Supplier {vendor} replied ({len(data['rows'])} message(s)). " + " ".join(parts)
        if needs_review:
            lead = f"{_num(data['replies_needing_review_total'])} supplier replies need your review."
        else:
            lead = f"{_num(data['total_matching'])} supplier replies have been processed."
        table = _table(["Category", "Replies", "Need review"],
                       [[c["category"], _num(c["replies"]), _num(c["need_review"])] for c in data["all_replies_by_category"]])
        tail = f"\n\n{_num(data['vendors_without_reply_total'])} contacted suppliers haven't replied yet."
        return f"{lead}\n\n{table}{tail}"
