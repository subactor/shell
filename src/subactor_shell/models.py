from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Role = Literal["system", "user", "assistant"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Session:
    id: str
    name: str
    provider: str
    model: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class Message:
    id: int
    session_id: str
    role: Role
    display_content: str
    context_content: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class Artifact:
    id: str
    original_path: str
    stored_path: Path
    mime_type: str
    size: int
    created_at: str


@dataclass(slots=True)
class ProviderProfile:
    name: str
    kind: str
    model: str
    base_url: str = ""
    endpoint: str = ""
    api_key_ref: str = ""
    auth_required: bool = False
    max_tokens: int = 4096
    max_output_tokens: int = 256
    structured_mode: str = "auto"
    reasoning_effort: str = ""
    anthropic_version: str = "2023-06-01"
    timeout_seconds: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    input_cost_per_million: float = 0.0
    cached_input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


@dataclass(slots=True)
class ChatChunk:
    text: str


@dataclass(slots=True)
class PreparedPrompt:
    display_content: str
    safe_context_content: str
    provider_content: str
    resolved_secret_values: list[str]
    metadata: dict[str, Any]
