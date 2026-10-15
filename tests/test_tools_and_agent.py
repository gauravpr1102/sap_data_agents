"""Governed tools and the Claude agent loop (with a scripted client)."""
from __future__ import annotations

import json

from sapkit import guard, outreach, replies
from sapkit.agent import PlanningAgent, system_prompt
from sapkit.tools import MAX_ROWS, Session, Toolbox, tool_definitions


def tb(con, cfg, plants=None):
    return Toolbox(con, cfg, Session("test", plants or list(cfg.plants)))


# ------------------------------------------------------------------ tools
def test_plant_scope_is_enforced(con, cfg):
    text, err = tb(con, cfg, ["1000"]).run("get_shortage_queue", {"plant": "2000", "within_days": 0, "limit": 5})
    assert err and "access" in json.loads(text)["error"]


def test_unknown_plant_is_refused(con, cfg):
    text, err = tb(con, cfg).run("get_missing_asn", {"plant": "9999", "limit": 5})
    assert err and "Unknown plant" in json.loads(text)["error"]


def test_summary_only_shows_allowed_plants(con, cfg):
    data = json.loads(tb(con, cfg, ["1000"]).run("get_shortage_summary", {})[0])
    assert {r["plant"] for r in data["rows"]} == {"1000"}


def test_material_number_with_or_without_zeros(con, cfg):
    mat = con.execute("SELECT matnr FROM v_shortage WHERE werks = '1000' LIMIT 1").fetchone()[0]
    a = json.loads(tb(con, cfg).run("get_shortage_detail", {"material": mat, "plant": "1000"})[0])
    b = json.loads(tb(con, cfg).run("get_shortage_detail", {"material": mat.lstrip("0"), "plant": "1000"})[0])
    assert a == b and a["is_short"]


def test_row_cap_and_total_matching(con, cfg):
    data = json.loads(tb(con, cfg).run("get_shortage_queue", {"plant": "1000", "within_days": 0, "limit": 500})[0])
    total = con.execute("SELECT count(*) FROM v_shortage WHERE werks = '1000'").fetchone()[0]
    assert data["returned"] == MAX_ROWS and data["total_matching"] == total and data["truncated"]
    assert data["data_as_of"] == "2026-10-14"


def test_bad_arguments_return_an_error_not_a_crash(con, cfg):
    text, err = tb(con, cfg).run("get_shortage_queue", {"plant": "1000"})
    assert err and "bad arguments" in json.loads(text)["error"]
    text, err = tb(con, cfg).run("drop_tables", {})
    assert err


def test_tool_schemas_are_strict(cfg):
    for t in tool_definitions(cfg):
        s = t["input_schema"]
        assert t["strict"] is True and s["additionalProperties"] is False
        assert set(s["required"]) == set(s["properties"]), t["name"]


def test_every_tool_runs(con, cfg, mailbox):
    outreach.run_outreach(con, cfg, mailbox)
    replies.process_replies(con, cfg, mailbox, replies.OfflineClassifier())
    vendor = con.execute("SELECT lifnr FROM ops_outreach LIMIT 1").fetchone()[0]
    mat = con.execute("SELECT matnr FROM v_shortage WHERE werks = '2000' LIMIT 1").fetchone()[0]
    args = {
        "get_shortage_summary": {},
        "get_shortage_queue": {"plant": "1000", "within_days": 7, "limit": 5},
        "get_shortage_detail": {"material": mat, "plant": "2000"},
        "get_missing_asn": {"plant": "3000", "limit": 5},
        "get_safety_stock_dips": {"plant": "1000", "limit": 5},
        "get_forecast_changes": {"plant": "2000", "ranks": [1, 2], "limit": 5},
        "get_outreach_status": {"plant": "1000"},
        "preview_supplier_email": {"vendor": vendor},
        "get_supplier_replies": {"vendor": "", "category": "", "needs_review_only": False},
        "get_drafts_for_review": {"status": "PENDING_REVIEW", "limit": 5},
    }
    assert set(args) == {t["name"] for t in tool_definitions(cfg)}
    for name, a in args.items():
        text, err = tb(con, cfg).run(name, a)
        assert not err, (name, text)


# ------------------------------------------------------------------ guard
def test_guard_detects_markdown_tables():
    assert guard.has_table("| a | b |\n|---|---|\n| 1 | 2 |")
    assert guard.has_table("a | b\n:--- | ---:\n1 | 2")
    assert not guard.has_table("Plant 1000 has 12 shortages - the worst is 10000123.")
    assert not guard.has_table("Summary\n---\nPlant 1000 is worst.")  # a horizontal rule is not a table
    assert guard.has_table("| Plant |\n|---|\n| 1000 |")  # one-column table
    assert guard.should_block("| a |\n|---|\n| 1 |", 0) and not guard.should_block("| a |\n|---|\n| 1 |", 1)


