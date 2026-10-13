from conftest import NEW_BODY
from app.game import moderation, physicalisation
from app.game.prompts import _DEFAULT_ROUTER
from app.providers import fake, registry

MOVE = "I shape a lantern with zzability"


def user(t):
    return [{"role": "user", "content": t}]


def test_fake_strong_tier_needs_both_switches(monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.delenv("ENGINE_FAKE_STRONG_TIER", raising=False)
    assert registry.has_strong_tier("fake") is False
    monkeypatch.setenv("ENGINE_FAKE_STRONG_TIER", "1")
    assert registry.has_strong_tier("fake") is True
    monkeypatch.setenv("ENGINE_FAKE_STRONG_TIER", "true")
    assert registry.has_strong_tier("fake") is False
    monkeypatch.setenv("ENGINE_FAKE_STRONG_TIER", "1")
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "0")
    assert registry.has_strong_tier("fake") is False
    print("PASS fake strong tier needs both switches set to 1")


def test_tuple_rules_need_every_marker(pack, monkeypatch):
    assert fake.reply_for(_DEFAULT_ROUTER, user(MOVE)) == "ability"
    assert fake.reply_for(_DEFAULT_ROUTER, user("I wait")) == "standard"
    tok = moderation.bind_pack("testpack")
    try:
        parse = physicalisation._parse_classifier_json
        assert parse(fake.reply_for(physicalisation._TIER1_SYSTEM, user(MOVE))) == {
            "scope": "local",
            "confidence": "low",
        }
        assert parse(fake.reply_for(physicalisation._TIER1_SYSTEM, user("I wait"))) == {
            "scope": "none",
            "confidence": "high",
        }
        assert parse(fake.reply_for(physicalisation._TIER2_SYSTEM, user(MOVE))) == {
            "scope": "local",
            "confidence": "high",
        }
    finally:
        moderation.unbind_pack(tok)
    assert fake.reply_for("You are the game master.", user(MOVE)) == fake.DEFAULT_REPLY
    assert (
        fake.reply_for("", user("zzreplycanary"))
        == "A clerk reads 123-45-6789 aloud from the ledger."
    )
    monkeypatch.setattr(fake, "RULES", [("", "first")] + fake.RULES)
    assert fake.reply_for(_DEFAULT_ROUTER, user(MOVE)) == "first"
    print("PASS tuple markers all match, string markers keep first-match order")


def test_consent_then_intent_then_narrative(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setenv("ENGINE_FAKE_STRONG_TIER", "1")
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]

    def turn(doc, **extra):
        r = selfhost.post("/api/game/turn", json={"save": doc, "player_input": MOVE, **extra})
        assert r.status_code == 200, r.text
        return r.json()

    b1 = turn(save)
    assert b1["kind"] == "escalation_consent_needed"
    assert b1["reason"] == "physics"
    tok = b1["consent_token"]
    b2 = turn(b1["save"], consent_token=tok)
    assert b2["kind"] == "ability_intent_clarification_needed"
    dropped = turn(b2["save"], ability_intent="internal")
    assert dropped["kind"] == "escalation_consent_needed"
    b3 = turn(b2["save"], consent_token=tok, ability_intent="internal")
    assert "kind" not in b3 and isinstance(b3.get("narrative"), str) and b3["narrative"], b3
    assert b3["save"]["turn"] == save["turn"] + 1
    print("PASS physics consent then intent then narrative; dropping consent asks again")


def test_keep_it_small_then_intent_then_narrative(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setenv("ENGINE_FAKE_STRONG_TIER", "1")
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]

    def turn(doc, **extra):
        r = selfhost.post("/api/game/turn", json={"save": doc, "player_input": MOVE, **extra})
        assert r.status_code == 200, r.text
        return r.json()

    b1 = turn(save)
    assert b1["kind"] == "escalation_consent_needed"
    b2 = turn(b1["save"], decline_escalation=True)
    assert b2["kind"] == "ability_intent_clarification_needed"
    b3 = turn(b2["save"], decline_escalation=True, ability_intent="external")
    assert "kind" not in b3 and isinstance(b3.get("narrative"), str) and b3["narrative"], b3
    dropped = turn(b2["save"], ability_intent="external")
    assert dropped["kind"] == "escalation_consent_needed"
    print("PASS keep it small then intent then narrative; dropping decline asks again")
