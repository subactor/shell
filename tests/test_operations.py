from __future__ import annotations

import argparse
import json

import httpx
import pytest
from rich.console import Console

from subactor_shell.app import build_parser
from subactor_shell.operations import (
    OperationSettings,
    OperationsClient,
    OperationsError,
    filter_tickets,
    run_operational_command,
)


def args(**values):
    defaults = {
        "command": "status",
        "open": False,
        "urgent": False,
        "queue": "",
        "state": "",
        "priority": "",
        "project": "",
        "text": "",
        "json": False,
        "confirm": "",
        "payload": None,
        "status": "",
    }
    return argparse.Namespace(**(defaults | values))


def client(handler, *, token="secret"):
    settings = OperationSettings("http://control.test", "http://planfile.test", token)
    return OperationsClient(settings, transport=httpx.MockTransport(handler))


def test_parser_exposes_canonical_operational_surface() -> None:
    parser = build_parser("subactor")
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["tickets", "--open", "--queue", "coding-agent"]).open is True
    assert parser.parse_args(["plans", "remote", "--status", "active"]).plans_command == "remote"
    assert parser.parse_args(["uri", "planfile://tickets/query/list"]).command == "uri"


def test_settings_reject_credentials_in_origins_and_support_token_file(tmp_path) -> None:
    token_file = tmp_path / "control.token"
    token_file.write_text("from-file\n", encoding="utf-8")
    settings = OperationSettings.from_environment({"SUBACTOR_ADMIN_TOKEN_FILE": str(token_file)})
    assert settings.token == "from-file"
    with pytest.raises(OperationsError, match="credentials"):
        OperationSettings.from_environment({"SUBACTOR_CONTROL_URL": "http://user:pass@control.test"})


def test_authenticated_request_never_runs_without_token() -> None:
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    api = client(handler, token="")
    with pytest.raises(OperationsError, match="Brak SUBACTOR_ADMIN_TOKEN"):
        api.request("GET", "/api/system/dashboard")
    assert called is False


def test_client_binds_bearer_and_rejects_cross_origin_path() -> None:
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        assert str(request.url) == "http://control.test/api/system/dashboard"
        return httpx.Response(200, json={"ok": True})

    payload, _ = client(handler).request("GET", "/api/system/dashboard")
    assert payload == {"ok": True}
    with pytest.raises(OperationsError, match="względna"):
        client(handler).request("GET", "https://attacker.test/steal")


def test_ticket_filtering_is_open_urgent_and_queue_aware() -> None:
    rows = [
        {"id": "PLF-1", "status": "active", "priority": "high", "execution": {"queue": "coding-agent"}},
        {"id": "PLF-2", "status": "done", "priority": "high", "execution": {"queue": "coding-agent"}},
        {"id": "PLF-3", "status": "active", "priority": "low", "execution": {"queue": "doctor-agent"}},
    ]
    selected = filter_tickets(rows, args(open=True, urgent=True, queue="coding"))
    assert [item["id"] for item in selected] == ["PLF-1"]


def test_uri_command_defaults_mutation_to_dry_run() -> None:
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    command = args(command="uri", uri="plesk://host/site/command/subdomain-ensure", payload='{"domain":"x.test"}')
    assert run_operational_command(command, Console(file=None, force_terminal=False), client(handler)) == 0
    assert captured["payload"]["apply"] is False


def test_writes_require_explicit_execute_confirmation() -> None:
    def handler(_request):
        return httpx.Response(200, json={"ok": True})

    with pytest.raises(OperationsError, match="--confirm EXECUTE"):
        run_operational_command(args(command="dispatch"), Console(), client(handler))
    with pytest.raises(OperationsError, match="--confirm EXECUTE"):
        run_operational_command(
            args(command="api", method="DELETE", path="/api/tokens/1"), Console(), client(handler)
        )

