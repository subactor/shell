from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from prompt_toolkit.formatted_text import ANSI


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command: str
    arguments: str
    description: str
    section: str
    aliases: tuple[str, ...] = ()
    shortcut: str | None = None
    exits: bool = False

    @property
    def usage(self) -> str:
        return f"{self.command} {self.arguments}".rstrip()


class CommandRegistry:
    """Declarative terminal surface shared by help and input resolution."""

    def __init__(self, commands: tuple[CommandSpec, ...]):
        self.commands = commands
        self._shortcuts: dict[str, str] = {}
        for item in commands:
            if not item.shortcut:
                continue
            shortcut = item.shortcut.strip().lower()
            if len(shortcut) != 1:
                raise ValueError(f"Skrót musi być pojedynczym znakiem: {item.shortcut}")
            if shortcut in self._shortcuts:
                raise ValueError(f"Powielony skrót powierzchni terminala: {shortcut}")
            self._shortcuts[shortcut] = item.command
        self._exit_commands = frozenset(
            value.lower()
            for item in commands
            if item.exits
            for value in (item.command, *item.aliases)
        )

    def resolve_shortcut(self, value: str) -> str:
        normalized = value.strip().lower()
        return self._shortcuts.get(normalized, value)

    def is_exit(self, value: str) -> bool:
        return value.strip().lower() in self._exit_commands

    def render_help(self) -> str:
        sections: list[str] = []
        seen: set[str] = set()
        for item in self.commands:
            if item.section in seen:
                continue
            seen.add(item.section)
            rows = [entry for entry in self.commands if entry.section == item.section]
            width = max(len(entry.usage) for entry in rows) + 4
            body = "\n".join(
                f"  {entry.usage:<{width}}{entry.description}"
                for entry in rows
            )
            sections.append(f"[bold]{item.section}[/bold]\n{body}")
        return "\n\n".join(sections) + "\n"


