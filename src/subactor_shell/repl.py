from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shlex
import signal
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .chat import ChatError, ChatService
from .control import ControlError, SubactorControlClient
from .fleet_status import fetch_fleet_overview, render_fleet_startup_banner
from .supervisor_chat import (
    SupervisorChatError,
    format_supervisor_chat_result,
    parse_supervisor_chat_args,
    run_supervisor_chat_command,
)
from .interaction import SurfaceInputKind, TerminalInteractionEngine
from .models import Session
from .operations import OperationSettings, OperationsClient, OperationsError, run_operational_command
from .orchestration import OrchestrationError
from .terminal import terminal_hyperlinks_enabled, ticket_link_lines
from .surface import DEFAULT_COMMAND_REGISTRY, render_prompt


HELP = DEFAULT_COMMAND_REGISTRY.render_help()
EXIT_COMMANDS = frozenset(
    value
    for command in DEFAULT_COMMAND_REGISTRY.commands
    if command.exits
    for value in (command.command, *command.aliases)
)


def is_exit_command(value: str) -> bool:
    return DEFAULT_COMMAND_REGISTRY.is_exit(value)


class ShellRepl:
    def __init__(self, chat: ChatService, session: Session, console: Console | None = None):
        self.chat = chat
        self.session = session
        self.console = console or Console()
        self.prompt = PromptSession()
        self.pending_attachments: list[Path] = []
        self.control = SubactorControlClient(chat.config.control, chat.resolver)
        self.current_menu = ""
        self.interaction = TerminalInteractionEngine(DEFAULT_COMMAND_REGISTRY)
        self.command_handlers = {
            "help": self._command_help,
            "clear": self._command_clear,
            "fleet": self._command_fleet,
            "supervisor": self._command_supervisor,
            "auth": self._command_auth,
            "session": self._command_session,
            "data": self._command_data,
            "vault": self._command_vault,
            "orchestration": self._command_orchestration,
            "status": self._command_status,
            "operations": self._command_operations,
            "control": self._command_control,
            "export": self._command_export,
        }
        violations = DEFAULT_COMMAND_REGISTRY.validate_handlers(set(self.command_handlers))
        if violations:
            raise ValueError("Niespójny rejestr komend: " + "; ".join(violations))

    async def run(self) -> None:
        hyperlinks = terminal_hyperlinks_enabled(is_terminal=self.console.is_terminal)
        try:
            await asyncio.to_thread(render_fleet_startup_banner, self.console, hyperlinks=hyperlinks)
        except Exception:
            pass
        self.console.print(
            f"[bold]Subactor Shell[/bold] — sesja [cyan]{self.session.id}[/cyan], "
            f"provider [green]{self.session.provider}[/green], model [green]{self.session.model}[/green]"
        )
        self.console.print("Wpisz /help, aby zobaczyć komendy. Sekrety podawaj jako {{secret:ALIAS}}.")

        while True:
            try:
                with patch_stdout(raw=True):
                    line = await self.prompt.prompt_async(self._prompt_text())
            except EOFError:
                self.console.print()
                return
            except KeyboardInterrupt:
                self.console.print()
                continue
            surface_input = self.interaction.interpret(line)
            if surface_input.kind is SurfaceInputKind.EMPTY:
                continue
            if surface_input.kind is SurfaceInputKind.EXIT:
                return
            if surface_input.kind is SurfaceInputKind.BACK:
                self.current_menu = ""
                continue

            try:
                if surface_input.kind is SurfaceInputKind.COMMAND:
                    keep_running = await self._command(surface_input.value)
                    if not keep_running:
                        return
                else:
                    await self._send(surface_input.value)
            except (ChatError, ControlError, OperationsError, OrchestrationError, SupervisorChatError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
                self.console.print(f"[red]Błąd:[/red] {exc}")

    def _prompt_text(self, colored: bool = True) -> Any:
        return render_prompt(
            attachment_count=len(self.pending_attachments),
            menu=getattr(self, "current_menu", ""),
            colored=colored,
        )

    async def _command(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise ValueError(f"Nieprawidłowe cudzysłowy: {exc}") from exc
        command = parts[0].lower()
        args = parts[1:]
        if is_exit_command(command):
            return False
        handler_id = DEFAULT_COMMAND_REGISTRY.handler_for(command)
        handler = self.command_handlers.get(handler_id or "")
        if not handler:
            raise ValueError(f"Nieznana komenda: {command}. Użyj /help")
        return await handler(command, args)

    async def _command_help(self, _command: str, _args: list[str]) -> bool:
        self.console.print(HELP)
        return True

    async def _command_clear(self, _command: str, _args: list[str]) -> bool:
        self.console.clear()
        return True

    async def _command_fleet(self, _command: str, _args: list[str]) -> bool:
        hyperlinks = terminal_hyperlinks_enabled(is_terminal=self.console.is_terminal)
        overview = await asyncio.to_thread(fetch_fleet_overview)
        render_fleet_startup_banner(self.console, overview, hyperlinks=hyperlinks)
        return True

    async def _command_supervisor(self, _command: str, args: list[str]) -> bool:
        parsed = parse_supervisor_chat_args(args)
        result = await asyncio.to_thread(run_supervisor_chat_command, parsed)
        self.console.print(format_supervisor_chat_result(result), markup=False, end="")
        if not result.get("ok"):
            raise SupervisorChatError(result.get("stderr") or "Supervisor zwrócił błąd")
        return True

    async def _command_auth(self, command: str, args: list[str]) -> bool:
        if command == "/login":
            self._require(args, 1, "/login <email>")
            from .auth import login_with_email
            control_url = getattr(self.chat.config, "control_url", "http://192.168.188.212:8091")
            res = login_with_email(args[0], control_url=control_url)
            self.console.print(f"[green]✓[/green] Wysłano link zatwierdzający na adres [cyan]{res.get('masked_email', args[0])}[/cyan].")
            self.console.print("[dim]Kliknij link w wiadomości e-mail, aby aktywować sesję CLI.[/dim]")
            return True
        from .auth import default_session_path, probe_auth_session
        control_url = getattr(self.chat.config, "control_url", "http://192.168.188.212:8091")
        bearer_token = os.environ.get("SUBACTOR_ADMIN_TOKEN")
        probe = probe_auth_session(control_url, bearer_token)
        self.console.print("[bold]  Uwierzytelnianie Process Control / SaaS[/bold]")
        self.console.print(f"  [dim]Adres usługi:[/dim] [cyan]{control_url}[/cyan]")
        self.console.print(f"  [dim]Status sesji:[/dim] {'[green]Uwierzytelniony[/green]' if probe.get('authenticated') else '[yellow]Sesja anonimowa / lokalna[/yellow]'}")
        if probe.get("identity"):
            self.console.print(f"  [dim]Tożsamość:[/dim]   [bold]{probe['identity']}[/bold]")
        self.console.print(f"  [dim]Plik sesji:[/dim]   {default_session_path()}")
        return True

    async def _command_session(self, command: str, args: list[str]) -> bool:
        if command == "/new":
            name = " ".join(args) or "Nowa rozmowa"
            self.session = self.chat.new_session(name=name)
            self.pending_attachments.clear()
            self.console.print(f"Nowa sesja: [cyan]{self.session.id}[/cyan]")
            return True
        if command == "/sessions":
            if args and args[0] in {"del", "delete", "rm"}:
                self._require(args, 2, "/sessions del ID")
                target_session = self._resolve_session(args[1])
                if target_session.id == self.session.id:
                    raise ValueError("Nie można usunąć bieżącej aktywnej sesji. Utwórz nową (/new) przed usunięciem.")
                self.chat.store.delete_session(target_session.id)
                self.console.print(f"Usunięto sesję [cyan]{target_session.id}[/cyan].")
                return True
            if args and args[0] == "prune":
                count = self.chat.store.prune_sessions(older_than_days=30)
                self.console.print(f"Wyczyszczono [cyan]{count}[/cyan] starych sesji.")
                return True
            self._print_sessions()
            return True

        if command == "/resume":
            self._require(args, 1, "/resume ID")
            self.session = self._resolve_session(args[0])
            self.pending_attachments.clear()
            self.console.print(f"Wznowiono [cyan]{self.session.id}[/cyan]")
            return True
        if command == "/provider":
            self._require(args, 1, "/provider NAZWA")
            profile = self.chat.config.provider(args[0])
            self.session = self.chat.store.update_session(
                self.session.id, provider=profile.name, model=profile.model
            )
            self.console.print(
                f"Provider: [green]{self.session.provider}[/green], model: [green]{self.session.model}[/green]"
            )
            return True
        if command == "/model":
            self._require(args, 1, "/model MODEL")
            self.session = self.chat.store.update_session(self.session.id, model=args[0])
            self.console.print(f"Model: [green]{self.session.model}[/green]")
            return True
        if command == "/attach":
            self._require(args, 1, "/attach PLIK")
            path = Path(args[0]).expanduser()
            if not path.is_file():
                raise ValueError(f"Brak pliku: {path}")
            self.pending_attachments.append(path)
            self.console.print(f"Do następnej wiadomości: {path}")
            return True
        if command == "/info":
            self._print_info()
            return True
        raise ValueError(f"Nieobsługiwana komenda sesji: {command}")

    async def _command_data(self, _command: str, args: list[str]) -> bool:
        self._handle_data(args)
        return True

    async def _command_vault(self, _command: str, args: list[str]) -> bool:
        self._handle_vault(args)
        return True

    async def _command_orchestration(self, command: str, args: list[str]) -> bool:
        if command == "/plans":
            self._print_plans()
            return True
        if command == "/plan":
            self._require(args, 1, "/plan ID")
            payload = self.chat.store.get_execution_plan(args[0])
            if not payload:
                raise ValueError(f"Nie ma planu {args[0]}")
            self._print_json(payload)
            return True
        if command == "/apply":
            self._require(args, 1, "/apply ID")
            payload = self.chat.store.get_execution_plan(args[0])
            if not payload:
                raise ValueError(f"Nie ma planu {args[0]}")
            confirmation = ""
            if str(payload.get("effect", "read")) != "read":
                with patch_stdout(raw=True):
                    confirmation = await self.prompt.prompt_async(
                        "Plan może zmienić system. Wpisz dokładnie EXECUTE: "
                    )
                if confirmation != "EXECUTE":
                    raise OrchestrationError("Anulowano apply")
            receipt = await self.chat.orchestration.apply_plan(
                args[0], confirmation=confirmation
            )
            self.console.print(self.chat.orchestration.format_receipt(receipt), markup=False)
            self.chat.context_builder.update_state(
                self.session.id,
                user_text=f"apply {args[0]}",
                receipt_id=receipt.id,
            )
            return True
        if command == "/receipts":
            self._print_receipts()
            return True
        if command == "/receipt":
            self._require(args, 1, "/receipt ID")
            payload = self.chat.store.get_execution_receipt(args[0])
            if not payload:
                raise ValueError(f"Nie ma receiptu {args[0]}")
            self._print_json(payload)
            return True
        if command == "/route":
            payload = self.chat.store.last_routing_decision(self.session.id)
            self._print_json(payload or {})
            return True
        if command == "/metrics":
            self._print_json(self.chat.store.usage_summary(self.session.id))
            return True
        if command == "/catalog":
            table = Table("Intent", "Execution", "Risk", "Źródło")
            for item in self.chat.orchestration.catalog.list():
                table.add_row(
                    item.id, str(item.execution.get("kind", "chat")), item.risk, item.source
                )
            self.console.print(table)
            return True
        if command == "/connectors":
            table = Table("Nazwa", "Kind", "Effect", "Operations")
            for item in self.chat.orchestration.registry.list():
                table.add_row(
                    item.name, item.kind, item.effect, ", ".join(item.allowed_operations)
                )
            self.console.print(table)
            return True
        raise ValueError(f"Nieobsługiwana komenda orkiestracji: {command}")

    async def _command_status(self, _command: str, _args: list[str]) -> bool:
        await self._send("pokaż status subactora")
        return True

    async def _command_operations(self, command: str, args: list[str]) -> bool:
        if command == "/orgs":
            await self._run_operational("orgs", args)
            return True
        if command == "/projects":
            await self._run_operational("projects", args)
            return True
        if command in {"/performance", "/perf"}:
            await self._run_operational("performance", args)
            return True
        raise ValueError(f"Nieobsługiwana komenda operacyjna: {command}")

    async def _command_control(self, _command: str, args: list[str]) -> bool:
        await self._handle_control(args)
        return True

    async def _command_export(self, _command: str, args: list[str]) -> bool:
        self._require(args, 1, "/export PLIK")
        target = Path(args[0]).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.chat.store.export_session(self.session.id)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.console.print(f"Zapisano: {target}")
        return True

    async def _send(self, text: str) -> None:
        cancel_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed_handler = False
        previous_sigint = signal.getsignal(signal.SIGINT)
        try:
            try:
                loop.add_signal_handler(signal.SIGINT, cancel_event.set)
                installed_handler = True
            except (NotImplementedError, RuntimeError):
                pass
            self.console.print("[bold cyan]agent>[/bold cyan] ", end="")
            answer_parts: list[str] = []
            async for chunk in self.chat.stream_message(
                self.session.id,
                text,
                attachment_paths=list(self.pending_attachments),
                cancel_event=cancel_event,
            ):
                answer_parts.append(chunk)
                self.console.print(Text(chunk), end="", soft_wrap=True)
            self.console.print()
            profile = self.chat.config.provider(self.session.provider)
            if profile.kind == "subactor_control":
                lines = ticket_link_lines(
                    "".join(answer_parts),
                    profile.base_url,
                    hyperlinks=terminal_hyperlinks_enabled(is_terminal=self.console.is_terminal),
                )
                if lines:
                    self.console.print("  [dim][tickety][/dim]")
                    for line in lines:
                        self.console.print(line)
            if cancel_event.is_set():
                self.console.print("[yellow]Anulowano.[/yellow]")
            if bool(self.chat.config.orchestration.get("show_route", False)):
                route = self.chat.store.last_routing_decision(self.session.id)
                if route:
                    self.console.print(
                        f"[dim]route={route['route']} intent={route['intent_id']} "
                        f"confidence={route['confidence']:.3f}[/dim]"
                    )
            self.pending_attachments.clear()
        except KeyboardInterrupt:
            cancel_event.set()
            self.console.print("\n[yellow]Anulowano.[/yellow]")
        finally:
            if installed_handler:
                loop.remove_signal_handler(signal.SIGINT)
                signal.signal(signal.SIGINT, previous_sigint)

    def _handle_data(self, args: list[str]) -> None:
        self._require(args, 1, "/data set|put|list|del ...")
        action = args[0].lower()
        if action == "set":
            self._require(args, 3, "/data set NAZWA WARTOŚĆ")
            self.chat.set_data_text(args[1], " ".join(args[2:]))
            self.console.print(f"Zapisano dane [cyan]{args[1]}[/cyan].")
        elif action == "put":
            self._require(args, 3, "/data put NAZWA PLIK")
            artifact = self.chat.set_data_file(args[1], Path(args[2]), self.session.id)
            self.console.print(f"Zapisano [cyan]{args[1]}[/cyan] jako sha256:{artifact.id}")
        elif action == "list":
            table = Table("Nazwa", "Typ", "Wartość/ID")
            for name, kind, value in self.chat.store.list_data():
                shown = value if kind == "artifact" else f"{len(value)} znaków"
                table.add_row(name, kind, shown)
            self.console.print(table)
        elif action == "del":
            self._require(args, 2, "/data del NAZWA")
            removed = self.chat.store.delete_data(args[1])
            self.console.print("Usunięto." if removed else "Nie znaleziono.")
        else:
            raise ValueError("Użyj /data set|put|list|del")

    def _handle_vault(self, args: list[str]) -> None:
        self._require(args, 1, "/vault bind|put|grant|list|unbind|wrap ...")
        action = args[0].lower()
        if action == "bind":
            self._require(args, 3, "/vault bind ALIAS REF")
            self.chat.bind_secret(args[1], args[2])
            self.console.print(f"Binding [cyan]{args[1]}[/cyan] zapisany; wartość nie została odczytana.")
        elif action == "put":
            self._require(args, 3, "/vault put ALIAS vault://MOUNT/SCIEZKA#POLE")
            reference = args[2]
            if not reference.startswith("vault://"):
                raise ValueError("/vault put wymaga referencji vault://")
            value = getpass.getpass("Wartość sekretu (bez echa): ")
            if not value:
                raise ValueError("Pusta wartość sekretu")
            self.chat.resolver.vault.write_field(reference, value)
            self.chat.bind_secret(args[1], reference)
            self.console.print(f"Zapisano sekret i binding [cyan]{args[1]}[/cyan].")
        elif action == "grant":
            self._require(args, 2, "/vault grant ALIAS")
            self.chat.grant_secret(args[1])
            self.console.print(
                f"Jednorazowy grant dla [cyan]{args[1]}[/cyan]. Zostanie zużyty przez następną wiadomość."
            )
        elif action == "list":
            table = Table("Alias", "Referencja", "Grant w pamięci")
            granted = set(self.chat.grants.list())
            for alias, reference in self.chat.store.list_secret_bindings():
                table.add_row(alias, reference, "tak" if alias in granted else "nie")
            self.console.print(table)
        elif action == "unbind":
            self._require(args, 2, "/vault unbind ALIAS")
            removed = self.chat.store.unbind_secret(args[1])
            self.console.print("Usunięto binding." if removed else "Nie znaleziono bindingu.")
        elif action == "wrap":
            self._require(args, 2, "/vault wrap ALIAS [TTL]")
            reference = self.chat.store.get_secret_binding(args[1])
            if not reference:
                raise ValueError(f"Brak bindingu {args[1]}")
            if not reference.startswith("vault://"):
                raise ValueError("Response wrapping działa tylko dla vault://")
            token = self.chat.resolver.vault.wrap_read(reference, args[2] if len(args) > 2 else "5m")
            self.console.print("[yellow]Wrapping token (jednorazowy; obejmuje całą odpowiedź ścieżki KV):[/yellow]")
            self.console.print(Text(token))
        else:
            raise ValueError("Użyj /vault bind|put|grant|list|unbind|wrap")

    async def _run_operational(self, command: str, args: list[str]) -> None:
        if command == "orgs":
            namespace = argparse.Namespace(command="orgs", resource=args[0] if args else "", json=False)
        elif command == "projects":
            recon = bool(args) and args[0].lower() in {"recon", "reconciliation", "--recon"}
            namespace = argparse.Namespace(command="projects", recon=recon, json=False)
        elif command == "performance":
            namespace = argparse.Namespace(command="performance", json=False)
        else:
            raise ValueError(f"Nieobsługiwana komenda operacyjna: {command}")

        def run() -> int:
            client = OperationsClient(OperationSettings.from_environment())
            return run_operational_command(namespace, self.console, client)

        code = await asyncio.to_thread(run)
        if code != 0:
            raise OperationsError(f"Komenda {command} zakończyła się kodem {code}")

    async def _handle_control(self, args: list[str]) -> None:
        self._require(args, 1, "/control tools|call ...")
        action = args[0].lower()
        if action == "tools":
            tools = await asyncio.to_thread(self.control.list_tools, strict=True)
            table = Table("Narzędzie", "Opis")
            for item in tools:
                table.add_row(str(item.get("name", "")), str(item.get("description", "")))
            self.console.print(table)
            return
        if action == "call":
            self._require(args, 3, "/control call TOOL JSON")
            name = args[1]
            arguments = json.loads(" ".join(args[2:]))
            if not isinstance(arguments, dict):
                raise ValueError("Argumenty narzędzia muszą być obiektem JSON")
            allow_execute = False
            if name == "cli.execute":
                with patch_stdout(raw=True):
                    confirmation = await self.prompt.prompt_async(
                        "cli.execute może zmienić system. Wpisz dokładnie EXECUTE: "
                    )
                allow_execute = confirmation == "EXECUTE"
                if not allow_execute:
                    raise ControlError("Anulowano cli.execute")
            result = await asyncio.to_thread(
                self.control.call_tool,
                name,
                arguments,
                allow_execute=allow_execute,
            )
            self._print_json(result)
            return
        raise ValueError("Użyj /control tools|call")

    def _print_plans(self) -> None:
        table = Table("ID", "Intent", "Effect", "Status", "Utworzono")
        for item in self.chat.store.list_execution_plans(self.session.id):
            table.add_row(
                str(item.get("id", "")),
                str(item.get("intent_id", "")),
                str(item.get("effect", "")),
                str(item.get("status", "")),
                str(item.get("created_at", "")),
            )
        self.console.print(table)

    def _print_receipts(self) -> None:
        table = Table("ID", "Plan", "OK", "Utworzono")
        for item in self.chat.store.list_execution_receipts(self.session.id):
            table.add_row(
                str(item.get("id", "")),
                str(item.get("plan_id", "")),
                str(bool(item.get("ok"))),
                str(item.get("created_at", "")),
            )
        self.console.print(table)

    def _resolve_session(self, value: str) -> Session:
        exact = self.chat.store.get_session(value)
        if exact:
            return exact
        matches = [item for item in self.chat.store.list_sessions() if item.id.startswith(value)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"Nie ma sesji pasującej do {value}")
        raise ValueError(f"Prefiks {value} jest niejednoznaczny")

    def _print_sessions(self) -> None:
        table = Table("ID", "Nazwa", "Provider", "Model", "Aktualizacja")
        for item in self.chat.store.list_sessions():
            table.add_row(item.id, item.name, item.provider, item.model, item.updated_at)
        self.console.print(table)

    def _print_info(self) -> None:
        self.console.print(
            {
                "id": self.session.id,
                "name": self.session.name,
                "provider": self.session.provider,
                "model": self.session.model,
                "pending_attachments": [str(path) for path in self.pending_attachments],
                "working_state": self.chat.store.get_session_state(self.session.id),
                "last_route": self.chat.store.last_routing_decision(self.session.id),
                "usage": self.chat.store.usage_summary(self.session.id),
            }
        )

    def _print_json(self, payload: Any) -> None:
        self.console.print_json(json.dumps(payload, ensure_ascii=False, default=str))

    @staticmethod
    def _require(args: list[str], count: int, usage: str) -> None:
        if len(args) < count:
            raise ValueError(f"Użycie: {usage}")
