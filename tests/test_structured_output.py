import asyncio
import json

import httpx

from subactor_shell.providers.openai_compat import OpenAICompatProvider


def test_openai_structured_output_uses_json_schema_and_captures_usage():
    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "id": "req_1",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "v": 1,
                                    "intent_id": "x",
                                    "mode": "execute",
                                    "args": {},
                                    "requirements": [],
                                    "constraints": [],
                                    "unresolved": [],
                                }
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 31,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            },
        )

    provider = OpenAICompatProvider(
        base_url="https://provider.test/v1",
        endpoint="/chat/completions",
        api_key="key",
        timeout_seconds=1,
        structured_mode="json_schema",
        transport=httpx.MockTransport(handler),
    )

    async def run():
        return await provider.complete_structured(
            [{"role": "user", "content": "x"}],
            model="m",
            json_schema={"type": "object"},
            schema_name="intent",
            max_output_tokens=100,
        )

    result = asyncio.run(run())
    assert result.data["intent_id"] == "x"
    assert result.usage.input_tokens == 120
    assert result.usage.cached_input_tokens == 80
    assert result.usage.output_tokens == 31
