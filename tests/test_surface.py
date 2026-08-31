from datetime import datetime
from pathlib import Path

import pytest

from subactor_shell.surface import (
    DEFAULT_COMMAND_REGISTRY,
    CommandRegistry,
    CommandSpec,
    render_prompt,
)


def test_shortcuts_and_help_come_from_the_same_registry():
    expected = {
        "s": "/status",
        "t": "/prs",
        "m": "/model",
        "p": "/provider",
        "f": "/fleet",
        "d": "/doctor",
        "h": "/help",
        "c": "/clear",
        "q": "/q",
    }

    help_text = DEFAULT_COMMAND_REGISTRY.render_help()

    for shortcut, command in expected.items():
        assert DEFAULT_COMMAND_REGISTRY.resolve_shortcut(shortcut) == command
        assert command in help_text


def test_exit_policy_is_declared_by_the_command_registry():
    for value in ("/q", "/quit", "/exit", "q", "quit", "exit"):
        assert DEFAULT_COMMAND_REGISTRY.is_exit(value)
    assert not DEFAULT_COMMAND_REGISTRY.is_exit("/export")


def test_prompt_rendering_is_deterministic_and_keeps_context():
    prompt = render_prompt(
        attachment_count=2,
        menu="tickets",
        colored=False,
        now=datetime(2026, 8, 30, 20, 36),
        username="tom",
        cwd=Path("/home/tom/github/subactor/shell"),
        home=Path("/home/tom"),
    )

    assert prompt == "⚡subactor/tom/github/subactor/shell/20:36/tickets +2 plik> "


def test_unknown_input_is_not_rewritten():
    assert DEFAULT_COMMAND_REGISTRY.resolve_shortcut("napraw status") == "napraw status"


def test_registry_rejects_duplicate_shortcuts_before_rendering_them():
    commands = (
        CommandSpec("/status", "", "status", "Control", shortcut="s"),
        CommandSpec("/sessions", "", "sessions", "Chat", shortcut="s"),
    )

    with pytest.raises(ValueError, match="Powielony skrót"):
        CommandRegistry(commands)
