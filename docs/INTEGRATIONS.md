# Integrations

## Mail: Gmail and Outlook

`src/sapkit/mail_adapters.py` implements the same `MailAdapter` interface as the
mock mailbox:

| | Gmail | Outlook (Microsoft Graph) |
|---|---|---|
| Create draft | `users.drafts.create` | `POST /me/messages` (creates a draft in Drafts) |
| Read a tracked thread | `users.threads.get` | `GET /me/messages?$filter=conversationId eq '…'` |
| Scopes requested | `gmail.compose`, `gmail.readonly` | `Mail.ReadWrite` |
| Send | **Not possible**: no method exists | **Not possible**: `Mail.Send` isn't requested |

**Consent and tokens:**
- Each buyer consents once (OAuth). The adapters take a
  `token_provider(buyer_email) -> access_token` function.
- Storing and refreshing tokens is the job of the client's secret store
  (Secret Manager, Key Vault). Tokens never sit in this code or its config.

**Status:** both adapters are tested against mocked Gmail and Graph APIs
(request shape, auth header, parsing, draft-only behaviour). They haven't been
run against a live mailbox; that needs a client tenant and a consenting buyer.

## SAP

Read-only, through extracts described in DATA_CONTRACT.md. Nothing writes to SAP.

## Claude

| Channel | How | Key |
|---|---|---|
| Web and API agent | Claude API, or Claude on Vertex AI | `ANTHROPIC_API_KEY`, or Google IAM (no key) |
| Claude Code / Claude Desktop | MCP server: `python -m sapkit mcp` | The user's own Claude login |
| Eval judge | Claude Code CLI or Claude API | Either |

## BI tools

The curated views (DuckDB or the generated BigQuery views) and the ops tables
are plain tables for any BI tool. In BigQuery, the row access policies apply to
BI users too.
