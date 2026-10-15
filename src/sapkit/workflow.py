"""Buyer approval and supplier follow-up (use case 4 and 5, operational side).

Draft lifecycle (one row per draft in ops_draft_review):

    PENDING_REVIEW --approve--> APPROVED --mark_sent--> SENT
          |    \\--edit--> PENDING_REVIEW (edited body, re-screened)
          \\--hold--> ON_HOLD --approve--> APPROVED

Rules, all enforced here and audited:
- Only a user whose role allows the action and whose scope covers the draft's
  purchasing group and plants may act on it.
- An edited body is screened again: no prices, no personal data.
- "mark_sent" records that the buyer sent the e-mail from their own mailbox;
  the kit itself never sends.

Follow-up ladder (ops_followup), for SENT drafts with no supplier reply:
    after 48 hours  -> REMINDER draft for the buyer to review
    after 4 days    -> ESCALATE_TO_BUYER notification
    after 6 days    -> FINAL_ESCALATION to the purchasing manager
Each step is created once (idempotent) and only proposes; a person acts.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import duckdb

from . import audit, guardrails
from .db import rows
from .rbac import Session

STATUSES = ("PENDING_REVIEW", "APPROVED", "ON_HOLD", "SENT")
TRANSITIONS = {
    "approve_draft": ({"PENDING_REVIEW", "ON_HOLD"}, "APPROVED"),
    "edit_draft": ({"PENDING_REVIEW", "ON_HOLD", "APPROVED"}, "PENDING_REVIEW"),
    "hold_draft": ({"PENDING_REVIEW", "APPROVED"}, "ON_HOLD"),
    "mark_sent": ({"APPROVED"}, "SENT"),
}
LADDER = [("REMINDER", dt.timedelta(hours=48)),
          ("ESCALATE_TO_BUYER", dt.timedelta(days=4)),
          ("FINAL_ESCALATION", dt.timedelta(days=6))]


class WorkflowError(Exception):
    pass


@dataclass
class ReviewResult:
    outreach_id: str
    status: str
    message: str


def register_drafts(con: duckdb.DuckDBPyConnection) -> int:
    """Give every draft without a review record a PENDING_REVIEW record."""
    before = con.execute("SELECT count(*) FROM ops_draft_review").fetchone()[0]
    con.execute("""INSERT INTO ops_draft_review (outreach_id, status)
                   SELECT o.outreach_id, 'PENDING_REVIEW' FROM ops_outreach o
                   WHERE NOT EXISTS (SELECT 1 FROM ops_draft_review r WHERE r.outreach_id = o.outreach_id)""")
    return con.execute("SELECT count(*) FROM ops_draft_review").fetchone()[0] - before


def _draft(con, outreach_id: str) -> dict:
    found = rows(con, """SELECT o.*, r.status, r.reviewer, r.edited_body
                         FROM ops_outreach o JOIN ops_draft_review r USING (outreach_id)
                         WHERE o.outreach_id = ?""", [outreach_id])
    if not found:
        raise WorkflowError(f"No draft {outreach_id}.")
    d = found[0]
    d["plants"] = [r[0] for r in con.execute(
        "SELECT DISTINCT werks FROM ops_outreach_item WHERE outreach_id = ?", [outreach_id]).fetchall()]
    return d


def _authorise(session: Session, action: str, draft: dict) -> None:
    if not session.can(action):
        raise WorkflowError(f"Your role does not allow {action.replace('_', ' ')}.")
    if not session.covers_group(draft["ekgrp"]):
        raise WorkflowError(f"Purchasing group {draft['ekgrp']} is not one of yours.")
    if not set(draft["plants"]) <= set(session.allowed_plants):
        raise WorkflowError("This draft includes plants you don't have access to.")


def review(con: duckdb.DuckDBPyConnection, session: Session, outreach_id: str, action: str,
           body: str | None = None, note: str | None = None) -> ReviewResult:
    if action not in TRANSITIONS:
        raise WorkflowError(f"Unknown action {action}. Actions: {', '.join(TRANSITIONS)}")
    try:
        draft = _draft(con, outreach_id)
        _authorise(session, action, draft)
        allowed_from, to = TRANSITIONS[action]
        if draft["status"] not in allowed_from:
            raise WorkflowError(f"Can't {action.replace('_', ' ')} a draft that is {draft['status']}.")
        if action == "edit_draft":
            if not body or not body.strip():
                raise WorkflowError("An edit needs the new e-mail body.")
            screened = guardrails.screen_output(body, [], addressed_to_supplier=True)
            if not screened.allowed:
                raise WorkflowError(f"Edit rejected: {screened.reason}.")
    except WorkflowError as e:
        audit.log(con, session.user, "draft_action_denied", outreach_id, {"action": action, "reason": str(e)})
        raise
    sets = ["status = ?", "reviewer = ?", "reviewed_at = now()", "note = coalesce(?, note)"]
    params: list = [to, session.user, note]
    if action == "edit_draft":
        sets.append("edited_body = ?")
        params.append(body)
    if action == "mark_sent":
        sets.append("sent_at = now()")
    con.execute(f"UPDATE ops_draft_review SET {', '.join(sets)} WHERE outreach_id = ?", params + [outreach_id])
    audit.log(con, session.user, f"draft_{action.replace('_draft', '')}", outreach_id,
              {"from": draft["status"], "to": to, "lifnr": draft["lifnr"], "note": note})
    return ReviewResult(outreach_id, to, f"Draft {outreach_id} is now {to}.")


def review_queue(con: duckdb.DuckDBPyConnection, session: Session, status: str = "PENDING_REVIEW",
                 limit: int = 50) -> tuple[int, list[dict]]:
    """Drafts the session may act on, in a given status."""
    groups = session.purchasing_groups
    plants = session.allowed_plants or ["__none__"]
    sql = f"""
        SELECT o.outreach_id, o.lifnr, o.ekgrp, o.item_count, r.status, r.reviewer, r.reviewed_at, r.sent_at
        FROM ops_outreach o JOIN ops_draft_review r USING (outreach_id)
        WHERE r.status = ?
          AND NOT EXISTS (SELECT 1 FROM ops_outreach_item i WHERE i.outreach_id = o.outreach_id
                          AND i.werks NOT IN ({','.join('?' * len(plants))}))
          {"AND o.ekgrp IN (" + ",".join("?" * len(groups)) + ")" if groups is not None and groups else ""}
          {"AND FALSE" if groups is not None and not groups else ""}
        ORDER BY o.lifnr, o.outreach_id"""
    params: list = [status, *plants, *(groups or [])]
    found = rows(con, sql, params)
    return len(found), found[:limit]


def run_followups(con: duckdb.DuckDBPyConnection, now: dt.datetime, actor: str = "batch") -> dict:
    """Create the follow-up steps that are due. Never sends anything."""
    sent = rows(con, """SELECT o.outreach_id, o.lifnr, o.ekgrp, r.sent_at
                        FROM ops_outreach o JOIN ops_draft_review r USING (outreach_id)
                        WHERE r.status = 'SENT' AND r.sent_at IS NOT NULL""")
    created: dict[str, int] = {}
    skipped_replied = 0
    for s in sent:
        replied = con.execute("""SELECT 1 FROM ops_reply_extract
                                 WHERE lifnr = ? AND processed_at >= ? LIMIT 1""",
                              [s["lifnr"], s["sent_at"]]).fetchone()
        if replied:
            skipped_replied += 1
            continue
        for step, after in LADDER:
            due = s["sent_at"] + after
            if now < due:
                break
            fid = f"{s['outreach_id']}:{step}"
            if con.execute("SELECT 1 FROM ops_followup WHERE followup_id = ?", [fid]).fetchone():
                continue
            con.execute("INSERT INTO ops_followup (followup_id, outreach_id, lifnr, step, due_at, status) "
                        "VALUES (?, ?, ?, ?, ?, 'OPEN')", [fid, s["outreach_id"], s["lifnr"], step, due])
            audit.log(con, actor, "followup_created", s["lifnr"], {"outreach_id": s["outreach_id"], "step": step})
            created[step] = created.get(step, 0) + 1
    return {"sent_drafts": len(sent), "already_replied": skipped_replied, "created": created}
