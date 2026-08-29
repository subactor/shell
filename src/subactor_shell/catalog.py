from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .intent_ir import IntentIR, IntentValidationError


_WORD_RE = re.compile(r"[\w.-]+", re.UNICODE)
_TEMPLATE_FIELD_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_.-]{0,63})\}")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"[^\w{}:/.=-]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def text_tokens(value: str) -> list[str]:
    return [token.casefold() for token in _WORD_RE.findall(normalize_text(value)) if token]


@dataclass(slots=True)
class IntentDefinition:
    id: str
    description: str
    phrases: list[str]
    required_args: list[str] = field(default_factory=list)
    optional_args: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=lambda: {"kind": "chat"})
    risk: str = "low"
    constraints: list[str] = field(default_factory=list)
    source: str = "builtin"

    @property
    def allowed_args(self) -> set[str]:
        return set(self.required_args) | set(self.optional_args) | set(self.defaults)

    def to_summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "required_args": self.required_args,
            "optional_args": self.optional_args,
            "risk": self.risk,
            "execution_kind": str(self.execution.get("kind", "chat")),
            "phrases": self.phrases[:6],
            "source": self.source,
        }

    def validate_ir(self, payload: IntentIR | dict[str, Any]) -> IntentIR:
        ir = payload if isinstance(payload, IntentIR) else IntentIR.from_dict(payload)
        if ir.intent_id != self.id:
            raise IntentValidationError("IntentIR nie odpowiada wybranej definicji")
        unknown = set(ir.args) - self.allowed_args
        if unknown:
            raise IntentValidationError(
                "IntentIR zawiera niedozwolone argumenty: " + ", ".join(sorted(unknown))
            )
        args = {**self.defaults, **ir.args}
        missing = [name for name in self.required_args if name not in args or args[name] in ("", None)]
        unresolved = list(dict.fromkeys([*ir.unresolved, *missing]))
        constraints = list(dict.fromkeys([*self.constraints, *ir.constraints]))
        return IntentIR(
            v=1,
            intent_id=ir.intent_id,
            mode=ir.mode,
            args=args,
            requirements=ir.requirements,
            constraints=constraints,
            unresolved=unresolved,
        )


@dataclass(slots=True)
class CandidateMatch:
    intent: IntentDefinition
    score: float
    matched_phrase: str
    extracted_args: dict[str, str] = field(default_factory=dict)
    exact: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent.id,
            "score": round(self.score, 6),
            "matched_phrase": self.matched_phrase,
            "extracted_args": self.extracted_args,
            "exact": self.exact,
        }


class IntentCatalog:
    def __init__(self, intents: Iterable[IntentDefinition]):
        self._by_id: dict[str, IntentDefinition] = {}
        for intent in intents:
            if intent.id:
                self._by_id[intent.id] = intent
        serialized = json.dumps(
            [item.to_summary() | {"execution": item.execution} for item in self.list()],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, intent_id: str) -> IntentDefinition | None:
        return self._by_id.get(intent_id)

    def list(self) -> list[IntentDefinition]:
        return [self._by_id[key] for key in sorted(self._by_id)]

    @classmethod
    def load(cls, paths: Iterable[Path] = ()) -> "IntentCatalog":
        intents = {item.id: item for item in builtin_intents()}
        for path in paths:
            for definition in load_intent_definitions(path):
                intents[definition.id] = definition
        return cls(intents.values())


class CandidateRetriever:
    def __init__(self, catalog: IntentCatalog):
        self.catalog = catalog
        self._docs: list[tuple[IntentDefinition, str, list[str]]] = []
        document_frequency: dict[str, int] = {}
        for intent in catalog.list():
            phrases = intent.phrases or [intent.description, intent.id]
            for phrase in phrases:
                tokens = text_tokens(_TEMPLATE_FIELD_RE.sub(" value ", phrase))
                self._docs.append((intent, phrase, tokens))
                for token in set(tokens):
                    document_frequency[token] = document_frequency.get(token, 0) + 1
        count = max(1, len(self._docs))
        self._idf = {
            token: math.log((count + 1) / (frequency + 0.5)) + 1.0
            for token, frequency in document_frequency.items()
        }

    def retrieve(self, text: str, *, top_k: int = 5) -> list[CandidateMatch]:
        normalized = normalize_text(text)
        query_tokens = text_tokens(normalized)
        query_set = set(query_tokens)
        best: dict[str, CandidateMatch] = {}
        for intent, phrase, phrase_tokens in self._docs:
            extracted = match_phrase_template(text, phrase)
            normalized_phrase = normalize_text(_TEMPLATE_FIELD_RE.sub(" value ", phrase))
            literal_exact = normalize_text(text) == normalize_text(phrase)
            exact = literal_exact or extracted is not None
            if exact:
                score = 1.0 if literal_exact else 0.985
            else:
                phrase_set = set(phrase_tokens)
                shared = query_set & phrase_set
                query_weight = sum(self._idf.get(token, 1.0) for token in query_set) or 1.0
                overlap = sum(self._idf.get(token, 1.0) for token in shared) / query_weight
                sequence = SequenceMatcher(None, normalized, normalized_phrase).ratio()
                containment = 1.0 if normalized_phrase and normalized_phrase in normalized else 0.0
                coverage = len(shared) / max(1, len(phrase_set))
                score = 0.48 * overlap + 0.24 * sequence + 0.18 * coverage + 0.10 * containment
            candidate = CandidateMatch(
                intent=intent,
                score=max(0.0, min(1.0, score)),
                matched_phrase=phrase,
                extracted_args=extracted or {},
                exact=exact,
            )
            previous = best.get(intent.id)
            if previous is None or candidate.score > previous.score:
                best[intent.id] = candidate
        return sorted(best.values(), key=lambda item: (-item.score, item.intent.id))[: max(1, top_k)]


