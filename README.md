# SAP planning agents on Claude

A governed AI assistant for SAP planners, buyers and purchasing managers, built
on Claude and shown on a dummy SAP client, **Acme Precision Manufacturing**
(3 plants, 15,000 material-plants of SAP-shaped data).

| # | Use case | What a user gets |
|---|---|---|
| 1 | Shortage intelligence | Ranked shortages, why each part is short, late shipping notices, safety-stock dips |
| 2 | Forecast-change prioritisation | Every forecast change ranked by lead time and size |
| 3 | Supplier resolution & eligibility | Who to contact for each change, with one plain reason when it's no |
| 4 | Buyer-approved drafting | One draft per supplier in the buyer's mailbox; approve / edit / hold; never auto-sent, never with prices |
| 5 | Supplier reply intelligence | Replies classified, commitments extracted, unclear or risky ones sent to a buyer, follow-ups proposed |

**What makes it production-grade:**
- RBAC enforced at five layers;
- guardrails in code: input screening, number grounding, no-pricing and personal-data checks, a content-filter hook;
- human approval for every supplier e-mail;
- a full audit log and per-question traces with cost;
- a 41-case eval gate on DeepEval, plus a Promptfoo red team;
- a data contract with data-quality checks;
- Terraform for Google Cloud (Cloud Run + IAP + BigQuery + Claude on Vertex AI).

## Start here

**One command** (Windows PowerShell, from the unzipped folder):

```
powershell -ExecutionPolicy Bypass -File setup_and_run.ps1
```

**macOS / Linux:** `bash setup_and_run.sh`

It installs everything, builds the dummy data, checks every business rule, runs
the end-to-end demo and opens the assistant. No API key is needed.

## Three ways to use it

| How | Command | Needs |
|---|---|---|
| **Web app** | `python -m sapkit web`, then open http://127.0.0.1:8080 and sign in as a demo user | Nothing |
| **Real Claude in Claude Code** | Run `claude` in this folder and approve the `sapkit` tools | Claude Code |
| **Terminal assistant** | `python -m sapkit ask` | Nothing (offline mode); Claude with `ANTHROPIC_API_KEY` or Vertex AI |

Demo users (from `config/rbac.yaml`):

| User | Role | Sees |
|---|---|---|
| `morgan.manager@acme-dummy.example` | Purchasing manager (default) | All plants, all groups; runs jobs |
| `avery.buyer@acme-dummy.example` | Buyer | Plant 1000, purchasing groups P01-P02 |
| `jordan.planner@acme-dummy.example` | Planner | Plant 1000, shortages only |
| `riley.planner@acme-dummy.example` | Planner | Plants 2000 and 3000 |
| `casey.auditor@acme-dummy.example` | Auditor | Audit log and metrics |
| `admin@acme-dummy.example` | Admin | Everything, including user and data admin |

Act as any of them with `--user`, e.g. `python -m sapkit ask --user avery.buyer@acme-dummy.example`.
In Claude Code the tools act as the user named in `.mcp.json` (set `SAPKIT_USER` before running setup to change it).

## Commands

| Area | Command |
|---|---|
| Data | `generate`, `load <dir>`, `export <dir>`, `dq`, `verify`, `bigquery` |
| Use | `ask`, `query <tool> '<json>'`, `web`, `mcp` |
| Jobs | `outreach`, `replies`, `followups`, `demo`, `report` |
| Review | `drafts list / show / approve / edit / hold / sent` |
| Oversight | `audit`, `metrics`, `users list / show / add / remove` |
| Quality | `eval [--runner offline\|claude-code\|api] [--judge]`, `pytest`, Promptfoo in `evals/redteam` |

Every command acts as a user and checks that user's permissions.

## Testing

```bash
python -m pytest                                   # 125+ tests
python -m sapkit verify                            # rules vs. answers built into the data
python -m sapkit eval                              # 41-case gate, offline agent
python -m sapkit eval --runner claude-code         # same cases, real Claude, no API key
cd evals/redteam && npx promptfoo eval             # 20-attack red team
```

## Documentation

| Doc | Covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, use-case paths, mapping to the reference build |
| [docs/SECURITY.md](docs/SECURITY.md) | RBAC, guardrails, threat model, test results, residual risks |
| [docs/EVALS.md](docs/EVALS.md) | Eval cases, DeepEval metrics, runners, gate, red team, tracing |
| [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md) | Extract format, validation, data-quality checks, onboarding a client |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Google Cloud deployment, pilot vs production, configuration |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Monthly cycle, monitoring, incidents, users, releases, cost |
| [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) | Gmail, Outlook, SAP, Claude channels |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every design decision, with the options considered |
| [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) | Test rounds and results |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each version |

## Layout

```
config/          acme.yaml (client settings), rbac.yaml (users and roles)
domain/          planning knowledge given to the model
sql/views.sql    all business rules
src/sapkit/      agent, tools, guardrails, rbac, workflow, loader, dq, web, mcp, evals, ...
evals/           cases.yaml (41 cases), redteam/ (Promptfoo)
deploy/          terraform/ for Google Cloud
tests/           pytest suite
docs/            documentation
```
