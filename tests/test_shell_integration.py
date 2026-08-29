import asyncio
from pathlib import Path

import pytest

from subactor_shell.catalog import builtin_intents
from subactor_shell.config import load_config
from subactor_shell.connectors import ConnectorExecutor, ConnectorRegistry
from subactor_shell.repl import is_exit_command
from subactor_shell.store import Store


class UnusedResolver:
    def resolve(self, reference: str) -> str:
        raise AssertionError(f"unexpected secret resolution: {reference}")


@pytest.mark.parametrize("command", ["/q", "/quit", "/exit", "q", "quit", "exit"])
def test_repl_exit_aliases(command: str):
    assert is_exit_command(command)


def test_open_tasks_phrase_routes_to_governed_subactor_cli():
    status = next(item for item in builtin_intents() if item.id == "control.status")
    assert "jakie zadania sa otwarte" in status.phrases
    assert status.execution == {
        "kind": "connector",
        "connector": "subactor_cli",
        "operation": "cli.status",
        "effect": "read",
    }


def test_subactor_status_connector_uses_fixed_argv(tmp_path: Path):
    executable = tmp_path / "subactor"
    executable.write_text("#!/bin/sh\nprintf 'services=15/15 autonomy_ready=false\\n'\n", encoding="utf-8")
    executable.chmod(0o700)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[control]\ncli_path = "{executable}"\ntimeout_seconds = 2\n',
        encoding="utf-8",
    )
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    registry = ConnectorRegistry(config)
    executor = ConnectorExecutor(config, store, UnusedResolver(), registry)  # type: ignore[arg-type]

    result = asyncio.run(executor._subactor_cli("cli.status"))

    assert result["source"] == "subactor-cli"
    assert "operation=cli.status exit=0" in result["message"]
    assert "services=15/15 autonomy_ready=false" in result["message"]


def test_one_command_json_output(tmp_path: Path):
    import argparse
    import json
    from io import StringIO
    from rich.console import Console
    from subactor_shell.app import _one
    from subactor_shell.chat import ChatService

    config_path = tmp_path / "config.toml"
    config_path.write_text('[defaults]\nprovider = "mock"\nmodel = "mock"\n', encoding="utf-8")
    config = load_config(config_path, tmp_path / "data")
    store = Store(config.data_dir / "state.sqlite3")
    chat = ChatService(config, store)

    out = StringIO()
    console = Console(file=out, no_color=True, width=120)
    args = argparse.Namespace(
        session=None,
        provider="mock",
        model="mock",
        grant=[],
        message="pokaż sesje",
        attach=[],
        json=True,
    )
    code = asyncio.run(_one(chat, args, console))
    assert code == 0
    raw = out.getvalue().strip()
    data = json.loads(raw)
    assert "session_id" in data
    assert "route" in data
    assert data["route"]["intent_id"] == "session.list"
    assert "receipt" in data
    assert data["receipt"]["ok"] is True
