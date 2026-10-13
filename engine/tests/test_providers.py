"""Provider adapters over httpx.MockTransport: requests, usage, errors."""

import hashlib
import json
import logging

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app import llm_client
from app.config import Settings, settings
from app.game.llm import MODEL_HAIKU, MODEL_SONNET
from app.llm_client import ProviderError, call_messages
from app.providers import fake, registry

KEY = "test-key-alpha"
CANARY = "echo-CANARYSFX"
OPENAI_LIKE = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "xai": "https://api.x.ai/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


class Recorder:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = payload
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)


ANTHROPIC_OK = {
    "content": [{"type": "text", "text": "hello"}],
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 3,
    },
}
OPENAI_OK = {
    "choices": [{"message": {"role": "assistant", "content": "hello"}}],
    "usage": {
        "prompt_tokens": 20,
        "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 8},
    },
}
SYSTEM = [
    {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "volatile"},
]
MESSAGES = [
    {
        "role": "user",
        "content": [{"type": "text", "text": "earlier", "cache_control": {"type": "ephemeral"}}],
    },
    {"role": "user", "content": "now"},
]


def test_anthropic_request_and_usage(bound):
    rec = Recorder(payload=ANTHROPIC_OK)
    bound("anthropic", rec)
    msg = call_messages(
        model=MODEL_HAIKU, system=SYSTEM, messages=MESSAGES, max_tokens=50, temperature=0.0
    )
    req = rec.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEY
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert rec.body == {
        "model": settings.anthropic_model_fast,
        "max_tokens": 50,
        "messages": MESSAGES,
        "system": SYSTEM,
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
    }
    assert [b.text for b in msg.content] == ["hello"]
    assert (
        msg.usage.input_tokens,
        msg.usage.output_tokens,
        msg.usage.cache_read_input_tokens,
        msg.usage.cache_creation_input_tokens,
    ) == (11, 7, 5, 3)


def test_anthropic_omits_empty_system(bound):
    rec = Recorder(payload=ANTHROPIC_OK)
    bound("anthropic", rec)
    call_messages(model=MODEL_SONNET, system=None, messages=[{"role": "user", "content": "x"}])
    assert "system" not in rec.body
    assert "temperature" not in rec.body
    assert "thinking" not in rec.body
    assert rec.body["output_config"] == {"effort": "low"}
    assert rec.body["model"] == settings.anthropic_model_strong


def test_anthropic_drops_trailing_assistant(bound):
    rec = Recorder(payload=ANTHROPIC_OK)
    bound("anthropic", rec)
    call_messages(
        model=MODEL_HAIKU,
        system="s",
        messages=[
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "[NARRATIVE]"},
        ],
    )
    assert rec.body["messages"] == [{"role": "user", "content": "x"}]


def test_anthropic_max_tokens_without_text_warns(bound, caplog):
    caplog.set_level(logging.WARNING, logger="app.providers.anthropic")
    rec = Recorder(
        payload={
            "content": [{"type": "thinking", "thinking": "", "signature": "sig"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 9, "output_tokens": 30},
        }
    )
    bound("anthropic", rec)
    msg = call_messages(
        model=MODEL_SONNET, system="s", messages=[{"role": "user", "content": "x"}], max_tokens=30
    )
    assert msg.content == []
    hits = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.providers.anthropic" and "max_tokens=30" in r.getMessage()
    ]
    assert len(hits) == 1
    assert settings.anthropic_model_strong in hits[0]


def test_physics_grounding_sends_no_temperature_to_anthropic(bound):
    from app.game import physics

    rec = Recorder(payload=ANTHROPIC_OK)
    bound("anthropic", rec)
    out = physics._ground_via_llm("bend the rain", model=MODEL_HAIKU)
    assert out[0] is None
    assert rec.body["model"] == settings.anthropic_model_fast
    assert "temperature" not in rec.body


