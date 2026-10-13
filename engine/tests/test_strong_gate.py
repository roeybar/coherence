"""Move-bound consent and transactional turn gates over a mock transport."""

import copy
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from app import store
from app.config import settings
from app.game import engine, escalation, lore, moderation, physics, physicalisation
from app.game.craft_gate import CraftRollPending
from app.game.dice_gate import DiceRollPending
from app.game.escalation import EscalationPending, StrongGate
from app.game.llm import MODEL_HAIKU
from app.game.prompts import CONSULT_SYSTEM
from app.game.state import CharacterProfile, GameConfig, save_to_doc
from app.providers import registry


MOVE = "I reshape the mist. I can only do this with effort."
ACTIVE = ContextVar("turn_replies", default=None)


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [])


class TurnReplies:
    def __init__(self, trigger="ability", scope="location", turn_class="ability"):
        self.trigger = trigger
        self.scope = scope
        self.turn_class = turn_class
        self.consult_reply = "OUTCOME: Mist settles. COST: Effort. COMPLICATION: A damp floor."
        self.post_dice = False
        self.checkpoint = False
        self.bodies = []

    @property
    def strong(self):
        return [body for body in self.bodies if body["model"] == settings.anthropic_model_strong]

    def __call__(self, request):
        import json

        body = json.loads(request.content)
        self.bodies.append(body)
        system = text_of(body.get("system"))
        if system == physicalisation._TIER1_SYSTEM:
            scope = "none" if self.trigger == "ability" else self.scope
            confidence = "low" if self.trigger == "tier2" else "high"
            reply = json.dumps({"scope": scope, "confidence": confidence})
        elif system == physicalisation._TIER2_SYSTEM:
            reply = json.dumps({"scope": self.scope, "confidence": "high"})
        elif system == physics._PHYSICALISER_SYSTEM:
            if self.trigger == "physics" and body["model"] == settings.anthropic_model_fast:
                reply = "unparseable grounding"
            else:
                reply = json.dumps(
                    {
                        "normalized_wish": physics.normalize_wish(MOVE),
                        "grounded_mechanism": {"source": "ambient water"},
                        "narrative_hint": "A cool mist gathers.",
                        "energy_demand": "effort",
                        "failure_modes": [],
                        "vulnerabilities": [],
                        "scale_of_consequence": "local",
                    }
                )
        elif system == CONSULT_SYSTEM:
            reply = self.consult_reply
        elif '"dc":<int 1-25>' in text_of(body["messages"][-1]["content"]):
            reply = '{"dc": 12, "skill": "balance", "stat": "DEX", "flavor": "Mist shifts."}'
        else:
            marker = " [roll:balance:12]" if self.post_dice else ""
            delta = {"checkpoint_worthy": True} if self.checkpoint else {}
            reply = (
                "[NARRATIVE]The mist settles around your boots."
                + marker
                + "[/NARRATIVE][STATE_DELTA]"
                + json.dumps(delta)
                + "[/STATE_DELTA]"
            )
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": reply}],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 3,
                },
            },
        )


