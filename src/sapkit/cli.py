"""Command line: `python -m sapkit <command>`.

Every command runs without an API key. Only `ask` (with a key) and `eval --live`
call Claude; without a key, `ask` uses the offline rule-based agent.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import db, outreach, replies, synth
from .config import ROOT, load_config
from .mail import MockMailbox
from .rbac import AccessDenied, Session, load_rbac
from .tools import Toolbox

DATA = ROOT / "data"
OUT = ROOT / "out"


def _cfg(args):
    return load_config(args.config)


DEFAULT_USER = "morgan.manager@acme-dummy.example"  # demo purchasing manager; override with --user


def _plants(args, cfg) -> list[str]:
    return args.plants.split(",") if getattr(args, "plants", None) else list(cfg.plants)


def _session(args, cfg) -> Session:
    """Resolve the caller through RBAC; --plants can only narrow their scope."""
    user = getattr(args, "user", None) or os.environ.get("SAPKIT_USER") or DEFAULT_USER
    try:
        session = load_rbac().session_for(user, cfg)
    except AccessDenied as e:
        print(f"Access denied: {e}", file=sys.stderr)
        sys.exit(3)
    if getattr(args, "plants", None):
        session = session.restricted_to(args.plants.split(","))
    return session


def _require(session: Session, action: str) -> None:
    if not session.can(action):
        print(f"Access denied: {session.user} ({', '.join(session.roles)}) may not {action.replace('_', ' ')}.",
              file=sys.stderr)
        sys.exit(3)


def _mailbox():
    return MockMailbox(DATA / "mailbox.json", OUT)


def cmd_generate(args):
    cfg = _cfg(args)
    t0 = time.time()
    res = synth.generate(cfg, DATA, materials_per_plant=args.materials, seed=args.seed)
    _reset_ops(quiet=True)
    print(f"Generated dummy SAP data for {cfg.client_name} in {time.time() - t0:.1f}s")
    for table, n in sorted(res["rows"].items()):
        print(f"  {table:<20}{n:>9,}")
    print(f"Database: {res['db']}")


def _reset_ops(quiet=False):
    if (OUT / "drafts").exists():
        shutil.rmtree(OUT / "drafts")
    for f in ("mailbox_state.json", "mailbox_threads.jsonl"):
        if (OUT / f).exists():
            (OUT / f).unlink()
    path = DATA / "acme.duckdb"
    if path.exists():
        import duckdb
        con = duckdb.connect(str(path))
        for t in ("ops_audit_log", "ops_outreach", "ops_outreach_item", "ops_disposition",
                  "ops_reply_extract", "ops_reply_commitment", "ops_thread_watermark"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.close()
    if not quiet:
        print("Cleared drafts, mailbox state and ops tables.")


def cmd_reset(args):
    _reset_ops()


def cmd_query(args):
    cfg = _cfg(args)
    session = _session(args, cfg)
    con = db.connect(cfg, session=session)
    tb = Toolbox(con, cfg, session, channel="cli")
    params = json.loads(args.args) if args.args else {}
    text, is_error = tb.run(args.tool, params)
    print(json.dumps(json.loads(text), indent=2))
    sys.exit(1 if is_error else 0)


def cmd_outreach(args):
    cfg = _cfg(args)
    session = _session(args, cfg)
    _require(session, "run_outreach")
    con = db.connect(cfg, session=session)
    res = outreach.run_outreach(con, cfg, _mailbox(), actor=session.user, plants=session.allowed_plants,
                                create_drafts=not args.dry_run)
    print(json.dumps(res, indent=2))
    if not args.dry_run:
        print(f"Drafts written to {OUT / 'drafts'}")


def _classifier(args, cfg):
    if args.offline or not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        if not args.offline:
            print("No Anthropic credentials found; using the offline keyword classifier.", file=sys.stderr)
        return replies.OfflineClassifier()
    return replies.ClaudeClassifier(cfg)


def cmd_replies(args):
    cfg = _cfg(args)
    session = _session(args, cfg)
    _require(session, "process_replies")
    con = db.connect(cfg)
    res = replies.process_replies(con, cfg, _mailbox(), _classifier(args, cfg), actor=session.user)
    print(json.dumps(res, indent=2))


def _has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def cmd_ask(args):
    cfg = _cfg(args)
    session = _session(args, cfg)
    con = db.connect(cfg, session=session)
    offline = args.offline or not _has_credentials()
    if offline:
        from .offline_agent import OFFLINE_NOTE, OfflineAgent
        agent = OfflineAgent(con, cfg, session)
        if not args.offline:
            print("No Anthropic API key found, so the agent is running in offline mode: same tools and data, "
                  "simple rules instead of Claude.\n", file=sys.stderr)
    else:
        from .agent import PlanningAgent
        agent = PlanningAgent(con, cfg, session)
    questions = [args.question] if args.question else None
    if questions is None:
        print(f"Planning assistant for {cfg.client_name}, plants {', '.join(session.allowed_plants)}"
              f"{' ' + OFFLINE_NOTE if offline else ''}.")
        print("Ask about shortages, late shipments, forecast changes, supplier outreach or replies. "
              "Press Enter on an empty line to quit.\n")
    import anthropic
    while True:
        try:
            q = questions.pop(0) if questions else (input("> ") if questions is None else "")
        except (EOFError, KeyboardInterrupt):  # Ctrl+Z / Ctrl+D / Ctrl+C, or no terminal attached
            print()
            break
        if not q:
            break
        try:
            res = agent.ask(q)
        except (anthropic.AuthenticationError, TypeError) as e:
            if isinstance(e, TypeError) and "authentication" not in str(e).lower():
                raise
            print("No usable Anthropic credentials. Set ANTHROPIC_API_KEY, or run with --offline.", file=sys.stderr)
            sys.exit(2)
        print(res.answer + ("\n" if questions is None else ""))
        if args.show_tools:
            print("\n[tools]", json.dumps(res.tool_calls))
        if questions is not None and not questions:
            break


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        if not rows:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def cmd_report(args):
    cfg = _cfg(args)
    con = db.connect(cfg)
    rep = OUT / "report"
    q = lambda sql: db.rows(con, sql)
    exports = {
        "shortages.csv": "SELECT werks, risk_rank, matnr, maktx, first_short_date, days_to_short, shortfall_qty, "
                         "recovery_date, parent_count, unit_cost, value_at_risk, risk_score FROM v_shortage ORDER BY werks, risk_rank",
        "missing_asn.csv": "SELECT werks, ebeln, matnr, lifnr, vendor_name, eindt, CAST(ship_by_date AS DATE) ship_by_date, "
                           "days_past_ship_by, open_qty FROM v_missing_asn ORDER BY werks, days_past_ship_by DESC",
        "safety_stock_dips.csv": "SELECT * FROM v_safety_stock_dip ORDER BY werks, first_dip_date",
        "forecast_changes.csv": "SELECT werks, change_rank, matnr, maktx, window_, plifz, prior_qty, current_qty, change_pct "
                                "FROM v_forecast_change ORDER BY werks, change_rank, matnr",
        "outreach_dispositions.csv": "SELECT * FROM ops_disposition WHERE run_id = (SELECT run_id FROM ops_disposition "
                                     "GROUP BY run_id ORDER BY count(*) DESC, run_id LIMIT 1) ORDER BY werks, matnr",
        "drafts.csv": "SELECT outreach_id, lifnr, ekgrp, buyer_email, vendor_email, item_count, thread_id FROM ops_outreach ORDER BY lifnr",
        "replies.csv": "SELECT lifnr, message_id, category, needs_buyer_review, instructions_to_ai_detected, summary, review_reasons "
                       "FROM ops_reply_extract ORDER BY lifnr, message_id",
    }
    for name, sql in exports.items():
        try:
            _write_csv(rep / name, q(sql))
        except Exception as e:  # ops tables are empty before the first outreach run
            print(f"  skipped {name}: {e}", file=sys.stderr)
    summary = q("""SELECT werks, count(*) shortages, count(*) FILTER (WHERE days_to_short <= 7) within_7,
                          round(sum(coalesce(value_at_risk, 0)), 0) value_at_risk FROM v_shortage GROUP BY werks ORDER BY werks""")
    lines = [f"# {cfg.client_name}: planning report", "", f"Data as of {cfg.as_of:%d %b %Y}, cover rule `{cfg.cover_rule}`.", "",
             "| Plant | Shortages | Within 7 days | Value at risk |", "|---|---:|---:|---:|"]
    for s in summary:
        lines.append(f"| {s['werks']} {cfg.plants[s['werks']]} | {s['shortages']:,} | {s['within_7']:,} | {s['value_at_risk']:,.0f} |")
    disp = q("SELECT disposition, count(*) n FROM ops_disposition WHERE run_id = (SELECT run_id FROM ops_disposition "
             "GROUP BY run_id ORDER BY count(*) DESC, run_id LIMIT 1) GROUP BY 1 ORDER BY 2 DESC") if _has(con, "ops_disposition") else []
    if disp:
        lines += ["", "## Outreach dispositions", "", "| Disposition | Items |", "|---|---:|"]
        lines += [f"| {d['disposition']} | {d['n']:,} |" for d in disp]
    cats = q("SELECT category, count(*) n, count(*) FILTER (WHERE needs_buyer_review) review FROM ops_reply_extract GROUP BY 1 ORDER BY 2 DESC") if _has(con, "ops_reply_extract") else []
    if cats:
        lines += ["", "## Supplier replies", "", "| Category | Replies | Need buyer review |", "|---|---:|---:|"]
        lines += [f"| {c['category']} | {c['n']} | {c['review']} |" for c in cats]
    (rep / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nCSV exports in {rep}")


def _has(con, table) -> bool:
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()[0] > 0


def cmd_demo(args):
    cfg = _cfg(args)
    if not (DATA / f"{cfg.client_id}.duckdb").exists() or args.regenerate:
        cmd_generate(argparse.Namespace(config=args.config, materials=args.materials, seed=args.seed))
    else:
        _reset_ops(quiet=True)
    con = db.connect(cfg)
    print("\n== Outreach ==")
    print(json.dumps(outreach.run_outreach(con, cfg, _mailbox(), actor="demo"), indent=2))
    print("\n== Supplier replies ==")
    print(json.dumps(replies.process_replies(con, cfg, _mailbox(), _classifier(args, cfg), actor="demo"), indent=2))
    print("\n== Rerun (should create nothing new) ==")
    print(json.dumps(outreach.run_outreach(con, cfg, _mailbox(), actor="demo"), indent=2))
    print(json.dumps(replies.process_replies(con, cfg, _mailbox(), _classifier(args, cfg), actor="demo"), indent=2))
    con.close()
    print("\n== Report ==")
    cmd_report(args)


def cmd_mcp(args):
    from .mcp_server import main as mcp_main
    if args.plants:
        os.environ["SAPKIT_PLANTS"] = args.plants
    os.environ["SAPKIT_USER"] = args.user or os.environ.get("SAPKIT_USER") or DEFAULT_USER
    mcp_main(args.config)


def cmd_eval(args):
    from .evals_runner import run_evals
    sys.exit(run_evals(_cfg(args), live=args.live, limit=args.limit, offline_agent=args.agent))


VERIFY_CHECKS = {
    "Shortage flag (ASN-only rule)": """
        SELECT count(*), count(*) FILTER (WHERE (s.matnr IS NOT NULL) = e.short_asn_only)
        FROM expected_shortage e LEFT JOIN v_shortage s USING (matnr, werks)""",
    "First short date and depth": """
        SELECT count(*), count(*) FILTER (WHERE s.first_short_date = e.first_short_asn_only
                                            AND abs(s.shortfall_qty - e.shortfall_asn_only) < 1e-6)
        FROM expected_shortage e JOIN v_shortage s USING (matnr, werks)""",
    "Assemblies fed (criticality)": """
        SELECT count(*), count(*) FILTER (WHERE s.parent_count = e.parent_count)
        FROM expected_shortage e JOIN v_shortage s USING (matnr, werks)""",
    "Missing shipping notice": """
        SELECT count(*), count(*) FILTER (WHERE (m.matnr IS NOT NULL) = e.missing_asn)
        FROM expected_shortage e LEFT JOIN (SELECT DISTINCT matnr, werks FROM v_missing_asn) m USING (matnr, werks)""",
    "Safety-stock dip": """
        SELECT count(*), count(*) FILTER (WHERE (d.matnr IS NOT NULL) = e.ss_dip)
        FROM expected_shortage e LEFT JOIN v_safety_stock_dip d USING (matnr, werks)""",
    "Forecast change rank": """
        SELECT count(*), count(*) FILTER (WHERE f.change_rank = e.expected_rank AND f.window_ = e.window_)
        FROM expected_forecast e LEFT JOIN v_forecast_change f USING (matnr, werks)""",
}


def cmd_verify(args):
    """Compare every rule with the answers the generator built in."""
    cfg = _cfg(args)
    con = db.connect(cfg)
    failures = 0
    print(f"Verifying rules against known answers ({cfg.client_name})\n")
    for name, sql in VERIFY_CHECKS.items():
        total, ok = con.execute(sql).fetchone()
        failures += total - ok
        print(f"  {'OK  ' if ok == total else 'FAIL'} {name:<32}{ok:>7,} / {total:,}")
    expected = {(r["matnr"], r["werks"]): r for r in db.rows(con, "SELECT * FROM expected_forecast")}
    decisions = outreach.decide_all(con, cfg)
    ok = sum(1 for d in decisions if d["disposition"] == expected[(d["matnr"], d["werks"])]["expected_disposition"]
             and (not expected[(d["matnr"], d["werks"])]["expected_vendor"]
                  or d["resolved_vendor"] == expected[(d["matnr"], d["werks"])]["expected_vendor"]))
    failures += len(decisions) - ok
    print(f"  {'OK  ' if ok == len(decisions) else 'FAIL'} {'Outreach disposition + supplier':<32}{ok:>7,} / {len(decisions):,}")
    print("\nAll rules match." if failures == 0 else f"\n{failures:,} mismatches.")
    sys.exit(1 if failures else 0)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sapkit", description="SAP planning agents on Claude (dummy client)")
    p.add_argument("--config", default=None, help="client config YAML (default config/acme.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="build the dummy SAP dataset")
    g.add_argument("--materials", type=int, default=5000, help="materials per plant")
    g.add_argument("--seed", type=int, default=2026)
    g.set_defaults(func=cmd_generate)

    sub.add_parser("reset", help="clear drafts, mailbox state and ops tables").set_defaults(func=cmd_reset)

    q = sub.add_parser("query", help="run one governed tool directly (no API key needed)")
    q.add_argument("tool")
    q.add_argument("args", nargs="?", help="JSON arguments")
    q.add_argument("--plants")
    q.set_defaults(func=cmd_query)

    o = sub.add_parser("outreach", help="decide eligibility and create supplier drafts")
    o.add_argument("--plants")
    o.add_argument("--dry-run", action="store_true")
    o.set_defaults(func=cmd_outreach)

    r = sub.add_parser("replies", help="process supplier replies on tracked threads")
    r.add_argument("--offline", action="store_true", help="keyword classifier, no API key")
    r.set_defaults(func=cmd_replies)

    a = sub.add_parser("ask", help="ask the planning agent (Claude with a key, offline rules without)")
    a.add_argument("question", nargs="?")
    a.add_argument("--offline", action="store_true", help="use the offline rule-based agent even if a key is set")
    a.add_argument("--plants")
    a.add_argument("--show-tools", action="store_true")
    a.set_defaults(func=cmd_ask)

    sub.add_parser("report", help="write CSV exports and a summary").set_defaults(func=cmd_report)

    d = sub.add_parser("demo", help="end-to-end run: outreach, replies, rerun, report")
    d.add_argument("--offline", action="store_true")
    d.add_argument("--regenerate", action="store_true")
    d.add_argument("--materials", type=int, default=5000)
    d.add_argument("--seed", type=int, default=2026)
    d.set_defaults(func=cmd_demo)

    m = sub.add_parser("mcp", help="serve the nine tools over MCP (stdio) for Claude Code / Claude Desktop")
    m.add_argument("--plants")
    m.set_defaults(func=cmd_mcp)

    sub.add_parser("verify", help="check every rule against the answers built into the data").set_defaults(func=cmd_verify)

    e = sub.add_parser("eval", help="agent evaluation questions (tool-level offline, or --live with Claude)")
    e.add_argument("--live", action="store_true", help="ask the real Claude agent (needs a key)")
    e.add_argument("--agent", action="store_true", help="ask the offline agent (no key needed)")
    e.add_argument("--limit", type=int, default=0)
    e.set_defaults(func=cmd_eval)

    for sp in (q, o, r, a, m):
        sp.add_argument("--user", help=f"who is acting (default: $SAPKIT_USER or {DEFAULT_USER})")
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
