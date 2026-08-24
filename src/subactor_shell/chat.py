from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from .artifacts import ArtifactManager, select_relevant_text
from .config import AppConfig
from .context_builder import ContextBuilder
from .models import Artifact, PreparedPrompt, Session
from .orchestration import OrchestrationOutcome, OrchestrationService
from .providers import ProviderBundle, build_provider
from .redaction import StreamingRedactor
from .secret_refs import SecretResolver
from .store import Store
from .token_budget import TokenUsage, estimate_text_tokens


class ChatError(RuntimeError):
    pass


_ALIAS = r"[A-Za-z][A-Za-z0-9_.-]{0,63}"
SECRET_PATTERN = re.compile(r"\{\{secret:(" + _ALIAS + r")\}\}")
DATA_PATTERN = re.compile(r"\{\{data:(" + _ALIAS + r")\}\}")
ALIAS_PATTERN = re.compile(r"^" + _ALIAS + r"$")


class OneTimeGrantRegistry:
    """In-memory, process-local grants; intentionally never persisted."""

    def __init__(self) -> None:
        self._aliases: set[str] = set()
        self._lock = threading.Lock()

    def grant(self, alias: str) -> None:
        validate_alias(alias)
        with self._lock:
            self._aliases.add(alias)

    def consume(self, alias: str) -> bool:
        with self._lock:
            if alias not in self._aliases:
                return False
            self._aliases.remove(alias)
            return True

    def list(self) -> list[str]:
        with self._lock:
            return sorted(self._aliases)


def validate_alias(alias: str) -> str:
    if not ALIAS_PATTERN.fullmatch(alias):
        raise ValueError("Nazwa musi pasować do [A-Za-z][A-Za-z0-9_.-]{0,63}")
    return alias


ProviderBuilder = Callable[[Any, SecretResolver], ProviderBundle]