def configure_turns(monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    monkeypatch.setattr(
        registry.get_provider("anthropic"),
        "transport",
        httpx.MockTransport(lambda request: ACTIVE.get()(request)),
    )
    monkeypatch.setattr(
        engine, "classify_turn", lambda text, config=None: (ACTIVE.get().turn_class, None)
    )
    monkeypatch.setattr(engine, "classify_stakes", lambda text, config=None: ("normal", None))
    monkeypatch.setattr(engine, "index_turn", lambda *args, **kw: None)
    monkeypatch.setattr(physics, "_embed", lambda text: np.ones(384, dtype=np.float32))
    monkeypatch.setattr(physics, "_now", lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    lore.clear_cache()


def make_save(policy="ask"):
    gs = engine.create_save(
        "mist",
        CharacterProfile(name="Walker"),
        GameConfig(pack_id="testpack", tier="pro", ability_escalation=policy),
    )
    gs.turn = 1
    gs.created = "2026-01-01T00:00:00+00:00"
    gs.history = [
        {"role": "assistant", "content": "An older courtyard.", "turn": 0},
        {"role": "user", "content": "I arrived.", "turn": 1},
        {"role": "assistant", "content": "Mist over stone.", "turn": 1},
    ]
    gs.state.npcs = {"observer": {"name": "Observer", "location": gs.state.location}}
    return gs


@contextmanager
def turn_bindings(replies, doc):
    rtok = ACTIVE.set(replies)
    stok = store.bind_save(doc)
    mtok = moderation.bind_pack("testpack")
    ptok = registry.bind_request("anthropic", SecretStr("canary-consent"))
    try:
        yield
    finally:
        registry.unbind_request(ptok)
        moderation.unbind_pack(mtok)
        store.unbind_save(stok)
        ACTIVE.reset(rtok)


def attempt(gs, replies, doc, player_input=MOVE, **kw):
    with turn_bindings(replies, doc):
        return engine.run_turn(gs, player_input, "local", **kw)


def consent_for(gs, replies, doc, player_input=MOVE, **kw):
    with pytest.raises(EscalationPending) as raised:
        attempt(gs, replies, doc, player_input, **kw)
    return raised.value.to_payload()["consent_token"]


@pytest.mark.parametrize("policy", ["ask", "auto", ""])
@pytest.mark.parametrize("trigger", ["tier2", "physics", "ability"])
def test_consent_once_covers_every_strong_site(gate_rig, policy, trigger):
    replies = gate_rig
    replies.trigger = trigger
    gs, doc = make_save(policy), {}
    before = copy.deepcopy(save_to_doc(gs))
    with pytest.raises(EscalationPending) as raised:
        attempt(gs, replies, doc, ability_intent="external")
    assert raised.value.reason == ("ability" if trigger == "ability" else "physics")
    assert not replies.strong
    assert save_to_doc(gs) == before
    assert doc["physics_cache"] == []
    token = raised.value.to_payload()["consent_token"]
    out = attempt(gs, replies, doc, consent_token=token, ability_intent="external")
    assert out["narrative"] and gs.turn == before["turn"] + 1
    expected = 1 if trigger == "ability" else 2
    assert len(replies.strong) == expected
    assert sum(text_of(b.get("system")) == CONSULT_SYSTEM for b in replies.strong) == 1
    assert out["model"] == MODEL_HAIKU
    print(f"PASS {policy or 'default'} {trigger}: one consent covers {expected} strong calls")


@pytest.mark.parametrize("trigger", ["tier2", "physics", "ability"])
@pytest.mark.parametrize("guard", ["decline", "never", "regenerated", "no_strong", "tier"])
def test_fast_only_guards(gate_rig, monkeypatch, trigger, guard):
    replies = gate_rig
    replies.trigger = trigger
    gs, doc = make_save("never" if guard == "never" else "ask"), {}
    kw = {"ability_intent": "external"}
    if guard != "never":
        kw["consent_token"] = consent_for(gs, replies, doc, ability_intent="external")
        replies.bodies.clear()
    if guard == "decline":
        kw["decline_escalation"] = True
    elif guard == "regenerated":
        kw["regenerated"] = True
    elif guard == "no_strong":
        monkeypatch.setattr(settings, "anthropic_model_strong", settings.anthropic_model_fast)
    elif guard == "tier":
        kw["effective_tier"] = "basic"
    out = attempt(gs, replies, doc, **kw)
    # Equal ids have no separate strong request; every body uses the fast id.
    if guard == "no_strong":
        assert all(b["model"] == settings.anthropic_model_fast for b in replies.bodies)
    else:
        assert not replies.strong
    assert not any(row["id"] == "consult" for row in out["beats"])
    assert out["narrative"] and out["model"] == MODEL_HAIKU
    if trigger == "tier2":
        assert gs.state.scope_breach_log[-1]["scope"] == replies.scope
    if trigger == "physics":
        with turn_bindings(replies, doc):
            result = physics.physicalise(MOVE)
        assert result["record"] is None
        assert result["error"] == "llm output failed to parse (fast tier only)"
    print(f"PASS {guard} {trigger}: fast only")


@pytest.mark.parametrize("change", ["input", "created", "turn", "earlier_history", "state", "undo"])
def test_token_rejects_changed_move(gate_rig, change):
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc)
    original = copy.deepcopy(gs)
    move = MOVE
    if change == "input":
        move += " Again."
    elif change == "created":
        gs.created = "2026-01-02T00:00:00+00:00"
    elif change == "turn":
        gs.turn += 1
    elif change == "earlier_history":
        gs.history[0]["content"] = "An older garden."
    elif change == "state":
        gs.state.entropy_attention = 7
    elif change == "undo":
        gs.undo_stack.append({"state": gs.state.to_dict(), "history": list(gs.history), "turn": 0})
        assert engine.run_undo(gs)["ok"]
        move += " Again."
    if change in ("earlier_history", "state"):
        assert gs.created == original.created and gs.turn == original.turn
        assert len(gs.history) == len(original.history)
        assert gs.history[-1] == original.history[-1]
    with pytest.raises(EscalationPending):
        attempt(gs, gate_rig, doc, move, consent_token=token)
    assert not gate_rig.strong
    print(f"PASS token rejects changed {change}")


@pytest.mark.parametrize("kind", ["forged", "expired"])
def test_bad_tokens_rejected(gate_rig, kind):
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc)
    if kind == "forged":
        body, _ = token.split(".")
        token = body + ".AAAA"
    else:
        import json

        payload = json.loads(escalation._b64u_decode(token.split(".")[0]))
        token = escalation.mint(
            save_id="local",
            turn_index=gs.turn,
            move=payload["m"],
            now=int(time.time()) - escalation.TOKEN_TTL_S - 2,
        )
    with pytest.raises(EscalationPending):
        attempt(gs, gate_rig, doc, consent_token=token)
    assert not gate_rig.strong
    print(f"PASS {kind} token makes zero strong calls")


