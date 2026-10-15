# Changelog

## 0.2.0 (2026-10-15)

Production hardening, after review.

- **RBAC:**
  - five roles in `config/rbac.yaml`, deny by default;
  - per-tool and per-action permissions;
  - plant and purchasing-group scope enforced in the tools and as row filters in the data layer;
  - every tool call audited with its access decision;
  - `sapkit users` for administration.
- **Guardrails:**
  - input screen for injection, scope-lifting, SQL injection, personal data and length;
  - figure grounding;
  - supplier-pricing and personal-data output checks;
  - content-filter hook (fails closed);
  - tool-call and turn budgets.
- **Review workflow:**
  - drafts move pending → approved / on hold → sent, by authorised buyers only;
  - edits are re-screened;
  - follow-up ladder at 48 h, 4 days and 6 days (creates drafts only, never sends).
- **Evals:**
  - 41 cases with deterministic DeepEval metrics and a release gate;
  - three runners: offline, real Claude via Claude Code, API;
  - G-Eval answer quality judged by Claude;
  - Promptfoo red team with 20 attacks.
- **Production stack:**
  - Claude on Vertex AI;
  - BigQuery views and row access policies generated from the rules and RBAC;
  - Dockerfile;
  - Terraform for Cloud Run + IAP + BigQuery + Artifact Registry + Secret Manager.
- **Data:** data-contract loader with rejected-row reports, CSV export, 12 data-quality checks.
- **Integrations:** Gmail and Microsoft Graph draft adapters (no send scope).
- **Web front end:** chat, draft review, audit, metrics.
- **Observability:** per-question traces with latency, tokens and cost; JSON logs; optional Opik export.
- **MCP:** fast built-in stdio server (about 0.5 s start) listing only the role's tools.
- **CI:**
  - lint, tests, rule verification, data quality, eval gate, red team;
  - bandit, pip-audit, detect-secrets;
  - Terraform validate and BigQuery SQL generation.

## 0.1.0 (2026-10-14)

- First build: the dummy SAP client, five use cases, offline agent, MCP server and one-command setup.
