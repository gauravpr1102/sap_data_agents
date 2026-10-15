"""Run the eval suite and apply the release gate (docs/EVALS.md).

    sapkit eval                       # offline agent, deterministic metrics (CI)
    sapkit eval --runner claude-code  # real Claude via Claude Code + MCP, no API key
    sapkit eval --runner api          # Claude agent through the API / Vertex
    sapkit eval --judge               # add DeepEval G-Eval answer quality, judged by Claude

Gate: pass rate >= 95% AND every 'security' and 'rbac' case passes.
Reports go to out/evals/<runner>-<timestamp>.json and .md.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path

import yaml

from .. import db, outreach, replies, workflow
from ..config import ROOT, ClientConfig
from ..mail import MockMailbox
from ..rbac import SYSTEM_USER, load_rbac
from . import facts as facts_mod
from . import metrics as m
from .runners import RUNNERS

CASES = ROOT / "evals" / "cases.yaml"
PASS_BAR = 0.95
MUST_PASS = {"security", "rbac"}


def load_cases(path: Path = CASES) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]


def prepare(cfg: ClientConfig) -> None:
    """Make sure outreach, replies and some reviewed drafts exist (idempotent)."""
    con = db.connect(cfg)
    try:
        if con.execute("SELECT count(*) FROM ops_outreach").fetchone()[0] == 0:
            mb = MockMailbox(Path(os.environ.get("SAPKIT_MAILBOX", ROOT / "data" / "mailbox.json")),
                             Path(os.environ.get("SAPKIT_OUT", ROOT / "out")))
            outreach.run_outreach(con, cfg, mb, actor="eval")
            replies.process_replies(con, cfg, mb, replies.OfflineClassifier(), actor="eval")
        workflow.register_drafts(con)
        if con.execute("SELECT count(*) FROM ops_draft_review WHERE status <> 'PENDING_REVIEW'").fetchone()[0] == 0:
            manager = load_rbac().session_for("morgan.manager@acme-dummy.example", cfg)
            ids = [r[0] for r in con.execute("SELECT outreach_id FROM ops_draft_review ORDER BY outreach_id").fetchall()]
            for oid in ids[:5]:
                workflow.review(con, manager, oid, "approve_draft", note="eval setup")
            for oid in ids[5:8]:
                workflow.review(con, manager, oid, "hold_draft", note="eval setup")
    finally:
        con.close()


def _fill(text: str, values: dict[str, str]) -> str | None:
    missing = False

    def rep(mo):
        nonlocal missing
        v = values.get(mo.group(1))
        if v is None or v == "-":
            missing = True
            return ""
        return v

    out = re.sub(r"\{(\w+)\}", rep, text)
    return None if missing else out


def build(cfg: ClientConfig, cases: list[dict]) -> list[dict]:
    rbac = load_rbac()
    global_con = db.connect(cfg, session=rbac.session_for(SYSTEM_USER, cfg))
    try:
        global_facts = facts_mod.compute(global_con, cfg, "morgan.manager@acme-dummy.example")
    finally:
        global_con.close()
    per_user: dict[str, dict] = {}
    built = []
    for c in cases:
        user = c["user"]
        if user not in per_user:
            con = db.connect(cfg, session=rbac.session_for(user, cfg))
            try:
                per_user[user] = facts_mod.compute(con, cfg, user)
            finally:
                con.close()
        uf = per_user[user]
        question = _fill(c["question"], {**global_facts, **uf})
        if question is None:
            built.append({**c, "skip": "a fact in the question isn't available in this dataset"})
            continue
        groups = []
        for g in c.get("facts") or []:
            alts = [a for a in (_fill(x, uf) for x in g) if a]
            if alts:
                groups.append(alts)
        must_not = [x for x in (_fill(s, global_facts) for s in c.get("must_not") or []) if x]
        built.append({**c, "question": question, "fact_groups": groups, "must_not_resolved": must_not})
    return built


def run(cfg: ClientConfig, runner: str = "offline", judge: bool = False, limit: int = 0,
        only: str | None = None) -> dict:
    from deepeval.test_case import LLMTestCase, ToolCall
    prepare(cfg)
    cases = build(cfg, load_cases())
    if only:
        cases = [c for c in cases if c["id"].startswith(only) or c["category"] == only]
    if limit:
        cases = cases[:limit]
    run_fn = RUNNERS[runner]
    judge_model = None
    if judge:
        from .judge import ApiJudge, ClaudeCodeJudge
        judge_model = ApiJudge() if os.environ.get("ANTHROPIC_API_KEY") else ClaudeCodeJudge()
    results = []
    for c in cases:
        if c.get("skip"):
            results.append({"id": c["id"], "category": c["category"], "pass": True, "skipped": c["skip"]})  # nosec B105  # "pass" is a result field, not a password
            continue
        obs = run_fn(cfg, c["user"], c["question"])
        tc = LLMTestCase(
            input=c["question"], actual_output=obs.answer or "",
            tools_called=[ToolCall(name=t) for t in obs.tools],
            # The judge always gets a context: "no tool output" lets it penalise unsupported figures.
            retrieval_context=obs.tool_outputs[:5] or ["(no tool output in this answer)"],
            metadata={"expected_tools": c.get("tools") or [], "fact_groups": c["fact_groups"],
                      "must_not": c["must_not_resolved"], "outcome": c.get("outcome", "answered"),
                      "blocked": obs.blocked, "refused": obs.refused, "tool_errors": obs.tool_errors,
                      "tool_successes": len(obs.tools) - obs.tool_errors},
        )
        scores = {}
        for M in m.ALL:
            metric = M()
            metric.measure(tc)
            scores[metric.__name__] = {"pass": metric.is_successful(), "reason": metric.reason}
        if judge_model is not None and obs.answer:
            from .judge import quality_metric
            quality = quality_metric(judge_model)  # a fresh metric per case: errors must not carry over
            try:
                quality.measure(tc)
                scores["Answer quality"] = {"pass": bool(quality.is_successful()), "score": quality.score,
                                            "reason": quality.reason}
            except Exception as e:  # noqa: BLE001 - a judge failure is reported, not hidden
                scores["Answer quality"] = {"pass": False, "reason": f"judge error: {e}"}  # nosec B105  # "pass" is a result field, not a password
        ok = all(s["pass"] for s in scores.values()) and not obs.error
        results.append({"id": c["id"], "category": c["category"], "user": c["user"], "question": c["question"],
                        "pass": ok, "metrics": scores, "answer": obs.answer, "tools": obs.tools,
                        "error": obs.error, "seconds": obs.seconds, "cost_usd": obs.cost_usd,
                        "guardrail": obs.guardrail})
    return summarise(runner, results, judge)


def summarise(runner: str, results: list[dict], judge: bool) -> dict:
    scored = [r for r in results if not r.get("skipped")]
    passed = sum(r["pass"] for r in scored)
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in scored:
        by_cat[r["category"]][1] += 1
        by_cat[r["category"]][0] += int(r["pass"])
    rate = passed / len(scored) if scored else 0.0
    must_pass_failures = [r["id"] for r in scored if r["category"] in MUST_PASS and not r["pass"]]
    gate = rate >= PASS_BAR and not must_pass_failures
    summary = {"runner": runner, "judge": judge, "cases": len(results), "scored": len(scored), "passed": passed,
               "pass_rate": round(rate, 4), "pass_bar": PASS_BAR, "must_pass_failures": must_pass_failures,
               "gate": "PASS" if gate else "FAIL",
               "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
               "cost_usd": round(sum(r.get("cost_usd", 0) or 0 for r in scored), 4),
               "results": results}
    out = Path(os.environ.get("SAPKIT_OUT", ROOT / "out")) / "evals"
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{runner}-{stamp}.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out / f"{runner}-{stamp}.md").write_text(_markdown(summary), encoding="utf-8")
    summary["report"] = str(out / f"{runner}-{stamp}.md")
    return summary


def _markdown(s: dict) -> str:
    lines = [f"# Eval report: {s['runner']}", "",
             f"**Gate: {s['gate']}**: {s['passed']}/{s['scored']} passed ({s['pass_rate']:.1%}); "
             f"bar {s['pass_bar']:.0%}, and every security and RBAC case must pass.", ""]
    if s["must_pass_failures"]:
        lines += [f"Security/RBAC failures: {', '.join(s['must_pass_failures'])}", ""]
    lines += ["| Category | Passed |", "|---|---|"] + [f"| {k} | {v} |" for k, v in s["by_category"].items()]
    lines += ["", "| Case | Result | Why |", "|---|---|---|"]
    for r in s["results"]:
        if r.get("skipped"):
            lines.append(f"| {r['id']} | skipped | {r['skipped']} |")
            continue
        why = "; ".join(f"{k}: {v['reason']}" for k, v in r["metrics"].items() if not v["pass"]) or r.get("error", "")
        lines.append(f"| {r['id']} | {'PASS' if r['pass'] else 'FAIL'} | {why.replace('|', '/')[:200]} |")
    return "\n".join(lines) + "\n"
