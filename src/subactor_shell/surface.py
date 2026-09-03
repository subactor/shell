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
    handler_id: str | None = None

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
        self._command_handlers: dict[str, str] = {}
        for item in commands:
            if not item.command.startswith("/") or not item.handler_id:
                continue
            names = (item.command, *item.aliases)
            for name in names:
                command = name.split()[0].lower()
                previous = self._command_handlers.get(command)
                if previous and previous != item.handler_id:
                    raise ValueError(
                        f"Komenda {command} wskazuje różne handlery: {previous}, {item.handler_id}"
                    )
                self._command_handlers[command] = item.handler_id

    def resolve_shortcut(self, value: str) -> str:
        normalized = value.strip().lower()
        return self._shortcuts.get(normalized, value)

    def is_exit(self, value: str) -> bool:
        return value.strip().lower() in self._exit_commands

    def handler_for(self, command: str) -> str | None:
        return self._command_handlers.get(command.strip().split()[0].lower())

    def validate_handlers(self, available: set[str] | frozenset[str]) -> tuple[str, ...]:
        errors: list[str] = []
        declared = {item.handler_id for item in self.commands if item.handler_id}
        for item in self.commands:
            if item.command.startswith("/") and not item.exits and not item.handler_id:
                errors.append(f"Komenda {item.command} nie deklaruje handlera")
        for handler_id in sorted(declared - set(available)):
            errors.append(f"Brak handlera REPL: {handler_id}")
        for handler_id in sorted(set(available) - declared):
            errors.append(f"Handler REPL bez komendy: {handler_id}")
        return tuple(errors)

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
    CommandSpec("/new", "[nazwa]", "nowa sesja", "Rozmowa", handler_id="session"),
    CommandSpec("/sessions", "[del ID|prune]", "lista, usunięcie lub czyszczenie starych sesji", "Rozmowa", handler_id="session"),
    CommandSpec("/resume", "ID", "wznowienie sesji", "Rozmowa", handler_id="session"),
    CommandSpec("/provider", "NAZWA", "zmiana profilu providera", "Rozmowa", shortcut="p", handler_id="session"),
    CommandSpec("/model", "MODEL", "zmiana modelu w sesji", "Rozmowa", shortcut="m", handler_id="session"),
    CommandSpec("/attach", "PLIK", "dołącz plik do następnej wiadomości", "Rozmowa", handler_id="session"),
    CommandSpec("/info", "", "aktywna sesja", "Rozmowa", handler_id="session"),
    CommandSpec("/login", "<email>", "uwierzytelnienie sesji CLI", "Rozmowa", handler_id="auth"),
    CommandSpec("/auth", "", "stan uwierzytelnienia Process Control", "Rozmowa", handler_id="auth"),
    CommandSpec("/prs", "", "otwarte Pull Requesty (subactor, if-uri)", "Flota i Autonomia", aliases=("/pr",), shortcut="t", handler_id="fleet"),
    CommandSpec("/doctor", "", "zadania diagnostyczne i naprawcze", "Flota i Autonomia", shortcut="d", handler_id="fleet"),
    CommandSpec("/fleet", "", "pełny podgląd ekosystemu (PR, zadania, usługi)", "Flota i Autonomia", shortcut="f", handler_id="fleet"),
    CommandSpec("/supervisor", "[status|observe|cycle|questions]", "status i cykl LLM supervisora (bez apply)", "Flota i Autonomia", aliases=("/sup",), handler_id="supervisor"),
    CommandSpec("/data", "set NAZWA WARTOŚĆ", "zapisz jawne dane tekstowe", "Dane", handler_id="data"),
    CommandSpec("/data", "put NAZWA PLIK", "zapisz plik jako artefakt", "Dane", handler_id="data"),
    CommandSpec("/data", "list", "lista danych", "Dane", handler_id="data"),
    CommandSpec("/data", "del NAZWA", "usuń dane", "Dane", handler_id="data"),
    CommandSpec("W wiadomości użyj:", "{{data:NAZWA}}", "", "Dane"),
    CommandSpec("/vault", "bind ALIAS REF", "zapisz wyłącznie referencję vault://, env:// lub file://", "Sekrety", handler_id="vault"),
    CommandSpec("/vault", "put ALIAS VAULT_REF", "wczytaj wartość bez echa, zapisz do KV v2 i utwórz binding", "Sekrety", handler_id="vault"),
    CommandSpec("/vault", "grant ALIAS", "jednorazowo zezwól na {{secret:ALIAS}}", "Sekrety", handler_id="vault"),
    CommandSpec("/vault", "list", "lista aliasów i referencji (bez wartości)", "Sekrety", handler_id="vault"),
    CommandSpec("/vault", "unbind ALIAS", "usuń binding", "Sekrety", handler_id="vault"),
    CommandSpec("/vault", "wrap ALIAS [TTL]", "utwórz jednorazowy wrapping token Vault", "Sekrety", handler_id="vault"),
    CommandSpec("/plans", "", "lista planów sesji", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/plan", "ID", "pokaż plan JSON", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/apply", "ID", "zastosuj plan; zmiany wymagają EXECUTE", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/receipts", "", "lista receipts sesji", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/receipt", "ID", "pokaż receipt JSON", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/route", "", "ostatnia decyzja routera", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/metrics", "", "tokeny, koszt i udział fast path", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/catalog", "", "lokalny katalog intentów", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/connectors", "", "allowlista connectorów", "Orkiestracja i tokeny", handler_id="orchestration"),
    CommandSpec("/status", "", "wywołaj cli.status", "Subactor Control", shortcut="s", handler_id="status"),
    CommandSpec("/orgs", "[zasób]", "rejestry organizacji (dashboard lub lista)", "Subactor Control", handler_id="operations"),
    CommandSpec("/projects", "[recon]", "portfolio projektów lub reconciliation", "Subactor Control", handler_id="operations"),
    CommandSpec("/performance", "", "ranking kosztu, częstotliwości, wzrostu i ROI", "Subactor Control", aliases=("/perf",), handler_id="operations"),
    CommandSpec("/control", "tools", "sprawdź zamkniętą granicę MCP", "Subactor Control", handler_id="control"),
    CommandSpec("/control", "call TOOL JSON", "wywołaj cli.status/plan/execute", "Subactor Control", handler_id="control"),
    CommandSpec("/export", "PLIK", "eksport rozmowy do JSON (bez rozwiniętych sekretów)", "Pozostałe", handler_id="export"),
    CommandSpec("/help", "", "ta pomoc", "Pozostałe", shortcut="h", handler_id="help"),
    CommandSpec("/clear", "", "wyczyść ekran terminala", "Pozostałe", shortcut="c", handler_id="clear"),
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
