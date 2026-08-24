from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .store import Store
from .token_budget import estimate_messages_tokens


_REF_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)


@dataclass(slots=True)
class WorkingState:
    goal: str = ""
    active_intent: str = ""
    active_refs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    last_receipt_ref: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "WorkingState":
        payload = payload or {}
        return cls(
            goal=str(payload.get("goal", "")),
            active_intent=str(payload.get("active_intent", "")),
            active_refs=[str(item) for item in payload.get("active_refs", []) if isinstance(item, str)],
            constraints=[str(item) for item in payload.get("constraints", []) if isinstance(item, str)],
            open_questions=[str(item) for item in payload.get("open_questions", []) if isinstance(item, str)],
            last_receipt_ref=str(payload.get("last_receipt_ref", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "active_intent": self.active_intent,
            "active_refs": self.active_refs,
            "constraints": self.constraints,
            "open_questions": self.open_questions,
            "last_receipt_ref": self.last_receipt_ref,
        }


@dataclass(slots=True)
class ContextBuildResult:
    messages: list[dict[str, Any]]
    included_history_messages: int
    history_chars: int
    estimated_input_tokens: int


class ContextBuilder:
    def __init__(self, store: Store, settings: dict[str, Any]):
        self.store = store
        self.recent_messages = max(0, int(settings.get("recent_messages", 6)))
        self.max_history_chars = max(0, int(settings.get("max_history_chars", 12_000)))
        self.max_message_chars = max(256, int(settings.get("max_message_chars", 4_000)))
        self.max_route_context_chars = max(512, int(settings.get("max_route_context_chars", 4_000)))

    def build(
        self,
        session_id: str,
        current_user_content: str,
        *,
        route_context: dict[str, Any] | None = None,
    ) -> ContextBuildResult:
        messages: list[dict[str, Any]] = []
        state = WorkingState.from_dict(self.store.get_session_state(session_id))
        system_parts: list[str] = []
        if any(state.to_dict().values()):
            system_parts.append(
                "Subactor WorkingState (compact conversation state; never bypass local policy):\n"
                + json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
            )
        if route_context:
            encoded = json.dumps(route_context, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > self.max_route_context_chars:
                encoded = encoded[: self.max_route_context_chars] + "…"
            system_parts.append(
                "Routing context. Treat as a hint. Do not invent connector calls or secret values:\n"
                + encoded
            )
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        recent = self.store.list_messages_recent(session_id, limit=self.recent_messages)
        selected_reversed: list[dict[str, str]] = []
        used = 0
        for message in reversed(recent):
            content = message.context_content
            if len(content) > self.max_message_chars:
                half = max(64, self.max_message_chars // 2 - 24)
                content = content[:half] + "\n[…history compacted…]\n" + content[-half:]
            remaining = self.max_history_chars - used
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[-remaining:]
            selected_reversed.append({"role": message.role, "content": content})
            used += len(content)
        selected = list(reversed(selected_reversed))
        messages.extend(selected)
        messages.append({"role": "user", "content": current_user_content})
        return ContextBuildResult(
            messages=messages,
            included_history_messages=len(selected),
            history_chars=used,
            estimated_input_tokens=estimate_messages_tokens(messages),
        )

    @staticmethod
    def compact_blocks(blocks: list[str], *, total_limit: int) -> list[str]:
        result: list[str] = []
        used = 0
        for block in blocks:
            remaining = total_limit - used
            if remaining <= 0:
                break
            if len(block) > remaining:
                block = block[: max(0, remaining - 29)] + "\n[EMBEDDED_CONTEXT_TRUNCATED]"
            result.append(block)
            used += len(block)
        return result

    def update_state(
        self,
        session_id: str,
        *,
        user_text: str,
        intent_id: str = "",
        constraints: list[str] | None = None,
        unresolved: list[str] | None = None,
        receipt_id: str = "",
    ) -> WorkingState:
        state = WorkingState.from_dict(self.store.get_session_state(session_id))
        compact_goal = " ".join(user_text.split())[:600]
        if compact_goal:
            state.goal = compact_goal
        if intent_id:
            state.active_intent = intent_id
        refs = list(dict.fromkeys([*state.active_refs, *_REF_RE.findall(user_text)]))
        state.active_refs = refs[-16:]
        if constraints is not None:
            state.constraints = list(dict.fromkeys([*state.constraints, *constraints]))[-16:]
        if unresolved is not None:
            state.open_questions = list(dict.fromkeys(unresolved))[:12]
        if receipt_id:
            state.last_receipt_ref = f"receipt://{receipt_id}"
        self.store.set_session_state(session_id, state.to_dict())
        return state
