"""Append-only audit log. The only place the kit records what it did."""
from __future__ import annotations

import json
import uuid
from typing import Any

import duckdb


def log(con: duckdb.DuckDBPyConnection, actor: str, event_type: str, subject: str,
        detail: dict[str, Any] | None = None) -> str:
    event_id = uuid.uuid4().hex
    con.execute(
        "INSERT INTO ops_audit_log (event_id, actor, event_type, subject, detail) VALUES (?, ?, ?, ?, ?)",
        [event_id, actor, event_type, subject, json.dumps(detail or {}, default=str, sort_keys=True)],
    )
    return event_id
