from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .catalog import CandidateMatch, CandidateRetriever, IntentCatalog, normalize_text
from .config import AppConfig
from .intent_ir import IntentIR, IntentValidationError, intent_ir_schema
from .models import Session
from .providers import ProviderBundle
from .providers.base import ProviderError
from .secret_refs import SecretResolver
from .store import Store
from .token_budget import TokenUsage


ProviderBuilder = Callable[[Any, SecretResolver], ProviderBundle]


@dataclass(slots=True)
class ParserUsageRecord:
    provider: str
    model: str
    purpose: str
    usage: TokenUsage
    latency_ms: int = 0


@dataclass(slots=True)
class RoutingDecision:
    route: str
    reason: str
    confidence: float = 0.0
    intent: IntentIR | None = None
    provider: str = ""
    model: str = ""
    candidates: list[CandidateMatch] = field(default_factory=list)
    parser_usage: list[ParserUsageRecord] = field(default_factory=list)
    parser_errors: list[str] = field(default_factory=list)
    cache_hit: bool = False

    @property
    def intent_id(self) -> str:
        return self.intent.intent_id if self.intent else ""

    def route_context(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "confidence": round(self.confidence, 4),
            "intent": self.intent.to_dict() if self.intent else None,
            "candidates": [
                {
                    "intent_id": item.intent.id,
                    "score": round(item.score, 4),
                    "required_args": item.intent.required_args,
                    "description": item.intent.description[:160],
                }
                for item in self.candidates[:5]
            ],
            "parser_errors": self.parser_errors[-3:],
        }


