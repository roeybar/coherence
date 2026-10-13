import secrets
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    coherence_mode: str = "selfhost"
    body_cap_bytes: int = 134217728
    # None means the mode default: hosted 15, self-host 0 (off).
    message_interval_seconds: Optional[float] = None
    rate_limit_max_keys: int = 100000
    trusted_proxy_cidrs: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    trusted_proxy_hops: int = 1
    unlock_per_minute: int = 5
    vault_idle_ttl_s: int = 1800
    vault_max_sessions: int = 64
    max_concurrent_requests: int = 4
    keys_dir: str = "/data/keys"
    # Test-only window ceiling for the fake provider; read only when ENGINE_FAKE_PROVIDER=1.
    engine_fake_context_tokens: Optional[int] = None
    # Consent-token HMAC key. Random per Settings instance unless set.
    escalation_hmac_key: bytes = Field(default=b"", validate_default=True)

    # Real model ids per provider and tier. No defaults: a missing key
    # fails at startup.
    anthropic_model_fast: str
    anthropic_model_strong: str
    openai_model_fast: str
    openai_model_strong: str
    xai_model_fast: str
    xai_model_strong: str
    gemini_model_fast: str
    gemini_model_strong: str

    @field_validator("escalation_hmac_key")
    @classmethod
    def _hmac_key(cls, v: bytes) -> bytes:
        return v or secrets.token_bytes(32)


settings = Settings()
