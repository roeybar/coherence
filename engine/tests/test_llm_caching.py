"""Cache markers per provider, driven through game.llm.call_llm_reply."""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import settings
from app.game.llm import MODEL_HAIKU, MODEL_SONNET, cache_split_system, call_llm_reply
from app.game.prompts import PREFILL_NARRATIVE
from app.providers import registry

HISTORY = [
    {"role": "user", "content": "I look around."},
    {"role": "assistant", "content": "Rain on neon."},
    {"role": "user", "content": "I walk on."},
]
SYSTEM = cache_split_system("stable pack prefix", "volatile turn part")


def _run(monkeypatch, provider, model, reply_payload):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply_payload)

    monkeypatch.setattr(registry.get_provider(provider), "transport", httpx.MockTransport(handler))
    tok = registry.bind_request(provider, SecretStr("test-key-caching"))
    try:
        raw, _usage, _stop, _truncated = call_llm_reply(SYSTEM, list(HISTORY), model=model)
    finally:
        registry.unbind_request(tok)
    assert len(sent) == 1
    return raw, sent[0]


ANTHROPIC_OK = {
    "content": [{"type": "text", "text": " the rest"}],
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
OPENAI_OK = {
    "choices": [{"message": {"content": "the rest"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def test_anthropic_fast_cache_no_prefill(monkeypatch):
    raw, body = _run(monkeypatch, "anthropic", MODEL_HAIKU, ANTHROPIC_OK)
    assert body["model"] == settings.anthropic_model_fast
    assert body["messages"][-1] == HISTORY[-1]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in body["system"][1]
    # The caller's messages[-2] is marked; the last message is the user turn.
    marked = body["messages"][len(HISTORY) - 2]
    assert marked["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert raw == " the rest"


def test_anthropic_strong_cache_no_prefill(monkeypatch):
    raw, body = _run(monkeypatch, "anthropic", MODEL_SONNET, ANTHROPIC_OK)
    assert body["model"] == settings.anthropic_model_strong
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-1] == HISTORY[-1]
    assert not raw.startswith(PREFILL_NARRATIVE)


@pytest.mark.parametrize("provider", ["openai", "xai", "gemini"])
@pytest.mark.parametrize("model,tier", [(MODEL_HAIKU, "fast"), (MODEL_SONNET, "strong")])
def test_openai_compat_no_prefill_no_cache(monkeypatch, provider, model, tier):
    raw, body = _run(monkeypatch, provider, model, OPENAI_OK)
    assert body["model"] == getattr(settings, f"{provider}_model_{tier}")
    assert "cache_control" not in json.dumps(body)
    assert PREFILL_NARRATIVE not in json.dumps(body)
    assert body["messages"][-1] == {"role": "user", "content": "I walk on."}
    assert body["messages"][0] == {
        "role": "system",
        "content": "stable pack prefix\n\nvolatile turn part",
    }
    assert raw == "the rest"


def test_unbound_has_no_capabilities():
    assert not registry.supports_cache_control(MODEL_SONNET)
    assert not registry.supports_prefill(MODEL_HAIKU)
