from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..intent_ir import parse_json_object
from ..token_budget import TokenUsage, estimate_messages_tokens, estimate_text_tokens
from .base import ChatProvider, ProviderError, StructuredCompletion


class OpenAICompatProvider(ChatProvider):
    def __init__(
        self,
        *,
        base_url: str,
        endpoint: str,
        api_key: str,
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        structured_mode: str = "auto",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url providera musi być adresem http:// lub https://")
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint or "/chat/completions"
        if not self.endpoint.startswith("/"):
            self.endpoint = "/" + self.endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.extra_headers = extra_headers or {}
        self.structured_mode = structured_mode.strip().lower() or "auto"
        self.transport = transport
        self.last_usage = TokenUsage()

    def _headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            **self.extra_headers,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        cancel_event=None,
    ) -> AsyncIterator[str]:
        is_responses = self.endpoint.rstrip("/").endswith("/responses")
        if is_responses:
            payload: dict[str, Any] = {"model": model, "input": messages, "stream": True}
        else:
            payload = {
                "model": model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        output_parts: list[str] = []
        usage = TokenUsage(
            input_tokens=estimate_messages_tokens(messages),
            estimated=True,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST", self.endpoint, headers=self._headers(stream=True), json=payload
                ) as response:
                    if response.status_code >= 400:
                        request_id = response.headers.get("x-request-id", "")
                        suffix = f", request_id={request_id}" if request_id else ""
                        raise ProviderError(
                            f"Provider openai_compat zwrócił HTTP {response.status_code}{suffix}"
                        )
                    async for line in response.aiter_lines():
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        line = line.strip()
                        if not line or line.startswith(":") or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        event_usage = self._extract_usage(event, is_responses=is_responses)
                        if event_usage.input_tokens or event_usage.output_tokens:
                            usage = event_usage
                        text = self._extract_text(event, is_responses=is_responses)
                        if text:
                            output_parts.append(text)
                            yield text
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Błąd połączenia z providerem openai_compat ({type(exc).__name__})"
            ) from exc
        if usage.output_tokens <= 0:
            usage.output_tokens = estimate_text_tokens("".join(output_parts))
            usage.estimated = True
        self.last_usage = usage

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
        is_responses = self.endpoint.rstrip("/").endswith("/responses")
        mode = self.structured_mode
        if mode == "auto":
            mode = "responses_json_schema" if is_responses else "json_schema"

        if is_responses:
            payload: dict[str, Any] = {
                "model": model,
                "input": messages,
                "stream": False,
                "max_output_tokens": max_output_tokens,
            }
            if reasoning_effort:
                payload["reasoning"] = {"effort": reasoning_effort}
            if mode in {"responses_json_schema", "json_schema"}:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": json_schema,
                    }
                }
            elif mode == "json_object":
                payload["text"] = {"format": {"type": "json_object"}}
            else:
                payload["instructions"] = (
                    "Return exactly one JSON object matching this schema and no Markdown: "
                    + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
                )
        else:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "max_tokens": max_output_tokens,
            }
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort
            if mode == "json_schema":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": json_schema,
                    },
                }
            elif mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            else:
                payload["messages"] = [
                    {
                        "role": "system",
                        "content": (
                            "Return exactly one JSON object and no Markdown. JSON Schema: "
                            + json.dumps(json_schema, ensure_ascii=False, separators=(",", ":"))
                        ),
                    },
                    *messages,
                ]

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    self.endpoint,
                    headers=self._headers(stream=False),
                    json=payload,
                )
            if response.status_code >= 400:
                request_id = response.headers.get("x-request-id", "")
                suffix = f", request_id={request_id}" if request_id else ""
                raise ProviderError(
                    f"Provider openai_compat zwrócił HTTP {response.status_code}{suffix}"
                )
            body = response.json()
            raw_text = self._extract_complete_text(body, is_responses=is_responses)
            usage = self._extract_usage(body, is_responses=is_responses)
            if usage.input_tokens <= 0:
                usage.input_tokens = estimate_messages_tokens(messages)
                usage.estimated = True
            if usage.output_tokens <= 0:
                usage.output_tokens = estimate_text_tokens(raw_text)
                usage.estimated = True
            self.last_usage = usage
            return StructuredCompletion(
                data=parse_json_object(raw_text),
                raw_text=raw_text,
                usage=usage,
                request_id=response.headers.get("x-request-id", "") or str(body.get("id", "")),
            )
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                f"Błąd structured output providera openai_compat ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _extract_text(event: dict[str, Any], *, is_responses: bool) -> str:
        if is_responses:
            if event.get("type") == "response.output_text.delta":
                delta = event.get("delta", "")
                return delta if isinstance(delta, str) else ""
            return ""
        try:
            content = event["choices"][0]["delta"].get("content", "")
        except (KeyError, IndexError, TypeError):
            return ""
        return OpenAICompatProvider._content_to_text(content)

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    @staticmethod
    def _extract_complete_text(body: dict[str, Any], *, is_responses: bool) -> str:
        if is_responses:
            output_text = body.get("output_text")
            if isinstance(output_text, str):
                return output_text
            parts: list[str] = []
            outputs = body.get("output", [])
            for output in outputs if isinstance(outputs, list) else []:
                if not isinstance(output, dict):
                    continue
                contents = output.get("content", [])
                for content in contents if isinstance(contents, list) else []:
                    if not isinstance(content, dict):
                        continue
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        choices = body.get("choices", [])
        if not isinstance(choices, list) or not choices:
            raise ProviderError("Provider nie zwrócił choices")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message", {}) if isinstance(first, dict) else {}
        if not isinstance(message, dict):
            return ""
        return OpenAICompatProvider._content_to_text(message.get("content", ""))

    @staticmethod
    def _extract_usage(body: dict[str, Any], *, is_responses: bool) -> TokenUsage:
        usage = body.get("usage", {})
        if not isinstance(usage, dict) or not usage:
            return TokenUsage()
        if is_responses:
            details = usage.get("input_tokens_details", {})
            cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
            return TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                cached_input_tokens=int(cached or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
            )
        details = usage.get("prompt_tokens_details", {})
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        return TokenUsage(
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            cached_input_tokens=int(cached or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
