from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import creds


def test_key_state_and_status(selfhost, caplog):
    cookie = selfhost.cookies.get("coh_session")
    state, secret, flags = creds.key_state(cookie, "anthropic")
    assert state == "ok" and flags == ()
    assert isinstance(secret, SecretStr) and secret.get_secret_value() == "canary-key-sh-1"
    assert creds.key_state(cookie, "openai") == ("other", None, ("anthropic",))
    assert creds.key_state("other", "anthropic") == ("locked", None, ())
    assert creds.key_state(cookie, "fake") == ("none", None, ())
    st = selfhost.get("/api/keys/status").json()
    assert st == {"anthropic": True, "openai": False, "gemini": False, "xai": False}
    assert "canary-key-sh-1" not in caplog.text


def test_second_session_sees_replace_and_delete(selfhost):
    other = TestClient(selfhost.app)
    assert other.post("/api/keys/unlock", json={"passphrase": "pass-one"}).status_code == 200
    a, b = selfhost.cookies.get("coh_session"), other.cookies.get("coh_session")
    assert a and b and a != b
    assert selfhost.put("/api/keys/anthropic", json={"key": "canary-key-sh-2"}).status_code == 200
    state, secret, _ = creds.key_state(b, "anthropic")
    assert state == "ok" and secret.get_secret_value() == "canary-key-sh-2"
    assert selfhost.delete("/api/keys/anthropic").status_code == 200
    assert creds.key_state(b, "anthropic") == ("none", None, ())
