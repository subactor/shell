from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": max(0, int(self.input_tokens)),
            "cached_input_tokens": max(0, int(self.cached_input_tokens)),
            "output_tokens": max(0, int(self.output_tokens)),
            "estimated": bool(self.estimated),
        }

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            estimated=self.estimated or other.estimated,
        )


def estimate_text_tokens(text: str) -> int:
    """Tokenizer-independent estimate used only when an API returns no usage."""

    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.6))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 2
    for item in messages:
        total += 4
        total += estimate_text_tokens(str(item.get("role", "")))
        total += estimate_text_tokens(str(item.get("content", "")))
    return total
