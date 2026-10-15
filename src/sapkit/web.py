"""Web front end (docs/DEPLOYMENT.md): chat, draft review, audit and metrics.

Identity:
- SAPKIT_AUTH=iap (production): the user is the identity Google IAP asserts in
  ``X-Goog-Authenticated-User-Email``. Requests without it are refused. Cloud
  Run is only reachable through IAP, so the header can't be forged from outside.
- SAPKIT_AUTH=dev (default, local only): a login page lists the users in
  config/rbac.yaml. Never deploy with dev auth.

Every endpoint resolves the user through RBAC first; the agent, tools and
workflow then enforce the same scope as the CLI and MCP server.

    python -m sapkit web            # http://127.0.0.1:8080
"""
from __future__ import annotations

import html
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import __version__, db, llm, telemetry, workflow
from .config import load_config
from .rbac import AccessDenied, Session, load_rbac

COOKIE = "sapkit_user"


class AskIn(BaseModel):
    question: str


class ReviewIn(BaseModel):
    body: str | None = None
    note: str | None = None


def _auth_mode() -> str:
    return os.environ.get("SAPKIT_AUTH", "dev")


def create_app(config_path: str | None = None) -> FastAPI:
    cfg = load_config(config_path)
    app = FastAPI(title="SAP planning assistant", version=__version__)

    def current_session(request: Request) -> Session:
        if _auth_mode() == "iap":
            raw = request.headers.get("x-goog-authenticated-user-email", "")
            user = raw.split(":", 1)[-1].strip()
            if not user:
                raise HTTPException(401, "No identity from the identity-aware proxy.")
        else:
            user = request.cookies.get(COOKIE, "")
            if not user:
                raise HTTPException(401, "Not signed in.")
        try:
            return load_rbac().session_for(user, cfg)
        except AccessDenied as e:
            raise HTTPException(403, str(e)) from None

    # ------------------------------------------------------------ pages
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__, "auth": _auth_mode()}

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        if _auth_mode() != "dev":
            raise HTTPException(404)
        users = load_rbac().users
        options = "".join(f'<option value="{html.escape(u)}">{html.escape(v.get("name", u))} '
                          f'({html.escape(", ".join(v.get("roles") or []) or "no role")})</option>'
                          for u, v in sorted(users.items()))
        return _page("Sign in", f"""
          <form method="post" action="/login" class="card">
            <h2>Sign in (local demo)</h2>
            <p class="muted">In production, sign-in comes from your company identity provider.</p>
            <select name="user">{options}</select>
            <button type="submit">Continue</button>
          </form>""")

    @app.post("/login")
    def login(user: str = Form(...)):
        if _auth_mode() != "dev":
            raise HTTPException(404)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, user, httponly=True, samesite="strict")
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login" if _auth_mode() == "dev" else "/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        try:
            s = current_session(request)
        except HTTPException as e:
            if e.status_code == 401 and _auth_mode() == "dev":
                return RedirectResponse("/login", status_code=303)
            return _page("No access", f'<div class="card"><h2>No access</h2><p>{html.escape(e.detail)}</p>'
                                      f'<a href="/logout">Sign out</a></div>')
        return _page("Planning assistant", _home_html(cfg, s))

    # ------------------------------------------------------------ API
    @app.get("/api/me")
    def me(s: Session = Depends(current_session)):
        return {"user": s.user, "name": s.display_name, "roles": list(s.roles), "plants": s.allowed_plants,
                "purchasing_groups": list(s.purchasing_groups) if s.purchasing_groups is not None else "all",
                "tools": sorted(s.tools) if s.tools is not None else "all",
                "actions": sorted(s.actions) if s.actions is not None else "all"}

    @app.post("/api/ask")
    def ask(body: AskIn, s: Session = Depends(current_session)):
        con = db.connect(cfg, session=s)
        try:
            st = llm.settings(cfg)
            if llm.has_credentials(st) and os.environ.get("SAPKIT_FORCE_OFFLINE") != "1":
                from .agent import PlanningAgent
                agent = PlanningAgent(con, cfg, s)
                mode = "claude"
            else:
                from .offline_agent import OfflineAgent
                agent = OfflineAgent(con, cfg, s)
                mode = "offline"
            res = agent.ask(body.question)
        finally:
            con.close()
        return {"answer": res.answer, "mode": mode, "tools": res.tool_calls, "blocked": res.blocked,
                "guardrail": res.guardrail, "trace_id": res.trace_id}

    @app.get("/api/drafts")
    def drafts(status: str = "PENDING_REVIEW", s: Session = Depends(current_session)):
        if status not in workflow.STATUSES:
            raise HTTPException(400, f"status must be one of {', '.join(workflow.STATUSES)}")
        if not s.can_use_tool("get_drafts_for_review"):
            raise HTTPException(403, "Your role can't see supplier drafts.")
        con = db.connect(cfg, session=s)
        try:
            total, data = workflow.review_queue(con, s, status, 100)
        finally:
            con.close()
        return {"status": status, "total": total, "drafts": data}

    @app.post("/api/drafts/{outreach_id}/{action}")
    def act(outreach_id: str, action: str, body: ReviewIn, s: Session = Depends(current_session)):
        con = db.connect(cfg, session=s)
        try:
            r = workflow.review(con, s, outreach_id, action, body.body, body.note)
        except workflow.WorkflowError as e:
            raise HTTPException(403 if "not one of yours" in str(e) or "does not allow" in str(e)
                                or "access" in str(e) else 409, str(e)) from None
        finally:
            con.close()
        return {"outreach_id": r.outreach_id, "status": r.status, "message": r.message}

    @app.get("/api/audit")
    def audit_log(limit: int = 100, s: Session = Depends(current_session)):
        if not s.can("read_audit"):
            raise HTTPException(403, "Only auditors and admins can read the audit log.")
        con = db.connect(cfg, session=s)
        try:
            data = db.rows(con, "SELECT event_at, actor, event_type, subject, detail FROM ops_audit_log "
                                "ORDER BY event_at DESC LIMIT ?", [max(1, min(limit, 1000))])
        finally:
            con.close()
        return {"events": data}

    @app.get("/api/metrics")
    def metrics(s: Session = Depends(current_session)):
        if not s.can("read_audit"):
            raise HTTPException(403, "Only auditors and admins can read metrics.")
        con = db.connect(cfg, session=s)
        try:
            return {"by_channel": telemetry.summary(con)}
        finally:
            con.close()

    @app.exception_handler(AccessDenied)
    def _denied(request: Request, exc: AccessDenied):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    return app


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root {{ --bg:#f6f8fc; --card:#fff; --ink:#1b2333; --muted:#55607a; --blue:#3561e8; --line:#e3e8f2; --amber:#b86e00; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font:15px/1.5 system-ui, -apple-system, Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--ink); }}
header {{ background:#0e1526; color:#fff; padding:12px 20px; display:flex; justify-content:space-between; align-items:center; }}
header a {{ color:#cadcfc; }} main {{ max-width:1100px; margin:20px auto; padding:0 16px; display:grid; gap:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }}
.muted {{ color:var(--muted); }} .pill {{ display:inline-block; background:#eef2fa; border-radius:999px; padding:2px 10px; margin-right:6px; font-size:13px; }}
textarea, select, input {{ width:100%; padding:10px; border:1px solid var(--line); border-radius:8px; font:inherit; }}
button {{ background:var(--blue); color:#fff; border:0; border-radius:8px; padding:9px 16px; font:inherit; cursor:pointer; margin-top:8px; }}
button.secondary {{ background:#eef2fa; color:var(--ink); }} table {{ border-collapse:collapse; width:100%; margin:8px 0; font-size:14px; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; }} th {{ background:#eef2fa; }}
#answer {{ white-space:normal; }} .blocked {{ color:var(--amber); }} .grid {{ display:grid; grid-template-columns:2fr 1fr; gap:16px; }}
@media (max-width:800px) {{ .grid {{ grid-template-columns:1fr; }} }}
</style></head><body>{body}</body></html>""")


def _home_html(cfg, s: Session) -> str:
    roles = "".join(f'<span class="pill">{html.escape(r)}</span>' for r in s.roles)
    plants = ", ".join(s.allowed_plants)
    can_drafts = s.can_use_tool("get_drafts_for_review")
    can_audit = s.can("read_audit")
    drafts_card = """
      <div class="card"><h3>Drafts awaiting review</h3><div id="drafts" class="muted">Loading…</div></div>""" if can_drafts else ""
    audit_link = '<p><a href="/api/audit">Audit log</a> · <a href="/api/metrics">Metrics</a></p>' if can_audit else ""
    return f"""
<header><strong>{html.escape(cfg.client_name)} · Planning assistant</strong>
  <span>{html.escape(s.display_name)} · <a href="/logout">Sign out</a></span></header>
<main>
  <div class="card">{roles}<span class="muted">Plants {html.escape(plants)} · Data as of {cfg.as_of:%d %b %Y}</span>{audit_link}</div>
  <div class="grid">
    <div class="card">
      <h3>Ask</h3>
      <textarea id="q" rows="2" placeholder="e.g. What's short at plant {html.escape(s.allowed_plants[0])} this week?"></textarea>
      <button onclick="ask()">Ask</button>
      <div id="answer" class="muted" style="margin-top:12px">Answers use only the data you're allowed to see.</div>
    </div>
    {drafts_card}
  </div>
</main>
<script>
function esc(t) {{ return t.replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}})[c]); }}
function md(text) {{
  const lines = esc(text).split("\\n"); let out = "", rows = [];
  const flush = () => {{ if (!rows.length) return; out += "<table>" + rows.map((r, i) => {{
      const cells = r.split("|").slice(1, -1).map(c => c.trim()); if (i === 1) return "";
      const tag = i === 0 ? "th" : "td"; return "<tr>" + cells.map(c => `<${{tag}}>${{c}}</${{tag}}>`).join("") + "</tr>"; }}).join("") + "</table>"; rows = []; }};
  for (const l of lines) {{ if (l.trim().startsWith("|")) rows.push(l.trim()); else {{ flush(); out += l ? `<p>${{l.replace(/\\*\\*(.+?)\\*\\*/g, "<b>$1</b>")}}</p>` : ""; }} }}
  flush(); return out;
}}
async function ask() {{
  const q = document.getElementById("q").value.trim(); if (!q) return;
  const a = document.getElementById("answer"); a.innerHTML = "Thinking…";
  const r = await fetch("/api/ask", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify({{question:q}})}});
  const d = await r.json();
  a.innerHTML = (d.blocked ? '<p class="blocked">Held back by a guardrail (' + esc(d.guardrail || "") + ')</p>' : "") + md(d.answer || d.detail || "")
    + `<p class="muted">Mode: ${{d.mode}} · tools: ${{(d.tools||[]).map(t => t.tool).join(", ") || "none"}} · trace ${{d.trace_id || ""}}</p>`;
}}
async function loadDrafts() {{
  const el = document.getElementById("drafts"); if (!el) return;
  const r = await fetch("/api/drafts"); const d = await r.json();
  if (!d.drafts) {{ el.textContent = d.detail || "Not available."; return; }}
  if (!d.drafts.length) {{ el.textContent = "Nothing waiting."; return; }}
  el.innerHTML = `<p>${{d.total}} waiting</p><table><tr><th>Vendor</th><th>Group</th><th>Items</th><th></th></tr>` +
    d.drafts.slice(0, 15).map(x => `<tr><td>${{x.lifnr.replace(/^0+/, "")}}</td><td>${{x.ekgrp}}</td><td>${{x.item_count}}</td>
      <td><button onclick="act('${{x.outreach_id}}','approve_draft')">Approve</button>
          <button class="secondary" onclick="act('${{x.outreach_id}}','hold_draft')">Hold</button></td></tr>`).join("") + "</table>";
}}
async function act(id, action) {{
  const r = await fetch(`/api/drafts/${{id}}/${{action}}`, {{method:"POST", headers:{{"Content-Type":"application/json"}}, body:"{{}}"}});
  const d = await r.json(); if (!r.ok) alert(d.detail); loadDrafts();
}}
document.getElementById("q").addEventListener("keydown", e => {{ if (e.key === "Enter" && !e.shiftKey) {{ e.preventDefault(); ask(); }} }});
loadDrafts();
</script>"""
