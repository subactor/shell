from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .surface import CommandRegistry


class SurfaceInputKind(str, Enum):
    EMPTY = "empty"
    EXIT = "exit"
    BACK = "back"
    COMMAND = "command"
    MESSAGE = "message"


@dataclass(frozen=True, slots=True)
class SurfaceInput:
    kind: SurfaceInputKind
    raw: str
    value: str


@dataclass(frozen=True, slots=True)
class NavigationPolicy:
    back_inputs: frozenset[str] = frozenset({"0", "b", "back", "esc"})

    def is_back(self, value: str) -> bool:
        return value.strip().lower() in self.back_inputs


class TerminalInteractionEngine:
    """Translate terminal input into an application-neutral surface event."""

    def __init__(
        self,
        command_registry: CommandRegistry,
        navigation: NavigationPolicy | None = None,
    ):
        self.command_registry = command_registry
        self.navigation = navigation or NavigationPolicy()

    def interpret(self, raw: str) -> SurfaceInput:
        value = str(raw or "").strip()
        if not value:
            return SurfaceInput(SurfaceInputKind.EMPTY, raw, "")
        if self.command_registry.is_exit(value):
            return SurfaceInput(SurfaceInputKind.EXIT, raw, value)
        if self.navigation.is_back(value):
            return SurfaceInput(SurfaceInputKind.BACK, raw, value)

        resolved = self.command_registry.resolve_shortcut(value)
        if self.command_registry.is_exit(resolved):
            return SurfaceInput(SurfaceInputKind.EXIT, raw, resolved)
        kind = SurfaceInputKind.COMMAND if resolved.startswith("/") else SurfaceInputKind.MESSAGE
        return SurfaceInput(kind, raw, resolved)