def test_digest_is_lazy_and_reused():
    calls = []

    def digest():
        calls.append("computed")
        return "one-move"

    for policy, allowed, declined, regenerated in (
        ("never", True, False, False),
        ("ask", False, False, False),
        ("ask", True, True, False),
        ("ask", True, False, True),
    ):
        gate = StrongGate(policy, allowed, None, declined, regenerated, "local", 1, digest)
        assert not gate.need("physics")
    assert not calls
    token = escalation.mint(save_id="local", turn_index=1, move="one-move")
    gate = StrongGate("ask", True, token, False, False, "local", 1, digest)
    assert gate.need("physics") and gate.need("ability")
    assert calls == ["computed"]
    print("PASS move digest is lazy and computed once for successive strong sites")


def test_identical_fork_and_pending_input_reuse(gate_rig):
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc)
    fork = copy.deepcopy(gs)
    fork.state.pending_input = {"text": MOVE, "gate": "ability_intent"}
    out = attempt(fork, gate_rig, {}, consent_token=token)
    assert out["narrative"] and len(gate_rig.strong) == 1
    print("PASS identical fork and pending-input-only changes reuse consent")


def test_consent_then_intent_then_completion(gate_rig):
    gate_rig.trigger = "tier2"
    gs, doc = make_save(), {}
    before = copy.deepcopy(save_to_doc(gs))
    token = consent_for(gs, gate_rig, doc)
    with pytest.raises(physicalisation.AbilityIntentAmbiguous):
        attempt(gs, gate_rig, doc, consent_token=token)
    assert save_to_doc(gs) == before and doc["physics_cache"] == []
    out = attempt(gs, gate_rig, doc, consent_token=token, ability_intent="external")
    assert out["narrative"] and len(gate_rig.strong) == 3
    assert sum(text_of(b.get("system")) == CONSULT_SYSTEM for b in gate_rig.strong) == 1
    print("PASS physics consent, intent gate, completion without second consent")


