import pytest
from fastapi.testclient import TestClient

from app import creds
from app.config import settings
from app.main import build_app
from conftest import NEW_BODY, sid, turn_body

KEY = "test-canary-key-7f3a"


def test_selfhost_key_errors(selfhost, monkeypatch, tmp_path):
    body = turn_body(selfhost)
    assert selfhost.post("/api/keys/lock").status_code == 200
    r = selfhost.post("/api/game/turn", json=body)
    assert r.status_code == 428
    assert r.json()["detail"] == {"error": "key_store_locked", "provider": "anthropic"}
    assert selfhost.post("/api/keys/unlock", json={"passphrase": "pass-one"}).status_code == 200
    r = selfhost.post("/api/game/turn", json=body, headers={"X-Provider": "openai"})
    assert r.status_code == 428
    assert r.json()["detail"] == {
        "error": "key_other_provider",
        "provider": "openai",
        "stored": ["anthropic"],
    }
    assert selfhost.delete("/api/keys/anthropic").status_code == 200
    r = selfhost.post("/api/game/turn", json=body)
    assert r.status_code == 428 and r.json()["detail"]["error"] == "byok_required"
    monkeypatch.setattr(settings, "keys_dir", str(tmp_path / "fresh"))
    monkeypatch.setattr(creds, "_vault", None)
    with TestClient(build_app("selfhost")) as fresh:
        fresh.headers.update({"X-Provider": "anthropic", "X-Coherence-Session": sid()})
        r = fresh.post("/api/game/turn", json=body)
    assert r.status_code == 428 and r.json()["detail"]["error"] == "byok_required"
    print("PASS selfhost_key_errors n=4")


def test_hosted_turn_without_key(hosted):
    body = turn_body(hosted)
    r = hosted.post("/api/game/turn", json=body, headers={"X-Provider-Key": ""})
    assert r.status_code == 428 and r.json()["detail"]["error"] == "byok_required"
    print("PASS hosted_turn_without_key")


@pytest.mark.parametrize("provider", creds.PROVIDERS)
def test_each_provider_creates_and_moves(selfhost, provider):
    assert selfhost.put("/api/keys/" + provider, json={"key": KEY}).status_code == 200
    assert selfhost.post("/api/keys/lock").status_code == 200
    assert selfhost.post("/api/keys/unlock", json={"passphrase": "pass-one"}).status_code == 200
    selfhost.headers["X-Provider"] = provider
    r = selfhost.post(
        "/api/game/new", json={"name": "t", "config": {"pack_id": "testpack", "one_memory": ""}}
    )
    assert r.status_code == 200
    r = selfhost.post("/api/game/turn", json={"save": r.json()["save"], "player_input": "I wait"})
    assert r.status_code == 200
    print("PASS provider_creates_and_moves provider=%s" % provider)


def test_custom_creation_without_key(hosted):
    r = hosted.post(
        "/api/game/new",
        json={"name": "t", "config": {"pack_id": "testpack", "world_type": "custom"}},
        headers={"X-Provider-Key": ""},
    )
    assert r.status_code == 428 and r.json()["detail"]["error"] == "byok_required"
    print("PASS custom_creation_without_key")


def test_locked_status(selfhost):
    assert selfhost.post("/api/keys/lock").status_code == 200
    r = selfhost.get("/api/keys/status")
    assert r.status_code == 428 and r.json()["detail"]["error"] == "key_store_locked"
    print("PASS locked_status")


@pytest.mark.parametrize("memory", ["", "a red kite"])
def test_canon_creation_without_key(clock, memory):
    with TestClient(build_app("hosted")) as client:
        client.headers.update({"X-Provider": "anthropic", "X-Coherence-Session": sid()})
        r = client.post(
            "/api/game/new",
            json={"name": "t", "config": {"pack_id": "neon_rain", "one_memory": memory}},
        )
    assert r.status_code == 428 and r.json()["detail"]["error"] == "byok_required"
    print("PASS canon_creation_without_key memory=%s" % ("typed" if memory else "blank"))


def test_canon_creation_with_blank_memory_makes_no_model_call(clock, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    with TestClient(build_app("hosted")) as client:
        client.headers.update(
            {
                "X-Provider": "anthropic",
                "X-Provider-Key": "canary-key-h-1",
                "X-Coherence-Session": sid(),
            }
        )
        r = client.post(
            "/api/game/new",
            json={"name": "t", "config": {"pack_id": "neon_rain", "one_memory": ""}},
        )
    assert r.status_code == 200, r.text
    assert len(calls) == 0, calls
    assert r.json()["save"]["config"]["one_memory"] == ""
    print("PASS canon_creation_with_blank_memory_makes_no_model_call")


def test_locked_creation(selfhost):
    assert selfhost.post("/api/keys/lock").status_code == 200
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 428 and r.json()["detail"]["error"] == "key_store_locked"
    print("PASS locked_creation")


def test_canon_start_then_turn_is_not_refused_by_the_message_limit(hosted, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    r = hosted.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    r = hosted.post("/api/game/turn", json={"save": r.json()["save"], "player_input": "I wait"})
    assert r.status_code == 200, r.text
    print("PASS canon_start_then_turn_is_not_refused_by_the_message_limit")


def test_custom_start_takes_the_message_slot(hosted, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    body = {
        "name": "c",
        "world_hint": "a harbour",
        "config": {"pack_id": "testpack", "world_type": "custom"},
    }
    r = hosted.post("/api/game/new", json=body)
    assert r.status_code == 200, r.text
    assert len(calls) == 1
    r = hosted.post("/api/game/turn", json={"save": r.json()["save"], "player_input": "I wait"})
    assert r.status_code == 429 and r.json()["detail"]["error"] == "rate_limited"
    print("PASS custom_start_takes_the_message_slot")
