import asyncio
import json

import httpx

from subactor_shell.providers.anthropic import AnthropicProvider
from subactor_shell.providers.openai_compat import OpenAICompatProvider


def collect(provider, messages, model="m"):
    async def run():
        return "".join([chunk async for chunk in provider.stream(messages, model=model)])

    return asyncio.run(run())


def test_openai_chat_completions_stream():
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer key"
        assert request.headers["x-extra"] == "ok"
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"] == "hello"
        body = (
            'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatProvider(
        base_url="https://provider.test/v1",
        endpoint="/chat/completions",
        api_key="key",
        timeout_seconds=1,
        extra_headers={"X-Extra": "ok", "Authorization": "must-not-win"},
        transport=httpx.MockTransport(handler),
    )
    assert collect(provider, [{"role": "user", "content": "hello"}]) == "AB"


def test_openai_responses_stream():
    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        assert payload["input"][0]["content"] == "hello"
        body = (
            'data: {"type":"response.output_text.delta","delta":"R1"}\n\n'
            'data: {"type":"response.output_text.delta","delta":"R2"}\n\n'
            'data: [DONE]\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = OpenAICompatProvider(
        base_url="https://provider.test/v1",
        endpoint="/responses",
        api_key="key",
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    assert collect(provider, [{"role": "user", "content": "hello"}]) == "R1R2"


def test_anthropic_messages_stream():
    def handler(request: httpx.Request):
        assert request.headers["x-api-key"] == "key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        payload = json.loads(request.content)
        assert payload["system"] == "system"
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        body = (
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"C"}}\n\n'
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"D"}}\n\n'
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    provider = AnthropicProvider(
        base_url="https://provider.test/v1",
        api_key="key",
        max_tokens=100,
        anthropic_version="2023-06-01",
        timeout_seconds=1,
        extra_headers={"x-api-key": "must-not-win"},
        transport=httpx.MockTransport(handler),
    )
    assert collect(
        provider,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
    ) == "CD"
