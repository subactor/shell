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
        "resource": "",
        "recon": False,
    }
    return argparse.Namespace(**(defaults | values))


def client(handler, *, bearer="test-value"):
    settings = OperationSettings("http://control.test", "http://planfile.test", bearer)
    return OperationsClient(settings, transport=httpx.MockTransport(handler))


def test_parser_exposes_bounded_operational_surface() -> None:
    parser = build_parser()
    assert parser.parse_args(["status"]).command == "status"
    assert parser.parse_args(["tickets", "--open", "--queue", "coding-agent"]).open is True
    assert parser.parse_args(["orgs"]).command == "orgs"
    assert parser.parse_args(["orgs", "organizations"]).resource == "organizations"
    assert parser.parse_args(["projects", "--recon"]).recon is True
    assert parser.parse_args(["plans", "remote", "--status", "active"]).plans_command == "remote"
    assert parser.parse_args(["uri", "planfile://tickets/query/list"]).command == "uri"
    assert parser.parse_args(["performance", "--json"]).command == "performance"


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

    api = client(handler, bearer="")
    with pytest.raises(OperationsError, match="Brak SUBACTOR_ADMIN_TOKEN"):
        api.request("GET", "/api/system/dashboard")
    assert called is False


def test_client_binds_bearer_and_rejects_cross_origin_path() -> None:
    def handler(request):
        assert request.headers["authorization"] == "Bearer test-value"
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


def test_orgs_dashboard_renders_resource_counts() -> None:
    def handler(request):
        assert str(request.url) == "http://control.test/api/org/dashboard"
        return httpx.Response(
            200,
            json={"dashboard": {"counts": {"organizations": 2, "contacts": 5}}},
        )

    command = args(command="orgs")
    assert run_operational_command(command, Console(file=None, force_terminal=False), client(handler)) == 0


def test_orgs_resource_lists_rows() -> None:
    def handler(request):
        assert str(request.url) == "http://control.test/api/org/organizations?limit=100"
        return httpx.Response(200, json={"rows": [{"id": "org-1", "name": "Subactor"}]})

    command = args(command="orgs", resource="organizations")
    assert run_operational_command(command, Console(file=None, force_terminal=False), client(handler)) == 0


def test_projects_portfolio_uses_org_projects() -> None:
    def handler(request):
        assert str(request.url) == "http://control.test/api/org/projects?limit=200"
        return httpx.Response(
            200,
            json={"rows": [{"id": "demo", "name": "Demo", "client_name": "ACME", "status": "active"}]},
        )

    command = args(command="projects", recon=False)
    assert run_operational_command(command, Console(file=None, force_terminal=False), client(handler)) == 0


def test_projects_recon_uses_reconciliation_endpoint() -> None:
    def handler(request):
        assert str(request.url) == "http://control.test/api/projects/reconciliation"
        return httpx.Response(
            200,
            json={"projects": [{"project_id": "demo", "state": "blocked", "blockers": ["dns_not_ready"]}]},
        )

    command = args(command="projects", recon=True)
    assert run_operational_command(command, Console(file=None, force_terminal=False), client(handler)) == 0


def test_performance_command_uses_fixed_observability_origin() -> None:
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:8135/api/process-costs"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={
            "minimum_samples": 12,
            "processes": [{"process_key": "proc://hot", "total_cost": 2, "unit_cost": 1, "frequency_per_day": 3, "version_cost_growth": 0.2, "predicted_roi": 1.5}],
            "rankings": {key: ["proc://hot"] for key in ("total_cost", "unit_cost", "frequency", "version_growth", "roi")},
        })

    assert run_operational_command(args(command="performance"), Console(file=None, force_terminal=False), client(handler)) == 0
