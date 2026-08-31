import pytest

from subactor_shell.interaction import (
    NavigationPolicy,
    SurfaceInput,
    SurfaceInputKind,
    TerminalInteractionEngine,
)
from subactor_shell.surface import DEFAULT_COMMAND_REGISTRY


@pytest.fixture
def engine() -> TerminalInteractionEngine:
    return TerminalInteractionEngine(DEFAULT_COMMAND_REGISTRY)


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_empty_input_is_explicit(engine: TerminalInteractionEngine, value: str):
    assert engine.interpret(value) == SurfaceInput(SurfaceInputKind.EMPTY, value, "")


@pytest.mark.parametrize("value", ["q", "quit", "exit", "/q", "/quit", "/exit"])
def test_exit_inputs_are_resolved_before_dispatch(engine: TerminalInteractionEngine, value: str):
    event = engine.interpret(value)

    assert event.kind is SurfaceInputKind.EXIT


@pytest.mark.parametrize("value", ["0", "b", "BACK", " esc "])
def test_back_navigation_is_policy_driven(engine: TerminalInteractionEngine, value: str):
    event = engine.interpret(value)

    assert event.kind is SurfaceInputKind.BACK


def test_letter_shortcut_becomes_the_registered_command(engine: TerminalInteractionEngine):
    assert engine.interpret(" s ") == SurfaceInput(
        SurfaceInputKind.COMMAND,
        " s ",
        "/status",
    )


def test_slash_command_is_not_rewritten(engine: TerminalInteractionEngine):
    assert engine.interpret("/projects recon").value == "/projects recon"
    assert engine.interpret("/projects recon").kind is SurfaceInputKind.COMMAND


def test_natural_language_remains_a_message(engine: TerminalInteractionEngine):
    event = engine.interpret("  pokaż stan systemu  ")

    assert event == SurfaceInput(
        SurfaceInputKind.MESSAGE,
        "  pokaż stan systemu  ",
        "pokaż stan systemu",
    )


def test_navigation_policy_can_be_replaced_without_repl_changes():
    engine = TerminalInteractionEngine(
        DEFAULT_COMMAND_REGISTRY,
        navigation=NavigationPolicy(back_inputs=frozenset({"wstecz"})),
    )

    assert engine.interpret("wstecz").kind is SurfaceInputKind.BACK
    assert engine.interpret("back").kind is SurfaceInputKind.MESSAGE
