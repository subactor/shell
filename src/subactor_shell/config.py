from __future__ import annotations

import copy
import os
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ProviderProfile


DEFAULT_CONFIG_TEXT = """# Konfiguracja Subactor Shell 0.2.

[defaults]
provider = "mock"
model = "mock"
max_attachment_bytes = 5242880
max_attachment_text_chars = 262144

# Pełna historia pozostaje w SQLite. Model otrzymuje WorkingState, kilka
# ostatnich wiadomości oraz lokalnie wybrane fragmenty danych/artefaktów.
[context]
recent_messages = 6
max_history_chars = 12000
max_message_chars = 4000
max_data_chars = 6000
max_attachment_prompt_chars = 8000
artifact_chunk_chars = 1800
max_artifact_chunks = 4
max_embedded_context_chars = 8000
max_route_context_chars = 4000

# Kaskada: phrase/template -> lokalny parser 4B -> tani parser zdalny ->
# provider rozmowy / duży model. Model generuje wyłącznie IntentIR v1.
[orchestration]
enabled = true
mode = "active" # active | shadow | off
local_parser_provider = ""
local_parser_model = ""
cheap_parser_provider = ""
cheap_parser_model = ""
large_provider = ""
large_model = ""
top_k = 5
min_candidate_score = 0.32
deterministic_threshold = 0.93
local_execute_threshold = 0.82
cheap_remote_threshold = 0.68
max_parser_output_tokens = 192
allow_destructive = false
show_route = false
intent_catalog_paths = []

[providers.mock]
kind = "mock"
model = "mock"

# Przykład lokalnego endpointu OpenAI-compatible (vLLM/SGLang/llama.cpp).
# Po uruchomieniu ustaw orchestration.local_parser_provider = "local_4b".
[providers.local_4b]
kind = "openai_compat"
base_url = "http://127.0.0.1:8000/v1"
endpoint = "/chat/completions"
api_key_ref = ""
auth_required = false
model = "local-4b-instruct"
max_tokens = 512
max_output_tokens = 192
structured_mode = "json_schema"
timeout_seconds = 60.0
input_cost_per_million = 0.0
cached_input_cost_per_million = 0.0
output_cost_per_million = 0.0

[vault]
address = "http://127.0.0.1:8200"
token_ref = "env://VAULT_TOKEN"
namespace = ""
verify_tls = true
timeout_seconds = 10.0

[control]
base_url = "http://127.0.0.1:8088"
cli_path = ""
account_id = "softreck"
provider = "chatgpt"
tool_id = "codex"
bearer_ref = "file://~/.config/subactor-shell/control.token"
allowed_tools = ["cli.status", "cli.plan", "cli.execute"]
timeout_seconds = 10.0

# Nazwane connectory są allowlistą. Process connector nie używa shell=True;
# plan JSON jest przekazywany przez stdin. Przykład:
# [connectors.my_script]
# kind = "process"
# command = ["/opt/subactor/bin/my-connector", "--json-stdin"]
# allowed_operations = ["project.inspect", "project.apply"]
# inherit_env = false
# pass_env = ["PATH", "LANG", "LC_ALL", "TZ"]
# timeout_seconds = 30.0
# output_limit_bytes = 65536
# effect = "external_write"
# [connectors.my_script.env_refs]
# API_TOKEN = "vault://secret/subactor/connector#token"
"""

DEFAULTS: dict[str, Any] = {
    "defaults": {
        "provider": "mock",
        "model": "mock",
        "max_attachment_bytes": 5 * 1024 * 1024,
        "max_attachment_text_chars": 256 * 1024,
    },
    "context": {
        "recent_messages": 6,
        "max_history_chars": 12_000,
        "max_message_chars": 4_000,
        "max_data_chars": 6_000,
        "max_attachment_prompt_chars": 8_000,
        "artifact_chunk_chars": 1_800,
        "max_artifact_chunks": 4,
        "max_embedded_context_chars": 8_000,
        "max_route_context_chars": 4_000,
    },
    "orchestration": {
        "enabled": True,
        "mode": "active",
        "local_parser_provider": "",
        "local_parser_model": "",
        "cheap_parser_provider": "",
        "cheap_parser_model": "",
        "large_provider": "",
        "large_model": "",
        "top_k": 5,
        "min_candidate_score": 0.32,
        "deterministic_threshold": 0.93,
        "local_execute_threshold": 0.82,
        "cheap_remote_threshold": 0.68,
        "max_parser_output_tokens": 192,
        "allow_destructive": False,
        "show_route": False,
        "intent_catalog_paths": [],
    },
    "providers": {
        "mock": {"kind": "mock", "model": "mock"},
        "local_4b": {
            "kind": "openai_compat",
            "base_url": "http://127.0.0.1:8000/v1",
            "endpoint": "/chat/completions",
            "api_key_ref": "",
            "auth_required": False,
            "model": "local-4b-instruct",
            "max_tokens": 512,
            "max_output_tokens": 192,
            "structured_mode": "json_schema",
            "timeout_seconds": 60.0,
            "input_cost_per_million": 0.0,
            "cached_input_cost_per_million": 0.0,
            "output_cost_per_million": 0.0,
        },
    },
    "vault": {
        "address": "http://127.0.0.1:8200",
        "token_ref": "env://VAULT_TOKEN",
        "namespace": "",
        "verify_tls": True,
        "timeout_seconds": 10.0,
    },
    "control": {
        "base_url": "http://127.0.0.1:8088",
        "cli_path": "",
        "account_id": "softreck",
        "provider": "chatgpt",
        "tool_id": "codex",
        "bearer_ref": "file://~/.config/subactor-shell/control.token",
        "allowed_tools": ["cli.status", "cli.plan", "cli.execute"],
        "timeout_seconds": 10.0,
    },
    "connectors": {},
}


