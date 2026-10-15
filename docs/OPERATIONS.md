# Operations runbook

## The monthly cycle

| Step | Who | Command / place |
|---|---|---|
| 1. New SAP extract lands | Integration | `sapkit load <dir>` (or the BigQuery load) |
| 2. Data quality | Data owner | `sapkit dq`: fix any P0 before going on |
| 3. Outreach run | Purchasing manager | `sapkit outreach --user <manager>` |
| 4. Review drafts | Buyers | Web app, or `sapkit drafts list / approve / edit / hold --user <buyer>` |
| 5. Send | Buyers | From their own mailbox, then `sapkit drafts sent <id>` |
| 6. Replies | Purchasing manager (scheduled daily) | `sapkit replies --user <manager>` |
| 7. Follow-ups | Purchasing manager (scheduled daily) | `sapkit followups --user <manager>`: creates reminders and escalations, never sends |

## Monitoring

- `sapkit metrics` (auditor/admin): questions, average and p95 latency, blocked
  or refused answers, tokens and cost per channel.
- `ops_trace`: one row per question; the trace id is shown in the web UI and in
  the API response, so a user complaint can be found exactly.
- `ops_audit_log`: every tool call with its access decision; every draft
  action, guardrail block, user change and job run.
- With `SAPKIT_LOG_JSON=1` each question also writes a JSON log line, ready for
  Cloud Logging metrics and alerts.

Suggested alerts:
- any `denied_scope` tool calls from one user in a short window (probing);
- guardrail blocks above 5% of questions;
- p95 latency above 30 s;
- any P0 data-quality failure;
- a content-filter outage (blocks with category `content_filter`).

## Incidents

| Symptom | First checks |
|---|---|
| "The numbers are wrong" | Trace id → tools and inputs in `ops_audit_log` → rerun the same tool with `sapkit query` → `sapkit dq` → compare with MD04 |
| A user sees too much or too little | `sapkit users show <id>`; check `config/rbac.yaml` and the IAP group |
| Answers are being held back | `ops_trace.guardrail`: `ungrounded_figure` usually means the model is calculating; check the question and the eval suite |
| Supplier replies not processed | `sapkit replies` output; failed writes are retried next run (the watermark doesn't move) |
| Claude Code shows no sapkit tools | Run setup again (it writes `.mcp.json`); `python -m sapkit mcp` should start in about a second |

## Users

```bash
sapkit users list
sapkit users show avery.buyer@client.com
sapkit users add new.buyer@client.com --name "New Buyer" --roles buyer --plants-list 1000 --groups P05 --user admin@client.com
sapkit users remove old.user@client.com --user admin@client.com
```

Changes edit `config/rbac.yaml` and are audited. Commit the file, so the change
is reviewed like code.

## Releases

1. A change goes through a pull request. CI runs:
   - lint;
   - all tests;
   - rule verification;
   - data quality;
   - the offline eval gate;
   - the Promptfoo red team;
   - bandit, pip-audit and detect-secrets;
   - `terraform validate`;
   - BigQuery SQL generation.
2. Before a release, run the `live-evals` workflow (real Claude, with the judge)
   and record the result in `CHANGELOG.md`.
3. Bump `__version__` and `pyproject.toml`, tag, build the image with that tag,
   then apply Terraform with the new image.
4. To roll back, apply Terraform with the previous image tag. Data and ops
   tables aren't touched by a release.

## Cost

`telemetry.PRICES` holds per-model token prices. `sapkit metrics` shows the
estimated spend per channel. Ways to reduce it:
- prompt caching (already on for the system prompt);
- lower effort for simple questions;
- Sonnet for reply classification after measuring quality on the client's own replies.
