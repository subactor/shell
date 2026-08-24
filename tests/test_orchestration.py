import asyncio
import json
import sys
from pathlib import Path

import pytest

from subactor_shell.chat import ChatService
from subactor_shell.config import load_config
from subactor_shell.orchestration import OrchestrationError
from subactor_shell.providers.base import ChatProvider, ProviderBundle
from subactor_shell.store import Store


class MustNotRunProvider(ChatProvider):
    async def stream(self, messages, *, model, cancel_event=None):
        raise AssertionError("deterministic route must not call LLM")
        yield ""  # pragma: no cover


class Resolver:
    def resolve(self, reference):
        raise AssertionError("not used")


def test_deterministic_read_bypasses_llm_and_records_receipt(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[defaults]
provider = "test"
model = "m"
[providers.test]
kind = "mock"
model = "m"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    chat = ChatService(
        config,
        store,
        resolver=Resolver(),  # type: ignore[arg-type]
        provider_builder=lambda profile, resolver: ProviderBundle(MustNotRunProvider(), []),
    )
    session = chat.new_session(name="A")
    answer = asyncio.run(chat.complete_message(session.id, "pokaż sesje"))
    assert "builtin:session.list" in answer
    assert store.last_routing_decision(session.id)["route"] == "deterministic"  # type: ignore[index]
    assert store.usage_summary(session.id)["calls"] == 0
    assert store.list_execution_receipts(session.id)[0]["ok"] is True


def test_external_write_is_compiled_to_named_process_connector_and_requires_apply(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("SUBACTOR_TEST_SHOULD_NOT_LEAK", "sensitive-parent-value")
    connector_script = tmp_path / "connector.py"
    connector_script.write_text(
        "import json,os,sys\np=json.load(sys.stdin)\n"
        "print(json.dumps({'operation':p['operation'],'value':p['args']['value'],"
        "'parent_env_leaked':bool(os.getenv('SUBACTOR_TEST_SHOULD_NOT_LEAK'))}))\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "intents.json"
    catalog.write_text(
        json.dumps(
            {
                "intents": [
                    {
                        "id": "demo.apply",
                        "description": "Apply a demo value",
                        "phrases": ["ustaw demo {value}"],
                        "required_args": ["value"],
                        "execution": {
                            "kind": "connector",
                            "connector": "demo",
                            "operation": "demo.apply",
                            "effect": "external_write",
                            "argument_map": {"value": "$args.value"},
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
provider = "mock"
model = "mock"
[orchestration]
intent_catalog_paths = ["{catalog}"]
[providers.mock]
kind = "mock"
model = "mock"
[connectors.demo]
kind = "process"
command = ["{sys.executable}", "{connector_script}"]
allowed_operations = ["demo.apply"]
effect = "external_write"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    chat = ChatService(config, store)
    session = chat.new_session()

    answer = asyncio.run(chat.complete_message(session.id, "ustaw demo alpha"))
    assert "pending_approval" in answer
    plan = store.list_execution_plans(session.id)[0]
    assert plan["steps"][0]["connector"] == "demo"
    assert plan["steps"][0]["args"] == {"value": "alpha"}

    with pytest.raises(OrchestrationError, match="EXECUTE"):
        asyncio.run(chat.orchestration.apply_plan(plan["id"], confirmation="no"))
    receipt = asyncio.run(
        chat.orchestration.apply_plan(plan["id"], confirmation="EXECUTE")
    )
    assert receipt.ok is True
    assert receipt.steps[0]["result"]["json"] == {
        "operation": "demo.apply",
        "value": "alpha",
        "parent_env_leaked": False,
    }
