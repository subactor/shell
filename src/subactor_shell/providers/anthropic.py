from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import ChatProvider, ProviderError


class AnthropicProvider(ChatProvider):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        max_tokens: int,
        anthropic_version: str,
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url providera musi być adresem http:// lub https://")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.anthropic_version = anthropic_version
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.transport = transport

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        cancel_event=None,
    ) -> AsyncIterator[str]:
        system_parts: list[str] = []
        api_messages: list[dict[str, str]] = []
        for item in messages:
            role = str(item.get("role", "user"))
            content = str(item.get("content", ""))
            if role == "system":
                system_parts.append(content)
            elif role in {"user", "assistant"}:
                api_messages.append({"role": role, "content": content})
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = {
            **self.extra_headers,
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                async with client.stream("POST", "/messages", headers=headers, json=payload) as response:
                    if response.status_code >= 400:
                        request_id = response.headers.get("request-id", "")
                        suffix = f", request_id={request_id}" if request_id else ""
                        raise ProviderError(
                            f"Provider anthropic zwrócił HTTP {response.status_code}{suffix}"
                        )
                    async for line in response.aiter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        line = line.strip()
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            text = delta.get("text", "") if isinstance(delta, dict) else ""
                            if isinstance(text, str) and text:
                                yield text
                        elif event.get("type") == "error":
                            raise ProviderError("Provider anthropic przerwał strumień błędem")
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Błąd połączenia z providerem anthropic ({type(exc).__name__})"
            ) from exc
