from __future__ import annotations

import httpx

from subactor_shell.app import build_parser
from subactor_shell.system_config import SystemConfigClient


def test_scope_and_account_cli_contract() -> None:
    parser = build_parser()
    scope = parser.parse_args(["scope", "--node", "current", "--kind", "code", "--json"])
    assert (scope.command, scope.node, scope.kind, scope.json) == ("scope", "current", "code", True)
    account = parser.parse_args(["account", "github", "--json"])
    assert (account.command, account.provider, account.json) == ("account", "github", True)


def test_system_config_client_uses_versioned_secret_free_projections() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/nodes/current/scopes":
            return httpx.Response(
                200,
                json={
                    "schema": "subactor.config-response/v1",
                    "data": {
                        "schema": "subactor.node-scope/v1",
                        "node": {"id": "nvidia", "hostname": "nvidia"},
                        "scope": {"code": {"repositories": []}, "data": {"resources": []}},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "schema": "subactor.config-response/v1",
                "data": {
                    "schema": "subactor.account-scope/v1",
                    "account": {"provider": "github", "configuredSubject": "tom-sapletta-com"},
                    "authentication": {"state": "ready", "effectiveScope": "not-enumerated"},
                    "settingsLocations": [],
                    "scope": {"code": [], "data": []},
                },
            },
        )

    client = SystemConfigClient(
        {"base_url": "http://config.test", "timeout_seconds": 1},
        transport=httpx.MockTransport(handler),
    )
    assert client.node_scope()["node"]["id"] == "nvidia"
    assert client.account("github")["authentication"]["state"] == "ready"
