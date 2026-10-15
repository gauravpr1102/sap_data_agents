"""Role-based access control (docs/SECURITY.md).

Identity comes from outside the model (the CLI flag, the MCP server's
environment, or the web front end's identity header). This module turns an
identity into a ``Session``: the plants, purchasing groups, tools and actions
that person may use. Unknown users and users without roles get nothing.

Enforcement happens in three places:
1. ``Toolbox.run`` refuses tools the session doesn't allow.
2. Each tool refuses plants outside the session.
3. ``db.connect(..., session=...)`` builds the rule views filtered to the
   session's plants and purchasing groups, so a tool can't read rows outside
   scope even if it has a bug (the local equivalent of BigQuery row-level
   security).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT, ClientConfig

DEFAULT_RBAC = ROOT / "config" / "rbac.yaml"
SYSTEM_USER = "system@sapkit"  # batch jobs; full scope, no tools


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Session:
    """Who is asking and what they may touch.

    ``purchasing_groups`` / ``tools`` / ``actions`` of ``None`` mean
    "unrestricted" and are only produced for the admin role and system jobs.
    """

    user: str
    allowed_plants: list[str]
    roles: tuple[str, ...] = ()
    purchasing_groups: tuple[str, ...] | None = None
    tools: frozenset[str] | None = None
    actions: frozenset[str] | None = None
    display_name: str = ""

    def can_use_tool(self, name: str) -> bool:
        return self.tools is None or name in self.tools

    def can(self, action: str) -> bool:
        return self.actions is None or action in self.actions

    def covers_group(self, ekgrp: str | None) -> bool:
        return self.purchasing_groups is None or (ekgrp is not None and ekgrp in self.purchasing_groups)

    def restricted_to(self, plants: list[str]) -> "Session":
        """Narrow plant scope further (never widen it)."""
        keep = [p for p in self.allowed_plants if p in set(plants)]
        return Session(self.user, keep, self.roles, self.purchasing_groups, self.tools, self.actions, self.display_name)


@dataclass
class Rbac:
    roles: dict[str, dict[str, Any]]
    users: dict[str, dict[str, Any]]
    path: Path = field(default=DEFAULT_RBAC)

    def session_for(self, user_id: str, cfg: ClientConfig) -> Session:
        if user_id == SYSTEM_USER:
            return Session(SYSTEM_USER, list(cfg.plants), ("system",), None, frozenset(), None, "System job")
        u = self.users.get(user_id.strip().lower())
        if u is None:
            raise AccessDenied(f"Unknown user '{user_id}'. Access is granted per user in config/rbac.yaml.")
        roles = tuple(u.get("roles") or [])
        if not roles:
            raise AccessDenied(f"User '{user_id}' has no role assigned.")
        unknown = [r for r in roles if r not in self.roles]
        if unknown:
            raise AccessDenied(f"User '{user_id}' has unknown role(s): {', '.join(unknown)}.")
        plants = [str(p) for p in (u.get("plants") or []) if str(p) in cfg.plants]
        if not plants:
            raise AccessDenied(f"User '{user_id}' has no plants assigned.")

        tools: set[str] = set()
        actions: set[str] = set()
        unrestricted_tools = unrestricted_actions = False
        for r in roles:
            role = self.roles[r]
            t = role.get("tools") or []
            a = role.get("actions") or []
            unrestricted_tools |= "*" in t
            unrestricted_actions |= "*" in a
            tools |= {x for x in t if x != "*"}
            actions |= {x for x in a if x != "*"}
        groups = u.get("purchasing_groups")
        # Buyers are limited to their groups; managers, planners and admins see all groups in their plants.
        if groups is None or "purchasing_manager" in roles or "admin" in roles:
            pgroups = None
        else:
            pgroups = tuple(str(g) for g in groups)
        return Session(
            user=user_id.strip().lower(),
            allowed_plants=plants,
            roles=roles,
            purchasing_groups=pgroups,
            tools=None if unrestricted_tools else frozenset(tools),
            actions=None if unrestricted_actions else frozenset(actions),
            display_name=u.get("name", user_id),
        )

    # ---------------------------------------------------------------- admin
    def save(self) -> None:
        data = {"roles": self.roles, "users": [{"id": k, **v} for k, v in sorted(self.users.items())]}
        header = "# Managed with `sapkit users`; edits are reviewed like code.\n"
        self.path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def upsert_user(self, user_id: str, name: str, roles: list[str], plants: list[str],
                    purchasing_groups: list[str] | None) -> None:
        bad = [r for r in roles if r not in self.roles]
        if bad:
            raise ValueError(f"Unknown role(s): {', '.join(bad)}. Roles: {', '.join(self.roles)}")
        rec: dict[str, Any] = {"name": name, "roles": roles, "plants": plants}
        if purchasing_groups:
            rec["purchasing_groups"] = purchasing_groups
        self.users[user_id.strip().lower()] = rec

    def remove_user(self, user_id: str) -> bool:
        return self.users.pop(user_id.strip().lower(), None) is not None


def load_rbac(path: str | Path | None = None) -> Rbac:
    p = Path(path) if path else DEFAULT_RBAC
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    users = {}
    for u in raw.get("users") or []:
        uid = str(u["id"]).strip().lower()
        users[uid] = {k: v for k, v in u.items() if k != "id"}
    return Rbac(roles=raw.get("roles") or {}, users=users, path=p)
