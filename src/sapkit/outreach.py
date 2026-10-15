"""Supplier eligibility (use case 3) and buyer-approved drafting (use case 4).

Eligibility is deterministic and ordered, so every changed material-plant gets
exactly one disposition and one reason (DECISIONS D11). Drafting groups the
eligible items into one e-mail per vendor and buyer, keyed so a rerun never
creates a second draft.
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass

import duckdb

from . import audit
from .config import ClientConfig
from .db import insert_many, rows
from .mail import Draft, MailAdapter

REASONS = {
    "ELIGIBLE": "Draft prepared for buyer review",
    "REPORT_ONLY": "Change rank is report-only for this client",
    "EXCLUDED_MATERIAL_GROUP": "Material group is excluded from supplier outreach",
    "EXCLUDED_VMI": "Vendor-managed inventory: the supplier plans this part",
    "AMBIGUOUS_SUPPLIER": "More than one possible supplier; a buyer must choose",
    "NO_SUPPLIER": "No open PO and no valid source-list entry",
    "EXCLUDED_INTERPLANT": "Supplied by another Acme plant, not an external supplier",
    "NO_BUYER_EMAIL": "Purchasing group has no buyer mailbox on file",
    "ROUTE_TO_SOURCING": "Supplier has no e-mail on file; routed to sourcing, not dropped",
}


@dataclass(frozen=True)
class Decision:
    disposition: str
    reason: str
    vendor: str | None


def decide(row: dict, cfg: ClientConfig) -> Decision:
    def d(code: str, vendor: str | None = None) -> Decision:
        return Decision(code, REASONS[code], vendor)

    draftable = set(cfg.get("forecast", "draftable_ranks", default=[1, 2, 3]))
    if row["change_rank"] not in draftable:
        return d("REPORT_ONLY")
    if row["matkl"] in set(cfg.get("outreach", "excluded_material_groups", default=[])):
        return d("EXCLUDED_MATERIAL_GROUP")
    if row["is_vmi"]:
        return d("EXCLUDED_VMI")
    if row["resolution"] == "AMBIGUOUS":
        return d("AMBIGUOUS_SUPPLIER")
    if row["resolution"] == "NONE" or not row["vendor"]:
        return d("NO_SUPPLIER")
    vendor = row["vendor"]
    if vendor in set((cfg.get("outreach", "interplant_vendors") or {}).keys()):
        return d("EXCLUDED_INTERPLANT", vendor)
    if not row["buyer_email"]:
        return d("NO_BUYER_EMAIL", vendor)
    if not row["vendor_email"]:
        return d("ROUTE_TO_SOURCING", vendor)
    return d("ELIGIBLE", vendor)


def decide_all(con: duckdb.DuckDBPyConnection, cfg: ClientConfig, plants: list[str] | None = None) -> list[dict]:
    sql = "SELECT * FROM v_outreach_input"
    params: list = []
    if plants:
        sql += f" WHERE werks IN ({','.join('?' * len(plants))})"
        params = list(plants)
    out = []
    for r in rows(con, sql + " ORDER BY werks, matnr", params):
        dec = decide(r, cfg)
        out.append({**r, "disposition": dec.disposition, "reason": dec.reason, "resolved_vendor": dec.vendor})
    return out


def idempotency_key(cfg: ClientConfig, capture_prev: str, capture_cur: str, lifnr: str, ekgrp: str) -> str:
    raw = "|".join([cfg.client_id, capture_prev, capture_cur, lifnr, ekgrp,
                    str(cfg.get("outreach", "template_version", default="v1"))])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _fmt_qty(x: float) -> str:
    return f"{x:,.0f}"


def render_email(cfg: ClientConfig, lifnr: str, buyer_name: str, items: list[dict]) -> tuple[str, str]:
    """Plain-text supplier e-mail. No prices are ever included."""
    increases = [i for i in items if i["current_qty"] > i["prior_qty"]]
    decreases = [i for i in items if i["current_qty"] < i["prior_qty"]]
    subject = f"Acme forecast update for supplier {lifnr.lstrip('0')} - please confirm ({len(items)} part{'s' if len(items) != 1 else ''})"
    lines = [
        "Hello,",
        "",
        f"Our latest planning run (as of {cfg.as_of.strftime('%d %b %Y')}) changed the volumes we expect "
        "to need from you for the parts below.",
    ]
    if increases and decreases:
        lines.append("Please confirm you can fulfil the increases, and hold or reschedule the decreases.")
    elif increases:
        lines.append("Please confirm you can fulfil the increased quantities.")
    else:
        lines.append("Please hold or reschedule against the reduced quantities.")
    lines += ["", f"{'Part':<12}{'Description':<28}{'Plant':<7}{'Window':<22}{'Previous':>10}{'New':>10}{'Change':>9}"]
    for i in sorted(items, key=lambda x: (x["change_rank"], x["matnr"])):
        window = "within lead time" if i["window_"] == "INSIDE" else "beyond lead time"
        pct = "new" if i["change_pct"] is None else f"{i['change_pct']:+.0f}%"
        lines.append(f"{i['matnr'].lstrip('0'):<12}{(i['maktx'] or '')[:27]:<28}{i['werks']:<7}{window:<22}"
                     f"{_fmt_qty(i['prior_qty']):>10}{_fmt_qty(i['current_qty']):>10}{pct:>9}")
    lines += [
        "",
        "For each part, please reply with:",
        "  1. Can you meet the new quantity?  Yes / Partly / No",
        "  2. If partly: the quantity you can confirm and by when",
        "  3. Any constraint we should know about",
        "",
        "Thank you,",
        buyer_name or "Acme Purchasing",
    ]
    body = "\n".join(lines)
    if not cfg.get("outreach", "include_pricing", default=False) and ("$" in body or "price" in body.lower()):
        raise ValueError("pricing must never appear in a supplier e-mail")
    return subject, body


def run_outreach(con: duckdb.DuckDBPyConnection, cfg: ClientConfig, mailbox: MailAdapter,
                 actor: str = "batch", plants: list[str] | None = None, create_drafts: bool = True) -> dict:
    run_id = uuid.uuid4().hex[:12]
    decisions = decide_all(con, cfg, plants)
    con.execute("BEGIN")
    try:
        insert_many(con,
            "INSERT INTO ops_disposition VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, d["matnr"], d["werks"], d["change_rank"], d["disposition"], d["reason"],
              d["resolved_vendor"], d["ekgrp"]) for d in decisions])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in decisions:
        if d["disposition"] == "ELIGIBLE":
            groups[(d["resolved_vendor"], d["ekgrp"])].append(d)

    created, skipped = 0, 0
    if create_drafts:
        for (lifnr, ekgrp), items in sorted(groups.items()):
            first = items[0]
            key = idempotency_key(cfg, first["capture_prev"], first["capture_cur"], lifnr, ekgrp)
            if con.execute("SELECT 1 FROM ops_outreach WHERE outreach_id = ?", [key]).fetchone():
                skipped += 1
                audit.log(con, actor, "draft_skipped_duplicate", lifnr, {"outreach_id": key})
                continue
            subject, body = render_email(cfg, lifnr, first["buyer_name"], items)
            receipt = mailbox.create_draft(Draft(key, first["buyer_email"], first["vendor_email"], subject, body), lifnr)
            con.execute("BEGIN")
            try:
                con.execute(
                    "INSERT INTO ops_outreach (outreach_id, client_id, capture_prev, capture_cur, lifnr, ekgrp, "
                    "template_version, buyer_email, vendor_email, thread_id, draft_ref, item_count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [key, cfg.client_id, first["capture_prev"], first["capture_cur"], lifnr, ekgrp,
                     cfg.get("outreach", "template_version"), first["buyer_email"], first["vendor_email"],
                     receipt.thread_id, receipt.draft_ref, len(items)])
                insert_many(con,
                    "INSERT INTO ops_outreach_item VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(key, i["matnr"], i["werks"], i["window_"], i["prior_qty"], i["current_qty"],
                      i["change_pct"], i["change_rank"]) for i in items])
                audit.log(con, actor, "draft_created", lifnr,
                          {"outreach_id": key, "thread_id": receipt.thread_id, "items": len(items)})
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            created += 1

    if create_drafts:
        from .workflow import register_drafts
        register_drafts(con)  # every new draft starts PENDING_REVIEW

    counts: dict[str, int] = defaultdict(int)
    for d in decisions:
        counts[d["disposition"]] += 1
    audit.log(con, actor, "outreach_run", run_id,
              {"dispositions": dict(counts), "drafts_created": created, "drafts_skipped": skipped})
    return {"run_id": run_id, "dispositions": dict(counts), "vendor_groups": len(groups),
            "drafts_created": created, "drafts_skipped_duplicate": skipped}


def preview(con: duckdb.DuckDBPyConnection, cfg: ClientConfig, lifnr: str, plants: list[str] | None = None) -> dict:
    lifnr = lifnr.zfill(10)
    items = [d for d in decide_all(con, cfg, plants) if d["disposition"] == "ELIGIBLE" and d["resolved_vendor"] == lifnr]
    if not items:
        return {"vendor": lifnr, "eligible_items": 0, "preview": None}
    subject, body = render_email(cfg, lifnr, items[0]["buyer_name"], items)
    return {"vendor": lifnr, "eligible_items": len(items), "to": items[0]["vendor_email"],
            "from": items[0]["buyer_email"], "subject": subject, "body": body}
