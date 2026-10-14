"""The offline agent: same tools and safeguards, no model, no key."""
from __future__ import annotations

import pytest

from sapkit import outreach, replies
from sapkit.evals_runner import build_cases
from sapkit.offline_agent import OfflineAgent
from sapkit.tools import Session


@pytest.fixture
def ready(con, cfg, mailbox):
    outreach.run_outreach(con, cfg, mailbox)
    replies.process_replies(con, cfg, mailbox, replies.OfflineClassifier())
    return con


def agent(con, cfg, plants=None):
    return OfflineAgent(con, cfg, Session("tester", plants or list(cfg.plants)))


def test_answers_all_evaluation_questions(ready, cfg):
    failures = []
    for c in build_cases(ready, cfg):
        res = agent(ready, cfg, c.plants or None).ask(c.question)
        used = [t["tool"] for t in res.tool_calls]
        low = res.answer.lower()
        ok = ((not c.tools or any(t in used for t in c.tools))
              and all(any(a.lower() in low for a in g) for g in c.facts)
              and not any(f.lower() in low for f in c.must_not_contain))
        if not ok:
            failures.append((c.id, used, res.answer[:200]))
    assert failures == []


@pytest.mark.parametrize("question,tool", [
    ("what's short at 1000 this week?", "get_shortage_queue"),
    ("any late shipments at plant 3000? which POs should have shipped", "get_missing_asn"),
    ("who hasn't replied?", "get_supplier_replies"),
    ("how many supplier emails for plant 1000?", "get_outreach_status"),
    ("which forecast changes at 2000 are biggest?", "get_forecast_changes"),
    ("safety stock problems at plant 2000", "get_safety_stock_dips"),
    ("compare value at risk across all plants", "get_shortage_summary"),
])
def test_routes_everyday_phrasing(ready, cfg, question, tool):
    res = agent(ready, cfg).ask(question)
    assert [t["tool"] for t in res.tool_calls][:1] == [tool], res.answer[:200]


def test_material_without_plant_searches_allowed_plants(ready, cfg):
    mat = ready.execute("SELECT matnr FROM v_shortage WHERE werks = '3000' LIMIT 1").fetchone()[0].lstrip("0")
    res = agent(ready, cfg, ["1000", "3000"]).ask(f"why is part {mat} short?")
    assert f"Material {mat}" in res.answer and "plant 3000" in res.answer


def test_scope_is_enforced(ready, cfg):
    res = agent(ready, cfg, ["1000"]).ask("what's short at plant 2000 this week?")
    assert "do not have access to plant 2000" in res.answer
    assert res.tool_calls[0]["error"] is True


def test_refuses_pricing_and_off_topic(ready, cfg):
    a = agent(ready, cfg)
    assert "can't put prices" in a.ask("Write the supplier e-mail with our target price").answer
    off = a.ask("tell me a joke")
    assert off.tool_calls == [] and "planning questions" in off.answer


def test_answers_are_plain_ascii_for_windows_consoles(ready, cfg):
    a = agent(ready, cfg)
    for q in ["which plant has the most shortages?", "which forecast changes at 1000 are biggest?",
              "who hasn't replied?", "how many supplier emails for plant 2000?"]:
        assert a.ask(q).answer.isascii(), q


def test_every_tool_call_is_audited(ready, cfg):
    before = ready.execute("SELECT count(*) FROM ops_audit_log WHERE actor LIKE 'agent-offline:%'").fetchone()[0]
    agent(ready, cfg).ask("which plant has the most shortages?")
    after = ready.execute("SELECT count(*) FROM ops_audit_log WHERE actor LIKE 'agent-offline:%'").fetchone()[0]
    assert after == before + 1
