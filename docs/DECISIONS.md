# Decision log

Each entry records what was decided, the options considered, and why. Newest
last. These were made during the build without a reviewer, so each is open to
challenge.

---

## D1 · Claude is the runtime, not just the build tool

**Options.** (a) Keep Gemini Enterprise/ADK as the runtime and use Claude Code
only to adapt the existing code; (b) run the agent itself on the Claude API.

**Decision.** (b). The ask was to *implement it in Claude* for a dummy client.
The reusable value is the governed tools and SQL; swapping the reasoning layer
is the smallest change, and option (b) proves that.

## D2 · Claude API with our own tool loop, not Managed Agents or the Agent SDK

**Options.** Manual tool loop on the Messages API; the SDK's beta tool runner;
Claude Managed Agents (Anthropic-hosted loop and sandbox); the Claude Agent SDK
(Claude Code as a library).

**Decision.** A manual loop on the Messages API. The tools touch governed
client data, so they must run in our process. The loop needs a hook after each
turn for the grounding guard. A manual loop is about 60 lines, has no beta
dependency, and is easy to test with a scripted client. Managed Agents and the
Agent SDK add a sandbox and built-in file/bash tools that this workload
doesn't need.

## D3 · Model: `claude-opus-5-5`, effort `medium`, adaptive thinking

**Options.** Opus 5.5 (most capable default), Sonnet 5.5 (cheaper), Haiku 4.5
(cheapest).

**Decision.** Opus 5.5 at `medium` effort, set explicitly (that is its
default). Thinking stays adaptive; on this model it can't be disabled.
The model ID and effort are in the client config, so a client can move
high-volume reply classification to Sonnet 5.5 after measuring quality on its
own replies.

## D4 · Refusal fallbacks are on by default

The API's server-side `fallbacks: "default"` option (beta
`server-side-fallback-2026-07-01`) is set on every call, so a rare policy
decline is retried on a fallback model inside the same call instead of
failing the run. It can be switched off in code (`use_fallbacks=False`), and it
is unavailable on Bedrock, Vertex and Foundry.

## D5 · No forced tool choice

Opus 5.5 rejects `tool_choice` `any`/`tool`. Tools are offered with `auto`,
and every tool schema is `strict: true` with `additionalProperties: false`, so
any call Claude makes is schema-valid.

## D6 · DuckDB as the analytical store

**Options.** BigQuery sandbox (closest to the reference), PostgreSQL, SQLite,
DuckDB.

**Decision.** DuckDB. It is a single file, needs no server or cloud access,
supports window functions and the SQL dialect ports almost unchanged to
BigQuery. That keeps the dummy client fully runnable on a laptop and inside a
1–1.5 day build. Moving to BigQuery means changing the connection and a handful
of date functions, not the rules.

## D7 · SAP-shaped tables, not a simplified schema

**Decision.** The dummy data uses SAP table and field names (MARA, MARC-BESKZ,
EKPO-BSTAE, EKES-EBTYP = 'LA', EORD-FLIFN, T024 and so on), with material
numbers as 18-character strings with leading zeros. SAP users recognise it,
and the mapping from a real extract is direct. Simplified parts:
- MD04 is rebuilt from its sources (stock, PO schedule lines, dependent
  requirements) rather than copied.
- Vendor e-mail sits in a minimal ADR6.
- VMI is a client Z-table (`zvmi`).

## D8 · Known answers by construction

The generator decides each material-plant's scenario first (for example "short
on day 12, covered by an unconfirmed PO") and writes that expected outcome to
`expected_*` tables *before* any SQL runs. The tests compare SQL results against
those tables. This gives an independent oracle at any data volume; checking
SQL against a second implementation of the same logic would only prove the
two agree.

## D9 · Forecast drives use case 2 only; dependent requirements drive shortages

To keep each answer attributable, the shortage projection uses on-hand stock,
PO supply and dependent requirements (RESB). The forecast snapshots feed the
forecast-change ranking. A real client may include planned independent
requirements in the projection too; that is a one-line union in
`v_demand_events`.

