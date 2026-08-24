from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from .catalog import IntentCatalog
from .compiler import CompileError, ExecutionPlan, PlanCompiler, compute_plan_hash
from .config import AppConfig
from .connectors import (
    ConnectorError,
    ConnectorExecutor,
    ConnectorRegistry,
    ExecutionReceipt,
)
from .models import Session
from .policy import PolicyEngine
from .providers import ProviderBundle
from .routing import Router, RoutingDecision
from .secret_refs import SecretResolver
from .store import Store


class OrchestrationError(RuntimeError):
    pass


ProviderBuilder = Callable[[Any, SecretResolver], ProviderBundle]


@dataclass(slots=True)
class OrchestrationOutcome:
    decision: RoutingDecision
    direct_text: str = ""
    provider: str = ""
    model: str = ""
    route_context: dict[str, Any] | None = None
    plan: ExecutionPlan | None = None
    receipt: ExecutionReceipt | None = None


class OrchestrationService:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        resolver: SecretResolver,
        provider_builder: ProviderBuilder,
    ):
        self.config = config
        self.store = store
        self.resolver = resolver
        self.provider_builder = provider_builder
        self.catalog = IntentCatalog.load(config.intent_catalog_paths())
        self.registry = ConnectorRegistry(config)
        self.router = Router(
            config,
            store,
            resolver,
            provider_builder,
            self.catalog,
        )
        self.compiler = PlanCompiler()
        self.policy = PolicyEngine(
            allow_destructive=bool(config.orchestration.get("allow_destructive", False))
        )
        self.executor = ConnectorExecutor(config, store, resolver, self.registry)
        self.mode = str(config.orchestration.get("mode", "active")).strip().lower()

    async def prepare(
        self,
        session: Session,
        text: str,
        *,
        cancel_event=None,
    ) -> OrchestrationOutcome:
        decision = await self.router.route(session, text, cancel_event=cancel_event)
        for record in decision.parser_usage:
            try:
                profile = self.config.provider(record.provider)
                pricing = {
                    "input_cost_per_million": profile.input_cost_per_million,
                    "cached_input_cost_per_million": profile.cached_input_cost_per_million,
                    "output_cost_per_million": profile.output_cost_per_million,
                }
            except (KeyError, ValueError):
                pricing = {}
            self.store.record_provider_usage(
                session.id,
                provider=record.provider,
                model=record.model,
                purpose=record.purpose,
                usage=record.usage,
                metadata={"latency_ms": record.latency_ms},
                **pricing,
            )
        self.store.record_routing_decision(
            session.id,
            route=decision.route,
            reason=decision.reason,
            intent_id=decision.intent_id,
            confidence=decision.confidence,
            provider=decision.provider,
            model=decision.model,
            candidates=[item.to_dict() for item in decision.candidates],
            metadata={
                "parser_errors": decision.parser_errors,
                "cache_hit": decision.cache_hit,
                "orchestration_mode": self.mode,
            },
        )

        if self.mode == "shadow":
            return OrchestrationOutcome(
                decision=decision,
                provider=self.router.large_provider or session.provider,
                model=self.router.large_model or session.model,
                route_context=decision.route_context() | {"shadow_mode": True},
            )

        if not decision.intent:
            return OrchestrationOutcome(
                decision=decision,
                provider=decision.provider or session.provider,
                model=decision.model or session.model,
                route_context=decision.route_context(),
            )

        definition = self.catalog.get(decision.intent.intent_id)
        if not definition:
            return self._fallback(session, decision, "Intent nie istnieje już w katalogu")
        if decision.intent.unresolved:
            missing = ", ".join(decision.intent.unresolved)
            return OrchestrationOutcome(
                decision=decision,
                direct_text=(
                    f"Rozpoznałem intent `{decision.intent.intent_id}`, ale brakuje pól: {missing}. "
                    "Doprecyzuj je w kolejnej wiadomości."
                ),
            )
        if str(definition.execution.get("kind", "chat")) == "chat":
            return self._fallback(session, decision, "Intent wymaga odpowiedzi konwersacyjnej")

        try:
            plan = self.compiler.compile(
                session_id=session.id,
                intent=decision.intent,
                definition=definition,
                state_fingerprint=self.current_state_fingerprint(),
                catalog_fingerprint=self.catalog.fingerprint,
            )
        except CompileError as exc:
            return OrchestrationOutcome(
                decision=decision,
                direct_text=f"Nie udało się skompilować IntentIR: {exc}",
            )

        connector_error = self._preflight_connectors(plan)
        policy = self.policy.evaluate(plan)
        if connector_error:
            plan.status = "blocked"
            self.store.save_execution_plan(plan.to_dict())
            return OrchestrationOutcome(
                decision=decision,
                plan=plan,
                direct_text=(
                    self.format_plan(plan)
                    + "\n\nPlan jest zablokowany: "
                    + connector_error
                    + ". Skonfiguruj nazwany connector/operation; bridge nie uruchomi arbitralnego shella."
                ),
            )
        if not policy.allowed:
            plan.status = "blocked"
            self.store.save_execution_plan(plan.to_dict())
            return OrchestrationOutcome(
                decision=decision,
                plan=plan,
                direct_text=self.format_plan(plan) + f"\n\nPlan zablokowany przez politykę: {policy.reason}",
            )

        if policy.auto_execute:
            plan.status = "running"
            self.store.save_execution_plan(plan.to_dict())
            receipt = await self.executor.execute(plan, approved=False)
            self.store.save_execution_receipt(receipt.to_dict())
            plan.status = "executed" if receipt.ok else "failed"
            self.store.update_plan_status(plan.id, plan.status)
            self.store.record_router_feedback(
                session.id,
                intent_id=plan.intent_id,
                route=decision.route,
                success=receipt.ok,
                metadata={"plan_id": plan.id, "receipt_id": receipt.id},
            )
            return OrchestrationOutcome(
                decision=decision,
                plan=plan,
                receipt=receipt,
                direct_text=self.format_receipt(receipt),
            )

        plan.status = "pending_approval" if policy.requires_approval else "planned"
        self.store.save_execution_plan(plan.to_dict())
        suffix = (
            f"\n\nAby wykonać plan, użyj `/apply {plan.id}` i wpisz dokładnie `EXECUTE`."
            if policy.requires_approval
            else f"\n\nPlan zapisano. Możesz użyć `/apply {plan.id}`."
        )
        return OrchestrationOutcome(
            decision=decision,
            plan=plan,
            direct_text=self.format_plan(plan) + suffix,
        )

    async def apply_plan(self, plan_id: str, *, confirmation: str = "") -> ExecutionReceipt:
        payload = self.store.get_execution_plan(plan_id)
        if not payload:
            raise OrchestrationError(f"Nie ma planu {plan_id}")
        plan = ExecutionPlan.from_dict(payload)
        if plan.status not in {"planned", "pending_approval", "failed"}:
            raise OrchestrationError(f"Plan ma status {plan.status} i nie może zostać zastosowany")
        if plan.plan_hash != compute_plan_hash(plan):
            raise OrchestrationError("Plan hash nie zgadza się z zapisaną treścią planu")
        if plan.effect != "read" and confirmation != "EXECUTE":
            raise OrchestrationError("Operacja zmienia stan; wymagane jest dokładne potwierdzenie EXECUTE")
        current = self.current_state_fingerprint()
        if current != plan.state_fingerprint:
            raise OrchestrationError(
                "Lokalny stan/katalog/connector registry zmienił się od utworzenia planu; utwórz nowy plan"
            )
        connector_error = self._preflight_connectors(plan)
        if connector_error:
            raise OrchestrationError(connector_error)
        policy = self.policy.evaluate(plan)
        if not policy.allowed:
            raise OrchestrationError(policy.reason)
        self.store.update_plan_status(plan.id, "running")
        receipt = await self.executor.execute(plan, approved=True)
        self.store.save_execution_receipt(receipt.to_dict())
        self.store.update_plan_status(plan.id, "executed" if receipt.ok else "failed")
        self.store.record_router_feedback(
            plan.session_id,
            intent_id=plan.intent_id,
            route="approved_plan",
            success=receipt.ok,
            metadata={"plan_id": plan.id, "receipt_id": receipt.id},
        )
        return receipt

    def current_state_fingerprint(self) -> str:
        encoded = ":".join(
            [self.store.state_fingerprint(), self.catalog.fingerprint, self.registry.fingerprint]
        )
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()

    def _preflight_connectors(self, plan: ExecutionPlan) -> str:
        try:
            for step in plan.steps:
                self.registry.validate_step(step)
        except ConnectorError as exc:
            return str(exc)
        return ""

    def _fallback(
        self, session: Session, decision: RoutingDecision, reason: str
    ) -> OrchestrationOutcome:
        provider = decision.provider or self.router.large_provider or session.provider
        model = decision.model or self.router.large_model
        if not model:
            try:
                model = self.config.provider(provider).model
            except (KeyError, ValueError):
                model = session.model
        context = decision.route_context()
        context["fallback_reason"] = reason
        return OrchestrationOutcome(
            decision=decision,
            provider=provider,
            model=model,
            route_context=context,
        )

    @staticmethod
    def format_plan(plan: ExecutionPlan) -> str:
        lines = [
            f"Plan `{plan.id}`",
            f"intent: `{plan.intent_id}`",
            f"effect: `{plan.effect}` · status: `{plan.status}`",
            f"hash: `{plan.plan_hash}`",
            "kroki:",
        ]
        for index, step in enumerate(plan.steps, start=1):
            args = json.dumps(step.args, ensure_ascii=False, separators=(",", ":"))
            if len(args) > 500:
                args = args[:500] + "…"
            lines.append(f"{index}. `{step.connector}:{step.operation}` ({step.effect}) args={args}")
        return "\n".join(lines)

    @staticmethod
    def format_receipt(receipt: ExecutionReceipt) -> str:
        # Builtin help is more useful as plain text than as nested JSON.
        if len(receipt.steps) == 1 and receipt.steps[0].get("ok"):
            result = receipt.steps[0].get("result")
            if isinstance(result, dict) and isinstance(result.get("message"), str):
                return result["message"] + f"\n\nReceipt `{receipt.id}`"
        lines = [
            f"Receipt `{receipt.id}` · plan `{receipt.plan_id}`",
            f"wynik: `{'ok' if receipt.ok else 'failed'}`",
            receipt.summary,
        ]
        for step in receipt.steps:
            state = "ok" if step.get("ok") else "failed"
            lines.append(f"- `{step.get('connector')}:{step.get('operation')}`: {state}")
            result = step.get("result")
            if isinstance(result, dict) and result:
                encoded = json.dumps(result, ensure_ascii=False, default=str)
                if len(encoded) > 1500:
                    encoded = encoded[:1500] + "…"
                lines.append(f"  {encoded}")
            if step.get("error"):
                lines.append(f"  {str(step['error'])[:1000]}")
        return "\n".join(lines)