def match_phrase_template(text: str, phrase: str) -> dict[str, str] | None:
    fields = list(_TEMPLATE_FIELD_RE.finditer(phrase))
    if not fields:
        return {} if normalize_text(text) == normalize_text(phrase) else None
    parts: list[str] = []
    cursor = 0
    for index, match in enumerate(fields):
        literal = phrase[cursor : match.start()]
        escaped = re.escape(literal).replace(r"\ ", r"\s+")
        parts.append(escaped)
        parts.append(fr"(?P<f{index}>.+?)")
        cursor = match.end()
    parts.append(re.escape(phrase[cursor:]).replace(r"\ ", r"\s+"))
    matched = re.match(r"^\s*" + "".join(parts) + r"\s*$", text, flags=re.IGNORECASE | re.UNICODE)
    if not matched:
        return None
    result: dict[str, str] = {}
    for index, field_match in enumerate(fields):
        value = matched.group(f"f{index}").strip().strip("\"'")
        if value:
            result[field_match.group(1)] = value
    return result


def builtin_intents() -> list[IntentDefinition]:
    return [
        IntentDefinition(
            id="bridge.help",
            description="Pokaż możliwości i bezpieczne komendy Subactor Shell Bridge.",
            phrases=["pomoc", "pokaż pomoc", "co potrafisz", "help"],
            execution={"kind": "builtin", "operation": "bridge.help", "effect": "read"},
        ),
        IntentDefinition(
            id="session.list",
            description="Pokaż zapisane sesje rozmów.",
            phrases=["pokaż sesje", "lista sesji", "ostatnie rozmowy", "wymień sesje"],
            optional_args=["limit"],
            defaults={"limit": 20},
            execution={
                "kind": "builtin",
                "operation": "session.list",
                "effect": "read",
                "argument_map": {"limit": "$args.limit"},
            },
        ),
        IntentDefinition(
            id="data.list",
            description="Pokaż nazwy jawnych danych i artefaktów bez rozwijania treści.",
            phrases=["pokaż dane", "lista danych", "jakie dane są zapisane"],
            execution={"kind": "builtin", "operation": "data.list", "effect": "read"},
        ),
        IntentDefinition(
            id="secret.list",
            description="Pokaż aliasy i referencje sekretów bez odczytywania wartości.",
            phrases=["pokaż bindingi sekretów", "lista sekretów", "jakie sekrety są podpięte"],
            execution={"kind": "builtin", "operation": "secret.list", "effect": "read"},
            constraints=["no_secret_export"],
        ),
        IntentDefinition(
            id="usage.summary",
            description="Pokaż telemetryczne zużycie tokenów i udział tras lokalnych.",
            phrases=["pokaż zużycie tokenów", "statystyki tokenów", "metryki llm", "koszt tokenów"],
            execution={"kind": "builtin", "operation": "usage.summary", "effect": "read"},
        ),
        IntentDefinition(
            id="control.status",
            description="Sprawdź status istniejącego Subactor Control przez cli.status.",
            phrases=[
                "pokaż status subactora",
                "sprawdź status subactora",
                "status control",
                "jakie zadania są otwarte",
                "jakie zadania sa otwarte",
                "pokaż otwarte zadania",
                "pokaz otwarte zadania",
                "co teraz",
            ],
            execution={
                "kind": "connector",
                "connector": "subactor_cli",
                "operation": "cli.status",
                "effect": "read",
            },
        ),
        IntentDefinition(
            id="control.tickets",
            description="Pokaż otwarte tickety w Planfile przez cli.tickets.",
            phrases=[
                "pokaż tickety",
                "lista ticketów",
                "otwarte tickety",
                "jakie są tickety",
                "pokaż otwarte tickety",
                "pokaz tickety",
                "tickety",
                "tickets",
            ],
            execution={
                "kind": "connector",
                "connector": "subactor_cli",
                "operation": "cli.tickets",
                "effect": "read",
            },
        ),
        IntentDefinition(
            id="control.projects",
            description="Pokaż portfolio projektów przez cli.projects.",
            phrases=[
                "pokaż projekty",
                "lista projektów",
                "portfolio projektów",
                "pokaz projekty",
                "projekty",
                "projects",
            ],
            execution={
                "kind": "connector",
                "connector": "subactor_cli",
                "operation": "cli.projects",
                "effect": "read",
            },
        ),
        IntentDefinition(
            id="control.registries",
            description="Pokaż rejestry Organization OS przez cli.registries.",
            phrases=[
                "pokaż rejestry",
                "lista rejestrów",
                "pokaz rejestry",
                "rejestry",
                "registries",
                "pokaż organizacje",
                "lista organizacji",
                "pokaz organizacje",
                "organizacje",
                "organizations",
            ],
            execution={
                "kind": "connector",
                "connector": "subactor_cli",
                "operation": "cli.registries",
                "effect": "read",
            },
        ),
        IntentDefinition(
            id="control.watch",
            description="Pokaż podgląd kolejek i procesów przez cli.watch.",
            phrases=[
                "pokaż procesy",
                "podgląd procesów",
                "podgląd kolejek",
                "stan kolejek",
                "watch",
                "kolejki agentów",
            ],
            execution={
                "kind": "connector",
                "connector": "subactor_cli",
                "operation": "cli.watch",
                "effect": "read",
            },
        ),
        IntentDefinition(
            id="control.plan",
            description="Utwórz plan w istniejącym Subactor Control przez cli.plan.",
            phrases=["zaplanuj {request}", "przygotuj plan {request}", "stwórz plan {request}"],
            required_args=["request"],
            execution={
                "kind": "connector",
                "connector": "subactor_control",
                "operation": "cli.plan",
                "effect": "read",
                "argument_map": {"request": "$args.request"},
            },
        ),
    ]



