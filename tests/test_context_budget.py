import asyncio
from pathlib import Path
from typing import Any

from subactor_shell.chat import ChatService
from subactor_shell.config import load_config
from subactor_shell.providers.base import ChatProvider, ProviderBundle
from subactor_shell.store import Store


class CapturingProvider(ChatProvider):
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    async def stream(self, messages, *, model, cancel_event=None):
        self.messages = messages
        yield "ok"


class Resolver:
    def resolve(self, reference):
        raise AssertionError("not used")


def test_full_transcript_is_persisted_but_only_recent_bounded_context_is_sent(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[defaults]
provider = "test"
model = "m"
[context]
recent_messages = 4
max_history_chars = 700
max_message_chars = 260
max_route_context_chars = 512
[providers.test]
kind = "mock"
model = "m"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    provider = CapturingProvider()
    chat = ChatService(
        config,
        store,
        resolver=Resolver(),  # type: ignore[arg-type]
        provider_builder=lambda profile, resolver: ProviderBundle(provider, []),
    )
    session = chat.new_session()
    for index in range(12):
        store.add_message(session.id, "user", f"old-{index}-" + "x" * 220)
        store.add_message(session.id, "assistant", f"answer-{index}-" + "y" * 220)

    assert asyncio.run(chat.complete_message(session.id, "now solve an unrelated puzzle")) == "ok"
    sent = "\n".join(str(item["content"]) for item in provider.messages)
    assert "old-0-" not in sent
    assert "answer-11-" in sent
    assert provider.messages[-1]["content"] == "now solve an unrelated puzzle"
    assert len(sent) < 1800
    # SQLite nadal zawiera pełny transcript i nową turę.
    assert len(store.list_messages(session.id)) == 26
    assert store.usage_summary(session.id)["input_tokens"] > 0
