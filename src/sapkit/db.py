"""DuckDB connection, rule rendering from config, and the ops (write) tables."""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

from .config import ROOT, ClientConfig

SQL_DIR = ROOT / "sql"

OPS_DDL = """
CREATE TABLE IF NOT EXISTS ops_audit_log (
    event_id     VARCHAR PRIMARY KEY,
    event_at     TIMESTAMP DEFAULT current_timestamp,
    actor        VARCHAR,          -- 'agent', 'batch', or a person
    event_type   VARCHAR,          -- tool_call, draft_created, draft_skipped, reply_processed, ...
    subject      VARCHAR,          -- what it is about (vendor, thread, tool name)
    detail       VARCHAR           -- JSON
);
CREATE TABLE IF NOT EXISTS ops_outreach (
    outreach_id      VARCHAR PRIMARY KEY,   -- idempotency key hash
    client_id        VARCHAR,
    capture_prev     VARCHAR,
    capture_cur      VARCHAR,
    lifnr            VARCHAR,
    ekgrp            VARCHAR,
    template_version VARCHAR,
    buyer_email      VARCHAR,
    vendor_email     VARCHAR,
    thread_id        VARCHAR,
    draft_ref        VARCHAR,
    item_count       INTEGER,
    created_at       TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS ops_outreach_item (
    outreach_id VARCHAR, matnr VARCHAR, werks VARCHAR, window_ VARCHAR,
    prior_qty DOUBLE, current_qty DOUBLE, change_pct DOUBLE, change_rank INTEGER
);
CREATE TABLE IF NOT EXISTS ops_disposition (
    run_id VARCHAR, matnr VARCHAR, werks VARCHAR, change_rank INTEGER,
    disposition VARCHAR, reason VARCHAR, lifnr VARCHAR, ekgrp VARCHAR
);
CREATE TABLE IF NOT EXISTS ops_reply_extract (
    client_id     VARCHAR,
    message_id    VARCHAR,
    thread_id     VARCHAR,
    lifnr         VARCHAR,
    category      VARCHAR,
    summary       VARCHAR,
    instructions_to_ai_detected BOOLEAN,
    needs_buyer_review BOOLEAN,
    review_reasons VARCHAR,
    classifier    VARCHAR,
    processed_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (client_id, message_id)
);
CREATE TABLE IF NOT EXISTS ops_reply_commitment (
    client_id VARCHAR, message_id VARCHAR, matnr VARCHAR, confirmed_qty DOUBLE,
    ship_date DATE, constraint_note VARCHAR, matched_outreach BOOLEAN
);
CREATE TABLE IF NOT EXISTS ops_trace (
    trace_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT current_timestamp,
    user_id VARCHAR, channel VARCHAR, question VARCHAR,
    latency_ms INTEGER, outcome VARCHAR, guardrail VARCHAR,
    tool_calls INTEGER, tools VARCHAR, model VARCHAR, model_calls INTEGER,
    input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT, cost_usd DOUBLE
);
CREATE TABLE IF NOT EXISTS ops_draft_review (
    outreach_id VARCHAR PRIMARY KEY,
    status VARCHAR,                -- PENDING_REVIEW, APPROVED, ON_HOLD, SENT
    reviewer VARCHAR,
    reviewed_at TIMESTAMP,
    edited_body VARCHAR,
    note VARCHAR,
    sent_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ops_followup (
    followup_id VARCHAR PRIMARY KEY,
    outreach_id VARCHAR, lifnr VARCHAR, step VARCHAR, due_at TIMESTAMP,
    status VARCHAR, created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS ops_dq_result (
    run_id VARCHAR, check_id VARCHAR, severity VARCHAR, owner VARCHAR,
    description VARCHAR, exceptions BIGINT, checked_rows BIGINT,
    created_at TIMESTAMP DEFAULT current_timestamp
);
CREATE TABLE IF NOT EXISTS ops_thread_watermark (
    thread_id VARCHAR PRIMARY KEY,
    last_message_id VARCHAR,
    last_sent_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT current_timestamp
);
"""


def _quote_list(values) -> str:
    vals = [str(v).replace("'", "''") for v in values]
    return ", ".join(f"'{v}'" for v in vals) or "''"


def scope_clause(session) -> str:
    """SQL predicate on MARC (alias c) limiting rows to the caller's scope."""
    if session is None:
        return ""
    plants = _quote_list(session.allowed_plants) if session.allowed_plants else "NULL"
    clause = f"AND c.werks IN ({plants})"
    if session.purchasing_groups is not None:
        groups = _quote_list(session.purchasing_groups) if session.purchasing_groups else "NULL"
        clause += f" AND c.ekgrp IN ({groups})"
    return clause


