from app.config import Settings


def test_hmac_key_random_per_load(monkeypatch):
    monkeypatch.delenv("ESCALATION_HMAC_KEY", raising=False)
    a, b = Settings().escalation_hmac_key, Settings().escalation_hmac_key
    assert len(a) == 32 and a != b
    monkeypatch.setenv("ESCALATION_HMAC_KEY", "")
    assert len(Settings().escalation_hmac_key) == 32


def test_hmac_key_from_env(monkeypatch):
    monkeypatch.setenv("ESCALATION_HMAC_KEY", "test-hmac-value")
    assert Settings().escalation_hmac_key == b"test-hmac-value"
