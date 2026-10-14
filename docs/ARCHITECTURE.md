# Architecture

This kit rebuilds the five SAP planning and procurement use cases of the earlier
Gemini-based reference implementation on Claude, for a dummy SAP client
("Acme Precision Manufacturing"). It is meant as a reusable starting point: a
new SAP client should mostly change `config/<client>.yaml` and the data
mapping, not the code.

## The one rule everything follows

**The model handles language. SQL and Python establish the facts.**

Claude reads the question, picks a tool, explains the rows it gets back, and
classifies supplier replies. It never computes a balance, a percentage, a rank
or a recipient. Those come from SQL views and deterministic Python, which are
tested against known answers.

## Layers

```
            ┌──────────────────────────────────────────────────────────┐
 Interface  │ CLI (sapkit ask / query / outreach / replies / report)   │
            └───────────────┬──────────────────────────────────────────┘
                            │ question + user scope (allowed plants)
            ┌───────────────▼──────────────────────────────────────────┐
 Agent      │ agent.py   Claude API tool loop (claude-opus-5-5)        │
            │            system prompt = domain pack (domain/*.md)     │
            │ guard.py   blocks tables no tool produced this turn      │
            └───────────────┬──────────────────────────────────────────┘
                            │ tool_use (strict JSON schemas)
            ┌───────────────▼──────────────────────────────────────────┐
 Tools      │ tools.py   9 governed tools; plant scope enforced here,  │
            │            row caps, as-of date + match count on results │
            └───────────────┬──────────────────────────────────────────┘
                            │ parameterised SQL only
            ┌───────────────▼──────────────────────────────────────────┐
 Rules      │ sql/views.sql  scope, supply, projection, shortage,      │
            │                missing ASN, forecast six-box, suppliers  │
            │ outreach.py    eligibility + reason codes, idempotent    │
            │ replies.py     tracked threads, watermark, persistence   │
            └───────────────┬──────────────────────────────────────────┘
                            │
            ┌───────────────▼──────────────────────────────────────────┐
 Data       │ DuckDB file: SAP-shaped raw tables (MARA, MARC, MARD,    │
            │ MBEW, EKKO, EKPO, EKET, EKES, EORD, RESB, LFA1, ADR6,    │
            │ T024, forecast snapshots) + ops tables (audit, drafts,   │
            │ reply extracts, watermarks)                              │
            └──────────────────────────────────────────────────────────┘
 Mail       mail.py  MailAdapter interface; MockMailbox for the dummy
            client (drafts written to out/drafts, replies from data/)
```

## Data flow per use case

| # | Use case | Path |
|---|---|---|
| 1 | Shortage intelligence | question → Claude → `get_shortage_queue` / `get_shortage_detail` / `get_missing_asn` → `v_shortage` / `v_missing_asn` → answer with as-of date |
| 2 | Forecast-change prioritisation | `v_forecast_change` compares two snapshot captures, splits inside/outside lead time, assigns ranks 1–7 |
| 3 | Supplier resolution & eligibility | `v_supplier_candidates` + `outreach.decide()` → one disposition and reason code per material-plant |
| 4 | Buyer-approved drafting | eligible items grouped per vendor + buyer → template render → `MailAdapter.create_draft` (idempotent key) → audit row. The buyer sends; the kit never does |
| 5 | Reply intelligence | tracked threads only → new messages after watermark → Claude structured-output classification → validation → idempotent persist → watermark advances only after persist |

## Where client differences live

| Moving part | Where it is configured |
|---|---|
| Scope (procurement type, MRP type, status, horizon) | `scope:` in the config |
| Cover rule (ASN-only vs all open POs) | `cover_rule:` |
| ASN control key, transit default | `asn:` |
| Forecast thresholds, draftable ranks | `forecast:` |
| Exclusions (material groups, interplant vendors), VMI | `outreach:` + `zvmi` table |
| Field mapping from a real extract | `synth.py` is the reference for the target table shapes; a real client needs a loader into the same shapes |

## Security and safety properties

- **Read-only to SAP.** Nothing writes to SAP tables. Writes go only to the ops tables (audit, drafts, reply extracts, watermarks).
- **Scope from identity, not from the model.** The allowed plants come from the caller's session; a tool refuses any other plant even if Claude asks for it.
- **Supplier email is untrusted data.** Reply classification runs with no tools at all, so text inside an email can't trigger an action. The schema also records whether a reply contained instructions aimed at the AI.
- **Idempotent writes.** Drafts are keyed by client, snapshot pair, vendor, buyer and template version; replies by client and message ID. A rerun creates nothing new.
- **Watermark after persistence.** If the write fails, the watermark doesn't move, so the next run retries the same messages.
- **No pricing in supplier emails**, enforced in the template code, not only in the prompt.

See `docs/DECISIONS.md` for why each choice was made.
