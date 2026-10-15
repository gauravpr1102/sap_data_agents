# Architecture

Five SAP planning and procurement use cases, built as a governed agent on
Claude, for a dummy SAP client (Acme Precision Manufacturing). It is meant as
a reusable product: a new SAP client changes configuration, data mapping and
users, not code.

## The one rule everything follows

**The model handles language. SQL and Python establish the facts.**

Claude reads the question, picks a tool, explains the rows it gets back, and
classifies supplier replies. It never computes a balance, a percentage, a rank
or a recipient. Those come from SQL views and deterministic Python that are
tested against known answers. The guardrails check that every number in an
answer came from a tool.

## Layers

```
            ┌────────────────────────────────────────────────────────────────────┐
 Front ends │ Web app (FastAPI, IAP sign-in)  ·  CLI  ·  Claude Code/Desktop (MCP) │
            └───────────────┬────────────────────────────────────────────────────┘
                            │ identity (IAP header / --user / SAPKIT_USER)
            ┌───────────────▼────────────────────────────────────────────────────┐
 Access     │ rbac.py: roles → plants, purchasing groups, tools, actions         │
            │          deny by default; unknown user = nothing                    │
            └───────────────┬────────────────────────────────────────────────────┘
            ┌───────────────▼────────────────────────────────────────────────────┐
 Agent      │ guardrails.py  input screen · content-filter hook · budgets         │
            │ agent.py       Claude tool loop (API or Vertex AI) · offline agent  │
            │ guard / figure grounding · disclosure checks · telemetry trace     │
            └───────────────┬────────────────────────────────────────────────────┘
                            │ tool calls (strict schemas; only the role's tools)
            ┌───────────────▼────────────────────────────────────────────────────┐
 Tools      │ tools.py  10 governed tools · scope check · arg limits · audit      │
            │ mcp_stdio.py  the same tools over MCP (like an MCP Toolbox)        │
            └───────────────┬────────────────────────────────────────────────────┘
            ┌───────────────▼────────────────────────────────────────────────────┐
 Rules      │ sql/views.sql  rules filled from config + per-user row filters      │
            │ outreach.py · workflow.py · replies.py · dq.py                      │
            └───────────────┬────────────────────────────────────────────────────┘
            ┌───────────────▼────────────────────────────────────────────────────┐
 Data       │ raw SAP tables (loader + data contract) · ops tables (audit, trace, │
            │ drafts, reviews, replies, follow-ups, DQ results)                   │
            │ DuckDB locally / pilot  →  BigQuery views + row access policies     │
            └────────────────────────────────────────────────────────────────────┘
 Mail       mail.py interface: mock mailbox, Gmail, Outlook (drafts only, no send)
```

## The five use cases

| # | Use case | Path |
|---|---|---|
| 1 | Shortage intelligence | question → agent → `get_shortage_queue` / `get_shortage_detail` / `get_missing_asn` / `get_safety_stock_dips` → scoped rule views → answer with as-of date |
| 2 | Forecast-change prioritisation | `v_forecast_change` compares two snapshots, splits inside/outside lead time, ranks 1–7 |
| 3 | Supplier resolution & eligibility | `v_supplier_candidates` + `outreach.decide()` → one outcome and reason per item |
| 4 | Buyer-approved drafting | grouped per vendor and buyer → template (no prices) → mail adapter draft → review workflow (pending → approved / on hold → sent) |
| 5 | Reply intelligence | tracked threads only → new messages after watermark → Claude structured-output classification (no tools) → validation → idempotent persist → follow-up ladder |

## Three ways in, one set of tools

| Front end | Model | Guardrails | Who it's for |
|---|---|---|---|
| Web app / API agent | Claude via API or Vertex AI | All layers | Business users |
| Offline agent (no key) | Rules route to tools | All layers | Demos, CI, environments without a model |
| Claude Code / Desktop over MCP | Claude on the user's login | RBAC, scope, arguments, audit (server-side) | Analysts, pilots |

## Where client differences live

| Moving part | Where |
|---|---|
| Plants, scope, cover rule, thresholds, exclusions | `config/<client>.yaml` |
| Users and roles | `config/rbac.yaml` |
| SAP extract | Data contract → `sapkit load` |
| Model endpoint | `llm.provider` (anthropic / vertex) |
| Mailbox | Gmail or Outlook adapter + per-buyer consent |
| Cloud | `deploy/terraform` variables |

## Mapping to the reference build

The kit keeps the shape of the production reference build; only the platform
under each layer is lighter for the pilot.

| Reference build (production) | This kit | Same principle |
|---|---|---|
| SAP → BigQuery raw dataset (read-only) | Raw SAP tables; BigQuery raw dataset in Terraform | SAP is the system of record; nothing writes back |
| Curated views | `sql/views.sql`, also generated as BigQuery views | Business rules live once, in reviewed SQL |
| Ops dataset (audit, comm log) | `ops_*` tables; BigQuery ops dataset in Terraform | Append-only audit; the only writes |
| MCP Toolbox on Cloud Run | `tools.py` + `sapkit mcp` | Agents only reach data through named, parameterised tools |
| Agent Engine (ADK + Gemini) | Claude tool loop; Claude on Vertex AI | The model handles language; tools establish facts |
| Gemini Enterprise UI | Web app behind IAP; Claude Code/Desktop | Users ask in plain language |
| Eval gate (≥30 scenarios, 95%) | 41 cases, 95% + all security/RBAC; Promptfoo red team; 125+ tests | Nothing ships without passing known answers |
| Buyer approves every send | Review workflow; adapters can't send | No autonomous sending |
| IAM, Secret Manager, Model Armor | IAP + RBAC + row access policies; Secret Manager; content-filter hook | Defence in depth |

See SECURITY.md, EVALS.md, DATA_CONTRACT.md, DEPLOYMENT.md, OPERATIONS.md and
DECISIONS.md for each area.
