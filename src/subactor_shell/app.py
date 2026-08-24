from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import stat
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import __version__
from .acp_agent import AcpAgent
from .chat import ChatService
from .compiler import ExecutionPlan
from .config import initialize_layout, load_config
from .control import SubactorControlClient
from .repl import ShellRepl
from .store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subactor-shell",
        description="Token-aware, bezpieczna i trwała rozmowa w shellu dla Subactor.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="ścieżka config.toml")
    parser.add_argument("--data-dir", type=Path, default=None, help="katalog danych i SQLite")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="utwórz prywatny config i katalog danych")

    chat = sub.add_parser("chat", help="uruchom interaktywny REPL")
    chat.add_argument("--session", help="ID istniejącej sesji")
    chat.add_argument("--provider", help="profil providera dla nowej sesji")
    chat.add_argument("--model", help="model dla nowej sesji")
    chat.add_argument("--name", default="Rozmowa shell", help="nazwa nowej sesji")

    one = sub.add_parser("one", help="wyślij jedną wiadomość")
    one.add_argument("message")
    one.add_argument("--session", help="ID istniejącej sesji")
    one.add_argument("--provider", help="profil providera dla nowej sesji")
    one.add_argument("--model", help="model dla nowej sesji")
    one.add_argument("--attach", action="append", type=Path, default=[])
    one.add_argument("--grant", action="append", default=[], help="jednorazowy grant aliasu sekretu")

    data = sub.add_parser("data", help="zapisuj jawne dane tekstowe i pliki")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    data_set = data_sub.add_parser("set", help="zapisz jawne dane tekstowe")
    data_set.add_argument("name")
    data_set.add_argument("value")
    data_put = data_sub.add_parser("put", help="zapisz plik jako artefakt")
    data_put.add_argument("name")
    data_put.add_argument("path", type=Path)
    data_put.add_argument("--session", help="sesja audytowa; domyślnie tworzona automatycznie")
    data_sub.add_parser("list", help="lista danych")
    data_delete = data_sub.add_parser("delete", help="usuń dane")
    data_delete.add_argument("name")

    vault = sub.add_parser("vault", help="bindingi i zapis sekretów Vault")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    vault_bind = vault_sub.add_parser("bind", help="zapisz wyłącznie referencję sekretu")
    vault_bind.add_argument("alias")
    vault_bind.add_argument("reference")
    vault_put = vault_sub.add_parser("put", help="zapisz wartość bez echa do KV v2")
    vault_put.add_argument("alias")
    vault_put.add_argument("reference")
    vault_sub.add_parser("list", help="lista aliasów i referencji")
    vault_unbind = vault_sub.add_parser("unbind", help="usuń binding")
    vault_unbind.add_argument("alias")
    vault_wrap = vault_sub.add_parser("wrap", help="utwórz Vault response-wrapping token")
    vault_wrap.add_argument("alias")
    vault_wrap.add_argument("--ttl", default="5m")

    sessions = sub.add_parser("sessions", help="lista zapisanych sesji")
    sessions.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="eksport sesji do JSON")
    export.add_argument("session")
    export.add_argument("output", type=Path)

    plans = sub.add_parser("plans", help="przeglądaj i stosuj skompilowane plany")
    plans_sub = plans.add_subparsers(dest="plans_command", required=True)
    plans_list = plans_sub.add_parser("list")
    plans_list.add_argument("--session")
    plans_list.add_argument("--json", action="store_true")
    plans_show = plans_sub.add_parser("show")
    plans_show.add_argument("plan_id")
    plans_apply = plans_sub.add_parser("apply")
    plans_apply.add_argument("plan_id")
    plans_apply.add_argument("--confirm", default="", help="dla zmian stanu: dokładnie EXECUTE")

    receipts = sub.add_parser("receipts", help="przeglądaj krótkie receipts wykonania")
    receipts_sub = receipts.add_subparsers(dest="receipts_command", required=True)
    receipts_list = receipts_sub.add_parser("list")
    receipts_list.add_argument("--session")
    receipts_list.add_argument("--json", action="store_true")
    receipts_show = receipts_sub.add_parser("show")
    receipts_show.add_argument("receipt_id")

    metrics = sub.add_parser("metrics", help="zużycie tokenów, koszt i udział tras lokalnych")
    metrics.add_argument("--session")
    metrics.add_argument("--json", action="store_true")

    catalog = sub.add_parser("catalog", help="lokalny katalog intentów")
    catalog.add_argument("--json", action="store_true")

    connectors = sub.add_parser("connectors", help="allowlista nazwanych connectorów")
    connectors.add_argument("--json", action="store_true")

    sub.add_parser("doctor", help="diagnostyka bez ujawniania sekretów")
    sub.add_parser("acp-agent", help="uruchom agenta ACP v1 po stdio")
    return parser