def load_intent_definitions(path: Path) -> list[IntentDefinition]:
    path = path.expanduser()
    if not path.exists():
        return []
    files = [path]
    if path.is_dir():
        files = sorted(item for item in path.rglob("*.json") if "schema" not in item.name.casefold())
    result: list[IntentDefinition] = []
    for file_path in files:
        if not file_path.is_file():
            continue
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("intents"), list):
            entries: list[Any] = payload["intents"]
        elif isinstance(payload, list):
            entries = payload
        else:
            entries = [payload]
        for entry in entries:
            definition = _definition_from_payload(entry, source=str(file_path))
            if definition:
                result.append(definition)
    return result


def _nested(payload: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = payload
        found = True
        for segment in path.split("."):
            if not isinstance(current, dict) or segment not in current:
                found = False
                break
            current = current[segment]
        if found:
            return current
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item).strip()]
    return []


def _definition_from_payload(payload: Any, *, source: str) -> IntentDefinition | None:
    if not isinstance(payload, dict):
        return None
    intent_id = _nested(
        payload,
        "intent_id",
        "intentId",
        "id",
        "name",
        "nlp_uri",
        "nlpUri",
        "uri",
        "nlp.uri",
    )
    if not isinstance(intent_id, str) or not intent_id.strip():
        return None
    intent_id = intent_id.strip()
    description = str(_nested(payload, "description", "summary", "title", "nlp.description") or intent_id)
    phrases: list[str] = []
    for key in ("phrases", "createPhrases", "examples", "utterances", "nlp.phrases", "nlp.createPhrases"):
        phrases.extend(_string_list(_nested(payload, key)))
    phrases = list(dict.fromkeys(item.strip() for item in phrases if item.strip())) or [description, intent_id]

    required = _string_list(
        _nested(
            payload,
            "required_args",
            "requiredArgs",
            "required",
            "situation_schema.required",
            "situationSchema.required",
            "input_schema.required",
            "inputSchema.required",
        )
    )
    properties = _nested(
        payload,
        "situation_schema.properties",
        "situationSchema.properties",
        "input_schema.properties",
        "inputSchema.properties",
        "args_schema.properties",
    )
    optional = [str(key) for key in properties if str(key) not in required] if isinstance(properties, dict) else []
    optional.extend(_string_list(_nested(payload, "optional_args", "optionalArgs")))
    optional = list(dict.fromkeys(optional))

    execution = _nested(payload, "execution", "runtime.execution")
    if not isinstance(execution, dict):
        connector = _nested(payload, "connector", "connector_id", "connectorId")
        operation = _nested(payload, "operation", "operation_id", "operationId")
        recipe = _nested(payload, "recipe", "recipe_uri", "recipeUri", "urirun")
        if connector and operation:
            execution = {
                "kind": "connector",
                "connector": str(connector),
                "operation": str(operation),
                "effect": str(_nested(payload, "effect") or "external_write"),
            }
        elif recipe:
            execution = {
                "kind": "process_pack",
                "connector": str(connector or "process_pack"),
                "operation": str(recipe),
                "effect": str(_nested(payload, "effect") or "external_write"),
            }
        else:
            execution = {"kind": "chat"}
    defaults = _nested(payload, "defaults", "nlp.defaults")
    if not isinstance(defaults, dict):
        defaults = {}
    constraints = _string_list(_nested(payload, "constraints", "policy.constraints"))
    risk = str(_nested(payload, "risk", "risk_class", "riskClass") or "low")
    return IntentDefinition(
        id=intent_id,
        description=description,
        phrases=phrases,
        required_args=required,
        optional_args=optional,
        defaults=defaults,
        execution=dict(execution),
        risk=risk,
        constraints=constraints,
        source=source,
    )
