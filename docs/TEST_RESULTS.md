# Test results

Two full rounds were run on 14 Oct 2026. Round two ran in a separate clean clone,
in a brand-new Python virtual environment installed only from `pyproject.toml`,
with a different random seed and a larger dataset. That catches anything that
only worked because of the build machine's state.

## Round 1: build environment

Dataset: seed 2026, 5,000 materials per plant (15,000 material-plants).

| Check | Result |
|---|---|
| `sapkit verify`: shortage flag | 15,000 / 15,000 |
| `sapkit verify`: first short date and depth | 5,210 / 5,210 |
| `sapkit verify`: assemblies fed | 5,210 / 5,210 |
| `sapkit verify`: missing shipping notice | 15,000 / 15,000 |
| `sapkit verify`: safety-stock dip | 15,000 / 15,000 |
| `sapkit verify`: forecast change rank | 5,214 / 5,214 |
| `sapkit verify`: outreach disposition + supplier | 5,214 / 5,214 |
| `sapkit demo --offline` | 1,302 drafts; 652 replies processed, 0 failed; rerun created 0 drafts and processed 0 replies |
| `sapkit eval` (offline tool layer) | 20 / 20 |
| `pytest` | 54 passed |

## Round 2: clean clone, fresh virtual environment

Dataset: seed 777, 6,000 materials per plant (18,000 material-plants).

| Check | Result |
|---|---|
| `sapkit verify`: shortage flag | 18,000 / 18,000 |
| `sapkit verify`: first short date and depth | 6,221 / 6,221 |
| `sapkit verify`: assemblies fed | 6,221 / 6,221 |
| `sapkit verify`: missing shipping notice | 18,000 / 18,000 |
| `sapkit verify`: safety-stock dip | 18,000 / 18,000 |
| `sapkit verify`: forecast change rank | 6,210 / 6,210 |
| `sapkit verify`: outreach disposition + supplier | 6,210 / 6,210 |
| `sapkit demo --offline` | 1,562 drafts; 785 replies processed, 0 failed; rerun created nothing |
| `sapkit eval` (offline tool layer) | 20 / 20 |
| `pytest` | 54 passed |
| Plant scope from the CLI (`--plants 1000`, asking about 2000) | Refused: "You do not have access to plant 2000." |
| `ask` with no API key | Clear message, exit code 2 (fixed after round 2; it was a raw traceback) |

## What the tests cover

- **Rules (16 tests):** every scenario type present. Shortage flag, date and depth match under both cover rules. ASN-only reveals exactly the shortages hidden by unconfirmed POs. An order acknowledgement is never treated as a shipping notice. Missing-ASN, safety-stock dip and forecast rank all match, and the older forecast capture is ignored. Out-of-scope materials never leak. The price unit is applied. The assembly count and risk rank are right. Supplier resolution never guesses, and expired source-list records are ignored.
- **Outreach (8 tests):** dispositions and vendors match for every changed item. All nine disposition types occur. Drafting creates one draft per vendor and buyer, and a rerun creates nothing. Drafts never contain pricing, go only to eligible items, and are addressed from the buyer to the vendor. A mixed increase/decrease e-mail asks for the right things. A preview creates nothing.
- **Replies (12 tests):** every expected category matches. A rerun processes nothing twice. An injected write failure leaves the watermark, and the message is retried exactly once. Prompt-injection replies are flagged, sent to a buyer, and don't change the classification. Commitments match requested parts, and vendors without a reply are counted. Validation rules covered: unrequested parts, nulls kept, "can meet" with a short quantity, clean "can meet". The Claude classifier's request shape is checked (model, effort, structured output, fallbacks, **no tools**), along with refusal handling and schema strictness.
- **Tools and agent (18 tests):**
  - Tools: plant scope enforced; unknown plant refused; the summary is limited to allowed plants; material numbers work with or without leading zeros; row cap and `total_matching`; bad arguments return an error, not a crash; every schema is strict; all nine tools run; all 20 eval questions are answerable from their tools.
  - Guard: tables detected, including one-column tables; horizontal rules ignored.
  - Agent loop, with a scripted Claude client: the system prompt is rendered from config; tool-call round trip and request shape (model, effort, fallbacks, cached system prompt, no forced tool choice); the guard blocks an ungrounded table after one nudge and allows a recovery through a tool call; a scope error reaches the model marked as an error; refusal handling; append-only history across turns; parallel tool results returned in one message.

## Not tested

- **The live Claude API.** No API key was available on the build machine. Every Claude request is built and checked against a scripted client, but no real model call was made. That means the agent's actual answer quality and the Claude reply classifier's accuracy are still unmeasured. Run `python -m sapkit eval --live` and `python -m sapkit replies` with a key to measure both.
- **Real SAP data and real mailboxes.** Only the synthetic data and the mock mailbox were used.

---

# Rounds A and B: offline agent, MCP and one-command setup

Run on 14 Oct 2026, after adding the offline agent, the MCP server and the setup
scripts. Each round started from a fresh unzip of the release zip into an empty
folder, exactly as a new user would.

| Check | Round A · Windows PowerShell script | Round B · bash script (Git Bash) |
|---|---|---|
| One-command setup (venv, install, generate, verify, demo, report) | Passed, 176 s from an empty folder | Passed, 154 s |
| Rule checks vs. answer key | All match (15,000) | All match (15,000), then again at 21,000 with seed 31 |
| Demo + rerun | Rerun adds nothing | 1,831 drafts, 870 replies, 0 failures; rerun adds nothing |
| `pytest` | 69 passed | 69 passed |
| `sapkit eval --agent` (offline agent) | 20 / 20 | 20 / 20, on both datasets |
| Offline `ask` in the terminal | Answers correctly | – |
| **Real Claude, no API key**: Claude Code over the generated `.mcp.json` | Correct: 414 replies to review; biggest change at 2000 is 10007951, +40.5% (42 → 59) | Correct: 376 missing-ASN lines at plant 3000 |
| Tool calls audited in `ops_audit_log` as `mcp:` | Yes | Yes |
| Folder path too long for Windows | Clear message, nothing created | – |

Found and fixed in these rounds:
- **Long Windows paths.** A folder path too long for Windows made DuckDB's library
  fail to load. The PowerShell script now checks the path first.
- **Git Bash path form.** Under Git Bash, `.mcp.json` got a `/c/...` path that
  Claude Code on Windows can't start. Claude still answered correctly, but by
  reading the report CSV instead of calling the tool. The bash script now
  converts the path with `cygpath`.

Still not run: the Claude API agent (`sapkit ask` with `ANTHROPIC_API_KEY`) and
`eval --live`. Claude itself has now been tested end to end through Claude Code
and MCP.
