"""Reply processing (use case 5): classification, safety and reruns."""
from __future__ import annotations

import json

import pytest

from sapkit import db, outreach, replies
from sapkit.replies import ClaudeClassifier, Extraction, OfflineClassifier, process_replies, validate


@pytest.fixture
def drafted(con, cfg, mailbox):
    outreach.run_outreach(con, cfg, mailbox)
    return con


def test_offline_classifier_matches_every_expected_category(drafted, cfg, mailbox):
    res = process_replies(drafted, cfg, mailbox, OfflineClassifier())
    assert res["failed"] == 0
    bad = db.rows(drafted, """SELECT e.message_id, e.kind, e.expected_category, r.category
                               FROM expected_reply e LEFT JOIN ops_reply_extract r USING (message_id)
                               WHERE r.category IS DISTINCT FROM e.expected_category""")
    assert bad == []
    assert res["processed"] == drafted.execute("SELECT count(*) FROM expected_reply").fetchone()[0]


def test_rerun_processes_nothing_twice(drafted, cfg, mailbox):
    first = process_replies(drafted, cfg, mailbox, OfflineClassifier())
    second = process_replies(drafted, cfg, mailbox, OfflineClassifier())
    assert first["processed"] > 0 and second["processed"] == 0
    assert drafted.execute("SELECT count(*) FROM ops_reply_extract").fetchone()[0] == first["processed"]


def test_failed_write_leaves_watermark_and_retries(drafted, cfg, mailbox):
    target = drafted.execute("SELECT message_id FROM expected_reply ORDER BY message_id LIMIT 1").fetchone()[0]
    first = process_replies(drafted, cfg, mailbox, OfflineClassifier(), fail_on_message_id=target)
    assert first["failed"] == 1
    assert drafted.execute("SELECT count(*) FROM ops_reply_extract WHERE message_id = ?", [target]).fetchone()[0] == 0
    assert drafted.execute("SELECT count(*) FROM ops_reply_commitment WHERE message_id = ?", [target]).fetchone()[0] == 0
    assert drafted.execute("SELECT count(*) FROM ops_thread_watermark WHERE last_message_id = ?", [target]).fetchone()[0] == 0
    second = process_replies(drafted, cfg, mailbox, OfflineClassifier())
    assert second["failed"] == 0
    assert drafted.execute("SELECT count(*) FROM ops_reply_extract WHERE message_id = ?", [target]).fetchone()[0] == 1


def test_injection_is_flagged_and_routed_to_a_buyer(drafted, cfg, mailbox):
    process_replies(drafted, cfg, mailbox, OfflineClassifier())
    rows = db.rows(drafted, """SELECT r.* FROM ops_reply_extract r JOIN expected_reply e USING (message_id)
                                WHERE e.injection""")
    assert rows
    for r in rows:
        assert r["instructions_to_ai_detected"] and r["needs_buyer_review"]
        assert r["category"] == "can_meet"  # the instruction did not change the classification


def test_commitments_match_requested_parts(drafted, cfg, mailbox):
    process_replies(drafted, cfg, mailbox, OfflineClassifier())
    unmatched = drafted.execute("SELECT count(*) FROM ops_reply_commitment WHERE NOT matched_outreach").fetchone()[0]
    assert unmatched == 0
    assert drafted.execute("SELECT count(*) FROM ops_reply_commitment").fetchone()[0] > 0


def test_vendors_without_reply_are_counted(drafted, cfg, mailbox):
    res = process_replies(drafted, cfg, mailbox, OfflineClassifier())
    replied = drafted.execute("SELECT count(DISTINCT lifnr) FROM ops_reply_extract").fetchone()[0]
    total = drafted.execute("SELECT count(DISTINCT lifnr) FROM ops_outreach").fetchone()[0]
    assert res["vendors_without_reply"] == total - replied > 0


# ------------------------------------------------------------------ validation rules
ITEMS = [{"matnr": "000000000010000005", "prior_qty": 100.0, "current_qty": 140.0}]


def test_validate_flags_unrequested_part_and_keeps_nulls():
    ex = Extraction("can_meet", "ok", False, [
        {"part_number": "10000005", "confirmed_qty": None, "ship_date": None, "constraint_note": None},
        {"part_number": "99999999", "confirmed_qty": 5, "ship_date": "2026-11-01", "constraint_note": None},
    ])
    commitments, review, reasons = validate(ex, ITEMS)
    assert commitments[0]["confirmed_qty"] is None and commitments[0]["matched_outreach"]
    assert not commitments[1]["matched_outreach"]
    assert review and any("99999999" in r for r in reasons)


def test_validate_flags_can_meet_with_short_quantity():
    ex = Extraction("can_meet", "ok", False, [
        {"part_number": "10000005", "confirmed_qty": 90, "ship_date": "2026-11-01", "constraint_note": None}])
    _, review, reasons = validate(ex, ITEMS)
    assert review and any("less than requested" in r for r in reasons)


def test_validate_clean_can_meet_needs_no_review():
    ex = Extraction("can_meet", "ok", False, [
        {"part_number": "10000005", "confirmed_qty": 140, "ship_date": "2026-11-01", "constraint_note": None}])
    _, review, reasons = validate(ex, ITEMS)
    assert not review and reasons == []


# ------------------------------------------------------------------ Claude classifier request shape
def test_claude_classifier_request_and_parse(cfg, fake):
    payload = {"category": "can_partially_meet", "summary": "Partial.", "instructions_to_ai_detected": False,
               "commitments": [{"part_number": "10000005", "confirmed_qty": 80, "ship_date": "2026-11-02",
                                "constraint_note": "capacity"}]}
    client = fake.FakeClient([fake.response([fake.text(json.dumps(payload))])])
    ex = ClaudeClassifier(cfg, client=client).classify("We can do 80 EA of part 10000005.", ITEMS)
    assert ex.category == "can_partially_meet" and ex.commitments[0]["confirmed_qty"] == 80
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert "tools" not in call, "the classifier must have no tools: e-mail text can't trigger actions"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "medium"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "<supplier_email>" in call["messages"][0]["content"]


def test_claude_classifier_refusal_goes_to_a_buyer(cfg, fake):
    client = fake.FakeClient([fake.response([], stop_reason="refusal")])
    ex = ClaudeClassifier(cfg, client=client).classify("...", ITEMS)
    _, review, reasons = validate(ex, ITEMS)
    assert ex.refused and review and any("declined" in r for r in reasons)


def test_reply_schema_is_strict():
    s = replies.REPLY_SCHEMA
    assert s["additionalProperties"] is False
    item = s["properties"]["commitments"]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == set(item["properties"])
