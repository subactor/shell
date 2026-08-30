from __future__ import annotations

import httpx

from subactor_shell.app import build_parser
from subactor_shell.system_config import SystemConfigClient


def test_scope_account_and_source_cli_contract() -> None:
    parser = build_parser()
    scope = parser.parse_args(["scope", "--node", "current", "--kind", "code", "--json"])
    assert (scope.command, scope.node, scope.kind, scope.json) == ("scope", "current", "code", True)
    account = parser.parse_args(["account", "github", "--json"])
    assert (account.command, account.provider, account.json) == ("account", "github", True)
    source = parser.parse_args(["source", "list", "--provides", "configuration.propose", "--json"])
    assert (source.command, source.source_command, source.provides, source.json) == (
        "source", "list", "configuration.propose", True,
    )
    resolution = parser.parse_args(["resolve", "credential.metadata", "--json"])
    assert (resolution.command, resolution.need, resolution.json) == ("resolve", "credential.metadata", True)


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
        if request.url.path == "/v1/sources":
            assert request.url.params.get("provides") == "configuration.propose"
            return httpx.Response(
                200,
                json={
                    "schema": "subactor.config-response/v1",
                    "data": [{
                        "schema": "subactor.configuration-source/v1",
                        "id": "supervisor-control",
                        "availability": {"state": "ready"},
                    }],
                },
            )
        if request.url.path == "/v1/sources/credential-vault":
            return httpx.Response(
                200,
                json={
                    "schema": "subactor.config-response/v1",
                    "data": {"schema": "subactor.configuration-source/v1", "id": "credential-vault"},
                },
            )
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "schema": "subactor.config-response/v1",
                    "data": {"schema": "subactor.configuration-capabilities/v1", "capabilities": []},
                },
            )
        if request.url.path == "/v1/resolve":
            assert request.url.params["need"] == "credential.metadata"
            return httpx.Response(
                200,
                json={
                    "schema": "subactor.config-response/v1",
                    "data": {"schema": "subactor.configuration-resolution/v1", "matchedSources": []},
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
    assert client.sources(provides="configuration.propose")[0]["id"] == "supervisor-control"
    assert client.source("credential-vault")["id"] == "credential-vault"
    assert client.capabilities()["capabilities"] == []
    assert client.resolve("credential.metadata")["matchedSources"] == []