def test_expired_token_in_gate_chain_asks_for_fresh_consent(gate_rig, monkeypatch):
    gate_rig.trigger = "tier2"
    gs, doc = make_save(), {}
    real = time.time()
    token = consent_for(gs, gate_rig, doc)
    with pytest.raises(physicalisation.AbilityIntentAmbiguous):
        attempt(gs, gate_rig, doc, consent_token=token)
    monkeypatch.setattr(escalation, "time", SimpleNamespace(time=lambda: real + 301))
    with pytest.raises(EscalationPending) as raised:
        attempt(gs, gate_rig, doc, consent_token=token, ability_intent="external")
    assert raised.value.reason == "physics"
    fresh = raised.value.to_payload()["consent_token"]
    out = attempt(gs, gate_rig, doc, consent_token=fresh, ability_intent="external")
    assert out["narrative"]
    assert sum(text_of(b.get("system")) == CONSULT_SYSTEM for b in gate_rig.strong) == 1
    print("PASS expired token in a gate chain re-asks and the newest token completes the move")


def test_decline_then_intent(gate_rig):
    gate_rig.trigger = "tier2"
    gs, doc = make_save(), {}
    with pytest.raises(physicalisation.AbilityIntentAmbiguous):
        attempt(gs, gate_rig, doc, decline_escalation=True)
    out = attempt(gs, gate_rig, doc, decline_escalation=True, ability_intent="external")
    assert out["narrative"] and not gate_rig.strong
    print("PASS decline survives the intent gate with zero strong calls")


def durable_fields(gs):
    state = gs.state.to_dict()
    return copy.deepcopy(
        {
            "state": {
                key: state.get(key)
                for key in (
                    "self_restrictions",
                    "entropy_attention",
                    "tier_exposure",
                    "scope_breach_log",
                )
            },
            "undo_count": len(gs.undo_stack),
            "checkpoint_state": gs.checkpoint_state,
            "checkpoint_history": gs.checkpoint_history,
            "checkpoint_turn": gs.checkpoint_turn,
        }
    )


def test_transactional_resend_matches_uninterrupted_move(gate_rig):
    gate_rig.trigger = "grounded"
    gs, doc = make_save(), {}
    baseline = copy.deepcopy(gs)
    before = copy.deepcopy(save_to_doc(gs))
    token = consent_for(gs, gate_rig, doc, ability_intent="external")
    assert save_to_doc(gs) == before and doc["physics_cache"] == []
    attempt(gs, gate_rig, doc, consent_token=token, ability_intent="external")
    attempt(baseline, gate_rig, {}, consent_token=token, ability_intent="external")
    assert durable_fields(gs) == durable_fields(baseline)
    assert len(gs.state.self_restrictions) == 1
    assert gs.state.entropy_attention > 0
    assert gs.state.tier_exposure["observer"]["tier_3_count"] == 1
    assert len(gs.state.scope_breach_log) == 1
    print("PASS gated resend writes restrictions, entropy, witnesses, breach and undo once")


@pytest.mark.parametrize("cache_hit", [False, True])
def test_cache_hit_and_miss_rollback(gate_rig, cache_hit):
    gate_rig.trigger = "grounded"
    gs, doc = make_save(), {}
    if cache_hit:
        with turn_bindings(gate_rig, doc):
            physics.physicalise(MOVE)
        assert doc["physics_cache"]
    before_save = copy.deepcopy(save_to_doc(gs))
    before_cache = copy.deepcopy(doc.get("physics_cache", []))
    cache_list = doc.setdefault("physics_cache", [])
    consent_for(gs, gate_rig, doc, ability_intent="external")
    assert save_to_doc(gs) == before_save
    assert doc["physics_cache"] == before_cache
    assert doc["physics_cache"] is cache_list
    print(f"PASS physics cache {'hit' if cache_hit else 'miss'} restored in place")


