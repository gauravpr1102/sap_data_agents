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

```bash
pip install -e ".[dev]"

python -m sapkit generate          # 15,000 material-plants of SAP-shaped data (~10 s)
python -m sapkit verify            # every rule vs. the answers built into the data
python -m sapkit demo --offline    # outreach -> replies -> rerun -> report, no API key needed
python -m sapkit eval              # 20 planner questions against the tool layer
python -m pytest                   # 54 tests
```

With an Anthropic API key:

```bash
export ANTHROPIC_API_KEY=...
python -m sapkit ask "What's short at plant 1000 this week, and why?" --show-tools
python -m sapkit ask                       # interactive
python -m sapkit ask "..." --plants 1000   # a planner who can only see plant 1000
python -m sapkit replies                   # classify replies with Claude instead of keywords
python -m sapkit eval --live               # the 20 questions through the real agent
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
| `ask ["question"] [--plants] [--show-tools]` | **Yes** | The Claude planning agent |
| `eval [--live]` | `--live` only | 20 evaluation questions |
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
