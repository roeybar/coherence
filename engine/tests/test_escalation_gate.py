from conftest import NEW_BODY
from test_escalation_text import MESSAGE

from app.config import settings
from app.game import engine
from app.providers import registry


def _ability_turn(selfhost, monkeypatch):
    monkeypatch.setattr(engine, "classify_turn", lambda text, config=None: ("ability", None))
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    r = selfhost.post("/api/game/turn", json={"save": r.json()["save"], "player_input": "I wait"})
    assert r.status_code == 200, r.text
    return r.json()


def test_no_strong_tier_means_no_dialog(selfhost, monkeypatch):
    body = _ability_turn(selfhost, monkeypatch)
    assert body.get("kind") != "escalation_consent_needed"
    assert body["save"]["turn"] == 1


def test_strong_tier_asks_with_the_approved_text(selfhost, monkeypatch):
    monkeypatch.setattr(registry, "has_strong_tier", lambda provider: provider == "fake")
    body = _ability_turn(selfhost, monkeypatch)
    assert body["kind"] == "escalation_consent_needed"
    assert body["message"] == MESSAGE
    assert body["reason"] == "ability"


def test_has_strong_tier(monkeypatch):
    assert registry.has_strong_tier("fake") is False
    monkeypatch.setattr(settings, "openai_model_fast", "model-a")
    monkeypatch.setattr(settings, "openai_model_strong", "model-a")
    assert registry.has_strong_tier("openai") is False
    monkeypatch.setattr(settings, "openai_model_strong", "model-b")
    assert registry.has_strong_tier("openai") is True
