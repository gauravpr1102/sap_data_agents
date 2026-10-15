"""Client configuration: one YAML file per SAP client."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "acme.yaml"


@dataclass(frozen=True)
class ClientConfig:
    raw: dict[str, Any] = field(repr=False)
    path: Path

    @property
    def client_id(self) -> str:
        return self.raw["client"]["id"]

    @property
    def client_name(self) -> str:
        return self.raw["client"]["name"]

    @property
    def as_of(self) -> dt.date:
        return dt.date.fromisoformat(str(self.raw["as_of_date"]))

    @property
    def plants(self) -> dict[str, str]:
        return {str(k): v for k, v in self.raw["plants"].items()}

    @property
    def horizon_days(self) -> int:
        return int(self.raw["scope"]["horizon_days"])

    @property
    def cover_rule(self) -> str:
        rule = self.raw["cover_rule"]
        if rule not in ("asn_only", "all_open"):
            raise ValueError(f"cover_rule must be asn_only or all_open, got {rule!r}")
        return rule

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def with_overrides(self, **top_level: Any) -> ClientConfig:
        raw = {**self.raw, **top_level}
        return ClientConfig(raw=raw, path=self.path)


def load_config(path: str | Path | None = None) -> ClientConfig:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = ClientConfig(raw=raw, path=p)
    _ = cfg.cover_rule  # validate early
    return cfg
