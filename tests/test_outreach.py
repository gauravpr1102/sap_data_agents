"""Eligibility and drafting (use cases 3 and 4)."""
from __future__ import annotations

from sapkit import db, outreach


def test_dispositions_match_expected(con, cfg):
    expected = {(r["matnr"], r["werks"]): r for r in db.rows(con, "SELECT * FROM expected_forecast")}
    decisions = outreach.decide_all(con, cfg)
    assert len(decisions) == len(expected)
    wrong = []
    for d in decisions:
        e = expected[(d["matnr"], d["werks"])]
        if d["disposition"] != e["expected_disposition"]:
            wrong.append((d["matnr"], e["supplier_scenario"], e["expected_disposition"], d["disposition"]))
        elif e["expected_vendor"] and d["resolved_vendor"] != e["expected_vendor"]:
            wrong.append((d["matnr"], "vendor", e["expected_vendor"], d["resolved_vendor"]))
    assert wrong == []
    seen = {d["disposition"] for d in decisions}
    assert set(outreach.REASONS) <= seen, f"missing dispositions: {set(outreach.REASONS) - seen}"


def test_one_draft_per_vendor_and_buyer(con, cfg, mailbox, workdir):
    res = outreach.run_outreach(con, cfg, mailbox)
    eligible = [d for d in outreach.decide_all(con, cfg) if d["disposition"] == "ELIGIBLE"]
    groups = {(d["resolved_vendor"], d["ekgrp"]) for d in eligible}
    assert res["drafts_created"] == len(groups) > 0
    assert len(list((workdir / "out" / "drafts").glob("*.txt"))) == len(groups)
    items = con.execute("SELECT count(*) FROM ops_outreach_item").fetchone()[0]
    assert items == len(eligible)


def test_rerun_creates_nothing_new(con, cfg, mailbox, workdir):
    first = outreach.run_outreach(con, cfg, mailbox)
    second = outreach.run_outreach(con, cfg, mailbox)
    assert second["drafts_created"] == 0
    assert second["drafts_skipped_duplicate"] == first["drafts_created"]
    assert con.execute("SELECT count(*) FROM ops_outreach").fetchone()[0] == first["drafts_created"]


def test_drafts_never_contain_pricing(con, cfg, mailbox, workdir):
    outreach.run_outreach(con, cfg, mailbox)
    for f in (workdir / "out" / "drafts").glob("*.txt"):
        body = f.read_text(encoding="utf-8").lower()
        assert "$" not in body and "price" not in body and "cost" not in body, f.name


def test_drafts_go_only_to_eligible_items(con, cfg, mailbox):
    outreach.run_outreach(con, cfg, mailbox)
    bad = con.execute("""
        SELECT count(*) FROM ops_outreach_item i JOIN expected_forecast e USING (matnr, werks)
        WHERE e.expected_disposition <> 'ELIGIBLE'""").fetchone()[0]
    assert bad == 0


def test_drafts_are_addressed_from_buyer_to_vendor(con, cfg, mailbox, workdir):
    outreach.run_outreach(con, cfg, mailbox)
    for o in db.rows(con, "SELECT * FROM ops_outreach LIMIT 20"):
        text = (workdir / "out" / "drafts" / f"{o['draft_ref']}.txt").read_text(encoding="utf-8")
        assert f"From: {o['buyer_email']}" in text
        assert f"To: {o['vendor_email']}" in text
        assert "DRAFT - not sent" in text


def test_mixed_email_asks_to_fulfil_and_hold(cfg):
    items = [
        {"matnr": "000000000010000001", "maktx": "Steel Bracket", "werks": "1000", "window_": "INSIDE",
         "prior_qty": 100.0, "current_qty": 150.0, "change_pct": 50.0, "change_rank": 1},
        {"matnr": "000000000010000002", "maktx": "Alloy Shaft", "werks": "1000", "window_": "OUTSIDE",
         "prior_qty": 200.0, "current_qty": 100.0, "change_pct": -50.0, "change_rank": 3},
    ]
    subject, body = outreach.render_email(cfg, "0000100001", "Avery", items)
    assert "fulfil the increases" in body and "hold or reschedule the decreases" in body
    assert "10000001" in body and "000000000010000001" not in body  # users see numbers without leading zeros
    assert "100001" in subject


def test_preview_creates_nothing(con, cfg, workdir):
    vendor = next(d["resolved_vendor"] for d in outreach.decide_all(con, cfg) if d["disposition"] == "ELIGIBLE")
    p = outreach.preview(con, cfg, vendor)
    assert p["eligible_items"] >= 1 and p["body"]
    assert con.execute("SELECT count(*) FROM ops_outreach").fetchone()[0] == 0
    assert not (workdir / "out" / "drafts").exists() or not list((workdir / "out" / "drafts").iterdir())
