from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class IntentValidationError(ValueError):
    pass


_ALLOWED_MODES = {"ask", "plan", "execute"}
_FORBIDDEN_ARGUMENT_NAMES = {
    "cmd",
    "command",
    "shell",
    "script",
    "executable",
    "endpoint",
    "connector",
    "connector_id",
    "api_key",
    "password",
    "secret",
    "token",
}


@dataclass(slots=True)
class IntentIR:
    v: int
    intent_id: str
    mode: str = "execute"
    args: dict[str, Any] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IntentIR":
        if not isinstance(payload, dict):
            raise IntentValidationError("IntentIR musi być obiektem JSON")
        allowed_top = {
            "v",
            "intent_id",
            "mode",
            "args",
            "requirements",
            "constraints",
            "unresolved",
        }
        unknown_top = set(payload) - allowed_top
        if unknown_top:
            raise IntentValidationError(
                "IntentIR zawiera nieznane pola: " + ", ".join(sorted(unknown_top))
            )
        if payload.get("v", 1) != 1:
            raise IntentValidationError("Obsługiwana jest wyłącznie wersja IntentIR v1")
        intent_id = payload.get("intent_id")
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise IntentValidationError("intent_id jest wymagany")
        mode = payload.get("mode", "execute")
        if mode not in _ALLOWED_MODES:
            raise IntentValidationError("mode musi być ask, plan albo execute")
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise IntentValidationError("args musi być obiektem")
        clean_args: dict[str, Any] = {}
        for raw_name, value in args.items():
            name = str(raw_name)
            if name.casefold() in _FORBIDDEN_ARGUMENT_NAMES:
                raise IntentValidationError(
                    f"Argument '{name}' jest zarezerwowany; model nie może wybierać wykonawcy ani sekretu"
                )
            _validate_json_value(name, value)
            clean_args[name] = value

        def string_list(name: str) -> list[str]:
            value = payload.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise IntentValidationError(f"{name} musi być tablicą stringów")
            return list(dict.fromkeys(item.strip() for item in value if item.strip()))

        return cls(
            v=1,
            intent_id=intent_id.strip(),
            mode=mode,
            args=clean_args,
            requirements=string_list("requirements"),
            constraints=string_list("constraints"),
            unresolved=string_list("unresolved"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "intent_id": self.intent_id,
            "mode": self.mode,
            "args": self.args,
            "requirements": self.requirements,
            "constraints": self.constraints,
            "unresolved": self.unresolved,
        }


def _validate_json_value(name: str, value: Any) -> None:
    if isinstance(value, str):
        lowered = value.casefold()
        if "{{secret:" in lowered or lowered.startswith(("vault://", "env://", "file://")):
            raise IntentValidationError(
                f"Argument '{name}' nie może zawierać sekretu ani referencji do sekretu"
            )
        if len(value) > 4096:
            raise IntentValidationError(f"Argument '{name}' jest zbyt długi")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise IntentValidationError(f"Argument '{name}' ma zbyt wiele elementów")
        for item in value:
            if isinstance(item, (dict, list)):
                raise IntentValidationError("Zagnieżdżone struktury nie są dozwolone w IntentIR")
            _validate_json_value(name, item)
        return
    raise IntentValidationError(f"Nieobsługiwany typ argumentu '{name}'")


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise IntentValidationError("Model nie zwrócił obiektu JSON")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise IntentValidationError("Niepoprawny JSON zwrócony przez model") from exc
    if not isinstance(parsed, dict):
        raise IntentValidationError("Model musi zwrócić jeden obiekt JSON")
    return parsed


def intent_ir_schema(candidate_ids: list[str]) -> dict[str, Any]:
    intent_property: dict[str, Any] = {"type": "string", "minLength": 1}
    if candidate_ids:
        intent_property["enum"] = candidate_ids
    scalar: dict[str, Any] = {"type": ["string", "number", "integer", "boolean", "null"]}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SubactorIntentIRV1",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "v",
            "intent_id",
            "mode",
            "args",
            "requirements",
            "constraints",
            "unresolved",
        ],
        "properties": {
            "v": {"const": 1},
            "intent_id": intent_property,
            "mode": {"type": "string", "enum": ["ask", "plan", "execute"]},
            "args": {
                "type": "object",
                "additionalProperties": {
                    "anyOf": [scalar, {"type": "array", "maxItems": 64, "items": scalar}]
                },
            },
            "requirements": {"type": "array", "items": {"type": "string"}},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "unresolved": {"type": "array", "items": {"type": "string"}},
        },
    }
