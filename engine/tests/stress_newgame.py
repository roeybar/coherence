import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app import creds, main
from app.main import build_app
from conftest import sid


def test_40_creations_without_key(clock, monkeypatch):
    # exercise key admission without the separate concurrency limit
    monkeypatch.setattr(main, "slots", threading.BoundedSemaphore(40))
    with TestClient(build_app("hosted")) as client:

        def create(_):
            r = client.post(
                "/api/game/new",
                json={"name": "t", "config": {"pack_id": "neon_rain", "one_memory": ""}},
                headers={"X-Provider": "anthropic", "X-Coherence-Session": sid()},
            )
            return r.status_code, r.json()["detail"]["error"]

        with ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(create, range(40)))
    assert results == [(428, "byok_required")] * 40
    print("PASS creations_without_key n=40")


def test_key_states_in_threads(selfhost):
    cookie = selfhost.cookies.get("coh_session")

    def read(_):
        for _ in range(200):
            why, key, others = creds.key_state(cookie, "anthropic")
            assert why == "ok" and key is not None and others == ()
            assert creds.key_state(cookie, "openai") == ("other", None, ("anthropic",))
            assert creds.key_state(sid(), "anthropic") == ("locked", None, ())
            assert creds.key_state("", "anthropic") == ("locked", None, ())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read, range(8)))
    print("PASS key_states_in_threads workers=8 iterations=200")


def test_provider_boundaries(selfhost):
    cookie = selfhost.cookies.get("coh_session")
    for provider in ("", "fake", "ANTHROPIC", "x" * 4096):
        assert creds.key_state(cookie, provider) == ("none", None, ())
    print("PASS provider_boundaries n=4")
