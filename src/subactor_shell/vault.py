from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any
from urllib.parse import quote, unquote, urlsplit

import httpx


class VaultError(RuntimeError):
    """Vault error that never includes request bodies or secret values."""


@dataclass(frozen=True, slots=True)
class VaultRef:
    mount: str
    path: str
    field: str

    @classmethod
    def parse(cls, value: str) -> "VaultRef":
        parsed = urlsplit(value)
        if parsed.scheme != "vault":
            raise ValueError("Referencja Vault musi zaczynać się od vault://")
        if parsed.username or parsed.password or parsed.query:
            raise ValueError("Referencja Vault nie może zawierać danych logowania ani query")
        mount = unquote(parsed.netloc).strip()
        path = unquote(parsed.path).strip("/")
        field = unquote(parsed.fragment).strip()
        if not mount or not path or not field:
            raise ValueError("Użyj formatu vault://MOUNT/SCIEZKA#POLE")
        segments = [mount, *path.split("/"), field]
        if any(not segment or segment in {".", ".."} for segment in segments):
            raise ValueError("Nieprawidłowa ścieżka Vault")
        return cls(mount=mount, path=path, field=field)

    @property
    def api_path(self) -> str:
        encoded_mount = quote(self.mount, safe="")
        encoded_path = "/".join(quote(part, safe="") for part in self.path.split("/"))
        return f"/v1/{encoded_mount}/data/{encoded_path}"


class VaultClient:
    def __init__(
        self,
        address: str,
        token_loader: Callable[[], str],
        *,
        namespace: str = "",
        verify_tls: bool = True,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.address = address.rstrip("/")
        if not self.address.startswith(("http://", "https://")):
            raise ValueError("vault.address musi być adresem http:// lub https://")
        self._token_loader = token_loader
        self.namespace = namespace.strip()
        self.verify_tls = verify_tls
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _headers(self, *, wrap_ttl: str = "") -> dict[str, str]:
        token = self._token_loader().strip()
        if not token:
            raise VaultError("Token Vault jest pusty")
        headers = {"X-Vault-Token": token}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        if wrap_ttl:
            headers["X-Vault-Wrap-TTL"] = wrap_ttl
        return headers

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.address,
            verify=self.verify_tls,
            timeout=self.timeout_seconds,
            transport=self.transport,
        )

    @staticmethod
    def _raise_status(response: httpx.Response, operation: str) -> None:
        request_id = response.headers.get("x-vault-request-id", "")
        suffix = f", request_id={request_id}" if request_id else ""
        raise VaultError(f"Vault: {operation} zakończone HTTP {response.status_code}{suffix}")

    def read_field(self, reference: str | VaultRef) -> str:
        ref = reference if isinstance(reference, VaultRef) else VaultRef.parse(reference)
        try:
            with self._client() as client:
                response = client.get(ref.api_path, headers=self._headers())
        except httpx.HTTPError as exc:
            raise VaultError(f"Vault: błąd połączenia podczas odczytu ({type(exc).__name__})") from exc
        if response.status_code != 200:
            self._raise_status(response, "odczyt")
        try:
            value = response.json()["data"]["data"][ref.field]
        except (KeyError, TypeError, ValueError) as exc:
            raise VaultError(f"Vault: brak pola '{ref.field}' w odpowiedzi KV v2") from exc
        if not isinstance(value, (str, int, float, bool)):
            raise VaultError(f"Vault: pole '{ref.field}' nie jest wartością skalarną")
        return str(value)

    def write_field(self, reference: str | VaultRef, value: str) -> None:
        ref = reference if isinstance(reference, VaultRef) else VaultRef.parse(reference)
        headers = self._headers()
        headers["Content-Type"] = "application/merge-patch+json"
        try:
            with self._client() as client:
                patch = client.patch(ref.api_path, headers=headers, json={"data": {ref.field: value}})
                if patch.status_code in {200, 204}:
                    return
                if patch.status_code not in {400, 403, 404, 405}:
                    self._raise_status(patch, "zapis PATCH")

                # Bezpieczny fallback: odczyt i zapis z CAS. Nie nadpisujemy
                # pozostałych pól na ślepo, gdy PATCH jest wyłączony.
                read = client.get(ref.api_path, headers=self._headers())
                if read.status_code == 404:
                    current: dict[str, Any] = {}
                    version = 0
                elif read.status_code == 200:
                    payload = read.json().get("data", {})
                    current = dict(payload.get("data", {}))
                    version = int(payload.get("metadata", {}).get("version", 0))
                else:
                    self._raise_status(read, "odczyt przed zapisem CAS")
                current[ref.field] = value
                post = client.post(
                    ref.api_path,
                    headers=self._headers(),
                    json={"options": {"cas": version}, "data": current},
                )
                if post.status_code not in {200, 204}:
                    self._raise_status(post, "zapis CAS")
        except httpx.HTTPError as exc:
            raise VaultError(f"Vault: błąd połączenia podczas zapisu ({type(exc).__name__})") from exc

    def wrap_read(self, reference: str | VaultRef, ttl: str = "5m") -> str:
        """Return a one-time Vault wrapping token for the whole KV response."""
        ref = reference if isinstance(reference, VaultRef) else VaultRef.parse(reference)
        if not ttl or len(ttl) > 16:
            raise ValueError("Nieprawidłowy TTL wrappingu")
        try:
            with self._client() as client:
                response = client.get(ref.api_path, headers=self._headers(wrap_ttl=ttl))
        except httpx.HTTPError as exc:
            raise VaultError(f"Vault: błąd połączenia podczas wrappingu ({type(exc).__name__})") from exc
        if response.status_code != 200:
            self._raise_status(response, "response wrapping")
        try:
            token = response.json()["wrap_info"]["token"]
        except (KeyError, TypeError, ValueError) as exc:
            raise VaultError("Vault: odpowiedź nie zawiera wrapping tokenu") from exc
        if not isinstance(token, str) or not token:
            raise VaultError("Vault: pusty wrapping token")
        return token

    def health(self) -> tuple[bool, str]:
        try:
            with self._client() as client:
                response = client.get("/v1/sys/health")
        except httpx.HTTPError as exc:
            return False, f"błąd połączenia ({type(exc).__name__})"
        # 200 active, 429 standby, 472/473 DR/performance standby są osiągalne.
        if response.status_code in {200, 429, 472, 473}:
            return True, f"HTTP {response.status_code}"
        return False, f"HTTP {response.status_code}"
