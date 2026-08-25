"""Provider adapter for the canonical Subactor Founder conversation API."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from ..token_budget import TokenUsage, estimate_messages_tokens, estimate_text_tokens
from .base import ChatProvider, ProviderError


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENSITIVE_QUERY_KEY = re.compile(r"(?:token|secret|password|api[_-]?key|authorization)", re.IGNORECASE)
_MAX_MESSAGE_CHARS = 2_000
_MAX_HISTORY_ITEMS = 8


def _bounded_text(value: Any, limit: int = _MAX_MESSAGE_CHARS) -> str:
    return _CONTROL_CHARS.sub("", str(value or "")).strip()[:limit]


def _safe_http_url(value: Any) -> str | None:
    candidate = _bounded_text(value, 2_048)
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if any(_SENSITIVE_QUERY_KEY.search(key) for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
        return None
    return candidate


class SubactorControlProvider(ChatProvider):
    """Stream grounded Founder answers from Subactor Control."""

    def __init__(
        self,
        *,
        base_url: str,
        endpoint: str,
        api_key: str,
        timeout_seconds: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProviderError("Nieprawidłowy adres Subactor Control")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderError("Adres Subactor Control nie może zawierać poświadczeń ani parametrów")
        if not endpoint.startswith("/"):
            raise ProviderError("Endpoint Subactor Control musi być ścieżką bezwzględną")
        if not api_key:
            raise ProviderError("Brak tokenu Subactor Control")
        self._base_url = base_url.rstrip("/")
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self.last_usage: TokenUsage | None = None

    @staticmethod
    def _request_payload(messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
        conversation: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "")
            content = _bounded_text(message.get("content"))
            if role in {"user", "assistant"} and content:
                conversation.append({"role": role, "content": content})
        conversation = conversation[-(_MAX_HISTORY_ITEMS + 1) :]
        user_index = next(
            (index for index in range(len(conversation) - 1, -1, -1) if conversation[index]["role"] == "user"),
            -1,
        )
        if user_index < 0:
            raise ProviderError("Brak wiadomości użytkownika dla Subactor Control")
        payload: dict[str, Any] = {
            "surface": "founder_autonomy",
            "text": conversation[user_index]["content"],
            "history": conversation[max(0, user_index - _MAX_HISTORY_ITEMS) : user_index],
        }
        preferred_model = _bounded_text(model, 120)
        if preferred_model and preferred_model not in {"control", "mock"}:
            payload["preferred_model"] = preferred_model
        return payload

    @staticmethod
    def _format_response(body: dict[str, Any]) -> str:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        summary = ""
        for value in (
            data.get("summary"),
            body.get("summary"),
            data.get("answer"),
            body.get("answer"),
            body.get("message"),
        ):
            summary = _bounded_text(value, 20_000)
            if summary:
                break
        if not summary:
            raise ProviderError("Subactor Control nie zwrócił odpowiedzi")

        lines = [summary]
        actions = body.get("actions") if isinstance(body.get("actions"), list) else data.get("actions", [])
        for action in (actions[:8] if isinstance(actions, list) else []):
            if not isinstance(action, dict):
                continue
            url = _safe_http_url(action.get("url"))
            if not url:
                continue
            label = _bounded_text(action.get("label") or action.get("title") or "Otwórz", 160)
            lines.append(f"  → {label}: {url}")

        diagnostics = body.get("diagnostics") if isinstance(body.get("diagnostics"), dict) else data.get("diagnostics")
        if isinstance(diagnostics, dict):
            observed = _bounded_text(diagnostics.get("observedLocal") or diagnostics.get("observed_at"), 120)
            duration = diagnostics.get("durationMs") or diagnostics.get("duration_ms")
            correlation = _bounded_text(diagnostics.get("correlationId") or diagnostics.get("correlation_id"), 160)
            details = [
                value
                for value in (
                    observed,
                    f"{duration} ms" if isinstance(duration, (int, float)) else "",
                    f"cid: {correlation}" if correlation else "",
                )
                if value
            ]
            if details:
                lines.append(f"\n  [diagnostyka] {' | '.join(details)}")
            markdown_url = _safe_http_url(
                diagnostics.get("markdownDownloadUrl") or diagnostics.get("markdown_url")
            )
            if markdown_url:
                lines.append(f"  markdown (uwierzytelnienie wymagane): {markdown_url}")
        return "\n".join(lines)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        cancel_event=None,
    ) -> AsyncIterator[str]:
        payload = self._request_payload(messages, model)
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Subactor Control jest niedostępny ({exc.__class__.__name__})") from exc

        if response.status_code >= 400:
            code = "request_failed"
            try:
                problem = response.json()
                if isinstance(problem, dict):
                    code = _bounded_text(problem.get("code") or problem.get("type") or code, 160)
            except ValueError:
                pass
            raise ProviderError(f"Subactor Control odrzucił żądanie: HTTP {response.status_code} ({code})")

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("Subactor Control zwrócił nieprawidłowy JSON") from exc
        if not isinstance(body, dict) or body.get("ok") is False:
            raise ProviderError("Subactor Control nie wykonał żądania")

        answer = self._format_response(body)
        self.last_usage = TokenUsage(
            input_tokens=estimate_messages_tokens(messages),
            output_tokens=estimate_text_tokens(answer),
            estimated=True,
        )
        for offset in range(0, len(answer), 256):
            if cancel_event is not None and cancel_event.is_set():
                return
            yield answer[offset : offset + 256]
            await asyncio.sleep(0)
