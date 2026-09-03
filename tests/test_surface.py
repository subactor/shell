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
    assert "/login <email>" in help_text
    assert "/auth" in help_text


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


def test_command_aliases_resolve_to_the_canonical_domain_handler():
    assert DEFAULT_COMMAND_REGISTRY.handler_for("/prs") == "fleet"
    assert DEFAULT_COMMAND_REGISTRY.handler_for("/pr") == "fleet"
    assert DEFAULT_COMMAND_REGISTRY.handler_for("/performance") == "operations"
    assert DEFAULT_COMMAND_REGISTRY.handler_for("/perf") == "operations"
    assert DEFAULT_COMMAND_REGISTRY.handler_for("/supervisor") == "supervisor"
    assert DEFAULT_COMMAND_REGISTRY.handler_for("/sup") == "supervisor"


def test_registry_and_repl_handler_sets_are_complete():
    available = {
        "auth",
        "clear",
        "control",
        "data",
        "export",
        "fleet",
        "help",
        "operations",
        "orchestration",
        "session",
        "status",
        "supervisor",
        "vault",
    }

    assert DEFAULT_COMMAND_REGISTRY.validate_handlers(available) == ()
    assert DEFAULT_COMMAND_REGISTRY.validate_handlers(available - {"auth"}) == (
        "Brak handlera REPL: auth",
    )