COMMANDS = (
    CommandSpec("/new", "[nazwa]", "nowa sesja", "Rozmowa"),
    CommandSpec("/sessions", "[del ID|prune]", "lista, usunięcie lub czyszczenie starych sesji", "Rozmowa"),
    CommandSpec("/resume", "ID", "wznowienie sesji", "Rozmowa"),
    CommandSpec("/provider", "NAZWA", "zmiana profilu providera", "Rozmowa", shortcut="p"),
    CommandSpec("/model", "MODEL", "zmiana modelu w sesji", "Rozmowa", shortcut="m"),
    CommandSpec("/attach", "PLIK", "dołącz plik do następnej wiadomości", "Rozmowa"),
    CommandSpec("/info", "", "aktywna sesja", "Rozmowa"),
    CommandSpec("/prs", "", "otwarte Pull Requesty (subactor, if-uri)", "Flota i Autonomia", aliases=("/pr",), shortcut="t"),
    CommandSpec("/doctor", "", "zadania diagnostyczne i naprawcze", "Flota i Autonomia", shortcut="d"),
    CommandSpec("/fleet", "", "pełny podgląd ekosystemu (PR, zadania, usługi)", "Flota i Autonomia", shortcut="f"),
    CommandSpec("/data", "set NAZWA WARTOŚĆ", "zapisz jawne dane tekstowe", "Dane"),
    CommandSpec("/data", "put NAZWA PLIK", "zapisz plik jako artefakt", "Dane"),
    CommandSpec("/data", "list", "lista danych", "Dane"),
    CommandSpec("/data", "del NAZWA", "usuń dane", "Dane"),
    CommandSpec("W wiadomości użyj:", "{{data:NAZWA}}", "", "Dane"),
    CommandSpec("/vault", "bind ALIAS REF", "zapisz wyłącznie referencję vault://, env:// lub file://", "Sekrety"),
    CommandSpec("/vault", "put ALIAS VAULT_REF", "wczytaj wartość bez echa, zapisz do KV v2 i utwórz binding", "Sekrety"),
    CommandSpec("/vault", "grant ALIAS", "jednorazowo zezwól na {{secret:ALIAS}}", "Sekrety"),
    CommandSpec("/vault", "list", "lista aliasów i referencji (bez wartości)", "Sekrety"),
    CommandSpec("/vault", "unbind ALIAS", "usuń binding", "Sekrety"),
    CommandSpec("/vault", "wrap ALIAS [TTL]", "utwórz jednorazowy wrapping token Vault", "Sekrety"),
    CommandSpec("/plans", "", "lista planów sesji", "Orkiestracja i tokeny"),
    CommandSpec("/plan", "ID", "pokaż plan JSON", "Orkiestracja i tokeny"),
    CommandSpec("/apply", "ID", "zastosuj plan; zmiany wymagają EXECUTE", "Orkiestracja i tokeny"),
    CommandSpec("/receipts", "", "lista receipts sesji", "Orkiestracja i tokeny"),
    CommandSpec("/receipt", "ID", "pokaż receipt JSON", "Orkiestracja i tokeny"),
    CommandSpec("/route", "", "ostatnia decyzja routera", "Orkiestracja i tokeny"),
    CommandSpec("/metrics", "", "tokeny, koszt i udział fast path", "Orkiestracja i tokeny"),
    CommandSpec("/catalog", "", "lokalny katalog intentów", "Orkiestracja i tokeny"),
    CommandSpec("/connectors", "", "allowlista connectorów", "Orkiestracja i tokeny"),
    CommandSpec("/status", "", "wywołaj cli.status", "Subactor Control", shortcut="s"),
    CommandSpec("/orgs", "[zasób]", "rejestry organizacji (dashboard lub lista)", "Subactor Control"),
    CommandSpec("/projects", "[recon]", "portfolio projektów lub reconciliation", "Subactor Control"),
    CommandSpec("/performance", "", "ranking kosztu, częstotliwości, wzrostu i ROI", "Subactor Control", aliases=("/perf",)),
    CommandSpec("/control", "tools", "sprawdź zamkniętą granicę MCP", "Subactor Control"),
    CommandSpec("/control", "call TOOL JSON", "wywołaj cli.status/plan/execute", "Subactor Control"),
    CommandSpec("/export", "PLIK", "eksport rozmowy do JSON (bez rozwiniętych sekretów)", "Pozostałe"),
    CommandSpec("/help", "", "ta pomoc", "Pozostałe", shortcut="h"),
    CommandSpec("/clear", "", "wyczyść ekran terminala", "Pozostałe", shortcut="c"),
    CommandSpec("/q", "| /quit | /exit", "wyjście; działa też q, quit, exit i Ctrl-C", "Pozostałe", aliases=("/quit", "/exit", "q", "quit", "exit"), shortcut="q", exits=True),
)

DEFAULT_COMMAND_REGISTRY = CommandRegistry(COMMANDS)


def render_prompt(
    *,
    attachment_count: int = 0,
    menu: str = "",
    colored: bool = True,
    now: datetime | None = None,
    username: str | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
    env: dict[str, str] | None = None,
) -> str | ANSI:
    marker = f" +{attachment_count} plik" if attachment_count else ""
    timestamp = (now or datetime.now()).strftime("%H:%M")
    environment = os.environ if env is None else env
    resolved_username = username or environment.get("USER", "tom")
    resolved_cwd = cwd or Path.cwd()
    resolved_home = home or Path.home()
    try:
        relative = resolved_cwd.relative_to(resolved_home).as_posix()
    except ValueError:
        relative = resolved_cwd.as_posix().lstrip("/")
    path_segment = f"/{relative}" if relative and relative != "." else ""
    path_text = f"{resolved_username}{path_segment}/"
    menu_segment = f"/{menu.lstrip('/')}" if menu else ""
    if not colored:
        return f"⚡subactor/{path_text}{timestamp}{menu_segment}{marker}> "
    suffix = f"{menu_segment}{marker}> "
    return ANSI(f"\x1b[33m⚡subactor\x1b[37m/{path_text}\x1b[32m{timestamp}\x1b[33m{suffix}\x1b[0m")
