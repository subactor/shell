import json

import httpx
import pytest

from subactor_shell.control import ControlError, SubactorControlClient


class Resolver:
    def resolve(self, reference):
        assert reference == "env://CONTROL_TOKEN"
        return "CONTROL-TOKEN-0123456789abcdef"


def config():
    return {
        "base_url": "http://control.test",
        "account_id": "softreck",
        "provider": "chatgpt",
        "tool_id": "codex",
        "bearer_ref": "env://CONTROL_TOKEN",
        "allowed_tools": ["cli.status", "cli.plan", "cli.execute"],
    }


def test_control_validates_boundary_and_calls_tool():
    calls = []

    def handler(request: httpx.Request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [
                            {"name": "cli.execute"},
                            {"name": "cli.status"},
                            {"name": "cli.plan"},
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": "ok"}},
        )

    client = SubactorControlClient(
        config(), Resolver(), transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
    )
    assert client.health() == (True, "ok=true")
    assert client.call_tool("cli.status", {}) == {"content": "ok"}
    assert [item["method"] for item in calls] == ["tools/list", "tools/call"]


def test_control_health_accepts_legacy_status_contract():
    def handler(request: httpx.Request):
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    client = SubactorControlClient(
        config(), Resolver(), transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
    )

    assert client.health() == (True, "status='ok'")


def test_control_rejects_boundary_drift_and_unconfirmed_execute():
    def drift_handler(request: httpx.Request):
        payload = json.loads(request.content)
        if payload["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"tools": [{"name": "cli.status"}, {"name": "vault.read"}]},
                },
            )
        return httpx.Response(500)

    client = SubactorControlClient(
        config(), Resolver(), transport=httpx.MockTransport(drift_handler)  # type: ignore[arg-type]
    )
    with pytest.raises(ControlError, match="Naruszona granica MCP"):
        client.list_tools()

    def valid_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"name": "cli.execute"},
                        {"name": "cli.plan"},
                        {"name": "cli.status"},
                    ]
                },
            },
        )

    valid = SubactorControlClient(
        config(), Resolver(), transport=httpx.MockTransport(valid_handler)  # type: ignore[arg-type]
    )
    with pytest.raises(ControlError, match="jawnego potwierdzenia"):
        valid.call_tool("cli.execute", {})
