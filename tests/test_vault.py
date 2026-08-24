import json

import httpx
import pytest

from subactor_shell.vault import VaultClient, VaultRef


def test_vault_ref_parses_kv_v2_uri():
    ref = VaultRef.parse("vault://secret/subactor/providers/openai#api_key")
    assert ref.mount == "secret"
    assert ref.path == "subactor/providers/openai"
    assert ref.field == "api_key"
    assert ref.api_path == "/v1/secret/data/subactor/providers/openai"


@pytest.mark.parametrize(
    "value",
    [
        "vault://secret/no-field",
        "vault:///missing-mount#field",
        "vault://secret/../bad#field",
        "env://TOKEN",
    ],
)
def test_vault_ref_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        VaultRef.parse(value)


def test_vault_read_and_patch_do_not_need_to_expose_value():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": {"data": {"api_key": "sekret"}, "metadata": {"version": 1}}},
            )
        if request.method == "PATCH":
            return httpx.Response(204)
        return httpx.Response(500)

    client = VaultClient(
        "http://vault.test",
        lambda: "vault-token",
        transport=httpx.MockTransport(handler),
    )
    assert client.read_field("vault://secret/app#api_key") == "sekret"
    client.write_field("vault://secret/app#api_key", "nowy")
    assert requests[0].headers["x-vault-token"] == "vault-token"
    assert json.loads(requests[1].content) == {"data": {"api_key": "nowy"}}


def test_vault_write_falls_back_to_read_merge_and_cas():
    seen_post = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_post
        if request.method == "PATCH":
            return httpx.Response(405)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": {"data": {"other": "keep"}, "metadata": {"version": 7}}},
            )
        if request.method == "POST":
            seen_post = json.loads(request.content)
            return httpx.Response(200, json={"data": {"version": 8}})
        return httpx.Response(500)

    client = VaultClient(
        "http://vault.test",
        lambda: "vault-token",
        transport=httpx.MockTransport(handler),
    )
    client.write_field("vault://secret/app#api_key", "nowy")
    assert seen_post == {
        "options": {"cas": 7},
        "data": {"other": "keep", "api_key": "nowy"},
    }
