"""
Authentication and session management module for Subactor Shell.
Supports email magic-link + PKCE login and session probing.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

import httpx


def default_session_path() -> Path:
    config_dir = Path.home() / ".config" / "subactor-shell"
    return config_dir / "session.json"


def normalize_email(value: str) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        raise ValueError("Podaj poprawny adres e-mail.")
    return email


def mask_email(email: str) -> str:
    try:
        local, domain = email.split("@", 1)
        masked_local = local[0] + "*" * max(2, len(local) - 1)
        return f"{masked_local}@{domain}"
    except Exception:
        return email


def create_pkce() -> tuple[str, str]:
    verifier_bytes = secrets.token_bytes(32)
    verifier = base64.urlsafe_b64encode(verifier_bytes).decode("ascii").rstrip("=")
    challenge_bytes = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(challenge_bytes).decode("ascii").rstrip("=")
    return verifier, challenge


def write_cli_session(session_data: dict[str, Any], session_file: Path | None = None, control_url: str = "http://127.0.0.1:8091") -> Path:
    target = session_file or default_session_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    
    record = {
        "schema": "subactor.cli-session/v1",
        "control_url": control_url,
        "token_type": "Bearer",
        "access_token": session_data.get("access_token", ""),
        "expires_at": session_data.get("expires_at"),
        "identity": session_data.get("identity"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    
    tmp_file = target.parent / f".tmp_session_{os.getpid()}_{secrets.token_hex(4)}"
    tmp_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    tmp_file.chmod(0o600)
    tmp_file.replace(target)
    target.chmod(0o600)
    return target


def probe_auth_session(control_url: str, bearer_token: str | None = None) -> dict[str, Any]:
    url = f"{control_url.rstrip('/')}/api/session"
    headers = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return {"ok": False, "authenticated": False, "status_code": resp.status_code}
    except Exception as err:
        return {"ok": False, "authenticated": False, "error": str(err)}


def login_with_email(email: str, control_url: str = "http://127.0.0.1:8091", timeout_seconds: int = 300) -> dict[str, Any]:
    normalized = normalize_email(email)
    verifier, challenge = create_pkce()
    
    req_url = f"{control_url.rstrip('/')}/api/auth/login-request"
    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.post(req_url, json={"email": normalized, "code_challenge": challenge})
            if resp.status_code >= 400:
                raise RuntimeError(f"Błąd logowania: {resp.text}")
            init_data = resp.json()
        except httpx.ConnectError:
            raise RuntimeError(f"Nie udało się połączyć z usługą logowania Subactor pod adresem {control_url}")
            
    exchange_id = init_data.get("exchange_id")
    if not exchange_id:
        return {"ok": True, "message": "Wysłano żądanie logowania.", "masked_email": mask_email(normalized)}
        
    return {
        "ok": True,
        "exchange_id": exchange_id,
        "masked_email": mask_email(normalized),
        "verifier": verifier,
    }
