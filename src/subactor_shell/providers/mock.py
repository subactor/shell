from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from .base import ChatProvider


class MockProvider(ChatProvider):
    """Offline provider useful for installation checks and tests."""

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        cancel_event=None,
    ) -> AsyncIterator[str]:
        last_user = next(
            (str(item.get("content", "")) for item in reversed(messages) if item.get("role") == "user"),
            "",
        )
        response = (
            "[mock] Odebrałem wiadomość w bezpiecznej sesji Subactor. "
            f"Model: {model}. Znaki wejścia: {len(last_user)}.\n\n{last_user}"
        )
        for index in range(0, len(response), 24):
            if cancel_event is not None and cancel_event.is_set():
                return
            await asyncio.sleep(0)
            yield response[index : index + 24]
