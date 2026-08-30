from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx


class SystemConfigError(RuntimeError):
    pass


class SystemConfigClient:
    def __init__(
        self,
        settings: dict[str, Any],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = str(settings.get("base_url", "http://127.0.0.1:8098")).rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("system_config.base_url jest nieprawidłowy")
        self.base_url = base_url
        self.timeout = float(settings.get("timeout_seconds", 5.0))
        self.transport = transport

    def _get(self, path: str) -> Any:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                headers={"accept": "application/json"},
            ) as client:
                response = client.get(path)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SystemConfigError("Nie można pobrać konfiguracji Subactor") from exc
        if not isinstance(payload, dict) or payload.get("schema") != "subactor.config-response/v1":
            raise SystemConfigError("Nieobsługiwany kontrakt odpowiedzi Subactor Config")
        return payload.get("data")

    def node_scope(self, node: str = "current") -> dict[str, Any]:
        response = self._get(f"/v1/nodes/{quote(node, safe='')}/scopes")
        if not isinstance(response, dict) or response.get("schema") != "subactor.node-scope/v1":
            raise SystemConfigError("Nieobsługiwany kontrakt scope node")
        return response

    def account(self, provider: str) -> dict[str, Any]:
        response = self._get(f"/v1/accounts/{quote(provider, safe='')}")
        if not isinstance(response, dict) or response.get("schema") != "subactor.account-scope/v1":
            raise SystemConfigError("Nieobsługiwany kontrakt scope konta")
        return response

    def sources(self, *, provides: str | None = None) -> list[dict[str, Any]]:
        query = f"?provides={quote(provides, safe='')}" if provides else ""
        response = self._get(f"/v1/sources{query}")
        if not isinstance(response, list) or any(
            not isinstance(item, dict) or item.get("schema") != "subactor.configuration-source/v1"
            for item in response
        ):
            raise SystemConfigError("Nieobsługiwany kontrakt źródeł konfiguracji")
        return response

    def source(self, source_id: str) -> dict[str, Any]:
        response = self._get(f"/v1/sources/{quote(source_id, safe='')}")
        if not isinstance(response, dict) or response.get("schema") != "subactor.configuration-source/v1":
            raise SystemConfigError("Nieobsługiwany kontrakt źródła konfiguracji")
        return response

    def capabilities(self) -> dict[str, Any]:
        response = self._get("/v1/capabilities")
        if not isinstance(response, dict) or response.get("schema") != "subactor.configuration-capabilities/v1":
            raise SystemConfigError("Nieobsługiwany kontrakt zdolności konfiguracji")
        return response

    def resolve(self, need: str) -> dict[str, Any]:
        response = self._get(f"/v1/resolve?need={quote(need, safe='')}")
        if not isinstance(response, dict) or response.get("schema") != "subactor.configuration-resolution/v1":
            raise SystemConfigError("Nieobsługiwany kontrakt rozwiązywania konfiguracji")
        return response