def test_post_dm_dice_rollback(gate_rig):
    gate_rig.trigger = "grounded"
    gate_rig.turn_class = "standard"
    gate_rig.post_dice = True
    gate_rig.checkpoint = True
    gs, doc = make_save("never"), {}
    baseline = copy.deepcopy(gs)
    before = copy.deepcopy(save_to_doc(gs))
    with pytest.raises(DiceRollPending):
        attempt(gs, gate_rig, doc, ability_intent="external")
    assert save_to_doc(gs) == before
    assert doc["physics_cache"] == []
    assert len(gs.undo_stack) == len(before["undo_stack"])
    attempt(gs, gate_rig, doc, ability_intent="external", dice_roll=20)
    attempt(baseline, gate_rig, {}, ability_intent="external", dice_roll=20)
    assert durable_fields(gs) == durable_fields(baseline)
    assert len(gs.undo_stack) == len(before["undo_stack"]) + 1
    print("PASS post-DM dice gate restores save, checkpoint, cache and undo")


@pytest.mark.parametrize("kind", ["craft", "dice"])
def test_ability_gate_follows_craft_and_dice(gate_rig, monkeypatch, kind):
    from app.game import craft_gate, dice_gate

    module = craft_gate if kind == "craft" else dice_gate
    detector = "detect_craft" if kind == "craft" else "detect_dice_request"
    pending = CraftRollPending if kind == "craft" else DiceRollPending
    monkeypatch.setattr(module, detector, lambda *args, **kw: True)
    gs, doc = make_save(), {}
    before = copy.deepcopy(save_to_doc(gs))
    with pytest.raises(pending):
        attempt(gs, gate_rig, doc)
    assert not gate_rig.strong
    assert save_to_doc(gs) == before and doc["physics_cache"] == []
    out = attempt(gs, gate_rig, doc, **{f"{kind}_roll": 15})
    assert out["narrative"] and not gate_rig.strong
    assert all(row["id"] != "consult" for row in out["beats"])
    print(f"PASS ability move with {kind} roll asks no consent and makes zero strong calls")


def test_post_dm_dice_after_consult_charges_once(gate_rig):
    gate_rig.turn_class = "ability"
    gate_rig.post_dice = True
    gs, doc = make_save("ask"), {}
    before = copy.deepcopy(save_to_doc(gs))
    token = consent_for(gs, gate_rig, doc)
    with pytest.raises(DiceRollPending):
        attempt(gs, gate_rig, doc, consent_token=token)
    assert save_to_doc(gs) == before and doc["physics_cache"] == []
    out = attempt(gs, gate_rig, doc, consent_token=token, dice_roll=20)
    assert out["narrative"]
    assert len(gate_rig.strong) == 1
    assert sum(text_of(b.get("system")) == CONSULT_SYSTEM for b in gate_rig.bodies) == 1
    print("PASS post-DM dice after a consult: one consent, one strong charge")


