import asyncio
from pathlib import Path
from typing import Any

from subactor_shell.acp_agent import AcpAgent
from subactor_shell.chat import ChatService
from subactor_shell.config import load_config
from subactor_shell.providers.base import ChatProvider, ProviderBundle
from subactor_shell.store import Store


class Provider(ChatProvider):
    async def stream(self, messages, *, model, cancel_event=None):
        yield "ACP "
        yield "works"


class Resolver:
    def resolve(self, reference):
        raise AssertionError("not used")


def test_acp_initialize_new_prompt_and_updates(tmp_path: Path):
    async def run():
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            """
[defaults]
provider = "mock"
model = "mock"
[providers.mock]
kind = "mock"
model = "mock"
""",
            encoding="utf-8",
        )
        config = load_config(config_path, tmp_path / "data")
        store = Store(config.data_dir / "state.sqlite3")
        chat = ChatService(
            config,
            store,
            resolver=Resolver(),  # type: ignore[arg-type]
            provider_builder=lambda profile, resolver: ProviderBundle(Provider(), []),
        )
        chat.bind_secret("TOKEN", "vault://secret/app#token")
        chat.grant_secret("TOKEN")
        agent = AcpAgent(chat)
        updates: list[dict[str, Any]] = []

        async def capture(session_id, update):
            updates.append({"sessionId": session_id, "update": update})

        agent._notify_update = capture  # type: ignore[method-assign]
        initialized = await agent._dispatch("initialize", {"protocolVersion": 1})
        assert initialized["protocolVersion"] == 1
        assert initialized["agentCapabilities"]["loadSession"] is True

        created = await agent._dispatch(
            "session/new", {"cwd": str(tmp_path), "mcpServers": []}
        )
        result = await agent._dispatch(
            "session/prompt",
            {
                "sessionId": created["sessionId"],
                "prompt": [
                    {"type": "text", "text": "hello"},
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "file:///tmp/a.txt",
                            "mimeType": "text/plain",
                            "text": "context {{secret:TOKEN}}",
                        },
                    },
                ],
            },
        )
        assert result == {"stopReason": "end_turn"}
        assert "".join(item["update"]["content"]["text"] for item in updates) == "ACP works"
        messages = store.list_messages(created["sessionId"])
        assert messages[0].role == "user"
        assert "<resource" in messages[0].display_content
        assert "{{secret:TOKEN}}" in messages[0].context_content
        assert chat.grants.list() == ["TOKEN"]
        assert messages[1].display_content == "ACP works"

    asyncio.run(run())