def _build_services(args: argparse.Namespace, *, create: bool = True):
    config = load_config(args.config, args.data_dir, create=create)
    store = Store(config.data_dir / "subactor-shell.sqlite3")
    chat = ChatService(config, store)
    return config, store, chat


def _mode(path: Path) -> str:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))
    except OSError:
        return "brak"


async def _one(chat: ChatService, args: argparse.Namespace, console: Console) -> int:
    session = chat.get_or_create_session(args.session, provider=args.provider, model=args.model)
    for alias in args.grant:
        chat.grant_secret(alias)
    async for chunk in chat.stream_message(session.id, args.message, attachment_paths=args.attach):
        console.print(chunk, end="", markup=False, soft_wrap=True)
    console.print()
    console.print(f"[dim]session={session.id}[/dim]")
    route = chat.store.last_routing_decision(session.id)
    if route and bool(chat.config.orchestration.get("show_route", False)):
        console.print(
            f"[dim]route={route['route']} intent={route['intent_id']} confidence={route['confidence']:.3f}[/dim]"
        )
    return 0


def _doctor(config, store: Store, chat: ChatService, console: Console) -> int:
    table = Table("Test", "Wynik", "Szczegóły")
    failures = 0

    def add(name: str, ok: bool, details: str) -> None:
        nonlocal failures
        if not ok:
            failures += 1
        table.add_row(name, "OK" if ok else "BŁĄD", details)

    add("config", config.config_path.exists(), f"{config.config_path} mode={_mode(config.config_path)}")
    add("data dir", config.data_dir.exists(), f"{config.data_dir} mode={_mode(config.data_dir)}")
    add("SQLite", store.db_path.exists(), f"{store.db_path} mode={_mode(store.db_path)}")
    try:
        names = config.provider_names()
        for name in names:
            config.provider(name)
        add("providers", bool(names), ", ".join(names) or "brak")
    except Exception as exc:
        add("providers", False, str(exc))

    orch = chat.orchestration
    add(
        "orchestration",
        orch.mode in {"active", "shadow", "off"},
        f"mode={orch.mode}; intents={len(orch.catalog.list())}; connectors={len(orch.registry.list())}",
    )
    add(
        "context budget",
        True,
        (
            f"recent={chat.context_builder.recent_messages}; "
            f"history_chars={chat.context_builder.max_history_chars}; "
            f"message_chars={chat.context_builder.max_message_chars}"
        ),
    )
    add("intent catalog", True, orch.catalog.fingerprint[:16])
    add("connector registry", True, orch.registry.fingerprint[:16])

    vault_ok, vault_details = chat.resolver.vault.health()
    add("Vault HTTP", vault_ok, vault_details)

    try:
        control = SubactorControlClient(config.control, chat.resolver)
        control_ok, control_details = control.health()
        add("Subactor Control", control_ok, control_details)
        if control_ok:
            try:
                names = [str(item.get("name")) for item in control.list_tools(strict=True)]
                add("MCP boundary", True, ", ".join(sorted(names)))
            except Exception as exc:
                add("MCP boundary", False, str(exc))
    except Exception as exc:
        add("Subactor Control", False, str(exc))

    console.print(table)
    console.print("Diagnostyka nie odczytuje ani nie drukuje wartości sekretów.")
    return 1 if failures else 0