# ------------------------------------------------------------------ agent loop
def test_system_prompt_is_rendered_from_config(cfg):
    sp = system_prompt(cfg)
    assert cfg.client_name in sp and "{" not in sp.replace("{}", "")
    assert "105-day" in sp and "15%" in sp and "Ranks 1, 2, 3 qualify" in sp


def test_agent_calls_tool_then_answers(con, cfg, fake):
    client = fake.FakeClient([
        fake.response([fake.tool("t1", "get_shortage_queue", {"plant": "1000", "within_days": 7, "limit": 3})],
                      stop_reason="tool_use"),
        fake.response([fake.text("Plant 1000 has shortages within 7 days.\n\n| Material | Short on |\n|---|---|\n| 1 | 2 |")]),
    ])
    agent = PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client)
    res = agent.ask("What's short at 1000 this week?")
    assert not res.blocked and res.tool_calls == [{"tool": "get_shortage_queue",
                                                    "input": {"plant": "1000", "within_days": 7, "limit": 3},
                                                    "error": False}]
    # The tool result went back to Claude with the right id and real data.
    second = client.calls[1]["messages"]
    tr = second[-1]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1" and "total_matching" in tr["content"]
    # Request shape: model, effort, fallbacks, cached system prompt, no forced tool choice.
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "medium"}
    assert call["fallbacks"] == "default" and "tool_choice" not in call
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert con.execute("SELECT count(*) FROM ops_audit_log WHERE event_type = 'tool_call'").fetchone()[0] == 1


def test_guard_blocks_ungrounded_table_after_one_nudge(con, cfg, fake):
    table = "| Material | Qty |\n|---|---|\n| 123 | 999 |"
    client = fake.FakeClient([fake.response([fake.text(table)]), fake.response([fake.text(table)])])
    res = PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client).ask("Give me a table")
    assert res.blocked and res.answer == guard.BLOCKED_MESSAGE
    assert client.calls[1]["messages"][-1]["content"] == guard.BLOCK_NUDGE


def test_guard_nudge_recovers_with_a_tool_call(con, cfg, fake):
    table = "| Plant | Shortages |\n|---|---|\n| 1000 | 5 |"
    client = fake.FakeClient([
        fake.response([fake.text(table)]),
        fake.response([fake.tool("t1", "get_shortage_summary", {})], stop_reason="tool_use"),
        fake.response([fake.text(table)]),
    ])
    res = PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client).ask("Table please")
    assert not res.blocked and res.tool_calls[0]["tool"] == "get_shortage_summary"


def test_scope_error_reaches_the_model_as_an_error(con, cfg, fake):
    client = fake.FakeClient([
        fake.response([fake.tool("t1", "get_missing_asn", {"plant": "3000", "limit": 5})], stop_reason="tool_use"),
        fake.response([fake.text("You don't have access to plant 3000.")]),
    ])
    res = PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client).ask("Missing ASNs at 3000?")
    tr = client.calls[1]["messages"][-1]["content"][0]
    assert tr["is_error"] is True and res.tool_calls[0]["error"] is True


def test_refusal_is_handled(con, cfg, fake):
    client = fake.FakeClient([fake.response([], stop_reason="refusal")])
    res = PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client).ask("...")
    assert res.refused and res.stop_reason == "refusal"


def test_history_is_append_only_across_turns(con, cfg, fake):
    client = fake.FakeClient([fake.response([fake.text("First.")]), fake.response([fake.text("Second.")])])
    agent = PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client)
    agent.ask("q1")
    agent.ask("q2")
    first_sent, second_sent = client.calls[0]["messages"], client.calls[1]["messages"]
    assert second_sent[: len(first_sent)] == first_sent  # earlier turns are never edited
    assert second_sent[-1] == {"role": "user", "content": "q2"}


def test_parallel_tool_calls_return_in_one_message(con, cfg, fake):
    client = fake.FakeClient([
        fake.response([fake.tool("a", "get_shortage_summary", {}),
                       fake.tool("b", "get_missing_asn", {"plant": "1000", "limit": 3})], stop_reason="tool_use"),
        fake.response([fake.text("Done.")]),
    ])
    PlanningAgent(con, cfg, Session("planner", ["1000"]), client=client).ask("overview")
    results = client.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
