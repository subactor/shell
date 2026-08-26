from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx

from .redaction import ExactRedactor
from .secret_refs import SecretResolver


class ControlError(RuntimeError):
    pass


class SubactorControlClient:
    def __init__(
        self,
        config: dict[str, Any],
        resolver: SecretResolver,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self.config = config
        self.resolver = resolver
        self.transport = transport
        self.base_url = str(config.get("base_url", "http://127.0.0.1:8088")).rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("control.base_url musi być adresem http:// lub https://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("control.base_url nie może zawierać credentiali ani query")
        self.account_id = self._identifier(str(config.get("account_id", "softreck")))
        self.provider = self._identifier(str(config.get("provider", "chatgpt")))
        self.tool_id = self._identifier(str(config.get("tool_id", "codex")))
        self.bearer_ref = str(config.get("bearer_ref", ""))
        self.allowed_tools = sorted(
            str(item) for item in config.get("allowed_tools", ["cli.status", "cli.plan", "cli.execute"])
        )
        self.timeout = float(config.get("timeout_seconds", 10.0))

    @staticmethod
    def _identifier(value: str) -> str:
        import re

        normalized = value.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", normalized) is None:
            raise ValueError(f"Nieprawidłowy identyfikator Subactor: {value}")
        return normalized

    @property
    def endpoint(self) -> str:
        return (
            f"/mcp/accounts/{self.account_id}/providers/{self.provider}/tools/{self.tool_id}"
        )

    def _token(self) -> str:
        if not self.bearer_ref:
            raise ControlError("Brak control.bearer_ref")
        token = self.resolver.resolve(self.bearer_ref)
        if len(token) < 16:
            raise ControlError("Bearer Subactor jest pusty albo zbyt krótki")
        return token

    def health(self) -> tuple[bool, str]:
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport
            ) as client:
                response = client.get("/health")
        except httpx.HTTPError as exc:
            return False, f"błąd połączenia ({type(exc).__name__})"
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return False, "odpowiedź nie jest JSON"
        if not isinstance(payload, dict):
            return False, "odpowiedź JSON nie jest obiektem"
        if payload.get("ok") is True:
            return True, "ok=true"
        status = payload.get("status")
        return status == "ok", f"status={status!r}"

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout, transport=self.transport
            ) as client:
                response = client.post(self.endpoint, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ControlError(f"Błąd połączenia z Subactor Control ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise ControlError(f"Subactor Control zwrócił HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise ControlError("Subactor Control zwrócił nieprawidłowy JSON") from exc
        if not isinstance(result, dict):
            raise ControlError("Subactor Control zwrócił nieprawidłową odpowiedź RPC")
        if "error" in result:
            # Redagujemy bearer na wypadek odbicia nagłówków przez serwer/proxy.
            safe = ExactRedactor([token]).redact(json.dumps(result["error"], ensure_ascii=False))
            raise ControlError(f"Błąd JSON-RPC Subactor: {safe}")
        return result

    def list_tools(self, *, strict: bool = True) -> list[dict[str, Any]]:
        response = self._rpc("tools/list")
        tools = response.get("result", {}).get("tools", [])
        if not isinstance(tools, list):
            raise ControlError("tools/list nie zwróciło listy")
        names = sorted(
            item.get("name") for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        if strict and names != self.allowed_tools:
            raise ControlError(
                f"Naruszona granica MCP: otrzymano {names}, oczekiwano {self.allowed_tools}"
            )
        return [item for item in tools if isinstance(item, dict)]

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allow_execute: bool = False,
    ) -> Any:
        self.list_tools(strict=True)
        if name not in self.allowed_tools:
            raise ControlError(f"Narzędzie '{name}' nie jest dozwolone")
        if name == "cli.execute" and not allow_execute:
            raise ControlError("cli.execute wymaga jawnego potwierdzenia")
        response = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return response.get("result")
