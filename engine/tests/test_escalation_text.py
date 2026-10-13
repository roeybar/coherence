from app.game.escalation import EscalationPending

MESSAGE = (
    "This move looks like an ability action. The stronger model set for your "
    "provider can work out how it plays out, and your provider may charge more "
    "for it. Continue to use it for this move, or Keep it small to stay on the "
    "usual model."
)

PHYSICS_MESSAGE = (
    "The usual model could not settle the physics of this move. The stronger "
    "model set for your provider can work it out, and your provider may charge "
    "more for it. Continue to use it for this move, or Keep it small to stay "
    "on the usual model."
)


def test_escalation_message_is_provider_neutral():
    p = EscalationPending(save_id="local", turn_index=3).to_payload()
    assert p["message"] == MESSAGE
    assert p["message"].isascii()
    low = p["message"].lower()
    for word in ("haiku", "sonnet", "$", "4x", "~"):
        assert word not in low
    assert p["reason"] == "ability"
    assert set(p) == {"kind", "consent_token", "turn_index", "ttl_s", "message", "reason"}


def test_physics_message_is_provider_neutral():
    p = EscalationPending(save_id="local", turn_index=3, reason="physics").to_payload()
    assert p["message"] == PHYSICS_MESSAGE
    assert p["reason"] == "physics"
    assert p["message"].isascii()
    for word in ("haiku", "sonnet", "opus", "$", "4x", "~"):
        assert word not in p["message"].lower()
    assert set(p) == {"kind", "consent_token", "turn_index", "ttl_s", "message", "reason"}
