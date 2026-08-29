"""Bounded operational HTTP commands exposed by Subactor Shell."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .control_env import apply_control_environment
from .terminal import canonical_ticket_links, terminal_hyperlinks_enabled


_TERMINAL_TICKET_STATES = frozenset({"done", "completed", "closed", "rejected", "cancelled"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_URI = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]+$", re.IGNORECASE)
_ENDPOINTS = """# Subactor Control API
GET  /health
GET  /api/system/dashboard
GET  /api/plans
GET  /api/integrations
GET  /api/delegation/manager
GET  /api/llm/status
POST /api/delegation/dispatch
POST /api/processes/run

# Canonical shell
subactor health
subactor status
subactor tickets --open
subactor orgs [resource]
subactor projects [--recon]
subactor plans remote
subactor uri <uri-process> [json-payload]
subactor get /api/system/dashboard
subactor get /api/org/dashboard
subactor get /api/org/projects?limit=200
subactor get /api/projects/reconciliation
subactor post /api/delegation/dispatch '{}' --confirm EXECUTE
subactor api GET|POST|PUT|PATCH|DELETE <path> [json-body]
"""


class OperationsError(RuntimeError):
    """Safe user-facing operational command failure."""


def _validated_origin(value: str, label: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OperationsError(f"{label} musi być adresem http(s)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OperationsError(f"{label} nie może zawierać credentials, query ani fragmentu")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _read_token(values: Mapping[str, str]) -> str:
    token = values.get("SUBACTOR_ADMIN_TOKEN", "").strip()
    token_file = values.get("SUBACTOR_ADMIN_TOKEN_FILE", "").strip()
    if token or not token_file:
        return token
    path = Path(token_file).expanduser()
    try:
        if path.stat().st_size > 16_384:
            raise OperationsError("Plik tokenu jest zbyt duży")
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OperationsError("Nie można odczytać pliku tokenu") from exc


@dataclass(frozen=True)
class OperationSettings:
    control_url: str
    planfile_url: str
    token: str
    timeout_seconds: float = 20.0

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "OperationSettings":
        if env is None:
            apply_control_environment()
            values = os.environ
        else:
            values = env
        control_url = _validated_origin(
            values.get("SUBACTOR_CONTROL_URL", "http://127.0.0.1:8091"), "SUBACTOR_CONTROL_URL"
        )
        planfile_url = _validated_origin(
            values.get("SUBACTOR_PLANFILE_URL", values.get("PLANFILE_URL", "http://127.0.0.1:8765")),
            "SUBACTOR_PLANFILE_URL",
        )
        return cls(control_url, planfile_url, _read_token(values))


class OperationsClient:
    def __init__(self, settings: OperationSettings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        service: str = "control",
        authenticated: bool = True,
    ) -> tuple[Any, str]:
        verb = method.upper()
        if verb not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise OperationsError(f"Niedozwolona metoda HTTP: {verb}")
        if not path.startswith("/") or path.startswith("//") or "\n" in path or "\r" in path:
            raise OperationsError("Ścieżka API musi być względna wobec skonfigurowanej usługi")
        if authenticated and not self.settings.token:
            raise OperationsError(
                "Brak SUBACTOR_ADMIN_TOKEN lub SUBACTOR_ADMIN_TOKEN_FILE; operacja nie została wysłana"
            )
        base = self.settings.planfile_url if service == "planfile" else self.settings.control_url
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        try:
            with httpx.Client(timeout=self.settings.timeout_seconds, transport=self._transport) as client:
                response = client.request(verb, f"{base}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise OperationsError(f"Usługa {service} jest niedostępna ({exc.__class__.__name__})") from exc
        if response.status_code >= 400:
            code = "request_failed"
            try:
                problem = response.json()
                if isinstance(problem, dict):
                    code = str(problem.get("code") or problem.get("type") or code)[:160]
            except ValueError:
                pass
            raise OperationsError(f"{service} odrzucił żądanie: HTTP {response.status_code} ({code})")
        try:
            return response.json(), response.text
        except ValueError:
            return response.text, response.text


def _ticket_open(ticket: Mapping[str, Any]) -> bool:
    return str(ticket.get("status", "")).lower() not in _TERMINAL_TICKET_STATES


def _ticket_urgent(ticket: Mapping[str, Any]) -> bool:
    priority = str(ticket.get("priority", "")).lower()
    labels = {str(value).lower() for value in ticket.get("labels", []) if isinstance(value, str)}
    name = str(ticket.get("name") or ticket.get("title") or "").upper()
    return priority in {"urgent", "critical", "high"} or "urgent" in labels or name.startswith(("PILNE", "URGENT"))


def filter_tickets(rows: Sequence[Mapping[str, Any]], args: Any) -> list[Mapping[str, Any]]:
    result = list(rows)
    if args.open or args.urgent:
        result = [item for item in result if _ticket_open(item)]
    if args.urgent:
        result = [item for item in result if _ticket_urgent(item)]
    filters = {
        "queue": args.queue,
        "state": args.state,
        "priority": args.priority,
        "project": args.project,
    }
    for field, expected in filters.items():
        if not expected:
            continue
        needle = expected.lower()
        if field in {"queue", "state"}:
            result = [item for item in result if needle in str(item.get("execution", {}).get(field, "")).lower()]
        else:
            result = [item for item in result if needle in str(item.get(field, "")).lower()]
    if args.text:
        needle = args.text.lower()
        result = [item for item in result if needle in json.dumps(item, ensure_ascii=False).lower()]
    return sorted(
        result,
        key=lambda item: (_ticket_urgent(item), str(item.get("updated_at") or item.get("created_at") or "")),
        reverse=True,
    )


def _json_payload(raw: str | None) -> Any:
    if raw is None:
        return None
    if len(raw) > 131_072:
        raise OperationsError("Payload JSON jest zbyt duży")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OperationsError("Payload musi być poprawnym JSON") from exc


def _require_execute(confirmation: str, operation: str) -> None:
    if confirmation != "EXECUTE":
        raise OperationsError(f"{operation} wymaga --confirm EXECUTE")


def _print_json(console: Console, payload: Any) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False))


_ORG_COLUMNS = ("name", "title", "subject", "email", "status", "owner_email", "updated_at", "id")


def _org_table_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    all_keys = {key for row in rows for key in row}
    preferred = [key for key in _ORG_COLUMNS if key in all_keys]
    extra = [key for key in sorted(all_keys) if key not in preferred and key not in {"created_by", "updated_by", "archived_at"}]
    return (preferred + extra)[:6]


def _print_orgs_dashboard(console: Console, payload: Mapping[str, Any]) -> None:
    dashboard = payload.get("dashboard") if isinstance(payload.get("dashboard"), dict) else {}
    counts = dashboard.get("counts") if isinstance(dashboard.get("counts"), dict) else {}
    table = Table("Zasób", "Rekordów")
    for key, value in sorted(counts.items(), key=lambda item: str(item[0])):
        table.add_row(str(key), str(value))
    console.print(f"{len(counts)} rejestr(ów) organizacji")
    console.print(table)


def _print_org_resource(console: Console, resource: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        console.print(f"Brak rekordów w zasobie {resource}.")
        return
    columns = _org_table_columns(rows)
    table = Table(*columns)
    for item in rows[:50]:
        table.add_row(
            *[
                str(item.get(column, ""))[:80]
                if not isinstance(item.get(column), (dict, list))
                else json.dumps(item.get(column), ensure_ascii=False)[:80]
                for column in columns
            ]
        )
    console.print(f"{len(rows)} rekord(ów) w {resource}")
    console.print(table)


def _print_projects_portfolio(console: Console, rows: Sequence[Mapping[str, Any]]) -> None:
    table = Table("ID", "Nazwa", "Klient", "Status", "Typ")
    for item in rows[:50]:
        table.add_row(
            str(item.get("id", "?")),
            str(item.get("name", ""))[:60],
            str(item.get("client_name", ""))[:30],
            str(item.get("status", "")),
            str(item.get("project_type", "")),
        )
    console.print(f"{len(rows)} projekt(ów)")
    console.print(table)


def _print_projects_reconciliation(console: Console, rows: Sequence[Mapping[str, Any]]) -> None:
    table = Table("Projekt", "Domena", "Stan", "Blokery")
    for item in rows[:50]:
        blockers = item.get("blockers", [])
        blocker_text = ", ".join(str(value) for value in blockers) if isinstance(blockers, list) else str(blockers)
        desired = item.get("desired") if isinstance(item.get("desired"), dict) else {}
        domain = str(desired.get("domain") or item.get("domain") or "")
        table.add_row(
            str(item.get("project_id") or item.get("id") or "?"),
            domain[:40],
            str(item.get("state") or item.get("status") or "?"),
            blocker_text[:80],
        )
    console.print(f"{len(rows)} projekt(ów) reconciliation")
    console.print(table)


def _projects_from_reconciliation(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, dict):
        return []
    projects = payload.get("projects")
    if isinstance(projects, list):
        return [item for item in projects if isinstance(item, dict)]
    reconciliation = payload.get("reconciliation")
    if isinstance(reconciliation, dict) and isinstance(reconciliation.get("projects"), list):
        return [item for item in reconciliation["projects"] if isinstance(item, dict)]
    return []


def _print_tickets(console: Console, rows: Sequence[Mapping[str, Any]], control_url: str) -> None:
    table = Table("Ticket", "Priorytet", "Status", "Kolejka", "Stan", "Nazwa")
    hyperlinks = terminal_hyperlinks_enabled(is_terminal=console.is_terminal)
    for item in rows[:50]:
        ticket = str(item.get("id", "?"))
        links = canonical_ticket_links(ticket, control_url, limit=1)
        ticket_text = Text(ticket)
        if links and hyperlinks:
            ticket_text.stylize(f"link {links[0][1]}")
        execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
        table.add_row(
            ticket_text,
            str(item.get("priority", "normal")),
            str(item.get("status", "?")),
            str(execution.get("queue", "?")),
            str(execution.get("state", "?")),
            str(item.get("name") or item.get("title") or "")[:80],
        )
    console.print(f"{len(rows)} ticket(s)")
    console.print(table)


def run_operational_command(args: Any, console: Console, client: OperationsClient) -> int:
    command = args.command
    if command == "health":
        payload, text = client.request("GET", "/health", authenticated=False)
        _print_json(console, payload) if not isinstance(payload, str) else console.print(text, markup=False)
        return 0
    if command == "status":
        payload, _ = client.request("GET", "/api/system/dashboard")
        _print_json(console, payload)
        return 0
    if command == "tickets":
        payload, _ = client.request("GET", "/tickets?sprint=all", service="planfile")
        rows = payload if isinstance(payload, list) else payload.get("tickets", [])
        filtered = filter_tickets([item for item in rows if isinstance(item, dict)], args)
        _print_json(console, filtered) if args.json else _print_tickets(console, filtered, client.settings.control_url)
        return 0
    if command == "orgs":
        resource = str(getattr(args, "resource", "") or "").strip()
        if resource:
            payload, _ = client.request("GET", f"/api/org/{resource}?limit=100")
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            filtered = [item for item in rows if isinstance(item, dict)]
            if args.json:
                _print_json(console, payload)
            else:
                _print_org_resource(console, resource, filtered)
        else:
            payload, _ = client.request("GET", "/api/org/dashboard")
            if args.json:
                _print_json(console, payload)
            else:
                _print_orgs_dashboard(console, payload if isinstance(payload, dict) else {})
        return 0
    if command == "projects":
        if bool(getattr(args, "recon", False)):
            payload, _ = client.request("GET", "/api/projects/reconciliation")
            rows = _projects_from_reconciliation(payload)
            if args.json:
                _print_json(console, payload)
            else:
                _print_projects_reconciliation(console, rows)
        else:
            payload, _ = client.request("GET", "/api/org/projects?limit=200")
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            filtered = [item for item in rows if isinstance(item, dict)]
            if args.json:
                _print_json(console, payload)
            else:
                _print_projects_portfolio(console, filtered)
        return 0
    if command == "plans":
        payload, _ = client.request("GET", "/api/plans?view=summary")
        plans = payload.get("plans", []) if isinstance(payload, dict) else []
        if args.status:
            plans = [item for item in plans if str(item.get("status", "")) == args.status]
        _print_json(console, plans)
        return 0
    if command == "dispatch":
        _require_execute(args.confirm, "dispatch")
        payload, _ = client.request("POST", "/api/delegation/dispatch", body={})
        _print_json(console, payload)
        return 0
    if command == "uri":
        if not _URI.fullmatch(args.uri):
            raise OperationsError("Nieprawidłowy URI procesu")
        body = _json_payload(args.payload) or {}
        if not isinstance(body, dict):
            raise OperationsError("Payload URI musi być obiektem JSON")
        if body.get("apply") is True:
            _require_execute(args.confirm, "URI apply")
        elif "/command/" in args.uri and "apply" not in body:
            body["apply"] = False
        payload, _ = client.request(
            "POST",
            "/api/processes/run",
            body={"uri": args.uri, "payload": body, "reason": f"Canonical Subactor shell: {args.uri}"},
        )
        _print_json(console, payload)
        return 0
    if command in {"api", "get", "post"}:
        method = args.method.upper() if command == "api" else command.upper()
        path = args.path
        raw_body = getattr(args, "payload", None)
        if method in _WRITE_METHODS:
            _require_execute(args.confirm, f"HTTP {method}")
        payload, text = client.request(method, path, body=_json_payload(raw_body))
        _print_json(console, payload) if not isinstance(payload, str) else console.print(text, markup=False)
        return 0
    if command == "endpoints":
        console.print(_ENDPOINTS, markup=False)
        return 0
    raise OperationsError(f"Nieobsługiwana komenda operacyjna: {command}")
