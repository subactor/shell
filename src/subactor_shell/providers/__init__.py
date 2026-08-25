from __future__ import annotations

from ..models import ProviderProfile
from ..secret_refs import SecretResolver
from .anthropic import AnthropicProvider
from .base import ChatProvider, ProviderBundle, ProviderError, StructuredCompletion
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider
from .subactor_control import SubactorControlProvider


def build_provider(profile: ProviderProfile, resolver: SecretResolver) -> ProviderBundle:
    kind = profile.kind.lower().strip()
    if kind == "mock":
        return ProviderBundle(provider=MockProvider(), sensitive_values=[])

    api_key = resolver.resolve(profile.api_key_ref) if profile.api_key_ref else ""
    if profile.auth_required and not api_key:
        raise ProviderError(f"Provider '{profile.name}' nie ma dostępnego klucza API")

    if kind == "openai_compat":
        provider: ChatProvider = OpenAICompatProvider(
            base_url=profile.base_url,
            endpoint=profile.endpoint,
            api_key=api_key,
            timeout_seconds=profile.timeout_seconds,
            extra_headers=profile.extra_headers,
            structured_mode=profile.structured_mode,
        )
    elif kind == "subactor_control":
        provider = SubactorControlProvider(
            base_url=profile.base_url,
            endpoint=profile.endpoint,
            api_key=api_key,
            timeout_seconds=profile.timeout_seconds,
        )
    elif kind == "anthropic":
        if not api_key:
            raise ProviderError(f"Provider '{profile.name}' wymaga klucza API")
        provider = AnthropicProvider(
            base_url=profile.base_url,
            api_key=api_key,
            max_tokens=profile.max_tokens,
            anthropic_version=profile.anthropic_version,
            timeout_seconds=profile.timeout_seconds,
            extra_headers=profile.extra_headers,
        )
    else:
        raise ProviderError(f"Nieobsługiwany kind providera: {profile.kind}")
    return ProviderBundle(provider=provider, sensitive_values=[api_key] if api_key else [])


__all__ = [
    "AnthropicProvider",
    "ChatProvider",
    "MockProvider",
    "OpenAICompatProvider",
    "SubactorControlProvider",
    "ProviderBundle",
    "ProviderError",
    "StructuredCompletion",
    "build_provider",
]
