"""Role-based access: identity resolution, tool permissions, data-layer scope."""
from __future__ import annotations

import json

import pytest

from sapkit import db, outreach, replies
from sapkit.rbac import SYSTEM_USER, AccessDenied, load_rbac
from sapkit.tools import Toolbox, tool_definitions


@pytest.fixture
def rbac():
    return load_rbac()


def test_unknown_and_roleless_users_are_denied(rbac, cfg):
    with pytest.raises(AccessDenied):
        rbac.session_for("nobody@example.com", cfg)
    with pytest.raises(AccessDenied):
        rbac.session_for("contractor@acme-dummy.example", cfg)


def test_identity_is_case_insensitive(rbac, cfg):
    assert rbac.session_for("JORDAN.Planner@acme-dummy.example", cfg).user == "jordan.planner@acme-dummy.example"


def test_role_tools_and_actions(rbac, cfg):
    planner = rbac.session_for("jordan.planner@acme-dummy.example", cfg)
    buyer = rbac.session_for("avery.buyer@acme-dummy.example", cfg)
    manager = rbac.session_for("morgan.manager@acme-dummy.example", cfg)
    admin = rbac.session_for("admin@acme-dummy.example", cfg)
    assert not planner.can_use_tool("get_supplier_replies") and planner.can_use_tool("get_shortage_queue")
    assert buyer.can("approve_draft") and not buyer.can("run_outreach")
    assert manager.can("run_outreach") and manager.purchasing_groups is None
    assert buyer.purchasing_groups == ("P01", "P02")
    assert admin.tools is None and admin.actions is None


def test_system_session_has_no_tools(rbac, cfg):
    s = rbac.session_for(SYSTEM_USER, cfg)
    assert not s.can_use_tool("get_shortage_summary") and s.allowed_plants == list(cfg.plants)


def test_restricted_to_only_narrows(rbac, cfg):
    s = rbac.session_for("riley.planner@acme-dummy.example", cfg).restricted_to(["1000", "2000"])
    assert s.allowed_plants == ["2000"]


def test_model_only_sees_permitted_tools(rbac, cfg):
    names = {t["name"] for t in tool_definitions(cfg, rbac.session_for("jordan.planner@acme-dummy.example", cfg))}
    assert "get_supplier_replies" not in names and "get_shortage_queue" in names


def test_denied_tool_is_refused_and_audited(rbac, cfg, workdir):
    s = rbac.session_for("jordan.planner@acme-dummy.example", cfg)
    con = db.connect(cfg, workdir / "db.duckdb", session=s)
    text, err = Toolbox(con, cfg, s, channel="test").run("get_supplier_replies",
                                                         {"vendor": "", "category": "", "needs_review_only": False})
    assert err and "role" in json.loads(text)["error"]
    row = con.execute("SELECT detail FROM ops_audit_log WHERE subject = 'get_supplier_replies'").fetchone()
    assert json.loads(row[0])["decision"] == "denied_tool"
    con.close()


def test_data_layer_scope_hides_other_plants_and_groups(rbac, cfg, workdir):
    buyer = rbac.session_for("avery.buyer@acme-dummy.example", cfg)
    con = db.connect(cfg, workdir / "db.duckdb", session=buyer)
    plants = {r[0] for r in con.execute("SELECT DISTINCT werks FROM v_scope").fetchall()}
    groups = {r[0] for r in con.execute("SELECT DISTINCT ekgrp FROM v_scope").fetchall()}
    assert plants == {"1000"} and groups <= {"P01", "P02"}
    # Every downstream view inherits the scope.
    assert con.execute("SELECT count(*) FROM v_shortage WHERE werks <> '1000'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM v_forecast_change WHERE ekgrp NOT IN ('P01', 'P02')").fetchone()[0] == 0
    con.close()


def test_reply_views_are_scoped(rbac, cfg, workdir, mailbox):
    con = db.connect(cfg, workdir / "db.duckdb")
    outreach.run_outreach(con, cfg, mailbox)
    replies.process_replies(con, cfg, mailbox, replies.OfflineClassifier())
    total = con.execute("SELECT count(*) FROM ops_reply_extract").fetchone()[0]
    con.close()
    buyer = rbac.session_for("avery.buyer@acme-dummy.example", cfg)
    c2 = db.connect(cfg, workdir / "db.duckdb", session=buyer)
    mine = c2.execute("SELECT count(*) FROM v_my_reply_extract").fetchone()[0]
    foreign = c2.execute("""SELECT count(*) FROM v_my_outreach WHERE ekgrp NOT IN ('P01', 'P02')""").fetchone()[0]
    c2.close()
    assert 0 < mine < total and foreign == 0


def test_user_admin_round_trip(tmp_path, rbac, cfg):
    rbac.path = tmp_path / "rbac.yaml"
    rbac.upsert_user("new.buyer@acme-dummy.example", "New Buyer", ["buyer"], ["2000"], ["P05"])
    rbac.save()
    again = load_rbac(tmp_path / "rbac.yaml")
    s = again.session_for("new.buyer@acme-dummy.example", cfg)
    assert s.allowed_plants == ["2000"] and s.purchasing_groups == ("P05",)
    with pytest.raises(ValueError):
        again.upsert_user("x@y.z", "X", ["superuser"], ["1000"], None)
    assert again.remove_user("new.buyer@acme-dummy.example")
