from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

from .vault import VaultClient


class SecretRefError(RuntimeError):
    pass


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretResolver:
    def __init__(self, vault_config: dict, *, vault_transport=None):
        self.vault_config = vault_config
        self._vault_transport = vault_transport
        self._vault_client: VaultClient | None = None

    @staticmethod
    def _read_env(reference: str) -> str:
        name = reference.removeprefix("env://")
        if not _ENV_NAME.fullmatch(name):
            raise SecretRefError("Nieprawidłowa referencja env://")
        value = os.environ.get(name)
        if value is None:
            raise SecretRefError(f"Brak zmiennej środowiskowej {name}")
        return value

    @staticmethod
    def _file_path(reference: str) -> Path:
        remainder = unquote(reference.removeprefix("file://"))
        if not remainder or "?" in remainder or "#" in remainder:
            raise SecretRefError("Nieprawidłowa referencja file://")
        return Path(remainder).expanduser()

    @classmethod
    def _read_file(cls, reference: str) -> str:
        path = cls._file_path(reference)
        try:
            stat = path.stat()
            if not path.is_file():
                raise SecretRefError(f"Referencja nie wskazuje pliku: {path}")
            if stat.st_size > 1024 * 1024:
                raise SecretRefError("Plik sekretu jest większy niż 1 MiB")
            return path.read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as exc:
            raise SecretRefError(f"Nie można odczytać pliku sekretu: {path}") from exc

    def resolve_without_vault(self, reference: str) -> str:
        if reference.startswith("env://"):
            return self._read_env(reference)
        if reference.startswith("file://"):
            return self._read_file(reference)
        if reference.startswith("vault://"):
            raise SecretRefError("Token dostępu do Vault nie może sam pochodzić z Vault")
        raise SecretRefError("Obsługiwane referencje to env://, file:// i vault://")

    @property
    def vault(self) -> VaultClient:
        if self._vault_client is None:
            token_ref = str(self.vault_config.get("token_ref", "env://VAULT_TOKEN"))
            self._vault_client = VaultClient(
                address=str(self.vault_config.get("address", "http://127.0.0.1:8200")),
                token_loader=lambda: self.resolve_without_vault(token_ref),
                namespace=str(self.vault_config.get("namespace", "")),
                verify_tls=bool(self.vault_config.get("verify_tls", True)),
                timeout_seconds=float(self.vault_config.get("timeout_seconds", 10.0)),
                transport=self._vault_transport,
            )
        return self._vault_client

    def resolve(self, reference: str) -> str:
        if reference.startswith("env://") or reference.startswith("file://"):
            return self.resolve_without_vault(reference)
        if reference.startswith("vault://"):
            return self.vault.read_field(reference)
        raise SecretRefError("Obsługiwane referencje to env://, file:// i vault://")
