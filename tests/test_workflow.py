"""Draft approval workflow and the follow-up ladder."""
from __future__ import annotations

import datetime as dt

import pytest

from sapkit import outreach, replies, workflow
from sapkit.rbac import load_rbac
from sapkit.workflow import WorkflowError


@pytest.fixture
def drafted(con, cfg, mailbox):
    outreach.run_outreach(con, cfg, mailbox)
    return con


def _draft_for_group(con, group):
    return con.execute("SELECT o.outreach_id FROM ops_outreach o JOIN ops_draft_review r USING (outreach_id) "
                       "WHERE o.ekgrp = ? AND r.status = 'PENDING_REVIEW' AND NOT EXISTS "
                       "(SELECT 1 FROM ops_outreach_item i WHERE i.outreach_id = o.outreach_id AND i.werks <> '1000') "
                       "ORDER BY 1 LIMIT 1", [group]).fetchone()[0]


def test_every_draft_starts_pending(drafted):
    n = drafted.execute("SELECT count(*) FROM ops_outreach").fetchone()[0]
    assert drafted.execute("SELECT count(*) FROM ops_draft_review WHERE status = 'PENDING_REVIEW'").fetchone()[0] == n


def test_full_lifecycle_by_the_right_buyer(drafted, cfg):
    buyer = load_rbac().session_for("avery.buyer@acme-dummy.example", cfg)
    oid = _draft_for_group(drafted, "P01")
    assert workflow.review(drafted, buyer, oid, "hold_draft", note="check volumes").status == "ON_HOLD"
    assert workflow.review(drafted, buyer, oid, "approve_draft").status == "APPROVED"
    assert workflow.review(drafted, buyer, oid, "mark_sent").status == "SENT"
    events = [r[0] for r in drafted.execute(
        "SELECT event_type FROM ops_audit_log WHERE subject = ? ORDER BY event_at", [oid]).fetchall()]
    assert events == ["draft_hold", "draft_approve", "draft_mark_sent"]


def test_buyer_cannot_touch_another_groups_draft(drafted, cfg):
    buyer = load_rbac().session_for("avery.buyer@acme-dummy.example", cfg)
    oid = _draft_for_group(drafted, "P03")
    with pytest.raises(WorkflowError, match="not one of yours"):
        workflow.review(drafted, buyer, oid, "approve_draft")
    assert drafted.execute("SELECT count(*) FROM ops_audit_log WHERE event_type = 'draft_action_denied'").fetchone()[0] == 1


def test_planner_cannot_approve(drafted, cfg):
    planner = load_rbac().session_for("jordan.planner@acme-dummy.example", cfg)
    with pytest.raises(WorkflowError, match="does not allow"):
        workflow.review(drafted, planner, _draft_for_group(drafted, "P01"), "approve_draft")


def test_invalid_transitions(drafted, cfg):
    manager = load_rbac().session_for("morgan.manager@acme-dummy.example", cfg)
    oid = _draft_for_group(drafted, "P01")
    with pytest.raises(WorkflowError, match="Can't mark sent"):
        workflow.review(drafted, manager, oid, "mark_sent")


def test_edit_is_rescreened_for_pricing(drafted, cfg):
    manager = load_rbac().session_for("morgan.manager@acme-dummy.example", cfg)
    oid = _draft_for_group(drafted, "P01")
    with pytest.raises(WorkflowError, match="pricing"):
        workflow.review(drafted, manager, oid, "edit_draft", body="Please confirm. Unit price $3.10.")
    r = workflow.review(drafted, manager, oid, "edit_draft", body="Please confirm the new quantities by Friday.")
    assert r.status == "PENDING_REVIEW"
    assert drafted.execute("SELECT edited_body FROM ops_draft_review WHERE outreach_id = ?", [oid]).fetchone()[0]


def test_review_queue_is_scoped(drafted, cfg):
    buyer = load_rbac().session_for("avery.buyer@acme-dummy.example", cfg)
    manager = load_rbac().session_for("morgan.manager@acme-dummy.example", cfg)
    n_buyer, rows = workflow.review_queue(drafted, buyer)
    n_manager, _ = workflow.review_queue(drafted, manager)
    assert 0 < n_buyer < n_manager and {r["ekgrp"] for r in rows} <= {"P01", "P02"}


def test_followup_ladder(drafted, cfg, mailbox):
    manager = load_rbac().session_for("morgan.manager@acme-dummy.example", cfg)
    ids = [r[0] for r in drafted.execute("SELECT outreach_id FROM ops_draft_review ORDER BY 1 LIMIT 3").fetchall()]
    for oid in ids:
        workflow.review(drafted, manager, oid, "approve_draft")
        workflow.review(drafted, manager, oid, "mark_sent")
    sent_at = drafted.execute("SELECT min(sent_at) FROM ops_draft_review WHERE status = 'SENT'").fetchone()[0]
    assert workflow.run_followups(drafted, sent_at + dt.timedelta(hours=10))["created"] == {}
    r = workflow.run_followups(drafted, sent_at + dt.timedelta(days=7))
    assert r["created"].get("REMINDER", 0) + r["already_replied"] == 3
    again = workflow.run_followups(drafted, sent_at + dt.timedelta(days=7))
    assert again["created"] == {}  # idempotent
    steps = {s for (s,) in drafted.execute("SELECT DISTINCT step FROM ops_followup").fetchall()}
    assert steps <= {"REMINDER", "ESCALATE_TO_BUYER", "FINAL_ESCALATION"}


def test_reply_after_send_stops_followups(drafted, cfg, mailbox):
    manager = load_rbac().session_for("morgan.manager@acme-dummy.example", cfg)
    for (oid,) in drafted.execute("SELECT outreach_id FROM ops_draft_review").fetchall():
        workflow.review(drafted, manager, oid, "approve_draft")
        workflow.review(drafted, manager, oid, "mark_sent")
    replies.process_replies(drafted, cfg, mailbox, replies.OfflineClassifier())
    sent_at = drafted.execute("SELECT max(sent_at) FROM ops_draft_review").fetchone()[0]
    r = workflow.run_followups(drafted, sent_at + dt.timedelta(days=7))
    assert r["already_replied"] > 0