@pytest.mark.parametrize("path", ["cache_hit", "cache_miss", "post_dice"])
def test_gated_response_save_is_unchanged(selfhost, gate_rig, path):
    from fastapi.encoders import jsonable_encoder

    gate_rig.trigger = "grounded"
    gs = make_save()
    doc = {"items": [], "memory": [], "physics_cache": []}
    if path == "cache_hit":
        with turn_bindings(gate_rig, doc):
            physics.physicalise(MOVE)
        assert doc["physics_cache"]
    elif path == "post_dice":
        gs.config.ability_escalation = "never"
        gate_rig.turn_class = "standard"
        gate_rig.post_dice = True
        gate_rig.checkpoint = True
    save = jsonable_encoder({**save_to_doc(gs), **doc})
    response = selfhost.post(
        "/api/game/turn",
        json={
            "save": save,
            "player_input": MOVE,
            "ability_intent": "external",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    expected = "dice_roll_needed" if path == "post_dice" else "escalation_consent_needed"
    assert body["kind"] == expected
    assert without_pending(body["save"]) == without_pending(save)
    print(f"PASS {path} gated response preserves the complete input save")


def test_raw_input_digest_survives_meme_rewrite(gate_rig, monkeypatch):
    from app.game import meme_recognition

    calls = []

    def recognise(*args, **kw):
        calls.append(1)
        return meme_recognition.Recognition(
            rewritten=f"I reshape the mist, variant {len(calls)}.", triggered=True
        )

    monkeypatch.setattr(meme_recognition, "recognise", recognise)
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc, player_input="a misty phrase")
    out = attempt(gs, gate_rig, doc, "a misty phrase", consent_token=token)
    assert len(calls) >= 2
    assert out["narrative"]
    assert len(gate_rig.strong) == 1
    print("PASS raw-input consent survives meme rewriting")


def test_turn_zero_gender_flip_is_restored_on_gate(gate_rig):
    gs, doc = make_save(), {}
    gs.turn = 0
    gs.profile.gender = "m"
    move = "i'm a girl. " + MOVE
    before = copy.deepcopy(save_to_doc(gs))
    token = consent_for(gs, gate_rig, doc, player_input=move)
    assert save_to_doc(gs) == before and gs.profile.gender == "m"
    out = attempt(gs, gate_rig, doc, move, consent_token=token)
    assert out["narrative"] and gs.profile.gender == "f"
    print("PASS turn-0 gender flip restored on a gated move")


def test_plain_move_without_provider_or_store(gate_rig, monkeypatch):
    from app.game import items

    gate_rig.turn_class = "standard"
    monkeypatch.setattr(
        items, "visible_to_player", lambda *args, **kw: {"scene": [], "carried": [], "npcs": {}}
    )
    gs = make_save()
    assert registry.current_provider() == "" and not registry.has_strong_tier("")
    with pytest.raises(store.StoreUnbound):
        store.current_save()
    # A mock LLM completes the ordinary beat without binding a provider.
    from app.game.beats import builtin

    monkeypatch.setattr(
        builtin,
        "call_llm_reply",
        lambda *args, **kw: (
            "[NARRATIVE]Mist over stone.[/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]",
            None,
            "end_turn",
            False,
        ),
    )
    monkeypatch.setattr(
        physicalisation,
        "classify_scope",
        lambda text: ({"scope": "none", "confidence": "high"}, None),
    )
    out = engine.run_turn(gs, "I wait", "local")
    assert out["narrative"] and not gate_rig.strong
    print("PASS plain move initialises without provider or bound store")


def without_pending(doc):
    result = copy.deepcopy(doc)
    result["state"].pop("pending_input", None)
    return result


def test_hosted_rate_limit_preserves_consent(hosted, clock, gate_rig):
    save = save_to_doc(make_save())
    save.update({"items": [], "memory": [], "physics_cache": []})
    first = hosted.post("/api/game/turn", json={"save": save, "player_input": MOVE})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["kind"] == "escalation_consent_needed"
    assert without_pending(body["save"]) == without_pending(save)
    resend = {"save": body["save"], "player_input": MOVE, "consent_token": body["consent_token"]}
    count = len(gate_rig.bodies)
    immediate = hosted.post("/api/game/turn", json=resend)
    assert immediate.status_code == 429, immediate.text
    assert len(gate_rig.bodies) == count
    clock.t += 15
    completed = hosted.post("/api/game/turn", json=resend)
    assert completed.status_code == 200, completed.text
    assert completed.json().get("kind") != "escalation_consent_needed"
    assert completed.json()["save"]["turn"] == save["turn"] + 1
    assert len(gate_rig.strong) == 1
    print("PASS hosted immediate resend 429, resend after 15 seconds reuses consent")
