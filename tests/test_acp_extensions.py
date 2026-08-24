import asyncio
from pathlib import Path

from subactor_shell.acp_agent import AcpAgent
from subactor_shell.chat import ChatService
from subactor_shell.config import load_config
from subactor_shell.providers.base import ChatProvider, ProviderBundle
from subactor_shell.store import Store


class MustNotRunProvider(ChatProvider):
    async def stream(self, messages, *, model, cancel_event=None):
        raise AssertionError("ACP deterministic route must not call the LLM")
        yield ""  # pragma: no cover


class Resolver:
    def resolve(self, reference):
        raise AssertionError("not used")


def test_acp_exposes_catalog_routes_metrics_plans_and_receipts(tmp_path: Path):
    async def run() -> None:
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
            provider_builder=lambda profile, resolver: ProviderBundle(
                MustNotRunProvider(), []
            ),
        )
        agent = AcpAgent(chat)
        initialized = await agent._dispatch("initialize", {"protocolVersion": 1})
        meta = initialized["agentCapabilities"]["_meta"]
        assert meta["com.subactor.intentIR"] == "v1"
        assert meta["com.subactor.executionPlans"] is True
        assert meta["com.subactor.namedConnectors"] is True

        created = await agent._dispatch("session/new", {"cwd": str(tmp_path)})
        session_id = created["sessionId"]
        await agent._dispatch(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": "pokaż sesje"}],
            },
        )

        catalog = await agent._dispatch("subactor/catalog/list", {})
        assert any(item["id"] == "session.list" for item in catalog["intents"])
        connectors = await agent._dispatch("subactor/connectors/list", {})
        assert any(item["name"] == "builtin" for item in connectors["connectors"])
        route = await agent._dispatch("subactor/route/get", {"sessionId": session_id})
        assert route["route"]["route"] == "deterministic"
        metrics = await agent._dispatch("subactor/metrics/get", {"sessionId": session_id})
        assert metrics["metrics"]["calls"] == 0
        plans = await agent._dispatch("subactor/plan/list", {"sessionId": session_id})
        assert len(plans["plans"]) == 1
        plan_id = plans["plans"][0]["id"]
        fetched_plan = await agent._dispatch("subactor/plan/get", {"planId": plan_id})
        assert fetched_plan["plan"]["intent_id"] == "session.list"
        receipts = await agent._dispatch(
            "subactor/receipt/list", {"sessionId": session_id}
        )
        assert len(receipts["receipts"]) == 1
        receipt_id = receipts["receipts"][0]["id"]
        fetched_receipt = await agent._dispatch(
            "subactor/receipt/get", {"receiptId": receipt_id}
        )
        assert fetched_receipt["receipt"]["ok"] is True

    asyncio.run(run())
