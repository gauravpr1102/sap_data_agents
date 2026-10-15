"""Gmail and Outlook (Microsoft Graph) mail adapters (docs/INTEGRATIONS.md).

Both implement ``MailAdapter``: create a draft in the buyer's own mailbox, and
read the messages of a thread the kit created. Neither can send: there is no
send method, and the OAuth scopes requested are compose/draft + read only.

Authentication is per buyer (the buyer consents once). The adapters take a
``token_provider(buyer_email) -> access_token`` so token storage and refresh
stay in the client's secret store (Secret Manager, Key Vault), never here.
"""
from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

import httpx

from .mail import Draft, DraftReceipt, Message

TokenProvider = Callable[[str], str]

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]
GRAPH_SCOPES = ["Mail.ReadWrite"]  # drafts + read; Mail.Send is deliberately NOT requested


class MailError(RuntimeError):
    pass


def _check(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise MailError(f"{what} failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json() if r.content else {}


class GmailAdapter:
    BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

    def __init__(self, token_provider: TokenProvider, http: httpx.Client | None = None):
        self.token_provider = token_provider
        self.http = http or httpx.Client(timeout=30)
        self._thread_owner: dict[str, str] = {}

    def _headers(self, buyer: str) -> dict:
        return {"Authorization": f"Bearer {self.token_provider(buyer)}"}

    def create_draft(self, draft: Draft, vendor: str) -> DraftReceipt:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = draft.from_addr, draft.to_addr, draft.subject
        msg["X-Sapkit-Ref"] = draft.idempotency_key
        msg.set_content(draft.body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = self.http.post(f"{self.BASE}/drafts", headers=self._headers(draft.from_addr),
                           json={"message": {"raw": raw}})
        body = _check(r, "Gmail drafts.create")
        thread_id = body["message"]["threadId"]
        self._thread_owner[thread_id] = draft.from_addr
        return DraftReceipt(thread_id=thread_id, draft_ref=body["id"])

    def list_thread_messages(self, thread_id: str, buyer: str | None = None) -> list[Message]:
        owner = buyer or self._thread_owner.get(thread_id)
        if not owner:
            raise MailError(f"unknown owner for thread {thread_id}")
        r = self.http.get(f"{self.BASE}/threads/{thread_id}", headers=self._headers(owner),
                          params={"format": "full"})
        out = []
        for m in _check(r, "Gmail threads.get").get("messages", []):
            headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
            body = _gmail_text(m.get("payload", {}))
            sent = dt.datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000)
            out.append(Message(headers.get("message-id", m["id"]), headers.get("from", ""), sent, body))
        return out


def _gmail_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        t = _gmail_text(part)
        if t:
            return t
    return ""


class OutlookAdapter:
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, token_provider: TokenProvider, http: httpx.Client | None = None):
        self.token_provider = token_provider
        self.http = http or httpx.Client(timeout=30)
        self._thread_owner: dict[str, str] = {}

    def _headers(self, buyer: str) -> dict:
        return {"Authorization": f"Bearer {self.token_provider(buyer)}"}

    def create_draft(self, draft: Draft, vendor: str) -> DraftReceipt:
        payload = {
            "subject": draft.subject,
            "body": {"contentType": "Text", "content": draft.body},
            "toRecipients": [{"emailAddress": {"address": draft.to_addr}}],
            "singleValueExtendedProperties": [
                {"id": "String {00020386-0000-0000-C000-000000000046} Name X-Sapkit-Ref",
                 "value": draft.idempotency_key}],
        }
        # POST /me/messages creates a draft in the signed-in buyer's Drafts folder; nothing is sent.
        r = self.http.post(f"{self.BASE}/me/messages", headers=self._headers(draft.from_addr), json=payload)
        body = _check(r, "Graph create draft")
        conv = body["conversationId"]
        self._thread_owner[conv] = draft.from_addr
        return DraftReceipt(thread_id=conv, draft_ref=body["id"])

    def list_thread_messages(self, thread_id: str, buyer: str | None = None) -> list[Message]:
        owner = buyer or self._thread_owner.get(thread_id)
        if not owner:
            raise MailError(f"unknown owner for conversation {thread_id}")
        r = self.http.get(f"{self.BASE}/me/messages", headers=self._headers(owner),
                          params={"$filter": f"conversationId eq '{thread_id}'",
                                  "$select": "internetMessageId,from,receivedDateTime,body,isDraft"})
        out = []
        for m in _check(r, "Graph list messages").get("value", []):
            if m.get("isDraft"):
                continue
            sent = dt.datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")).replace(tzinfo=None)
            sender = m.get("from", {}).get("emailAddress", {}).get("address", "")
            out.append(Message(m.get("internetMessageId", m["id"]), sender, sent, m.get("body", {}).get("content", "")))
        return out


__all__ = ["GmailAdapter", "OutlookAdapter", "MailError", "GMAIL_SCOPES", "GRAPH_SCOPES", "parsedate_to_datetime"]
