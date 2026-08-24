from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .catalog import IntentDefinition
from .intent_ir import IntentIR
from .models import utc_now


class CompileError(ValueError):
    pass


_EFFECT_ORDER = {"read": 0, "local_write": 1, "external_write": 2, "destructive": 3}


@dataclass(slots=True)
class ExecutionStep:
    id: str
    kind: str
    connector: str
    operation: str
    args: dict[str, Any]
    effect: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "connector": self.connector,
            "operation": self.operation,
            "args": self.args,
            "effect": self.effect,
        }


@dataclass(slots=True)
class ExecutionPlan:
    id: str
    session_id: str
    intent_id: str
    mode: str
    steps: list[ExecutionStep]
    effect: str
    approval: str
    risk: str
    constraints: list[str]
    state_fingerprint: str
    catalog_fingerprint: str
    status: str = "planned"
    plan_hash: str = ""
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "mode": self.mode,
            "steps": [step.to_dict() for step in self.steps],
            "effect": self.effect,
            "approval": self.approval,
            "risk": self.risk,
            "constraints": self.constraints,
            "state_fingerprint": self.state_fingerprint,
            "catalog_fingerprint": self.catalog_fingerprint,
            "status": self.status,
            "plan_hash": self.plan_hash,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExecutionPlan":
        steps = [
            ExecutionStep(
                id=str(item.get("id", "")),
                kind=str(item.get("kind", "connector")),
                connector=str(item.get("connector", "")),
                operation=str(item.get("operation", "")),
                args=dict(item.get("args", {})),
                effect=str(item.get("effect", payload.get("effect", "read"))),
            )
            for item in payload.get("steps", [])
            if isinstance(item, dict)
        ]
        return cls(
            id=str(payload["id"]),
            session_id=str(payload["session_id"]),
            intent_id=str(payload.get("intent_id", "")),
            mode=str(payload.get("mode", "execute")),
            steps=steps,
            effect=str(payload.get("effect", "read")),
            approval=str(payload.get("approval", "none")),
            risk=str(payload.get("risk", "low")),
            constraints=[str(item) for item in payload.get("constraints", [])],
            state_fingerprint=str(payload.get("state_fingerprint", "")),
            catalog_fingerprint=str(payload.get("catalog_fingerprint", "")),
            status=str(payload.get("status", "planned")),
            plan_hash=str(payload.get("plan_hash", "")),
            created_at=str(payload.get("created_at", utc_now())),
        )


def compute_plan_hash(plan: ExecutionPlan | dict[str, Any]) -> str:
    payload = plan.to_dict() if isinstance(plan, ExecutionPlan) else dict(plan)
    payload.pop("plan_hash", None)
    payload.pop("status", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PlanCompiler:
    def compile(
        self,
        *,
        session_id: str,
        intent: IntentIR,
        definition: IntentDefinition,
        state_fingerprint: str,
        catalog_fingerprint: str,
    ) -> ExecutionPlan:
        execution = definition.execution
        kind = str(execution.get("kind", "chat"))
        if kind == "chat":
            raise CompileError("Intent konwersacyjny nie ma deterministycznego planu")
        raw_steps = execution.get("steps")
        step_specs = raw_steps if isinstance(raw_steps, list) and raw_steps else [execution]
        steps: list[ExecutionStep] = []
        for index, spec in enumerate(step_specs, start=1):
            if not isinstance(spec, dict):
                raise CompileError("execution.steps musi zawierać obiekty")
            step_kind = str(spec.get("kind", kind))
            if step_kind == "builtin":
                connector = "builtin"
            else:
                connector = str(spec.get("connector", execution.get("connector", "")))
            operation = str(spec.get("operation", execution.get("operation", "")))
            if not connector or not operation:
                raise CompileError("Definicja wykonania wymaga nazwanego connectora i operation")
            effect = str(spec.get("effect", execution.get("effect", "read")))
            if effect not in _EFFECT_ORDER:
                raise CompileError(f"Nieobsługiwany effect: {effect}")
            args = self._map_args(spec.get("argument_map", execution.get("argument_map")), intent.args)
            steps.append(
                ExecutionStep(
                    id=f"step_{index}",
                    kind="connector",
                    connector=connector,
                    operation=operation,
                    args=args,
                    effect=effect,
                )
            )
        top_effect = max((step.effect for step in steps), key=lambda item: _EFFECT_ORDER[item])
        plan = ExecutionPlan(
            id="plan_" + uuid.uuid4().hex,
            session_id=session_id,
            intent_id=intent.intent_id,
            mode=intent.mode,
            steps=steps,
            effect=top_effect,
            approval="none" if top_effect == "read" else "required",
            risk=definition.risk,
            constraints=list(dict.fromkeys([*definition.constraints, *intent.constraints])),
            state_fingerprint=state_fingerprint,
            catalog_fingerprint=catalog_fingerprint,
        )
        plan.plan_hash = compute_plan_hash(plan)
        return plan

    @staticmethod
    def _map_args(mapping: Any, args: dict[str, Any]) -> dict[str, Any]:
        if mapping is None:
            return dict(args)
        if not isinstance(mapping, dict):
            raise CompileError("argument_map musi być obiektem")
        result: dict[str, Any] = {}
        for target, source in mapping.items():
            if isinstance(source, str) and source.startswith("$args."):
                key = source[6:]
                if key in args:
                    result[str(target)] = args[key]
            elif source == "$all_args":
                result[str(target)] = dict(args)
            else:
                result[str(target)] = source
        return result