def _xdg_path(env_name: str, fallback: str) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else Path(fallback).expanduser()


def default_config_dir() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", "~/.config") / "subactor-shell"


def default_data_dir() -> Path:
    return _xdg_path("XDG_DATA_HOME", "~/.local/share") / "subactor-shell"


def default_config_path() -> Path:
    overridden = os.environ.get("SUBACTOR_SHELL_CONFIG")
    return Path(overridden).expanduser() if overridden else default_config_dir() / "config.toml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def ensure_private_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


@dataclass(slots=True)
class AppConfig:
    raw: dict[str, Any]
    config_path: Path
    data_dir: Path

    @property
    def default_provider(self) -> str:
        return str(self.raw["defaults"].get("provider", "mock"))

    @property
    def default_model(self) -> str:
        return str(self.raw["defaults"].get("model", "mock"))

    @property
    def max_attachment_bytes(self) -> int:
        return int(self.raw["defaults"].get("max_attachment_bytes", 5 * 1024 * 1024))

    @property
    def max_attachment_text_chars(self) -> int:
        return int(self.raw["defaults"].get("max_attachment_text_chars", 256 * 1024))

    @property
    def context(self) -> dict[str, Any]:
        return dict(self.raw.get("context", {}))

    @property
    def orchestration(self) -> dict[str, Any]:
        return dict(self.raw.get("orchestration", {}))

    @property
    def vault(self) -> dict[str, Any]:
        return dict(self.raw.get("vault", {}))

    @property
    def control(self) -> dict[str, Any]:
        return dict(self.raw.get("control", {}))

    @property
    def connectors(self) -> dict[str, Any]:
        value = self.raw.get("connectors", {})
        if not isinstance(value, dict):
            raise ValueError("connectors musi być tabelą TOML")
        return copy.deepcopy(value)

    def intent_catalog_paths(self) -> list[Path]:
        values = self.orchestration.get("intent_catalog_paths", [])
        if not isinstance(values, list):
            raise ValueError("orchestration.intent_catalog_paths musi być tablicą")
        result: list[Path] = []
        for value in values:
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                path = self.config_path.parent / path
            result.append(path)
        return result

    def provider_names(self) -> list[str]:
        return sorted(str(name) for name in self.raw.get("providers", {}))

    def provider(self, name: str) -> ProviderProfile:
        providers = self.raw.get("providers", {})
        if name not in providers:
            available = ", ".join(sorted(providers)) or "brak"
            raise KeyError(f"Nieznany provider '{name}'. Dostępne: {available}")
        item = dict(providers[name])
        model = str(item.get("model") or self.default_model)
        headers = item.get("extra_headers", {})
        if not isinstance(headers, dict):
            raise ValueError(f"providers.{name}.extra_headers musi być tabelą TOML")
        api_key_ref = str(item.get("api_key_ref", ""))
        return ProviderProfile(
            name=name,
            kind=str(item.get("kind", "openai_compat")),
            model=model,
            base_url=str(item.get("base_url", "")),
            endpoint=str(item.get("endpoint", "")),
            api_key_ref=api_key_ref,
            auth_required=bool(item.get("auth_required", bool(api_key_ref))),
            max_tokens=int(item.get("max_tokens", 4096)),
            max_output_tokens=int(item.get("max_output_tokens", item.get("max_tokens", 256))),
            structured_mode=str(item.get("structured_mode", "auto")),
            reasoning_effort=str(item.get("reasoning_effort", "")),
            anthropic_version=str(item.get("anthropic_version", "2023-06-01")),
            timeout_seconds=float(item.get("timeout_seconds", 120.0)),
            extra_headers={str(k): str(v) for k, v in headers.items()},
            input_cost_per_million=float(item.get("input_cost_per_million", 0.0)),
            cached_input_cost_per_million=float(
                item.get("cached_input_cost_per_million", item.get("input_cost_per_million", 0.0))
            ),
            output_cost_per_million=float(item.get("output_cost_per_million", 0.0)),
        )


def initialize_layout(
    config_path: Path | None = None, data_dir: Path | None = None
) -> tuple[Path, Path]:
    config_path = (config_path or default_config_path()).expanduser()
    data_dir = (data_dir or default_data_dir()).expanduser()
    ensure_private_dir(config_path.parent)
    ensure_private_dir(data_dir)
    ensure_private_dir(data_dir / "artifacts")
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    ensure_private_file(config_path)
    return config_path, data_dir


def load_config(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    *,
    create: bool = True,
) -> AppConfig:
    config_path = (config_path or default_config_path()).expanduser()
    data_dir = (data_dir or default_data_dir()).expanduser()
    if create:
        initialize_layout(config_path, data_dir)
    if config_path.exists():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
    else:
        loaded = {}
    raw = _deep_merge(DEFAULTS, loaded)
    return AppConfig(raw=raw, config_path=config_path, data_dir=data_dir)
