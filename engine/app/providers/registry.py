"""Per-request provider binding, model aliases and capabilities.

bind_request / unbind_request run inside the sync request handler, in
the same try/finally as bind_save. Sync FastAPI dependencies drop
ContextVar changes, so they are never called from a Depends.
"""

import os
from contextvars import ContextVar, Token
from typing import Optional

from pydantic import SecretStr

from app.config import settings
from app.providers.anthropic import AnthropicProvider
from app.providers.base import TIER_FAST, TIER_STRONG, Provider
from app.providers.fake import FakeProvider
from app.providers.openai_compat import OpenAICompatProvider

REAL_PROVIDERS = ("anthropic", "openai", "xai", "gemini")

_PROVIDERS: dict[str, Provider] = {
    "anthropic": AnthropicProvider(),
    "openai": OpenAICompatProvider("openai"),
    "xai": OpenAICompatProvider("xai"),
    "gemini": OpenAICompatProvider("gemini"),
    "fake": FakeProvider(),
}

_bound: ContextVar[Optional[tuple[str, SecretStr]]] = ContextVar("provider_request", default=None)


def fake_enabled() -> bool:
    return os.environ.get("ENGINE_FAKE_PROVIDER") == "1"


def available_providers() -> tuple[str, ...]:
    return REAL_PROVIDERS + (("fake",) if fake_enabled() else ())


def get_provider(name: str) -> Provider:
    if name not in available_providers():
        raise ValueError("unknown provider")
    return _PROVIDERS[name]


def bind_request(provider: str, key: SecretStr) -> Token:
    get_provider(provider)
    return _bound.set((provider, key))


def unbind_request(token: Token) -> None:
    _bound.reset(token)


def current_provider() -> str:
    b = _bound.get()
    return b[0] if b else ""


def current_key() -> Optional[SecretStr]:
    b = _bound.get()
    return b[1] if b else None


def resolve_model(provider: str, model: str) -> str:
    """Map a tier alias to the provider's real model id. The fake
    provider takes the alias as is."""
    if provider == "fake" or model not in (TIER_FAST, TIER_STRONG):
        return model
    tier = "fast" if model == TIER_FAST else "strong"
    return getattr(settings, f"{provider}_model_{tier}")


def _anthropic_like() -> bool:
    return current_provider() in ("anthropic", "fake")


def supports_cache_control(model: str) -> bool:
    return _anthropic_like()


def supports_prefill(model: str) -> bool:
    # No supported provider accepts an assistant prefill.
    return False


def has_strong_tier(provider: str) -> bool:
    if provider == "fake":
        # test-only: the browser e2e stack gives the fake provider a strong tier
        return fake_enabled() and os.environ.get("ENGINE_FAKE_STRONG_TIER") == "1"
    strong = getattr(settings, f"{provider}_model_strong", "") or ""
    fast = getattr(settings, f"{provider}_model_fast", "") or ""
    return bool(strong) and strong != fast