## D10 · Ranking: six boxes plus a "new demand" rank

| Rank | Window | Change |
|---|---|---|
| 1 | Inside lead time | ≥ high threshold |
| 2 | Inside lead time | ≥ low, < high |
| 3 | Outside lead time | ≥ high |
| 4 | Outside lead time | ≥ low, < high |
| 5 | Inside lead time | < low (report only) |
| 6 | Outside lead time | < low (report only) |
| 7 | Either | New demand (no prior quantity), sent for review |

Thresholds and draftable ranks come from config (defaults 15% / 5%, ranks
1–3).

## D11 · Eligibility is ordered, and every no gets a reason code

Order: not draftable → excluded material group → VMI → supplier resolution
(open PO vendor, then fixed source list, then unique valid source list; else
ambiguous / none) → interplant vendor → missing buyer email → missing vendor
email (routed to sourcing, never dropped) → eligible. Ordered rules mean each
material-plant gets exactly one reason, which is what a buyer needs to see.

## D12 · Mail adapter interface with a mock mailbox

The dummy client gets a `MockMailbox` that writes drafts to `out/drafts/` and
serves supplier replies from `data/mailbox.json`. Gmail and Outlook adapters
implement the same three methods (`create_draft`, `list_thread_messages`,
`thread_for`). They were not built here, because they need a real tenant and
consent.

## D13 · Reply classification: structured outputs, no tools, offline fallback

Classification uses `output_config.format` with a JSON schema (7 categories,
per-part commitments with nullable quantity and date, an
`instructions_to_ai_detected` flag). No tools are offered on that call, so
supplier text can't cause an action. An `--offline` keyword classifier exists
only so the pipeline can be demonstrated and tested without an API key; it is
tuned to the synthetic templates and is not meant for real replies.

## D14 · Domain knowledge as markdown packs in the system prompt

**Options.** Claude Agent Skills (requires the code-execution container), or
markdown packs loaded into the system prompt and cached.

**Decision.** Markdown packs in `domain/`, loaded in a fixed order and cached
with `cache_control`. They work on every platform (Claude API, Vertex,
Bedrock, Foundry) and need no container. The same files can be published as
Agent Skills later without rewriting.

## D15 · Bulk-load the dataset through CSV

The first version inserted rows one at a time; 4,500 material-plants took
over eight minutes to load. Writing CSV files and using DuckDB's `COPY` brings
15,000 material-plants down to about 10 seconds. Values are quoted so an empty
SAP field (`''`) stays distinct from a missing one (`NULL`), which matters for
fields like MARC-MMSTA and EKPO-LOEKZ.

## D16 · pandas is a runtime dependency

Profiling the outreach run showed most of its time going to failed imports:
DuckDB probes for pandas on every parameterised query, and without pandas
installed each probe searches the whole import path. Installing pandas cut the
full outreach run from 52 s to 7 s. pandas isn't otherwise used.

## D17 · The mock mailbox's thread registry is append-only

Rewriting one JSON state file per draft made drafting quadratic. A JSONL file
with one line per thread is append-only, crash-safe, and fast.

## D18 · `get_supplier_replies` gained a `needs_review_only` filter

Found by evaluation case C14 on the 15,000 material-plant dataset: "Which
replies need my review?" had no way to be answered precisely, because the
tool returned all replies and the agent would have had to count. Following
the rule that the model never computes a figure, the tool now filters on
review and returns the review count per category.

## D19 · Single-column tables count as tables for the guard

Found by a unit test: the grounding guard's pattern needed at least two columns.
It now catches any markdown separator row containing a pipe, while still
ignoring a plain `---` horizontal rule.

## D20 · Default dataset: 5,000 materials per plant

15,000 material-plants, with about 5,000 shortages, 5,000 forecast changes,
1,300 supplier drafts and 650 supplier replies. That is enough volume to make
ranking, row caps and reruns meaningful. It generates in about 10 seconds. The
test suite uses its own smaller dataset (400 per plant) so it stays quick.