class ChatService:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        *,
        resolver: SecretResolver | None = None,
        provider_builder: ProviderBuilder = build_provider,
    ):
        self.config = config
        self.store = store
        self.resolver = resolver or SecretResolver(config.vault)
        self.provider_builder = provider_builder
        self.grants = OneTimeGrantRegistry()
        self.artifacts = ArtifactManager(
            config.data_dir / "artifacts",
            max_bytes=config.max_attachment_bytes,
            max_text_chars=config.max_attachment_text_chars,
        )
        self.context_builder = ContextBuilder(store, config.context)
        self.orchestration = OrchestrationService(
            config,
            store,
            self.resolver,
            provider_builder,
        )

    def new_session(
        self,
        *,
        name: str = "Nowa rozmowa",
        provider: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> Session:
        provider = provider or self.config.default_provider
        profile = self.config.provider(provider)
        model = model or profile.model or self.config.default_model
        return self.store.create_session(name, provider, model, session_id=session_id)

    def get_or_create_session(
        self,
        session_id: str | None,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> Session:
        if session_id:
            found = self.store.get_session(session_id)
            if not found:
                raise ChatError(f"Nie ma sesji {session_id}")
            return found
        return self.new_session(provider=provider, model=model)

    def bind_secret(self, alias: str, reference: str) -> None:
        validate_alias(alias)
        if reference.startswith("vault://"):
            from .vault import VaultRef

            VaultRef.parse(reference)
        elif not reference.startswith(("env://", "file://")):
            raise ValueError("Sekret musi używać vault://, env:// albo file://")
        self.store.bind_secret(alias, reference)

    def grant_secret(self, alias: str) -> None:
        validate_alias(alias)
        if not self.store.get_secret_binding(alias):
            raise ChatError(f"Brak bindingu sekretu '{alias}'")
        self.grants.grant(alias)

    def set_data_text(self, name: str, value: str) -> None:
        validate_alias(name)
        self.store.set_data(name, "text", value)

    def set_data_file(self, name: str, source: Path, session_id: str | None = None) -> Artifact:
        validate_alias(name)
        artifact = self.artifacts.import_file(source)
        if session_id:
            self.store.add_artifact(artifact, session_id)
        else:
            raise ChatError("Do zapisu pliku danych wymagana jest aktywna sesja")
        self.store.set_data(name, "artifact", artifact.id)
        return artifact

    def _expand_data(self, text: str, *, query: str) -> tuple[str, list[str]]:
        names: list[str] = []
        settings = self.config.context
        max_chars = max(256, int(settings.get("max_data_chars", 6000)))
        chunk_chars = max(256, int(settings.get("artifact_chunk_chars", 1800)))
        max_chunks = max(1, int(settings.get("max_artifact_chunks", 4)))

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            item = self.store.get_data(name)
            if not item:
                raise ChatError(f"Brak danych '{{{{data:{name}}}}}'")
            kind, value = item
            names.append(name)
            if kind == "text":
                selected, truncated = select_relevant_text(
                    value,
                    query,
                    max_chars=max_chars,
                    chunk_chars=chunk_chars,
                    max_chunks=max_chunks,
                )
                return selected + ("\n[DATA_SELECTED_LOCALLY]" if truncated else "")
            artifact = self.store.get_artifact(value)
            if not artifact:
                raise ChatError(f"Brak artefaktu danych '{name}' ({value})")
            return self.artifacts.render_for_prompt(
                artifact,
                query=query,
                max_chars=max_chars,
                chunk_chars=chunk_chars,
                max_chunks=max_chunks,
            )

        return DATA_PATTERN.sub(replace, text), sorted(set(names))

    def _prepare_prompt(
        self,
        text: str,
        attachments: list[Artifact],
        additional_context_blocks: list[str] | None = None,
    ) -> PreparedPrompt:
        additional_context_blocks = additional_context_blocks or []

        # Only placeholders typed in the current user message can consume a
        # one-time grant. Data, files and ACP resources remain untrusted text.
        aliases = list(dict.fromkeys(SECRET_PATTERN.findall(text)))
        resolved: dict[str, str] = {}
        for alias in aliases:
            reference = self.store.get_secret_binding(alias)
            if not reference:
                raise ChatError(f"Brak bindingu sekretu '{{{{secret:{alias}}}}}'")
            if not self.grants.consume(alias):
                raise ChatError(
                    f"Sekret '{alias}' wymaga jednorazowego grantu: /vault grant {alias}"
                )
            resolved[alias] = self.resolver.resolve(reference)

        safe_text, data_names = self._expand_data(text, query=text)
        provider_text_with_secrets = SECRET_PATTERN.sub(
            lambda match: resolved[match.group(1)], text
        )
        provider_text, provider_data_names = self._expand_data(
            provider_text_with_secrets, query=text
        )
        data_names = sorted(set(data_names) | set(provider_data_names))

        settings = self.config.context
        embedded_limit = max(0, int(settings.get("max_embedded_context_chars", 8000)))
        safe_blocks = self.context_builder.compact_blocks(
            list(additional_context_blocks), total_limit=embedded_limit
        )
        provider_blocks = list(safe_blocks)
        if attachments:
            attachment_limit = max(256, int(settings.get("max_attachment_prompt_chars", 8000)))
            chunk_chars = max(256, int(settings.get("artifact_chunk_chars", 1800)))
            max_chunks = max(1, int(settings.get("max_artifact_chunks", 4)))
            rendered = [
                self.artifacts.render_for_prompt(
                    item,
                    query=text,
                    max_chars=attachment_limit,
                    chunk_chars=chunk_chars,
                    max_chunks=max_chunks,
                )
                for item in attachments
            ]
            rendered = self.context_builder.compact_blocks(
                rendered, total_limit=attachment_limit
            )
            safe_blocks.extend(rendered)
            provider_blocks.extend(rendered)

        safe_context = safe_text
        provider_content = provider_text
        if safe_blocks:
            safe_context += "\n\n" + "\n\n".join(safe_blocks)
            provider_content += "\n\n" + "\n\n".join(provider_blocks)

        display_content = text
        if additional_context_blocks:
            display_content += "\n\n" + "\n\n".join(additional_context_blocks)

        return PreparedPrompt(
            display_content=display_content,
            safe_context_content=safe_context,
            provider_content=provider_content,
            resolved_secret_values=list(resolved.values()),
            metadata={
                "secret_aliases": aliases,
                "data_names": data_names,
                "artifact_ids": [item.id for item in attachments],
                "embedded_context_blocks": len(additional_context_blocks),
            },
        )

    def _prepare_local_message(
        self,
        text: str,
        attachments: list[Artifact],
        additional_context_blocks: list[str] | None,
    ) -> PreparedPrompt:
        """Persist a local route without reading or consuming any secret grant."""

        additional_context_blocks = additional_context_blocks or []
        safe_text, data_names = self._expand_data(text, query=text)
        settings = self.config.context
        blocks = self.context_builder.compact_blocks(
            list(additional_context_blocks),
            total_limit=max(0, int(settings.get("max_embedded_context_chars", 8000))),
        )
        if attachments:
            attachment_limit = max(256, int(settings.get("max_attachment_prompt_chars", 8000)))
            rendered = [
                self.artifacts.render_for_prompt(
                    item,
                    query=text,
                    max_chars=attachment_limit,
                    chunk_chars=int(settings.get("artifact_chunk_chars", 1800)),
                    max_chunks=int(settings.get("max_artifact_chunks", 4)),
                )
                for item in attachments
            ]
            blocks.extend(
                self.context_builder.compact_blocks(rendered, total_limit=attachment_limit)
            )
        safe_context = safe_text + ("\n\n" + "\n\n".join(blocks) if blocks else "")
        display = text + (
            "\n\n" + "\n\n".join(additional_context_blocks)
            if additional_context_blocks
            else ""
        )
        return PreparedPrompt(
            display_content=display,
            safe_context_content=safe_context,
            provider_content=safe_context,
            resolved_secret_values=[],
            metadata={
                "secret_aliases": [],
                "data_names": data_names,
                "artifact_ids": [item.id for item in attachments],
                "embedded_context_blocks": len(additional_context_blocks),
                "local_route": True,
            },
        )

    async def stream_message(
        self,
        session_id: str,
        text: str,
        *,
        attachment_paths: list[Path] | None = None,
        additional_context_blocks: list[str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[str]:
        session = self.store.get_session(session_id)
        if not session:
            raise ChatError(f"Nie ma sesji {session_id}")
        if not text.strip() and not attachment_paths and not additional_context_blocks:
            raise ChatError("Pusta wiadomość")

        attachments: list[Artifact] = []
        for path in attachment_paths or []:
            artifact = self.artifacts.import_file(path)
            self.store.add_artifact(artifact, session_id)
            attachments.append(artifact)

        outcome = await self.orchestration.prepare(
            session,
            text,
            cancel_event=cancel_event,
        )
        route_metadata = {
            "route": outcome.decision.route,
            "intent_id": outcome.decision.intent_id,
            "confidence": outcome.decision.confidence,
        }

        if outcome.direct_text:
            prepared_local = self._prepare_local_message(
                text,
                attachments,
                additional_context_blocks,
            )
            self.store.add_message(
                session_id,
                "user",
                prepared_local.display_content,
                prepared_local.safe_context_content,
                prepared_local.metadata | {"routing": route_metadata},
            )
            self.store.add_message(
                session_id,
                "assistant",
                outcome.direct_text,
                outcome.direct_text,
                {
                    "routing": route_metadata,
                    "plan_id": outcome.plan.id if outcome.plan else "",
                    "receipt_id": outcome.receipt.id if outcome.receipt else "",
                    "local": True,
                },
            )
            intent = outcome.decision.intent
            self.context_builder.update_state(
                session_id,
                user_text=text,
                intent_id=intent.intent_id if intent else "",
                constraints=intent.constraints if intent else None,
                unresolved=intent.unresolved if intent else None,
                receipt_id=outcome.receipt.id if outcome.receipt else "",
            )
            yield outcome.direct_text
            return

        prepared = self._prepare_prompt(
            text,
            attachments,
            additional_context_blocks=additional_context_blocks,
        )
        context = self.context_builder.build(
            session_id,
            prepared.provider_content,
            route_context=outcome.route_context,
        )
        user_metadata = prepared.metadata | {
            "routing": route_metadata,
            "context": {
                "included_history_messages": context.included_history_messages,
                "history_chars": context.history_chars,
                "estimated_input_tokens": context.estimated_input_tokens,
            },
        }
        self.store.add_message(
            session_id,
            "user",
            prepared.display_content,
            prepared.safe_context_content,
            user_metadata,
        )

        provider_name = outcome.provider or session.provider
        profile = self.config.provider(provider_name)
        model = outcome.model or (session.model if provider_name == session.provider else profile.model)
        bundle = self.provider_builder(profile, self.resolver)
        redactor = StreamingRedactor(
            [*bundle.sensitive_values, *prepared.resolved_secret_values]
        )
        parts: list[str] = []
        cancelled = False
        stream_error: Exception | None = None
        try:
            async for raw in bundle.provider.stream(
                context.messages,
                model=model,
                cancel_event=cancel_event,
            ):
                safe = redactor.feed(raw)
                if safe:
                    parts.append(safe)
                    yield safe
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
            cancelled = cancelled or bool(cancel_event is not None and cancel_event.is_set())
            tail = redactor.finish()
            if tail:
                parts.append(tail)
                yield tail
        except asyncio.CancelledError:
            cancelled = True
            tail = redactor.finish()
            if tail:
                parts.append(tail)
            raise
        except Exception as exc:
            stream_error = exc
            raise
        finally:
            answer = "".join(parts)
            provider_usage = getattr(bundle.provider, "last_usage", None)
            if not isinstance(provider_usage, TokenUsage):
                provider_usage = TokenUsage(
                    input_tokens=context.estimated_input_tokens,
                    output_tokens=estimate_text_tokens(answer),
                    estimated=True,
                )
            else:
                if provider_usage.input_tokens <= 0:
                    provider_usage.input_tokens = context.estimated_input_tokens
                    provider_usage.estimated = True
                if provider_usage.output_tokens <= 0 and answer:
                    provider_usage.output_tokens = estimate_text_tokens(answer)
                    provider_usage.estimated = True
            self.store.record_provider_usage(
                session_id,
                provider=provider_name,
                model=model,
                purpose="chat_response",
                usage=provider_usage,
                input_cost_per_million=profile.input_cost_per_million,
                cached_input_cost_per_million=profile.cached_input_cost_per_million,
                output_cost_per_million=profile.output_cost_per_million,
                metadata={
                    "route": outcome.decision.route,
                    "cancelled": cancelled,
                    "error": type(stream_error).__name__ if stream_error else "",
                },
            )
            if answer:
                self.store.add_message(
                    session_id,
                    "assistant",
                    answer,
                    answer,
                    {
                        "cancelled": cancelled,
                        "routing": route_metadata,
                        "usage": provider_usage.to_dict(),
                    },
                )
            intent = outcome.decision.intent
            self.context_builder.update_state(
                session_id,
                user_text=text,
                intent_id=intent.intent_id if intent else "",
                constraints=intent.constraints if intent else None,
                unresolved=intent.unresolved if intent else None,
            )

    async def complete_message(
        self,
        session_id: str,
        text: str,
        *,
        attachment_paths: list[Path] | None = None,
        additional_context_blocks: list[str] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        parts: list[str] = []
        async for chunk in self.stream_message(
            session_id,
            text,
            attachment_paths=attachment_paths,
            additional_context_blocks=additional_context_blocks,
            cancel_event=cancel_event,
        ):
            parts.append(chunk)
        return "".join(parts)