def _data_command(chat: ChatService, args: argparse.Namespace, console: Console) -> int:
    action = args.data_command
    if action == "set":
        chat.set_data_text(args.name, args.value)
        console.print(f"Zapisano dane [cyan]{args.name}[/cyan].")
    elif action == "put":
        session = chat.get_or_create_session(args.session) if args.session else chat.new_session(name="Import danych")
        artifact = chat.set_data_file(args.name, args.path, session.id)
        console.print(f"Zapisano [cyan]{args.name}[/cyan] jako sha256:{artifact.id}; session={session.id}")
    elif action == "list":
        table = Table("Nazwa", "Typ", "Wartość/ID")
        for name, kind, value in chat.store.list_data():
            shown = value if kind == "artifact" else f"{len(value)} znaków"
            table.add_row(name, kind, shown)
        console.print(table)
    elif action == "delete":
        removed = chat.store.delete_data(args.name)
        console.print("Usunięto." if removed else "Nie znaleziono.")
        return 0 if removed else 1
    return 0


def _vault_command(chat: ChatService, args: argparse.Namespace, console: Console) -> int:
    action = args.vault_command
    if action == "bind":
        chat.bind_secret(args.alias, args.reference)
        console.print(f"Binding [cyan]{args.alias}[/cyan] zapisany; wartość nie została odczytana.")
    elif action == "put":
        if not args.reference.startswith("vault://"):
            raise ValueError("vault put wymaga referencji vault://")
        value = getpass.getpass("Wartość sekretu (bez echa): ")
        if not value:
            raise ValueError("Pusta wartość sekretu")
        chat.resolver.vault.write_field(args.reference, value)
        chat.bind_secret(args.alias, args.reference)
        console.print(f"Zapisano sekret i binding [cyan]{args.alias}[/cyan].")
    elif action == "list":
        table = Table("Alias", "Referencja")
        for alias, reference in chat.store.list_secret_bindings():
            table.add_row(alias, reference)
        console.print(table)
    elif action == "unbind":
        removed = chat.store.unbind_secret(args.alias)
        console.print("Usunięto binding." if removed else "Nie znaleziono bindingu.")
        return 0 if removed else 1
    elif action == "wrap":
        reference = chat.store.get_secret_binding(args.alias)
        if not reference:
            raise ValueError(f"Brak bindingu {args.alias}")
        if not reference.startswith("vault://"):
            raise ValueError("Response wrapping działa tylko dla vault://")
        token = chat.resolver.vault.wrap_read(reference, args.ttl)
        console.print(token, markup=False)
    return 0


def _print_plans(store: Store, args: argparse.Namespace, console: Console) -> int:
    if args.plans_command == "show":
        payload = store.get_execution_plan(args.plan_id)
        if not payload:
            raise ValueError(f"Nie ma planu {args.plan_id}")
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return 0
    if args.plans_command == "list":
        plans = store.list_execution_plans(args.session)
        if args.json:
            console.print_json(json.dumps(plans, ensure_ascii=False))
        else:
            table = Table("ID", "Session", "Intent", "Effect", "Status", "Utworzono")
            for item in plans:
                table.add_row(
                    str(item.get("id", "")),
                    str(item.get("session_id", "")),
                    str(item.get("intent_id", "")),
                    str(item.get("effect", "")),
                    str(item.get("status", "")),
                    str(item.get("created_at", "")),
                )
            console.print(table)
        return 0
    return 0


async def _apply_plan(chat: ChatService, args: argparse.Namespace, console: Console) -> int:
    payload = chat.store.get_execution_plan(args.plan_id)
    if not payload:
        raise ValueError(f"Nie ma planu {args.plan_id}")
    plan = ExecutionPlan.from_dict(payload)
    confirmation = args.confirm
    if plan.effect != "read" and not confirmation and sys.stdin.isatty():
        confirmation = input("Wpisz dokładnie EXECUTE, aby zastosować plan: ")
    receipt = await chat.orchestration.apply_plan(args.plan_id, confirmation=confirmation)
    console.print(chat.orchestration.format_receipt(receipt), markup=False)
    return 0 if receipt.ok else 1


