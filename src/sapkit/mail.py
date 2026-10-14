"""Mail adapters. The kit creates drafts and reads tracked threads; it never sends.

``MailAdapter`` is the interface a Gmail or Outlook adapter implements. The
dummy client uses ``MockMailbox``: drafts land in ``out/drafts/`` as readable
files, and supplier replies come from ``data/mailbox.json``.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Draft:
    idempotency_key: str
    from_addr: str
    to_addr: str
    subject: str
    body: str


@dataclass(frozen=True)
class DraftReceipt:
    thread_id: str
    draft_ref: str


@dataclass(frozen=True)
class Message:
    message_id: str
    from_addr: str
    sent_at: dt.datetime
    body: str


class MailAdapter(Protocol):
    def create_draft(self, draft: Draft, vendor: str) -> DraftReceipt: ...
    def list_thread_messages(self, thread_id: str) -> list[Message]: ...


class MockMailbox:
    """File-backed mailbox for the dummy client.

    Supplier replies in ``mailbox.json`` are keyed by vendor. A vendor's replies
    are delivered to that vendor's first thread, the way a supplier answers the
    first e-mail they received.
    """

    def __init__(self, inbox_path: Path, out_dir: Path):
        self.inbox = json.loads(Path(inbox_path).read_text(encoding="utf-8")) if Path(inbox_path).exists() else {}
        self.out_dir = Path(out_dir)
        self.drafts_dir = self.out_dir / "drafts"
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        # Append-only thread registry: one JSON line per thread, so creating a
        # draft never rewrites the whole file.
        self.state_path = self.out_dir / "mailbox_threads.jsonl"
        self.state: dict = {"threads": {}}
        if self.state_path.exists():
            for line in self.state_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.state["threads"][rec.pop("thread_id")] = rec

    def create_draft(self, draft: Draft, vendor: str) -> DraftReceipt:
        thread_id = f"thr-{draft.idempotency_key}"
        draft_ref = f"draft-{draft.idempotency_key}"
        if thread_id not in self.state["threads"]:
            rec = {"vendor": vendor, "seq": len(self.state["threads"]),
                   "created_at": dt.datetime.now().isoformat(timespec="seconds")}
            self.state["threads"][thread_id] = rec
            with open(self.state_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"thread_id": thread_id, **rec}) + "\n")
        text = (f"From: {draft.from_addr}\nTo: {draft.to_addr}\nSubject: {draft.subject}\n"
                f"X-Draft-Ref: {draft_ref}\nX-Status: DRAFT - not sent; the buyer reviews and sends\n\n{draft.body}\n")
        (self.drafts_dir / f"{draft_ref}.txt").write_text(text, encoding="utf-8")
        return DraftReceipt(thread_id=thread_id, draft_ref=draft_ref)

    def _first_thread(self, vendor: str) -> str | None:
        threads = [(v["seq"], t) for t, v in self.state["threads"].items() if v["vendor"] == vendor]
        return min(threads)[1] if threads else None

    def list_thread_messages(self, thread_id: str) -> list[Message]:
        info = self.state["threads"].get(thread_id)
        if not info or self._first_thread(info["vendor"]) != thread_id:
            return []
        return [Message(m["message_id"], m["from"], dt.datetime.fromisoformat(m["sent_at"]), m["body"])
                for m in self.inbox.get(info["vendor"], [])]
