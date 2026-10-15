"""Which Claude endpoint to use (docs/DEPLOYMENT.md).

``llm.provider`` in the client config:
- ``anthropic``: the Claude API, authenticated with ANTHROPIC_API_KEY.
- ``vertex``: Claude on Google Cloud Vertex AI, authenticated with Google
  Application Default Credentials. Billing, IAM and data handling stay in the
  client's Google Cloud project, and no Anthropic key exists.

Server-side refusal fallbacks are a Claude API feature; on Vertex they are
switched off (the agent handles a refusal itself).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .config import ClientConfig


@dataclass(frozen=True)
class LlmSettings:
    provider: str
    model: str
    effort: str
    vertex_project: str | None
    vertex_region: str

    @property
    def supports_fallbacks(self) -> bool:
        return self.provider == "anthropic"


def settings(cfg: ClientConfig) -> LlmSettings:
    llm = cfg.get("llm") or {}
    legacy = cfg.get("replies") or {}
    provider = os.environ.get("SAPKIT_LLM_PROVIDER") or llm.get("provider", "anthropic")
    if provider not in ("anthropic", "vertex"):
        raise ValueError(f"llm.provider must be 'anthropic' or 'vertex', got {provider!r}")
    return LlmSettings(
        provider=provider,
        model=llm.get("model") or legacy.get("model", "claude-opus-5-5"),
        effort=llm.get("effort") or legacy.get("effort", "medium"),
        vertex_project=os.environ.get("SAPKIT_VERTEX_PROJECT") or llm.get("vertex_project"),
        vertex_region=os.environ.get("SAPKIT_VERTEX_REGION") or llm.get("vertex_region", "global"),
    )


def make_client(s: LlmSettings) -> Any:
    if s.provider == "vertex":
        if not s.vertex_project:
            raise ValueError("llm.vertex_project (or SAPKIT_VERTEX_PROJECT) is required for the vertex provider")
        from anthropic import AnthropicVertex
        return AnthropicVertex(project_id=s.vertex_project, region=s.vertex_region)
    import anthropic
    return anthropic.Anthropic()


def has_credentials(s: LlmSettings) -> bool:
    if s.provider == "vertex":
        return bool(s.vertex_project)
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