def _receipts_command(store: Store, args: argparse.Namespace, console: Console) -> int:
    if args.receipts_command == "show":
        payload = store.get_execution_receipt(args.receipt_id)
        if not payload:
            raise ValueError(f"Nie ma receiptu {args.receipt_id}")
        console.print_json(json.dumps(payload, ensure_ascii=False))
        return 0
    receipts = store.list_execution_receipts(args.session)
    if args.json:
        console.print_json(json.dumps(receipts, ensure_ascii=False))
    else:
        table = Table("ID", "Plan", "Session", "OK", "Utworzono")
        for item in receipts:
            table.add_row(
                str(item.get("id", "")),
                str(item.get("plan_id", "")),
                str(item.get("session_id", "")),
                str(bool(item.get("ok"))),
                str(item.get("created_at", "")),
            )
        console.print(table)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    command = args.command or "chat"
    if args.command is None:
        args.session = None
        args.provider = None
        args.model = None
        args.name = "Rozmowa shell"
    try:
        if command == "init":
            config_path, data_dir = initialize_layout(args.config, args.data_dir)
            console.print(f"Config: [cyan]{config_path}[/cyan] mode={_mode(config_path)}")
            console.print(f"Dane:   [cyan]{data_dir}[/cyan] mode={_mode(data_dir)}")
            return

        config, store, chat = _build_services(args)
        if command == "chat":
            session = chat.get_or_create_session(args.session) if args.session else chat.new_session(
                name=args.name, provider=args.provider, model=args.model
            )
            asyncio.run(ShellRepl(chat, session, console).run())
        elif command == "one":
            raise SystemExit(asyncio.run(_one(chat, args, console)))
        elif command == "data":
            raise SystemExit(_data_command(chat, args, console))
        elif command == "vault":
            raise SystemExit(_vault_command(chat, args, console))
        elif command == "sessions":
            sessions = store.list_sessions()
            if args.json:
                console.print_json(
                    json.dumps(
                        [
                            {
                                "id": item.id,
                                "name": item.name,
                                "provider": item.provider,
                                "model": item.model,
                                "created_at": item.created_at,
                                "updated_at": item.updated_at,
                            }
                            for item in sessions
                        ],
                        ensure_ascii=False,
                    )
                )
            else:
                table = Table("ID", "Nazwa", "Provider", "Model", "Aktualizacja")
                for item in sessions:
                    table.add_row(item.id, item.name, item.provider, item.model, item.updated_at)
                console.print(table)
        elif command == "export":
            payload = store.export_session(args.session)
            output = args.output.expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            console.print(f"Zapisano {output}")
        elif command == "plans":
            if args.plans_command == "apply":
                raise SystemExit(asyncio.run(_apply_plan(chat, args, console)))
            raise SystemExit(_print_plans(store, args, console))
        elif command == "receipts":
            raise SystemExit(_receipts_command(store, args, console))
        elif command == "metrics":
            payload = store.usage_summary(args.session)
            if args.json:
                console.print_json(json.dumps(payload, ensure_ascii=False))
            else:
                console.print_json(json.dumps(payload, ensure_ascii=False))
        elif command == "catalog":
            payload = [item.to_summary() for item in chat.orchestration.catalog.list()]
            if args.json:
                console.print_json(json.dumps(payload, ensure_ascii=False))
            else:
                table = Table("Intent", "Execution", "Risk", "Źródło")
                for item in chat.orchestration.catalog.list():
                    table.add_row(
                        item.id,
                        str(item.execution.get("kind", "chat")),
                        item.risk,
                        item.source,
                    )
                console.print(table)
        elif command == "connectors":
            payload = [item.public_dict() for item in chat.orchestration.registry.list()]
            if args.json:
                console.print_json(json.dumps(payload, ensure_ascii=False))
            else:
                table = Table("Nazwa", "Kind", "Effect", "Operations")
                for item in chat.orchestration.registry.list():
                    table.add_row(item.name, item.kind, item.effect, ", ".join(item.allowed_operations))
                console.print(table)
        elif command == "doctor":
            raise SystemExit(_doctor(config, store, chat, console))
        elif command == "acp-agent":
            asyncio.run(AcpAgent(chat).run_stdio())
        else:
            parser.error(f"Nieznana komenda: {command}")
    except KeyboardInterrupt:
        Console(stderr=True).print("\nAnulowano.")
        raise SystemExit(130)
    except Exception as exc:
        Console(stderr=True).print(f"[red]Błąd:[/red] {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