class Router:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        resolver: SecretResolver,
        provider_builder: ProviderBuilder,
        catalog: IntentCatalog,
    ):
        self.config = config
        self.store = store
        self.resolver = resolver
        self.provider_builder = provider_builder
        self.catalog = catalog
        self.retriever = CandidateRetriever(catalog)
        options = config.orchestration
        self.enabled = bool(options.get("enabled", True))
        self.mode = str(options.get("mode", "active")).strip().lower()
        self.top_k = max(1, int(options.get("top_k", 5)))
        self.min_candidate_score = float(options.get("min_candidate_score", 0.32))
        self.deterministic_threshold = float(options.get("deterministic_threshold", 0.93))
        self.local_threshold = float(options.get("local_execute_threshold", 0.82))
        self.cheap_threshold = float(options.get("cheap_remote_threshold", 0.68))
        self.max_output_tokens = max(64, int(options.get("max_parser_output_tokens", 192)))
        self.cache_ttl = max(1, int(options.get("cache_ttl_seconds", 86_400)))
        self.local_provider = str(options.get("local_parser_provider", "")).strip()
        self.local_model = str(options.get("local_parser_model", "")).strip()
        self.cheap_provider = str(options.get("cheap_parser_provider", "")).strip()
        self.cheap_model = str(options.get("cheap_parser_model", "")).strip()
        self.large_provider = str(options.get("large_provider", "")).strip()
        self.large_model = str(options.get("large_model", "")).strip()

    async def route(self, session: Session, text: str, *, cancel_event=None) -> RoutingDecision:
        if not self.enabled or self.mode == "off":
            return RoutingDecision(
                route="chat_provider",
                reason="Orkiestracja DSL jest wyłączona",
                provider=session.provider,
                model=session.model,
            )

        candidates = self.retriever.retrieve(text, top_k=self.top_k)
        top_score = candidates[0].score if candidates else 0.0
        margin = top_score - (candidates[1].score if len(candidates) > 1 else 0.0)
        cache_key = self._cache_key(text)
        cached = self.store.cache_get(cache_key)
        if cached:
            definition = self.catalog.get(str(cached.get("intent_id", "")))
            if definition:
                try:
                    intent = definition.validate_ir(cached)
                    return RoutingDecision(
                        route="cache",
                        reason="Powtórzone polecenie odtworzono z cache walidowanego IntentIR",
                        confidence=float(cached.get("_confidence", 0.95)),
                        intent=intent,
                        candidates=candidates,
                        cache_hit=True,
                    )
                except IntentValidationError:
                    pass

        deterministic = self._deterministic(candidates)
        if deterministic:
            intent, confidence = deterministic
            self._cache(cache_key, intent, confidence)
            return RoutingDecision(
                route="deterministic",
                reason="Exact/template phrase match oraz lokalna walidacja argumentów",
                confidence=confidence,
                intent=intent,
                candidates=candidates,
            )

        if not candidates or top_score < self.min_candidate_score:
            return RoutingDecision(
                route="chat_provider",
                reason="Brak wystarczająco bliskiego intentu w lokalnym katalogu",
                confidence=top_score,
                provider=self.large_provider or session.provider,
                model=self.large_model or session.model,
                candidates=candidates,
            )

        attempts: list[ParserUsageRecord] = []
        errors: list[str] = []
        configured: list[tuple[str, str, str, float]] = []
        seen: set[tuple[str, str]] = set()
        for route, provider_name, model_name, threshold in (
            ("local_4b", self.local_provider, self.local_model, self.local_threshold),
            ("cheap_remote", self.cheap_provider, self.cheap_model, self.cheap_threshold),
            ("large_remote", self.large_provider, self.large_model, 0.50),
        ):
            if not provider_name:
                continue
            profile = self.config.provider(provider_name)
            model = model_name or profile.model
            key = (provider_name, model)
            if key in seen:
                continue
            seen.add(key)
            configured.append((route, provider_name, model, threshold))

        for route, provider_name, model_name, threshold in configured:
            intent, usage_record, error, confidence = await self._parse_with_provider(
                text,
                candidates,
                route=route,
                provider_name=provider_name,
                model_name=model_name,
                top_score=top_score,
                margin=margin,
            )
            if usage_record:
                attempts.append(usage_record)
            if error:
                errors.append(error)
            if intent is not None and confidence >= threshold:
                self._cache(cache_key, intent, confidence)
                return RoutingDecision(
                    route=route,
                    reason="Poprawny IntentIR z krótkiej listy kandydatów i walidacji lokalnej",
                    confidence=self._calibrated(intent.intent_id, route, confidence),
                    intent=intent,
                    provider=provider_name,
                    model=model_name,
                    candidates=candidates,
                    parser_usage=attempts,
                    parser_errors=errors,
                )

        return RoutingDecision(
            route="chat_provider",
            reason="Parsery DSL nie zwróciły pewnego IntentIR; fallback do rozmowy z ograniczonym kontekstem",
            confidence=top_score,
            provider=self.large_provider or session.provider,
            model=self.large_model or session.model,
            candidates=candidates,
            parser_usage=attempts,
            parser_errors=errors,
        )

    def _deterministic(self, candidates: list[CandidateMatch]) -> tuple[IntentIR, float] | None:
        if not candidates:
            return None
        candidate = candidates[0]
        if not candidate.exact and candidate.score < self.deterministic_threshold:
            return None
        definition = candidate.intent
        effect = str(definition.execution.get("effect", "read"))
        mode = "execute" if effect == "read" else "plan"
        ir = IntentIR.from_dict(
            {
                "v": 1,
                "intent_id": definition.id,
                "mode": mode,
                "args": {**definition.defaults, **candidate.extracted_args},
                "requirements": [],
                "constraints": definition.constraints,
                "unresolved": [],
            }
        )
        ir = definition.validate_ir(ir)
        confidence = 0.99 if candidate.exact else candidate.score
        if ir.unresolved:
            confidence = min(confidence, 0.69)
        return ir, confidence

    async def _parse_with_provider(
        self,
        text: str,
        candidates: list[CandidateMatch],
        *,
        route: str,
        provider_name: str,
        model_name: str,
        top_score: float,
        margin: float,
    ) -> tuple[IntentIR | None, ParserUsageRecord | None, str, float]:
        started = time.perf_counter()
        try:
            profile = self.config.provider(provider_name)
            bundle = self.provider_builder(profile, self.resolver)
        except (KeyError, ValueError, ProviderError) as exc:
            return None, None, f"{route}: nie można uruchomić providera ({exc})", 0.0

        shortlist = [
            {
                "intent_id": item.intent.id,
                "description": item.intent.description,
                "required_args": item.intent.required_args,
                "optional_args": item.intent.optional_args,
                "defaults": item.intent.defaults,
                "risk": item.intent.risk,
            }
            for item in candidates
        ]
        prompt = {
            "user_text": text,
            "candidate_intents": shortlist,
            "rules": [
                "Wybierz wyłącznie intent_id z candidate_intents.",
                "Nie twórz command, shell, connector, endpoint ani sekretów.",
                "args może zawierać wyłącznie pola zadeklarowane przez wybrany intent.",
                "Brakujące wymagane pola wpisz do unresolved.",
                "Dla odczytu użyj execute; dla zmiany stanu użyj plan.",
            ],
        }
        messages = [
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
        ]
        try:
            completion = await bundle.provider.complete_structured(
                messages,
                model=model_name,
                json_schema=intent_ir_schema([item.intent.id for item in candidates]),
                schema_name="subactor_intent_ir_v1",
                max_output_tokens=self.max_output_tokens,
                reasoning_effort=profile.reasoning_effort or None,
            )
            definition = self.catalog.get(str(completion.data.get("intent_id", "")))
            allowed_ids = {item.intent.id for item in candidates}
            if not definition or definition.id not in allowed_ids:
                raise IntentValidationError("Model wybrał intent spoza shortlisty")
            intent = definition.validate_ir(completion.data)
            confidence = min(0.97, 0.57 + 0.28 * top_score + 0.15 * max(0.0, margin))
            if intent.unresolved:
                confidence = min(confidence, 0.69)
            record = ParserUsageRecord(
                provider=provider_name,
                model=model_name,
                purpose=f"intent_parser:{route}",
                usage=completion.usage,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return intent, record, "", confidence
        except (ProviderError, IntentValidationError, ValueError, KeyError) as exc:
            return (
                None,
                ParserUsageRecord(
                    provider=provider_name,
                    model=model_name,
                    purpose=f"intent_parser:{route}",
                    usage=TokenUsage(),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                ),
                f"{route}: {exc}",
                0.0,
            )

    def _calibrated(self, intent_id: str, route: str, base: float) -> float:
        historical = self.store.historical_success(intent_id, route)
        if historical is None:
            return base
        return max(0.0, min(1.0, 0.75 * base + 0.25 * historical))

    def _cache_key(self, text: str) -> str:
        payload = normalize_text(text) + "\0" + self.catalog.fingerprint
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache(self, key: str, intent: IntentIR, confidence: float) -> None:
        payload = intent.to_dict()
        payload["_confidence"] = confidence
        self.store.cache_set(key, payload, self.cache_ttl)
