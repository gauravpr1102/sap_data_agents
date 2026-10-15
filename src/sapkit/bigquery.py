"""BigQuery deployment of the rules and the access policies (docs/DEPLOYMENT.md).

The business rules are written once, in sql/views.sql. For a BigQuery
deployment this module translates them (with sqlglot) into BigQuery views in a
curated dataset reading the raw SAP dataset, the same raw -> curated split as
the reference build. It also turns config/rbac.yaml into BigQuery row access
policies, so plant and purchasing-group scope is enforced by BigQuery itself.

Output is plain SQL files for review and for `bq query` / Terraform; nothing
here connects to Google Cloud. Every generated statement is parsed back as
BigQuery SQL before it's written, so syntax errors fail here, not in a deploy.
"""
from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from .config import ClientConfig
from .db import SQL_DIR, scope_clause  # noqa: F401  (scope_clause documents the local equivalent)
from .rbac import Rbac

RAW_TABLES = {"t001w", "t024", "lfa1", "adr6", "mara", "marc", "mard", "mbew", "ekko", "ekpo", "eket", "ekes",
              "eord", "zlane", "zvmi", "resb", "forecast_snapshot", "expected_shortage", "expected_forecast"}


def _rendered(cfg: ClientConfig, capture_prev: str, capture_cur: str) -> str:
    from .db import _quote_list
    bands = cfg.get("risk", "urgency_bands") or [{"max_days": 9999, "factor": 1}]
    urgency = "CASE " + " ".join(
        f"WHEN days_to_short <= {int(b['max_days'])} THEN {int(b['factor'])}" for b in bands) + " ELSE 1 END"
    params = {
        "as_of": cfg.as_of.isoformat(), "horizon_days": str(cfg.horizon_days),
        "plants": _quote_list(cfg.plants),
        "procurement_types": _quote_list(cfg.get("scope", "procurement_types")),
        "exclude_mrp_types": _quote_list(cfg.get("scope", "exclude_mrp_types")),
        "active_statuses": _quote_list(cfg.get("scope", "active_statuses")),
        "cover_rule": cfg.cover_rule, "asn_key": cfg.get("asn", "required_confirmation_key"),
        "default_transit_days": str(int(cfg.get("asn", "default_transit_days"))),
        "lookback_days": str(int(cfg.get("asn", "lookback_days"))),
        "capture_prev": capture_prev, "capture_cur": capture_cur,
        "hi_pct": str(float(cfg.get("forecast", "change_threshold_high_pct"))),
        "lo_pct": str(float(cfg.get("forecast", "change_threshold_low_pct"))),
        "urgency_case": urgency,
        "session_scope": "",  # in BigQuery, scope comes from row access policies (below)
    }
    sql = (SQL_DIR / "views.sql").read_text(encoding="utf-8")
    for k, v in params.items():
        sql = sql.replace("{{" + k + "}}", v)
    return sql


def views_sql(cfg: ClientConfig, raw_dataset: str, curated_dataset: str,
              capture_prev: str, capture_cur: str) -> list[str]:
    """CREATE OR REPLACE VIEW statements for BigQuery, one per rule view."""
    statements = [s.strip() for s in sqlglot.transpile(
        _strip_comments(_rendered(cfg, capture_prev, capture_cur)), read="duckdb", write="duckdb") if s.strip()]
    views = {re.search(r"VIEW\s+(\w+)", s, re.I).group(1) for s in statements if re.search(r"VIEW\s+(\w+)", s, re.I)}
    out = []
    for stmt in statements:
        tree = sqlglot.parse_one(stmt, read="duckdb")
        ctes = {c.alias for c in tree.find_all(exp.CTE)}
        tree = tree.transform(_date_arithmetic)
        for t in tree.find_all(exp.Table):
            name = t.name
            if t.args.get("db") or name in ctes:
                continue
            if name in views:
                t.set("db", exp.to_identifier(curated_dataset))
            elif name in RAW_TABLES:
                t.set("db", exp.to_identifier(raw_dataset))
        bq = tree.sql(dialect="bigquery", pretty=True)
        bq = re.sub(r"CREATE OR REPLACE TEMPORARY VIEW", "CREATE OR REPLACE VIEW", bq, flags=re.I)
        sqlglot.parse_one(bq, read="bigquery")  # must parse as BigQuery, or we stop here
        out.append(bq + ";")
    return out


def row_access_policies(rbac: Rbac, cfg: ClientConfig, raw_dataset: str, service_account: str) -> list[str]:
    """Row access policies on MARC (scope root) from rbac.yaml. Unlisted principals see no rows."""
    stmts = [f"DROP ALL ROW ACCESS POLICIES ON `{raw_dataset}.marc`;",
             f"CREATE ROW ACCESS POLICY rap_service ON `{raw_dataset}.marc`\n"
             f"  GRANT TO (\"serviceAccount:{service_account}\")\n  FILTER USING (TRUE);"]
    for uid, u in sorted(rbac.users.items()):
        roles = u.get("roles") or []
        plants = [p for p in (u.get("plants") or []) if str(p) in cfg.plants]
        if not roles or not plants:
            continue
        cond = "werks IN (" + ", ".join(f"'{p}'" for p in plants) + ")"
        groups = u.get("purchasing_groups")
        if groups and "purchasing_manager" not in roles and "admin" not in roles:
            cond += " AND ekgrp IN (" + ", ".join(f"'{g}'" for g in groups) + ")"
        name = "rap_" + re.sub(r"[^a-z0-9]", "_", uid.lower())[:100]
        stmt = (f"CREATE ROW ACCESS POLICY {name} ON `{raw_dataset}.marc`\n"
                f"  GRANT TO (\"user:{uid}\")\n  FILTER USING ({cond});")
        stmts.append(stmt)
    return stmts


def write_bundle(cfg: ClientConfig, rbac: Rbac, out_dir: Path, project: str, raw: str, curated: str,
                 service_account: str, capture_prev: str, capture_cur: str) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_ds, cur_ds = f"{project}.{raw}", f"{project}.{curated}"
    views = views_sql(cfg, raw_ds, cur_ds, capture_prev, capture_cur)
    (out_dir / "10_curated_views.sql").write_text(
        "-- Generated from sql/views.sql by `sapkit bigquery`. Do not edit by hand.\n\n" + "\n\n".join(views),
        encoding="utf-8")
    policies = row_access_policies(rbac, cfg, raw_ds, service_account)
    (out_dir / "20_row_access_policies.sql").write_text(
        "-- Generated from config/rbac.yaml by `sapkit bigquery`. Do not edit by hand.\n\n" + "\n\n".join(policies),
        encoding="utf-8")
    return {"views": len(views), "policies": len(policies), "dir": str(out_dir)}


def _date_arithmetic(node: exp.Expression) -> exp.Expression:
    """date +/- INTERVAL n DAY -> DATE_ADD/DATE_SUB, so BigQuery keeps the DATE type.

    In BigQuery `DATE + INTERVAL` yields a DATETIME, which then can't be compared
    with DATE columns; DATE_ADD returns a DATE.
    """
    if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Interval):
        iv = node.expression
        unit = exp.var((iv.text("unit") or "DAY").upper())
        amount = iv.this
        if isinstance(amount, exp.Literal) and amount.is_string:
            amount = exp.Literal.number(amount.this)
        cls = exp.DateAdd if isinstance(node, exp.Add) else exp.DateSub
        return cls(this=node.this, expression=amount, unit=unit)
    return node


def _strip_comments(sql: str) -> str:
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