def scoped_ops_views(session) -> str:
    """Outreach and reply records the caller may see: their plants and purchasing groups."""
    if session is None:
        o_filter = "TRUE"
    else:
        plants = _quote_list(session.allowed_plants) if session.allowed_plants else "NULL"
        o_filter = f"EXISTS (SELECT 1 FROM ops_outreach_item i WHERE i.outreach_id = o.outreach_id AND i.werks IN ({plants}))"
        if session.purchasing_groups is not None:
            groups = _quote_list(session.purchasing_groups) if session.purchasing_groups else "NULL"
            o_filter += f" AND o.ekgrp IN ({groups})"
    return f"""
CREATE OR REPLACE TEMP VIEW v_my_outreach AS SELECT o.* FROM ops_outreach o WHERE {o_filter};
CREATE OR REPLACE TEMP VIEW v_my_reply_extract AS
    SELECT r.* FROM ops_reply_extract r WHERE r.lifnr IN (SELECT lifnr FROM v_my_outreach);
"""


def latest_capture_pair(con: duckdb.DuckDBPyConnection) -> tuple[str, str]:
    rows = con.execute(
        "SELECT capture_id FROM forecast_snapshot GROUP BY capture_id "
        "ORDER BY max(captured_at) DESC LIMIT 2").fetchall()
    if len(rows) < 2:
        raise RuntimeError("Need at least two forecast captures to compare")
    return rows[1][0], rows[0][0]


def render_rules(cfg: ClientConfig, con: duckdb.DuckDBPyConnection, session=None) -> str:
    pair = cfg.get("forecast", "capture_pair", default="latest")
    if pair == "latest":
        prev, cur = latest_capture_pair(con)
    else:
        prev, cur = pair
    bands = cfg.get("risk", "urgency_bands") or [{"max_days": 9999, "factor": 1}]
    urgency = "CASE " + " ".join(
        f"WHEN days_to_short <= {int(b['max_days'])} THEN {int(b['factor'])}" for b in bands) + " ELSE 1 END"
    params = {
        "as_of": cfg.as_of.isoformat(),
        "horizon_days": str(cfg.horizon_days),
        "plants": _quote_list(cfg.plants),
        "procurement_types": _quote_list(cfg.get("scope", "procurement_types")),
        "exclude_mrp_types": _quote_list(cfg.get("scope", "exclude_mrp_types")),
        "active_statuses": _quote_list(cfg.get("scope", "active_statuses")),
        "cover_rule": cfg.cover_rule,
        "asn_key": cfg.get("asn", "required_confirmation_key"),
        "default_transit_days": str(int(cfg.get("asn", "default_transit_days"))),
        "lookback_days": str(int(cfg.get("asn", "lookback_days"))),
        "capture_prev": prev,
        "capture_cur": cur,
        "hi_pct": str(float(cfg.get("forecast", "change_threshold_high_pct"))),
        "lo_pct": str(float(cfg.get("forecast", "change_threshold_low_pct"))),
        "urgency_case": urgency,
        "session_scope": scope_clause(session),
    }
    sql = (SQL_DIR / "views.sql").read_text(encoding="utf-8")
    for k, v in params.items():
        sql = sql.replace("{{" + k + "}}", v)
    if "{{" in sql:
        missing = sql[sql.index("{{"): sql.index("{{") + 40]
        raise RuntimeError(f"Unfilled placeholder in views.sql near: {missing}")
    # Temp views: per-connection, never persisted, so two configs never collide.
    return sql.replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TEMP VIEW")


def connect(cfg: ClientConfig, db_path: str | Path | None = None, read_only: bool = False,
            session=None) -> duckdb.DuckDBPyConnection:
    # SAPKIT_DB lets a separate process (the MCP server, a test) point at another copy.
    path = Path(db_path or os.environ.get("SAPKIT_DB") or ROOT / "data" / f"{cfg.client_id}.duckdb")
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run `sapkit generate` first.")
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        con.execute(OPS_DDL)
    con.execute(render_rules(cfg, con, session))
    if not read_only:
        con.execute(scoped_ops_views(session))
    return con


def rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def insert_many(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> None:
    """executemany that accepts an empty list (DuckDB rejects one)."""
    if params:
        con.executemany(sql, params)
