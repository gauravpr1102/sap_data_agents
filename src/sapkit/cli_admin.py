"""Operational commands: users, drafts, follow-ups, data load/export, data quality, BigQuery, audit, web.

Registered by cli.main(). Every command resolves the acting user through RBAC
and checks the action before doing anything.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from . import __version__, db, dq, loader, telemetry, workflow
from .config import ROOT
from .rbac import load_rbac


def _ctx(args):
    from .cli import _cfg, _session
    cfg = _cfg(args)
    return cfg, _session(args, cfg)


def _need(session, action):
    from .cli import _require
    _require(session, action)


# ------------------------------------------------------------------ users
def cmd_users(args):
    cfg, s = _ctx(args)
    rb = load_rbac()
    if args.action == "list":
        for uid, u in sorted(rb.users.items()):
            print(f"{uid:<40} {', '.join(u.get('roles') or ['(no role)']):<22} plants {','.join(map(str, u.get('plants') or []))}"
                  + (f"  groups {','.join(u['purchasing_groups'])}" if u.get("purchasing_groups") else ""))
        return
    if args.action == "show":
        try:
            t = rb.session_for(args.id, cfg)
        except PermissionError as e:
            print(f"Access denied: {e}")
            sys.exit(3)
        print(json.dumps({"user": t.user, "roles": t.roles, "plants": t.allowed_plants,
                          "purchasing_groups": t.purchasing_groups,
                          "tools": sorted(t.tools) if t.tools is not None else "all",
                          "actions": sorted(t.actions) if t.actions is not None else "all"}, indent=2))
        return
    _need(s, "manage_users")
    con = db.connect(cfg)
    if args.action == "add":
        rb.upsert_user(args.id, args.name or args.id, args.roles.split(","), args.plants_list.split(","),
                       args.groups.split(",") if args.groups else None)
        rb.save()
        _audit(con, s.user, "user_upserted", args.id, {"roles": args.roles, "plants": args.plants_list,
                                                         "groups": args.groups})
        print(f"Saved {args.id}. Commit config/rbac.yaml so the change is reviewed.")
    elif args.action == "remove":
        ok = rb.remove_user(args.id)
        rb.save()
        _audit(con, s.user, "user_removed", args.id, {"existed": ok})
        print(f"Removed {args.id}." if ok else f"{args.id} was not listed.")
    con.close()


def _audit(con, actor, event, subject, detail):
    from . import audit
    audit.log(con, actor, event, subject, detail)


# ------------------------------------------------------------------ drafts and follow-ups
def cmd_drafts(args):
    cfg, s = _ctx(args)
    con = db.connect(cfg, session=s)
    try:
        if args.action == "list":
            total, data = workflow.review_queue(con, s, args.status, args.limit)
            print(f"{total} draft(s) {args.status.replace('_', ' ').lower()} that you can act on")
            for d in data:
                print(f"  {d['outreach_id']}  vendor {d['lifnr'].lstrip('0'):<8} group {d['ekgrp']}  items {d['item_count']}")
            return
        if args.action == "show":
            d = db.rows(con, "SELECT o.*, r.status, r.edited_body FROM v_my_outreach o JOIN ops_draft_review r "
                             "USING (outreach_id) WHERE outreach_id = ?", [args.id])
            if not d:
                print("No such draft in your scope.")
                sys.exit(1)
            path = ROOT / "out" / "drafts" / f"{d[0]['draft_ref']}.txt"
            print(d[0]["edited_body"] or (path.read_text(encoding="utf-8") if path.exists() else "(body in the buyer's mailbox)"))
            print(f"\nStatus: {d[0]['status']}")
            return
        action = {"approve": "approve_draft", "edit": "edit_draft", "hold": "hold_draft", "sent": "mark_sent"}[args.action]
        body = Path(args.body_file).read_text(encoding="utf-8") if getattr(args, "body_file", None) else None
        try:
            r = workflow.review(con, s, args.id, action, body, args.note)
        except workflow.WorkflowError as e:
            print(f"Refused: {e}", file=sys.stderr)
            sys.exit(3)
        print(r.message)
    finally:
        con.close()


def cmd_followups(args):
    cfg, s = _ctx(args)
    _need(s, "run_reminders")
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now()
    con = db.connect(cfg)
    try:
        print(json.dumps(workflow.run_followups(con, now, actor=s.user), indent=2))
    finally:
        con.close()


# ------------------------------------------------------------------ data
def cmd_load(args):
    cfg, s = _ctx(args)
    _need(s, "manage_data")
    target = ROOT / "data" / f"{cfg.client_id}.duckdb"
    try:
        results = loader.load(Path(args.source), target, ROOT / "out" / "load")
    except loader.ContractError as e:
        print(f"Load stopped: {e}", file=sys.stderr)
        sys.exit(2)
    for r in results:
        if r.read:
            print(f"  {r.table:<18} read {r.read:>8,}  loaded {r.loaded:>8,}  rejected {r.rejected:>6,}"
                  + (f"  ({', '.join(f'{k}: {v}' for k, v in r.reasons.items())})" if r.reasons else ""))
    rejected = sum(r.rejected for r in results)
    print(f"Loaded into {target}." + (f" {rejected:,} rows rejected; see out/load/." if rejected else ""))


def cmd_export(args):
    cfg, s = _ctx(args)
    _need(s, "manage_data")
    counts = loader.export(ROOT / "data" / f"{cfg.client_id}.duckdb", Path(args.to))
    print(f"Wrote {len(counts)} files ({sum(counts.values()):,} rows) to {args.to}. "
          "This is the format a client's extract must follow (docs/DATA_CONTRACT.md).")


def cmd_dq(args):
    cfg, s = _ctx(args)
    con = db.connect(cfg)
    try:
        res = dq.run_checks(con, cfg.as_of.isoformat())
    finally:
        con.close()
    for r in res:
        print(f"  {r['status']:<4} {r['check']} {r['severity']}  {r['exceptions']:>7,} / {r['checked']:>8,}  "
              f"{r['description']}  [{r['owner']}]")
    blocking = dq.blocking_failures(res)
    print("\nNo blocking (P0) issues." if not blocking else f"\n{len(blocking)} blocking (P0) issue(s): answers are unreliable.")
    sys.exit(1 if blocking else 0)


def cmd_bigquery(args):
    cfg, s = _ctx(args)
    _need(s, "manage_data")
    con = db.connect(cfg)
    try:
        prev, cur = db.latest_capture_pair(con)
    finally:
        con.close()
    from . import bigquery
    res = bigquery.write_bundle(cfg, load_rbac(), Path(args.out), args.project, args.raw, args.curated,
                                args.service_account, prev, cur)
    print(f"Wrote {res['views']} views and {res['policies']} row access policies to {res['dir']}.")


# ------------------------------------------------------------------ oversight
def cmd_audit(args):
    cfg, s = _ctx(args)
    _need(s, "read_audit")
    con = db.connect(cfg)
    try:
        for e in db.rows(con, "SELECT event_at, actor, event_type, subject, detail FROM ops_audit_log "
                              "ORDER BY event_at DESC LIMIT ?", [args.limit]):
            print(f"{e['event_at']:%Y-%m-%d %H:%M:%S}  {e['actor']:<45} {e['event_type']:<22} {e['subject'] or ''}")
    finally:
        con.close()


def cmd_metrics(args):
    cfg, s = _ctx(args)
    _need(s, "read_audit")
    con = db.connect(cfg)
    try:
        print(json.dumps(telemetry.summary(con), indent=2, default=str))
    finally:
        con.close()


def cmd_retention(args):
    """Delete traces older than the retention period; the audit log is kept for its own (longer) period."""
    cfg, s = _ctx(args)
    _need(s, "manage_data")
    trace_days = int(cfg.get("retention", "trace_days", default=90))
    audit_days = int(cfg.get("retention", "audit_days", default=2555))
    con = db.connect(cfg)
    try:
        t = con.execute("SELECT count(*) FROM ops_trace WHERE created_at < now() - to_days(?)", [trace_days]).fetchone()[0]
        a = con.execute("SELECT count(*) FROM ops_audit_log WHERE event_at < now() - to_days(?)", [audit_days]).fetchone()[0]
        if not args.dry_run:
            con.execute("DELETE FROM ops_trace WHERE created_at < now() - to_days(?)", [trace_days])
            con.execute("DELETE FROM ops_audit_log WHERE event_at < now() - to_days(?)", [audit_days])
            _audit(con, s.user, "retention_applied", "ops", {"traces_deleted": t, "audit_deleted": a,
                                                              "trace_days": trace_days, "audit_days": audit_days})
    finally:
        con.close()
    verb = "Would delete" if args.dry_run else "Deleted"
    print(f"{verb} {t:,} traces older than {trace_days} days and {a:,} audit events older than {audit_days} days.")


def cmd_bench(args):
    """Latency of every tool the user may call, over N runs (no model involved)."""
    import statistics
    import time as _t

    from .tools import Toolbox, tool_definitions
    cfg, s = _ctx(args)
    con = db.connect(cfg, session=s)
    plant = s.allowed_plants[0]
    mat = con.execute("SELECT matnr FROM v_shortage LIMIT 1").fetchone()
    vendor = con.execute("SELECT min(vendor) FROM v_supplier_candidates WHERE vendor IS NOT NULL").fetchone()
    sample = {"get_shortage_summary": {}, "get_shortage_queue": {"plant": plant, "within_days": 7, "limit": 10},
              "get_shortage_detail": {"material": (mat or [""])[0], "plant": plant},
              "get_missing_asn": {"plant": plant, "limit": 10}, "get_safety_stock_dips": {"plant": plant, "limit": 10},
              "get_forecast_changes": {"plant": plant, "ranks": [1], "limit": 10}, "get_outreach_status": {"plant": plant},
              "preview_supplier_email": {"vendor": (vendor or [""])[0] or ""},
              "get_supplier_replies": {"vendor": "", "category": "", "needs_review_only": False},
              "get_drafts_for_review": {"status": "PENDING_REVIEW", "limit": 10}}
    tb = Toolbox(con, cfg, s, channel="bench")
    print(f"{'tool':<26}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}")
    try:
        for t in tool_definitions(cfg, s):
            times = []
            for _ in range(args.runs):
                t0 = _t.perf_counter()
                tb.run(t["name"], sample[t["name"]])
                times.append((_t.perf_counter() - t0) * 1000)
            times.sort()
            p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
            print(f"{t['name']:<26}{statistics.median(times):>9.0f}{p95:>9.0f}{times[-1]:>9.0f}")
    finally:
        con.close()


def cmd_web(args):
    import uvicorn
    print(f"Planning assistant on http://{args.host}:{args.port}  (auth: dev login; set SAPKIT_AUTH=iap behind IAP)")
    uvicorn.run("sapkit.web:create_app", factory=True, host=args.host, port=args.port, log_level="warning")


def cmd_version(args):
    print(__version__)


def register(sub):
    u = sub.add_parser("users", help="list, show, add or remove users (rbac.yaml)")
    u.add_argument("action", choices=["list", "show", "add", "remove"])
    u.add_argument("id", nargs="?")
    u.add_argument("--name")
    u.add_argument("--roles", default="")
    u.add_argument("--plants-list", default="")
    u.add_argument("--groups")
    u.set_defaults(func=cmd_users)

    d = sub.add_parser("drafts", help="review supplier drafts: list, show, approve, edit, hold, sent")
    d.add_argument("action", choices=["list", "show", "approve", "edit", "hold", "sent"])
    d.add_argument("id", nargs="?")
    d.add_argument("--status", default="PENDING_REVIEW", choices=list(workflow.STATUSES))
    d.add_argument("--limit", type=int, default=25)
    d.add_argument("--note")
    d.add_argument("--body-file", help="new e-mail body for 'edit'")
    d.set_defaults(func=cmd_drafts)

    f = sub.add_parser("followups", help="create due reminders and escalations (never sends)")
    f.add_argument("--now", help="ISO timestamp to evaluate the ladder at (default: now)")
    f.set_defaults(func=cmd_followups)

    ld = sub.add_parser("load", help="load a client's SAP extract (CSV per table) with validation")
    ld.add_argument("source")
    ld.set_defaults(func=cmd_load)

    ex = sub.add_parser("export", help="write the current SAP tables as CSV in the data-contract format")
    ex.add_argument("to")
    ex.set_defaults(func=cmd_export)

    sub.add_parser("dq", help="run the data-quality checks").set_defaults(func=cmd_dq)

    b = sub.add_parser("bigquery", help="generate BigQuery views and row access policies")
    b.add_argument("--project", required=True)
    b.add_argument("--raw", default="sapkit_raw")
    b.add_argument("--curated", default="sapkit_mm")
    b.add_argument("--service-account", required=True)
    b.add_argument("--out", default=str(ROOT / "out" / "bigquery"))
    b.set_defaults(func=cmd_bigquery)

    a = sub.add_parser("audit", help="show the audit log (auditor/admin)")
    a.add_argument("--limit", type=int, default=50)
    a.set_defaults(func=cmd_audit)

    sub.add_parser("metrics", help="questions, latency, blocks, tokens and cost per channel").set_defaults(func=cmd_metrics)

    w = sub.add_parser("web", help="run the web front end")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8080)
    w.set_defaults(func=cmd_web)

    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)

    rt = sub.add_parser("retention", help="delete traces and audit events past their retention period")
    rt.add_argument("--dry-run", action="store_true")
    rt.set_defaults(func=cmd_retention)

    bn = sub.add_parser("bench", help="tool latency p50/p95 for the acting user")
    bn.add_argument("--runs", type=int, default=20)
    bn.set_defaults(func=cmd_bench)
    return [u, d, f, ld, ex, b, a, rt, bn, sub.choices["dq"], sub.choices["metrics"]]
