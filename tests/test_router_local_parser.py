import asyncio
import json
from pathlib import Path

from subactor_shell.chat import ChatService
from subactor_shell.config import load_config
from subactor_shell.providers.base import ChatProvider, ProviderBundle, StructuredCompletion
from subactor_shell.store import Store
from subactor_shell.token_budget import TokenUsage


class StructuredParser(ChatProvider):
    def __init__(self) -> None:
        self.structured_calls = 0
        self.stream_calls = 0

    async def stream(self, messages, *, model, cancel_event=None):
        self.stream_calls += 1
        raise AssertionError("validated local read route must not call chat streaming")
        yield ""  # pragma: no cover

    async def complete_structured(
        self,
        messages,
        *,
        model,
        json_schema,
        schema_name,
        max_output_tokens,
        reasoning_effort=None,
    ):
        self.structured_calls += 1
        assert schema_name == "subactor_intent_ir_v1"
        assert max_output_tokens <= 192
        assert json_schema["properties"]["intent_id"]["enum"] == ["demo.sessions"]
        return StructuredCompletion(
            data={
                "v": 1,
                "intent_id": "demo.sessions",
                "mode": "execute",
                "args": {"limit": 10},
                "requirements": [],
                "constraints": [],
                "unresolved": [],
            },
            raw_text='{"v":1,"intent_id":"demo.sessions"}',
            usage=TokenUsage(input_tokens=173, output_tokens=31),
            request_id="req_local_parser",
        )


class Resolver:
    def resolve(self, reference):
        raise AssertionError("not used")


def test_local_4b_parser_emits_intent_ir_then_builtin_executes(tmp_path: Path):
    catalog = tmp_path / "intents.json"
    catalog.write_text(
        json.dumps(
            {
                "intents": [
                    {
                        "id": "demo.sessions",
                        "description": "Pokaż robocze sesje użytkownika.",
                        "phrases": ["przejrzyj sesje robocze"],
                        "optional_args": ["limit"],
                        "defaults": {"limit": 10},
                        "execution": {
                            "kind": "builtin",
                            "operation": "session.list",
                            "effect": "read",
                            "argument_map": {"limit": "$args.limit"},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
[defaults]
provider = "parser"
model = "local-4b"
[orchestration]
intent_catalog_paths = ["{catalog}"]
local_parser_provider = "parser"
local_parser_model = "local-4b"
top_k = 1
min_candidate_score = 0.0
deterministic_threshold = 1.1
local_execute_threshold = 0.0
[providers.parser]
kind = "mock"
model = "local-4b"
input_cost_per_million = 0.0
output_cost_per_million = 0.0
""",
        encoding="utf-8",
    )
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    parser = StructuredParser()
    chat = ChatService(
        config,
        store,
        resolver=Resolver(),  # type: ignore[arg-type]
        provider_builder=lambda profile, resolver: ProviderBundle(parser, []),
    )
    session = chat.new_session(name="Parser")

    answer = asyncio.run(
        chat.complete_message(session.id, "czy możesz przejrzeć robocze sesje zapisane lokalnie")
    )

    assert "builtin:session.list" in answer
    assert parser.structured_calls == 1
    assert parser.stream_calls == 0
    decision = store.last_routing_decision(session.id)
    assert decision is not None
    assert decision["route"] == "local_4b"
    assert decision["intent_id"] == "demo.sessions"
    usage = store.usage_summary(session.id)
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 173
    assert usage["output_tokens"] == 31
