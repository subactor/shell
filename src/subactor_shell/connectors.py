from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .compiler import ExecutionPlan, ExecutionStep
from .config import AppConfig
from .control import SubactorControlClient
from .models import utc_now
from .redaction import ExactRedactor
from .secret_refs import SecretResolver
from .store import Store


class ConnectorError(RuntimeError):
    pass


_EFFECT_ORDER = {"read": 0, "local_write": 1, "external_write": 2, "destructive": 3}


@dataclass(slots=True)
class ConnectorDefinition:
    name: str
    kind: str
    allowed_operations: list[str]
    effect: str = "read"
    command: list[str] = field(default_factory=list)
    env_refs: dict[str, str] = field(default_factory=dict)
    inherit_env: bool = False
    pass_env: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    output_limit_bytes: int = 65_536
    base_url: str = ""
    path: str = ""
    method: str = "POST"
    bearer_ref: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "allowed_operations": sorted(self.allowed_operations),
            "effect": self.effect,
            "command": self.command,
            "env_ref_names": sorted(self.env_refs),
            "inherit_env": self.inherit_env,
            "pass_env": sorted(self.pass_env),
            "base_url": self.base_url,
            "path": self.path,
            "method": self.method,
            "has_bearer_ref": bool(self.bearer_ref),
        }


@dataclass(slots=True)
class ExecutionReceipt:
    id: str
    plan_id: str
    session_id: str
    ok: bool
    steps: list[dict[str, Any]]
    summary: str
    state_after: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "ok": self.ok,
            "steps": self.steps,
            "summary": self.summary,
            "state_after": self.state_after,
            "created_at": self.created_at,
        }


class ConnectorRegistry:
    def __init__(self, config: AppConfig):
        self._items: dict[str, ConnectorDefinition] = {
            "builtin": ConnectorDefinition(
                name="builtin",
                kind="builtin",
                allowed_operations=[
                    "bridge.help",
                    "session.list",
                    "data.list",
                    "secret.list",
                    "usage.summary",
                ],
                effect="read",
            ),
            "subactor_control": ConnectorDefinition(
                name="subactor_control",
                kind="control",
                allowed_operations=[str(item) for item in config.control.get("allowed_tools", [])],
                effect="external_write",
            ),
            "subactor_cli": ConnectorDefinition(
                name="subactor_cli",
                kind="subactor_cli",
                allowed_operations=["cli.status"],
                effect="read",
            ),
        }
        for name, raw in config.connectors.items():
            if not isinstance(raw, dict):
                raise ValueError(f"connectors.{name} musi być tabelą TOML")
            definition = self._from_config(str(name), raw)
            self._items[definition.name] = definition
        public = [self._items[name].public_dict() for name in sorted(self._items)]
        encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def list(self) -> list[ConnectorDefinition]:
        return [self._items[name] for name in sorted(self._items)]

    def get(self, name: str) -> ConnectorDefinition | None:
        return self._items.get(name)

    def validate_step(self, step: ExecutionStep) -> ConnectorDefinition:
        definition = self.get(step.connector)
        if not definition:
            raise ConnectorError(f"Connector '{step.connector}' nie jest skonfigurowany")
        if step.operation not in definition.allowed_operations:
            raise ConnectorError(
                f"Operation '{step.operation}' nie jest dozwolona dla connectora '{step.connector}'"
            )
        if step.effect not in _EFFECT_ORDER or definition.effect not in _EFFECT_ORDER:
            raise ConnectorError("Connector lub krok ma nieprawidłowy effect")
        if _EFFECT_ORDER[step.effect] > _EFFECT_ORDER[definition.effect]:
            raise ConnectorError(
                f"Krok ma effect {step.effect}, większy niż limit connectora {definition.effect}"
            )
        return definition

    @staticmethod
    def _from_config(name: str, raw: dict[str, Any]) -> ConnectorDefinition:
        kind = str(raw.get("kind", "")).strip().lower()
        allowed = raw.get("allowed_operations", [])
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(f"connectors.{name}.allowed_operations musi być niepustą tablicą")
        effect = str(raw.get("effect", "external_write"))
        if effect not in _EFFECT_ORDER:
            raise ValueError(f"connectors.{name}.effect jest nieprawidłowy")
        env_refs = raw.get("env_refs", {})
        if not isinstance(env_refs, dict):
            raise ValueError(f"connectors.{name}.env_refs musi być tabelą")
        definition = ConnectorDefinition(
            name=name,
            kind=kind,
            allowed_operations=[str(item) for item in allowed],
            effect=effect,
            env_refs={str(k): str(v) for k, v in env_refs.items()},
            inherit_env=bool(raw.get("inherit_env", False)),
            timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
            output_limit_bytes=max(1024, int(raw.get("output_limit_bytes", 65_536))),
        )
        if kind == "process":
            command = raw.get("command", [])
            if not isinstance(command, list) or not command:
                raise ValueError(f"connectors.{name}.command musi być niepustą tablicą argv")
            definition.command = [str(item) for item in command]
            executable = Path(definition.command[0]).expanduser()
            if not executable.is_absolute():
                raise ValueError(f"connectors.{name}.command[0] musi być ścieżką absolutną")
            definition.command[0] = str(executable)
            pass_env = raw.get(
                "pass_env",
                ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT", "WINDIR"],
            )
            if not isinstance(pass_env, list) or not all(
                isinstance(item, str) and item and "=" not in item and "\0" not in item
                for item in pass_env
            ):
                raise ValueError(f"connectors.{name}.pass_env musi być tablicą nazw zmiennych")
            definition.pass_env = list(dict.fromkeys(pass_env))
        elif kind == "http":
            definition.base_url = str(raw.get("base_url", "")).rstrip("/")
            definition.path = str(raw.get("path", "/"))
            definition.method = str(raw.get("method", "POST")).upper()
            definition.bearer_ref = str(raw.get("bearer_ref", ""))
            parsed = urlsplit(definition.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(f"connectors.{name}.base_url musi być adresem HTTP(S)")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"connectors.{name}.base_url nie może zawierać credentiali ani query")
            if definition.method not in {"POST", "PUT", "PATCH", "DELETE", "GET"}:
                raise ValueError(f"connectors.{name}.method nie jest dozwolona")
        else:
            raise ValueError(f"connectors.{name}.kind musi być process albo http")
        return definition


