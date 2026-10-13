"""200 concurrent calls with distinct bound keys: no key cross-talk."""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from pydantic import SecretStr

from app.config import settings
from app.game.llm import MODEL_HAIKU, MODEL_SONNET, call_llm_reply
from app.llm_client import call_messages
from app.providers import fake, registry
from app.providers.anthropic import MODEL_PARAMS

N = 200


def _echo_key(request):
    # Hold each request briefly so the calls overlap.
    time.sleep(0.005)
    key = request.headers.get("x-api-key") or request.headers["authorization"][7:]
    model = json.loads(request.content)["model"]
    if "x-api-key" in request.headers:
        return httpx.Response(200, json={"content": [{"type": "text", "text": key}], "usage": {}})
    return httpx.Response(
        200, json={"choices": [{"message": {"content": key}}], "usage": {}, "model": model}
    )


def _one(i, provider, barrier):
    key = f"stress-key-{i:04d}"
    barrier.wait()
    tok = registry.bind_request(provider, SecretStr(key))
    try:
        msg = call_messages(
            model=MODEL_HAIKU, system="s", messages=[{"role": "user", "content": str(i)}]
        )
        assert registry.current_key().get_secret_value() == key
    finally:
        registry.unbind_request(tok)
    return key, msg.content[0].text


def test_concurrent_bound_keys_no_crosstalk(monkeypatch):
    for name in registry.REAL_PROVIDERS:
        monkeypatch.setattr(
            registry.get_provider(name), "transport", httpx.MockTransport(_echo_key)
        )
    workers = 50
    barrier = threading.Barrier(workers, timeout=30)
    providers = registry.REAL_PROVIDERS
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(lambda i: _one(i, providers[i % len(providers)], barrier), range(N))
        )
    assert len(results) == N
    assert all(key == echoed for key, echoed in results)
    assert len({key for key, _ in results}) == N
    assert registry.current_provider() == ""


def test_concurrent_fake_hashes(monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(fake, "seen_key_hashes", [])
    workers = 50
    barrier = threading.Barrier(workers, timeout=30)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda i: _one(i, "fake", barrier), range(N)))
    want = sorted(hashlib.sha256(k.encode()).hexdigest() for k, _ in results)
    assert sorted(fake.seen_key_hashes) == want
    assert len(set(fake.seen_key_hashes)) == N


def test_concurrent_anthropic_bodies_conform(monkeypatch):
    sent, lock = [], threading.Lock()

    def handler(request):
        with lock:
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(handler)
    )
    caps = (5, 1024, 5000)
    workers = 50
    barrier = threading.Barrier(workers, timeout=30)

    def one(i):
        model = MODEL_HAIKU if i % 2 == 0 else MODEL_SONNET
        cap = caps[(i // 2) % len(caps)]
        barrier.wait()
        tok = registry.bind_request("anthropic", SecretStr(f"stress-key-{i:04d}"))
        try:
            if i % 5 == 0:
                call_messages(
                    model=model,
                    system="s",
                    max_tokens=cap,
                    messages=[
                        {"role": "user", "content": str(i)},
                        {"role": "assistant", "content": "[NARRATIVE]"},
                    ],
                    temperature=0.0,
                )
            else:
                call_llm_reply(
                    "s", [{"role": "user", "content": str(i)}], model=model, max_tokens=cap
                )
        finally:
            registry.unbind_request(tok)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, range(N)))
    ids = {settings.anthropic_model_fast, settings.anthropic_model_strong}
    assert ids <= set(MODEL_PARAMS)
    assert len(sent) == N
    assert {b["model"] for b in sent} == ids
    assert {b["max_tokens"] for b in sent} == set(caps)
    for body in sent:
        assert body["messages"][-1]["role"] == "user"
        assert "temperature" not in body
        want = MODEL_PARAMS[body["model"]]
        for field in ("thinking", "output_config"):
            assert body.get(field) == want.get(field)
    print("PASS anthropic bodies n=%d: user-last, no temperature, per-id fields" % N)