@pytest.mark.parametrize("name", sorted(OPENAI_LIKE))
def test_openai_compat_request_and_usage(bound, name):
    rec = Recorder(payload=OPENAI_OK)
    bound(name, rec)
    msgs = MESSAGES + [{"role": "assistant", "content": "[PREFILL]"}]
    msg = call_messages(model=MODEL_SONNET, system=SYSTEM, messages=msgs, max_tokens=64)
    req = rec.requests[0]
    assert str(req.url) == OPENAI_LIKE[name]
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert rec.body == {
        "model": getattr(settings, f"{name}_model_strong"),
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": "stable\n\nvolatile"},
            {"role": "user", "content": "earlier"},
            {"role": "user", "content": "now"},
        ],
    }
    assert "cache_control" not in req.content.decode()
    assert [b.text for b in msg.content] == ["hello"]
    assert (msg.usage.input_tokens, msg.usage.output_tokens, msg.usage.cache_read_input_tokens) == (
        12,
        4,
        8,
    )


@pytest.mark.parametrize("name", ["anthropic", "openai", "xai", "gemini"])
@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "byok_rejected"),
        (403, "byok_rejected"),
        (429, "provider_rate_limited"),
        (500, "provider_unavailable"),
        (503, "provider_unavailable"),
    ],
)
def test_http_errors_map_to_fixed_detail(bound, caplog, name, status, kind):
    caplog.set_level(logging.DEBUG)
    body = {"error": {"type": "authentication_error", "message": f"invalid key {CANARY}"}}
    bound(name, Recorder(status=status, payload=body))
    with pytest.raises(ProviderError) as ei:
        call_messages(model=MODEL_HAIKU, system="s", messages=[{"role": "user", "content": "x"}])
    exc = ei.value
    assert exc.status_code == status
    assert exc.detail == {"kind": kind, "provider": name, "status": status}
    seen = " ".join(
        [
            str(exc),
            repr(exc),
            repr(exc.args),
            repr(exc.detail),
            repr(exc.__cause__),
            repr(exc.__context__),
            caplog.text,
        ]
    )
    assert "CANARYSFX" not in seen


def test_network_error_maps_to_unavailable(bound):
    def boom(request):
        raise httpx.ConnectError(f"refused {CANARY}")

    bound("openai", boom)
    with pytest.raises(ProviderError) as ei:
        call_messages(model=MODEL_HAIKU, system="s", messages=[{"role": "user", "content": "x"}])
    assert ei.value.status_code == 503
    assert ei.value.detail == {"kind": "provider_unavailable", "provider": "openai", "status": None}
    assert "CANARYSFX" not in str(ei.value) + repr(ei.value.__context__)


def test_unbound_call_needs_key():
    with pytest.raises(ProviderError) as ei:
        call_messages(model=MODEL_HAIKU, system="s", messages=[{"role": "user", "content": "x"}])
    assert ei.value.status_code == 428
    assert set(ei.value.detail) == {"kind", "provider", "status"}
    assert ei.value.detail["kind"] == "byok_required"


def test_fake_unreachable_without_flag(monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    assert "fake" not in registry.available_providers()
    with pytest.raises(ValueError):
        registry.bind_request("fake", SecretStr(KEY))
    with pytest.raises(ValueError):
        registry.get_provider("fake")
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "0")
    with pytest.raises(ValueError):
        registry.bind_request("fake", SecretStr(KEY))


def test_fake_records_key_hash_and_has_anthropic_caps(monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(fake, "seen_key_hashes", [])
    tok = registry.bind_request("fake", SecretStr(KEY))
    try:
        assert registry.supports_cache_control(MODEL_SONNET)
        assert not registry.supports_prefill(MODEL_HAIKU)
        assert not registry.supports_prefill(MODEL_SONNET)
        msg = call_messages(
            model=MODEL_HAIKU, system="s", messages=[{"role": "user", "content": "x"}]
        )
    finally:
        registry.unbind_request(tok)
    assert msg.content[0].text == fake.DEFAULT_REPLY
    assert fake.seen_key_hashes == [hashlib.sha256(KEY.encode()).hexdigest()]


def test_unknown_provider_refused():
    with pytest.raises(ValueError):
        registry.bind_request("nope", SecretStr(KEY))


def test_missing_model_id_fails_settings(monkeypatch):
    monkeypatch.delenv("XAI_MODEL_STRONG", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_message_shape_kept():
    assert llm_client._Message.__dataclass_fields__.keys() == {
        "content",
        "usage",
        "stop_reason",
        "truncated",
    }
