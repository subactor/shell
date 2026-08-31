from pathlib import Path

import httpx
import pytest

from subactor_shell.auth import (
    normalize_email,
    mask_email,
    create_pkce,
    write_cli_session,
    probe_auth_session,
)


def test_email_validation():
    assert normalize_email("  Test@Example.COM  ") == "test@example.com"
    with pytest.raises(ValueError):
        normalize_email("invalid-email")


def test_email_masking():
    assert mask_email("tom@subactor.com") == "t**@subactor.com"
    assert mask_email("alexander@example.com") == "a********@example.com"


def test_pkce_generation():
    verifier, challenge = create_pkce()
    assert len(verifier) >= 43
    assert len(challenge) >= 43
    assert verifier != challenge


def test_write_cli_session(tmp_path: Path):
    session_file = tmp_path / "session.json"
    session_data = {
        "access_token": "sub_test_token_123",
        "identity": "tester@example.com",
        "expires_at": 1800000000,
    }
    saved_path = write_cli_session(session_data, session_file=session_file)
    assert saved_path.is_file()
    assert oct(saved_path.stat().st_mode & 0o777) == "0o600"
    content = saved_path.read_text(encoding="utf-8")
    assert "sub_test_token_123" in content


def test_probe_auth_session(monkeypatch: pytest.MonkeyPatch):
    real_client = httpx.Client

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://control.test/api/session"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"ok": True, "authenticated": True})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        "subactor_shell.auth.httpx.Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    res = probe_auth_session("http://control.test", bearer_token="test-token")

    assert res.get("ok") is True
    assert res.get("authenticated") is True
