from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..token_budget import TokenUsage, estimate_messages_tokens, estimate_text_tokens


class ProviderError(RuntimeError):
    """Sanitized provider error; never include request payloads or credentials."""


@dataclass(slots=True)
class StructuredCompletion:
    data: dict[str, Any]
    raw_text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    request_id: str = ""


class ChatProvider(ABC):
    @abstractmethod
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        cancel_event=None,
    ) -> AsyncIterator[str]:
        raise NotImplementedError

    async def complete_structured(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        json_schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
    ) -> StructuredCompletion:
        """Portable fallback for providers without native structured output.

        Native provider implementations should override this method. The fallback
        still validates JSON locally; it does not trust a model's prose.
        """

        import json

        from ..intent_ir import parse_json_object

        instruction = {
            "task": "Return exactly one JSON object matching the schema. No Markdown.",
            "schema_name": schema_name,
            "json_schema": json_schema,
        }
        prompted = [
            {
                "role": "system",
                "content": json.dumps(instruction, ensure_ascii=False, separators=(",", ":")),
            },
            *messages,
        ]
        raw = "".join([chunk async for chunk in self.stream(prompted, model=model)])
        return StructuredCompletion(
            data=parse_json_object(raw),
            raw_text=raw,
            usage=TokenUsage(
                input_tokens=estimate_messages_tokens(prompted),
                output_tokens=estimate_text_tokens(raw),
                estimated=True,
            ),
        )


@dataclass(slots=True)
class ProviderBundle:
    provider: ChatProvider
    sensitive_values: list[str]
