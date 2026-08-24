import asyncio
from pathlib import Path
from typing import Any

import pytest

from subactor_shell.chat import ChatError, ChatService
from subactor_shell.config import load_config
from subactor_shell.providers.base import ChatProvider, ProviderBundle
from subactor_shell.store import Store


class FakeResolver:
    def resolve(self, reference: str) -> str:
        assert reference == "vault://secret/app#token"
        return "SUPER-SECRET-VALUE"


class CapturingProvider(ChatProvider):
    def __init__(self):
        self.messages: list[dict[str, Any]] = []

    async def stream(self, messages, *, model, cancel_event=None):
        self.messages = messages
        yield "answer SUPER-"
        yield "SECRET-VALUE and API-"
        yield "KEY"


def make_service(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[defaults]
provider = "test"
model = "model-x"

[providers.test]
kind = "mock"
model = "model-x"
""",
        encoding="utf-8",
    )
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    provider = CapturingProvider()

    def builder(profile, resolver):
        return ProviderBundle(provider=provider, sensitive_values=["API-KEY"])

    service = ChatService(
        config,
        store,
        resolver=FakeResolver(),  # type: ignore[arg-type]
        provider_builder=builder,
    )
    return service, provider


def test_secret_requires_grant_and_is_never_persisted(tmp_path: Path):
    service, provider = make_service(tmp_path)
    session = service.new_session()
    service.bind_secret("TOKEN", "vault://secret/app#token")

    with pytest.raises(ChatError, match="jednorazowego grantu"):
        asyncio.run(service.complete_message(session.id, "use {{secret:TOKEN}}"))
    assert service.store.list_messages(session.id) == []

    service.grant_secret("TOKEN")
    answer = asyncio.run(service.complete_message(session.id, "use {{secret:TOKEN}}"))
    assert answer == "answer [REDACTED] and [REDACTED]"
    assert provider.messages[-1]["content"] == "use SUPER-SECRET-VALUE"

    persisted = service.store.list_messages(session.id)
    assert persisted[0].display_content == "use {{secret:TOKEN}}"
    assert persisted[0].context_content == "use {{secret:TOKEN}}"
    assert "SUPER-SECRET-VALUE" not in "".join(message.context_content for message in persisted)
    assert "API-KEY" not in "".join(message.context_content for message in persisted)

    # Grant został zużyty.
    with pytest.raises(ChatError, match="jednorazowego grantu"):
        asyncio.run(service.complete_message(session.id, "again {{secret:TOKEN}}"))


def test_data_placeholder_is_expanded_and_saved_as_context(tmp_path: Path):
    service, provider = make_service(tmp_path)
    session = service.new_session()
    service.set_data_text("PROJECT", "subactor")
    asyncio.run(service.complete_message(session.id, "name={{data:PROJECT}}"))
    assert provider.messages[-1]["content"] == "name=subactor"
    persisted = service.store.list_messages(session.id)[0]
    assert persisted.display_content == "name={{data:PROJECT}}"
    assert persisted.context_content == "name=subactor"


def test_data_cannot_inject_secret_placeholder_or_consume_grant(tmp_path: Path):
    service, provider = make_service(tmp_path)
    session = service.new_session()
    service.bind_secret("TOKEN", "vault://secret/app#token")
    service.set_data_text("UNTRUSTED", "{{secret:TOKEN}}")
    service.grant_secret("TOKEN")

    asyncio.run(service.complete_message(session.id, "inspect {{data:UNTRUSTED}}"))
    assert provider.messages[-1]["content"] == "inspect {{secret:TOKEN}}"
    assert service.grants.list() == ["TOKEN"]
