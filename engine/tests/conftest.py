import logging
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PACKS = Path(__file__).parent / "fixtures" / "packs"
NEW_BODY = {"name": "t", "config": {"pack_id": "testpack", "one_memory": "a red kite"}}


def sid() -> str:
    return str(uuid.uuid4())


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def pack(monkeypatch):
    from app.game import lore, moderation

    monkeypatch.setattr(lore, "LORE_ROOT", PACKS)
    monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", "testpack")
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


@pytest.fixture
def clock(monkeypatch):
    from app import ratelimit

    c = Clock()
    monkeypatch.setattr(ratelimit, "clock", c)
    return c


@pytest.fixture
def selfhost(monkeypatch, tmp_path, pack):
    """Self-host client with the store unlocked and an anthropic key set."""
    from fastapi.testclient import TestClient
    from app import creds
    from app.config import settings
    from app.main import build_app

    monkeypatch.setattr(settings, "keys_dir", str(tmp_path / "keys"))
    monkeypatch.setattr(creds, "_vault", None)
    c = TestClient(build_app("selfhost"))
    assert c.post("/api/keys/unlock", json={"passphrase": "pass-one"}).status_code == 200
    assert c.put("/api/keys/anthropic", json={"key": "canary-key-sh-1"}).status_code == 200
    c.headers.update({"X-Provider": "anthropic", "X-Coherence-Session": sid()})
    return c


@pytest.fixture
def hosted(pack, clock):
    from fastapi.testclient import TestClient
    from app.main import build_app

    c = TestClient(build_app("hosted"))
    c.headers.update(
        {
            "X-Provider": "anthropic",
            "X-Provider-Key": "canary-key-h-1",
            "X-Coherence-Session": sid(),
        }
    )
    return c


_CREATE_LOCK = threading.Lock()


def turn_body(client) -> dict:
    """A fresh save plus a player move. A canon start needs a key and counts
    against the action limit; it runs with the message interval off, so the move
    under test meets the limiter exactly as the first message."""
    from app import ratelimit

    with _CREATE_LOCK:
        lim = ratelimit.messages
        keep, lim.interval = lim.interval, 0.0
        try:
            save = client.post("/api/game/new", json=NEW_BODY).json()["save"]
        finally:
            lim.interval = keep
    return {"save": save, "player_input": "I wait"}


@pytest.fixture
def calls(monkeypatch):
    from app.providers import fake

    recorded_calls = []
    complete = fake.FakeProvider.complete

    def recorded(self, **kwargs):
        recorded_calls.append(kwargs)
        return complete(self, **kwargs)

    monkeypatch.setattr(fake.FakeProvider, "complete", recorded)
    return recorded_calls


@pytest.fixture
def call_lines():
    from app import llm_client

    lines = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    llm_client.call_log.addHandler(handler)
    try:
        yield lines
    finally:
        llm_client.call_log.removeHandler(handler)


@pytest.fixture
def bound(monkeypatch):
    import httpx
    from pydantic import SecretStr
    from app.providers import registry
    from test_providers import KEY

    tokens = []

    def _bind(name, rec, key=KEY):
        monkeypatch.setattr(registry.get_provider(name), "transport", httpx.MockTransport(rec))
        tokens.append(registry.bind_request(name, SecretStr(key)))

    yield _bind
    for t in reversed(tokens):
        registry.unbind_request(t)


@pytest.fixture
def gate_rig(pack, monkeypatch):
    from app.game import lore
    from test_strong_gate import ACTIVE, TurnReplies, configure_turns

    configure_turns(monkeypatch)
    replies = TurnReplies()
    token = ACTIVE.set(replies)
    yield replies
    ACTIVE.reset(token)
    lore.clear_cache()
