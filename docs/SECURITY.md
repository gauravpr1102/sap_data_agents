# Security

How the kit protects client data and what's been tested. Read this together with
the threat model below. The residual risks at the end are deliberate trade-offs,
listed so they can be revisited.

## Principles

1. **SAP stays the system of record.** The kit never writes to SAP tables. Its
   only writes go to its own ops tables (audit, drafts, replies, traces).
2. **Identity comes from outside the model.** The identity provider (Google
   IAP in production), the CLI `--user` flag or the MCP server's environment
   says who is asking. Nothing the user types, or the model says, changes it.
3. **Deny by default.** Users not in `config/rbac.yaml`, or without a role, get nothing.
4. **Enforce in code, at more than one layer.** Every rule that matters is
   enforced by code, not only by the prompt.
5. **Every decision leaves a record.** Each tool call is audited with its
   access decision, along with every draft action and every guardrail block.

## Access control (RBAC)

Roles are defined in `config/rbac.yaml`; changes are reviewed like code.

| Role | Sees | Can do |
|---|---|---|
| planner | Shortages, late shipping notices, safety-stock dips, forecast changes in their plants | Read only |
| buyer | Planner data plus outreach, drafts and replies for **their purchasing groups** | Approve, edit, hold drafts; record a draft as sent |
| purchasing_manager | Everything in their plants, all purchasing groups | Buyer actions plus run outreach, reply processing, follow-ups |
| auditor | Shortage summary, audit log, metrics | Read the audit log |
| admin | Everything | Manage users and data; every job |

Enforcement points:

| Layer | What it stops |
|---|---|
| Identity (IAP header / CLI / MCP env) | Unknown users and users without a role |
| Tool list given to the model | The model never sees tools the role doesn't allow |
| `Toolbox.run` | A tool the role doesn't allow; a plant outside scope; malformed or oversized arguments |
| Data layer (scoped views; BigQuery row access policies in production) | Rows outside the user's plants and purchasing groups, even if a tool has a bug |
| Workflow | Draft actions outside the user's role, purchasing group or plants |
| Web API | Endpoints the role can't use (drafts, audit, metrics) |

## Guardrails

In the order a request meets them (`src/sapkit/guardrails.py`, `agent.py`):

1. **Input screen.** Blocks prompt-injection and jailbreak patterns, attempts to
   lift scope ("ignore my restrictions"), SQL-injection-style text, payment card
   and national-ID numbers, and oversized input, before any model or tool sees
   them.
2. **Content-filter hook.** Set `SAPKIT_CONTENT_FILTER_URL` to route inputs and
   outputs through an external classifier (e.g. Model Armor behind a small
   proxy). It **fails closed**: if the filter can't be reached, the request is refused.
3. **Budgets.** A maximum of 12 tool calls and 10 model turns per question.
4. **Grounding.**
   - A table can't appear unless a tool produced data this turn.
   - Every significant number in an answer must appear in this turn's tool
     results. One retry is allowed, then the answer is held back.
5. **Disclosure.**
   - No prices or costs in anything addressed to a supplier, including buyer edits.
   - No personal data echoed back.
6. **Human approval.**
   - The kit creates drafts only.
   - A buyer approves and sends from their own mailbox.
   - The mail adapters request no send permission at all.
7. **Supplier e-mail is data, not instructions.**
   - Reply classification runs with no tools.
   - Embedded instructions are flagged and the reply goes to a buyer.

## Threat model (STRIDE)

| Threat | Example | Mitigation | Tested by |
|---|---|---|---|
| Spoofing | Typing "I am the admin" | Identity from IAP/CLI/env only; input screen | `test_web_iap_mode`, red team "Claimed authority", eval SE-03 |
| Spoofing | Forged identity header | Cloud Run ingress internal + load balancer + IAP; dev login disabled when `SAPKIT_AUTH=iap` | `test_web_iap_mode` |
| Tampering | SQL injection through a material or vendor field | Bound parameters only; input screen; argument length limits | `test_input_screen_blocks`, `test_bad_arguments...`, eval SE-04, red team |
| Tampering | Edited draft adds prices | Edits re-screened before saving | `test_edit_is_rescreened_for_pricing` |
| Repudiation | "I never approved that" | Append-only audit with the human actor on every action | `test_full_lifecycle_by_the_right_buyer` |
| Information disclosure | Planner reads another plant | Tool + data-layer scope | `test_data_layer_scope...`, eval RB-01…06, red team |
| Information disclosure | System prompt extraction | Input screen; prompt holds no secrets | eval SE-01, red team |
| Information disclosure | Supplier pricing leaks | Template code + output screen | `test_drafts_never_contain_pricing`, eval SE-02 |
| Denial of service | Looping agent, huge prompts | Turn/tool budgets, input length cap, row caps | `test_tool_call_budget`, `test_row_cap...` |
| Elevation of privilege | Prompt injection in a supplier reply | Classifier has no tools; flag + buyer review | `test_injection_is_flagged...`, red team "Hidden instruction" |
| Elevation of privilege | Agent asked to send or change SAP | No send or SAP-write capability exists | red team "excessive agency" |

## Testing

| Check | Tool | Result (latest) |
|---|---|---|
| Unit, integration and security tests | pytest | All pass (see TEST_RESULTS.md) |
| Eval gate, incl. 8 security and 6 RBAC cases | DeepEval metrics, `sapkit eval` | Offline 41/41; real Claude: see EVALS.md |
| Red team, 20 attacks | Promptfoo | 20/20 offline agent; 20/20 real Claude |
| Static analysis | bandit | No findings (3 false positives annotated) |
| Dependency vulnerabilities | pip-audit | None known |
| Secrets in the repository | detect-secrets | None |

## Residual risks

- **Claude Code / Claude Desktop channel.** When Claude is the front end over
  MCP, our input and output guardrails don't run; only RBAC, scope, argument
  checks and audit (all server-side) do. In testing, a run where the tools
  failed to connect produced an invented answer. The fast stdio server
  removed the connection failure, but this channel is still best kept for
  analysts and pilots. Business users should use the web front end, where every
  guardrail runs.
- **Dev authentication** (`SAPKIT_AUTH=dev`) trusts a cookie and exists for local
  demos only. Production must run with `SAPKIT_AUTH=iap` behind IAP.
- **Pattern-based input screening** catches known attack shapes, not every
  possible one. It's one layer of several, and the content-filter hook is where
  a model-based classifier plugs in.
- **The pilot store** is a DuckDB file (single writer). See DEPLOYMENT.md for
  the move to BigQuery for the ops tables.