class ConnectorExecutor:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        resolver: SecretResolver,
        registry: ConnectorRegistry,
    ):
        self.config = config
        self.store = store
        self.resolver = resolver
        self.registry = registry

    async def execute(self, plan: ExecutionPlan, *, approved: bool) -> ExecutionReceipt:
        step_receipts: list[dict[str, Any]] = []
        all_ok = True
        for step in plan.steps:
            started = utc_now()
            try:
                definition = self.registry.validate_step(step)
                result = await self._execute_step(plan, step, definition, approved=approved)
                step_receipts.append(
                    {
                        "step_id": step.id,
                        "connector": step.connector,
                        "operation": step.operation,
                        "ok": True,
                        "started_at": started,
                        "finished_at": utc_now(),
                        "result": result,
                    }
                )
            except Exception as exc:
                all_ok = False
                step_receipts.append(
                    {
                        "step_id": step.id,
                        "connector": step.connector,
                        "operation": step.operation,
                        "ok": False,
                        "started_at": started,
                        "finished_at": utc_now(),
                        "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                    }
                )
                break
        summary = (
            f"Wykonano {len(step_receipts)}/{len(plan.steps)} kroków."
            if all_ok
            else f"Wykonanie zatrzymane na kroku {len(step_receipts)}."
        )
        return ExecutionReceipt(
            id="receipt_" + uuid.uuid4().hex,
            plan_id=plan.id,
            session_id=plan.session_id,
            ok=all_ok,
            steps=step_receipts,
            summary=summary,
            state_after=self.store.state_fingerprint(),
        )

    async def _execute_step(
        self,
        plan: ExecutionPlan,
        step: ExecutionStep,
        definition: ConnectorDefinition,
        *,
        approved: bool,
    ) -> Any:
        if definition.kind == "builtin":
            return self._builtin(step.operation, step.args, plan.session_id)
        if definition.kind == "control":
            client = SubactorControlClient(self.config.control, self.resolver)
            return await asyncio.to_thread(
                client.call_tool,
                step.operation,
                step.args,
                allow_execute=approved and step.operation == "cli.execute",
            )
        if definition.kind == "subactor_cli":
            return await self._subactor_cli(step.operation)
        if definition.kind == "process":
            return await self._process(plan, step, definition)
        if definition.kind == "http":
            return await self._http(plan, step, definition)
        raise ConnectorError(f"Nieobsługiwany connector kind: {definition.kind}")

    async def _subactor_cli(self, operation: str) -> dict[str, Any]:
        if operation != "cli.status":
            raise ConnectorError(f"Nieznana operacja Subactor CLI: {operation}")
        configured = str(self.config.control.get("cli_path", "")).strip()
        discovered = configured or shutil.which("subactor") or ""
        executable = Path(discovered).expanduser()
        if not executable.is_absolute() or not executable.is_file():
            raise ConnectorError(
                "Nie znaleziono Subactor CLI; ustaw control.cli_path na bezwzględną ścieżkę"
            )
        executable = executable.resolve()
        allowed_env = {
            "HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PATH",
            "SUBACTOR_ADMIN_TOKEN",
            "SUBACTOR_CONTROL_URL",
            "SUBACTOR_PLANFILE_URL",
            "TZ",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
        env = {name: value for name, value in os.environ.items() if name in allowed_env}
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "status",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=float(self.config.control.get("timeout_seconds", 10.0)),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ConnectorError("Subactor CLI przekroczył timeout") from exc
        output = stdout[:65_536].decode("utf-8", errors="replace").strip()
        error = stderr[:4_096].decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise ConnectorError(
                f"Subactor CLI zakończył się kodem {process.returncode}: {error[:500]}"
            )
        occurred_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return {
            "message": (
                f"[{occurred_at}] source=subactor-cli operation=cli.status exit=0\n{output}"
            ),
            "occurred_at": occurred_at,
            "source": "subactor-cli",
        }

    def _builtin(self, operation: str, args: dict[str, Any], session_id: str) -> Any:
        if operation == "bridge.help":
            return {
                "message": (
                    "Subactor Shell Bridge: trwałe rozmowy, WorkingState, IntentIR, "
                    "lokalne plany, named connectors, Vault refs, ACP i telemetria tokenów."
                )
            }
        if operation == "session.list":
            return {
                "sessions": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "provider": item.provider,
                        "model": item.model,
                        "updated_at": item.updated_at,
                    }
                    for item in self.store.list_sessions(limit=int(args.get("limit", 20)))
                ]
            }
        if operation == "data.list":
            return {
                "data": [
                    {
                        "name": name,
                        "kind": kind,
                        "value": value if kind == "artifact" else f"{len(value)} chars",
                    }
                    for name, kind, value in self.store.list_data()
                ]
            }
        if operation == "secret.list":
            return {
                "bindings": [
                    {"alias": alias, "reference": reference}
                    for alias, reference in self.store.list_secret_bindings()
                ],
                "values_read": False,
            }
        if operation == "usage.summary":
            return self.store.usage_summary(session_id)
        raise ConnectorError(f"Nieznana operacja builtin: {operation}")

    async def _process(
        self,
        plan: ExecutionPlan,
        step: ExecutionStep,
        definition: ConnectorDefinition,
    ) -> Any:
        env = (
            os.environ.copy()
            if definition.inherit_env
            else {name: os.environ[name] for name in definition.pass_env if name in os.environ}
        )
        sensitive: list[str] = []
        for name, reference in definition.env_refs.items():
            value = self.resolver.resolve(reference)
            env[name] = value
            sensitive.append(value)
        payload = json.dumps(
            {
                "plan_id": plan.id,
                "plan_hash": plan.plan_hash,
                "session_id": plan.session_id,
                "intent_id": plan.intent_id,
                "operation": step.operation,
                "args": step.args,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *definition.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=definition.timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            if process is not None:
                process.kill()
                await process.wait()
            raise ConnectorError("Connector process przekroczył timeout") from exc
        limit = definition.output_limit_bytes
        redactor = ExactRedactor(sensitive)
        out_text = redactor.redact(stdout[:limit].decode("utf-8", errors="replace"))
        err_text = redactor.redact(stderr[:limit].decode("utf-8", errors="replace"))
        if process.returncode != 0:
            raise ConnectorError(
                f"Connector process zakończył się kodem {process.returncode}: {err_text[:500]}"
            )
        result: dict[str, Any] = {
            "exit_code": int(process.returncode or 0),
            "truncated": len(stdout) > limit or len(stderr) > limit,
        }
        try:
            parsed = json.loads(out_text)
            if isinstance(parsed, (dict, list)):
                result["json"] = parsed
            else:
                result["stdout"] = out_text[:4000]
        except json.JSONDecodeError:
            result["stdout"] = out_text[:4000]
        if err_text:
            result["stderr"] = err_text[:2000]
        return result

    async def _http(
        self,
        plan: ExecutionPlan,
        step: ExecutionStep,
        definition: ConnectorDefinition,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        sensitive: list[str] = []
        if definition.bearer_ref:
            token = self.resolver.resolve(definition.bearer_ref)
            headers["Authorization"] = f"Bearer {token}"
            sensitive.append(token)
        body = {
            "plan_id": plan.id,
            "plan_hash": plan.plan_hash,
            "session_id": plan.session_id,
            "intent_id": plan.intent_id,
            "operation": step.operation,
            "args": step.args,
        }
        url = urljoin(definition.base_url.rstrip("/") + "/", definition.path.lstrip("/"))
        try:
            async with httpx.AsyncClient(timeout=definition.timeout_seconds) as client:
                response = await client.request(definition.method, url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"Błąd HTTP connectora ({type(exc).__name__})") from exc
        raw = response.content[: definition.output_limit_bytes]
        text = ExactRedactor(sensitive).redact(raw.decode("utf-8", errors="replace"))
        if response.status_code >= 400:
            raise ConnectorError(f"HTTP connector zwrócił {response.status_code}: {text[:500]}")
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = text[:4000]
        return {
            "status": response.status_code,
            "body": parsed,
            "truncated": len(response.content) > definition.output_limit_bytes,
        }
