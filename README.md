# SAP planning agents on Claude: dummy client kit

Five SAP planning and procurement use cases, rebuilt on Claude for a dummy SAP
client, **Acme Precision Manufacturing** (three plants, 15,000 material-plants
of synthetic SAP data by default).

| # | Use case | What it does |
|---|---|---|
| 1 | Shortage intelligence | Projects each material's balance over 105 days, ranks shortages by risk, flags late shipping notices and safety-stock dips |
| 2 | Forecast-change prioritisation | Compares two forecast snapshots and ranks every change 1–7 by lead-time window and size |
| 3 | Supplier resolution & eligibility | Decides who (if anyone) should be contacted for each change, with one reason per item |
| 4 | Buyer-approved drafting | One combined draft per supplier and buyer, never sent automatically, never with prices |
| 5 | Supplier reply intelligence | Reads tracked threads, classifies replies, extracts commitments, routes anything unclear to a buyer |

Claude (`claude-opus-5-5`) answers planners' questions through nine governed
tools and classifies supplier replies. Every number comes from SQL; Claude never
calculates one. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/DECISIONS.md](docs/DECISIONS.md).

## Quick start

One command sets everything up and opens the planning assistant. No API key is needed.

**Windows (PowerShell):**
```
powershell -ExecutionPolicy Bypass -File setup_and_run.ps1
```

**macOS / Linux:**
```
bash setup_and_run.sh
```

It creates a virtual environment, installs the kit, builds the dummy SAP data,
checks every rule, runs the end-to-end demo and writes a report, then opens the
assistant. Ask it things like:

- What's short at plant 1000 this week?
- Why is material 10000004 short?
- Which purchase orders at plant 3000 should have shipped by now?
- Which forecast changes at plant 2000 are the biggest?
- How many supplier e-mails would go out for plant 1000?
- Which supplier replies need my review?

**Without a key, the assistant runs in offline mode.** It uses the same nine
tools, the same data, the same plant-access rules and the same audit log, but
picks the tool with simple rules instead of Claude. It answers all 20
evaluation questions correctly, but it only understands planning questions phrased
along those lines. With `ANTHROPIC_API_KEY` set, `ask` uses Claude instead.

**To use real Claude without an API key:** if you have Claude Code, run `claude`
in this folder after setup and approve the `sapkit` tools when asked. Claude then
answers from the same tools using your own Claude login. (Claude Desktop works
the same way: add the server from `.mcp.json` to its config.)

Step by step, if you prefer:

```bash
pip install -e ".[dev]"
python -m sapkit generate          # 15,000 material-plants of SAP-shaped data (~10 s)
python -m sapkit verify            # every rule vs. the answers built into the data
python -m sapkit demo --offline    # outreach -> replies -> rerun -> report
python -m sapkit ask               # the assistant (offline without a key)
python -m sapkit eval --agent      # 20 planner questions through the offline agent
python -m pytest                   # 69 tests
```

With an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=...       # PowerShell: $env:ANTHROPIC_API_KEY = "..."
python -m sapkit ask "What's short at plant 1000 this week, and why?" --show-tools
python -m sapkit replies           # classify replies with Claude instead of keywords
python -m sapkit eval --live       # the 20 questions through the real Claude agent
```

## Commands

| Command | Needs a key | What it does |
|---|---|---|
| `generate [--materials N] [--seed S]` | No | Build the dummy SAP database (`data/acme.duckdb`) and supplier mailbox |
| `verify` | No | Check every rule against the answers the generator built in |
| `query <tool> '<json args>' [--plants]` | No | Run one governed tool directly, exactly as the agent would |
| `outreach [--plants] [--dry-run]` | No | Decide eligibility and write drafts to `out/drafts/` |
| `replies [--offline]` | Optional | Process supplier replies; Claude if a key is set, keywords otherwise |
| `report` | No | Write `out/report/summary.md` and CSV exports |
| `demo [--offline] [--regenerate]` | Optional | All of the above, end to end, including a rerun that must create nothing new |
| `ask ["question"] [--plants] [--offline] [--show-tools]` | Optional | The planning assistant: Claude with a key, offline rules without |
| `eval [--agent] [--live]` | `--live` only | 20 evaluation questions: tool layer, offline agent (`--agent`) or Claude (`--live`) |
| `mcp [--plants]` | No | Serve the nine tools over MCP for Claude Code / Claude Desktop |
| `reset` | No | Clear drafts, mailbox state and ops tables |

### Example tool queries

```bash
python -m sapkit query get_shortage_summary
python -m sapkit query get_shortage_queue '{"plant": "1000", "within_days": 7, "limit": 10}'
python -m sapkit query get_shortage_detail '{"material": "10000004", "plant": "1000"}'
python -m sapkit query get_missing_asn '{"plant": "2000", "limit": 10}'
python -m sapkit query get_forecast_changes '{"plant": "3000", "ranks": [1, 3], "limit": 10}'
python -m sapkit query get_outreach_status '{"plant": "2000"}'
python -m sapkit query get_supplier_replies '{"vendor": "", "category": "", "needs_review_only": true}'
python -m sapkit query get_shortage_queue '{"plant": "2000", "within_days": 7, "limit": 5}' --plants 1000   # refused
```

## Layout

```
config/acme.yaml      everything client-specific (scope, cover rule, thresholds, exclusions)
domain/*.md           the planning knowledge Claude gets in its system prompt
sql/views.sql         all business rules, filled in from the config
src/sapkit/
  synth.py            dummy SAP data with known answers
  db.py               DuckDB connection, rule rendering, ops tables
  tools.py            the nine governed tools + strict schemas
  agent.py            Claude tool loop with grounding guard
  offline_agent.py    rule-based agent over the same tools, for use without a key
  mcp_server.py       the same tools over MCP (like the reference build's MCP Toolbox)
  guard.py            blocks tables no tool produced
  outreach.py         eligibility + drafting
  replies.py          reply classification (Claude or offline) + safe persistence
  mail.py             mail adapter interface + mock mailbox
  evals_runner.py     20 evaluation questions
  cli.py              commands above
tests/                pytest suite
docs/                 architecture, decisions, test results
```

## Onboarding a real SAP client

1. Copy `config/acme.yaml` and set the client's plants, scope, cover rule, thresholds and exclusions.
2. Load the client's extract into the same table shapes (`synth.SCHEMA` lists them; they are standard SAP tables and fields).
3. Run `verify`-style checks against a handful of materials the client's planners can confirm by hand.
4. Replace `MockMailbox` with a Gmail or Outlook adapter implementing `MailAdapter`.
