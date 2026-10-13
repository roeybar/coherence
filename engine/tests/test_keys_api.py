import hashlib

import httpx
from fastapi.testclient import TestClient

from app.providers import fake, registry
from conftest import sid, turn_body


def test_locked_routes_428_and_wrong_passphrase(selfhost):
    c = TestClient(selfhost.app)
    for r in (
        c.get("/api/keys/status"),
        c.put("/api/keys/openai", json={"key": "x"}),
        c.delete("/api/keys/openai"),
    ):
        assert r.status_code == 428 and r.json()["detail"]["error"] == "key_store_locked"
    r = c.post("/api/keys/unlock", json={"passphrase": "pass-two"})
    assert r.status_code == 403
    assert r.json() == {"detail": {"error": "wrong_passphrase", "provider": None}}
    sc = r.headers.get("set-cookie", "")
    assert sc == ""


def test_cookie_flags_and_lock(selfhost):
    r = selfhost.post("/api/keys/unlock", json={"passphrase": "pass-one"})
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc
    selfhost.post("/api/keys/lock")
    assert selfhost.get("/api/keys/status").status_code == 428


def test_unlock_rate_limited(selfhost, clock):
    codes = [
        selfhost.post("/api/keys/unlock", json={"passphrase": "wrong"}).status_code
        for _ in range(6)
    ]
    assert codes[-1] == 429 and 429 not in codes[:4]


def test_od8_header_ignored_when_locked(selfhost):
    body = turn_body(selfhost)
    selfhost.post("/api/keys/lock")
    r = selfhost.post("/api/game/turn", json=body, headers={"X-Provider-Key": "canary-hdr-9"})
    assert r.status_code == 428 and r.json()["detail"]["error"] == "key_store_locked"


def test_key_hashes_in_order(hosted, clock, monkeypatch):
    monkeypatch.setattr(fake, "seen_key_hashes", [])
    for key in ("canary-key-a", "canary-key-b"):
        clock.t += 15
        r = hosted.post("/api/game/turn", json=turn_body(hosted), headers={"X-Provider-Key": key})
        assert r.status_code == 200
    want = [hashlib.sha256(k.encode()).hexdigest() for k in ("canary-key-a", "canary-key-b")]
    assert fake.seen_key_hashes[0] == want[0] and fake.seen_key_hashes[-1] == want[1]
    assert set(fake.seen_key_hashes) == set(want)


def test_without_flag_reaches_real_adapter(hosted, clock, monkeypatch, caplog):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER")
    for name in ("anthropic", "openai"):
        monkeypatch.setattr(
            registry.get_provider(name),
            "transport",
            httpx.MockTransport(lambda req: httpx.Response(401, text="bad key echo-CANARYSFX")),
        )
    for name in ("anthropic", "openai"):
        clock.t += 15
        r = hosted.post(
            "/api/game/turn",
            json=turn_body(hosted),
            headers={"X-Provider": name, "X-Coherence-Session": sid()},
        )
        assert r.status_code == 428 and r.json()["detail"] == {
            "error": "byok_rejected",
            "provider": name,
        }
        assert "CANARYSFX" not in r.text
    assert "CANARYSFX" not in caplog.text


def test_unknown_provider_404_shape(selfhost):
    selfhost.post("/api/keys/unlock", json={"passphrase": "pass-one"})
    for r in (selfhost.put("/api/keys/nope", json={"key": "x"}), selfhost.delete("/api/keys/nope")):
        assert r.status_code == 404
        assert r.json() == {"detail": {"error": "unknown_provider", "provider": None}}


def test_key_store_unavailable_500(monkeypatch, tmp_path, pack):
    from app import creds
    from app.config import settings
    from app.main import build_app

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "keys_dir", str(blocker / "keys"))
    monkeypatch.setattr(creds, "_vault", None)
    r = TestClient(build_app("selfhost")).post("/api/keys/unlock", json={"passphrase": "p"})
    assert r.status_code == 500
    assert r.json() == {"detail": {"error": "key_store_unavailable", "provider": None}}


def test_damaged_store_on_put_and_delete_403(selfhost, tmp_path):
    from app import creds
    from app.config import settings

    path = tmp_path / "keys" / creds.STORE_FILE
    assert settings.keys_dir == str(tmp_path / "keys") and path.is_file()
    path.write_text("{}")
    want = {"detail": {"error": "wrong_passphrase", "provider": None}}
    for r in (
        selfhost.put("/api/keys/openai", json={"key": "k"}),
        selfhost.delete("/api/keys/openai"),
    ):
        assert r.status_code == 403 and r.json() == want
