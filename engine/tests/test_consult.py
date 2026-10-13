"""A bounded rules consult precedes fast-tier ability narration."""

import inspect
import json

import httpx
import pytest

from app.config import settings
from app.game import engine, lore, moderation, pack_beats
from app.game.beats import builtin
from app.game.beats.builtin import (
    ConsultBeat,
    DMActBeat,
    DMWorldStepBeat,
    GroundingBeat,
    NpcReactBeat,
    build_pipeline_for,
)
from app.game.beats.state import TurnState
from app.game.context import ContextEngine
from app.game.llm import MODEL_HAIKU
from app.game.prompts import CONSULT_SYSTEM
from app.game.state import save_to_doc
from app.providers import registry

from test_strong_gate import (
    MOVE,
    attempt,
    consent_for,
    make_save,
    text_of,
    turn_bindings,
)


def is_act(body):
    return "BEAT FOCUS - act" in text_of(body.get("system")) + text_of(
        body["messages"][-1]["content"]
    )


def test_pipeline_composition():
    assert [type(beat) for beat in build_pipeline_for("ability", consult=True).beats] == [
        ConsultBeat,
        DMActBeat,
        GroundingBeat,
        NpcReactBeat,
    ]
    print("PASS ability consult=True: Consult, DMAct, Grounding, NpcReact")
    for kw in ({}, {"consult": False}, {"stakes": "mundane"}):
        beats = build_pipeline_for("ability", **kw).beats
        assert [type(beat) for beat in beats] == [DMActBeat, GroundingBeat, NpcReactBeat]
    print("PASS default and consult=False ability: three beats, DMAct first")
    assert [type(beat) for beat in build_pipeline_for("standard", consult=True).beats] == [
        DMWorldStepBeat
    ]
    print("PASS standard consult=True: DMWorldStep only")
    source = inspect.getsource(engine._run_turn_inner)
    assert "consult=ts.consult" in source and "stakes=stakes_label" in source
    source = inspect.getsource(DMActBeat._call_and_parse)
    assert "ts.turn_model" in source and "model=MODEL_SONNET" not in source
    print("PASS engine passes consult and stakes; DMAct reads turn_model")


def test_pack_triggers_still_precede_consult():
    pack = {
        "pack_beats": {
            "beats": [{"id": "mist", "label": "Mist", "text": "The floor is damp.", "when": {}}],
            "overrides": [{"beat": "grounding", "mode": "off"}],
        }
    }
    beats = build_pipeline_for("ability", pack=pack, consult=True).beats
    assert isinstance(beats[0], pack_beats.PackTriggerBeat)
    assert isinstance(beats[1], ConsultBeat)
    assert isinstance(beats[2], DMActBeat)
    assert pack_beats._public_name(beats[1]) is None
    assert not hasattr(beats[1], "_public_name")
    print("PASS pack triggers precede consult and consult has no override target")


def test_consult_is_minimal_and_dmact_is_fast(gate_rig):
    gs, doc = make_save("auto"), {}
    token = consent_for(gs, gate_rig, doc)
    gate_rig.bodies.clear()
    out = attempt(gs, gate_rig, doc, consent_token=token)
    assert out["narrative"] and len(gate_rig.strong) == 1
    consult = gate_rig.strong[0]
    assert text_of(consult["system"]) == CONSULT_SYSTEM
    assert consult["max_tokens"] == 2000
    assert len(consult["messages"]) == 1 and consult["messages"][0]["role"] == "user"
    message = text_of(consult["messages"][0]["content"])
    assert MOVE in message and "I arrived." not in message
    assert len(CONSULT_SYSTEM) + len(message) <= 9000
    act = next(body for body in gate_rig.bodies if is_act(body))
    assert act["model"] == settings.anthropic_model_fast
    stable_prefix = act["system"][0]["text"][:200]
    assert stable_prefix not in text_of(consult["system"]) + message
    context = text_of(act["messages"][-1]["content"])
    ruling = "[RULING - DM only, from the rules consult]\n" + gate_rig.consult_reply + "\n[/RULING]"
    assert ruling in context and context.index(ruling) < context.index(MOVE)
    row = next(row for row in out["beats"] if row["id"] == "consult")
    assert row["tokens_in"] == 11 and row["tokens_out"] == 7
    assert row["cache_read"] == 5 and row["cache_creation"] == 3
    assert all(
        body["model"] == settings.anthropic_model_fast
        for body in gate_rig.bodies
        if body is not consult
    )
    print("PASS consented auto: one 2000-token consult, minimal input, fast DMAct with ruling")


def test_empty_consult_still_narrates_without_retry(gate_rig, caplog):
    gate_rig.consult_reply = ""
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc)
    gate_rig.bodies.clear()
    out = attempt(gs, gate_rig, doc, consent_token=token)
    assert out["narrative"] and len(gate_rig.strong) == 1
    act = next(body for body in gate_rig.bodies if is_act(body))
    assert "[RULING" not in text_of(act["messages"][-1]["content"])
    assert "ConsultBeat: empty rules ruling" in caplog.text
    print("PASS empty consult logs once, does not retry and still narrates")


def test_failed_consult_still_narrates_without_ruling(gate_rig, monkeypatch, caplog):
    def respond(request):
        body = json.loads(request.content)
        if body["model"] == settings.anthropic_model_strong:
            gate_rig.bodies.append(body)
            return httpx.Response(529, json={"error": {"type": "overloaded_error"}})
        return gate_rig(request)

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc)
    assert not gate_rig.strong
    gate_rig.bodies.clear()
    out = attempt(gs, gate_rig, doc, consent_token=token)
    assert out["narrative"] and out["model"] == MODEL_HAIKU
    assert len(gate_rig.strong) == 1
    assert text_of(gate_rig.strong[0]["system"]) == CONSULT_SYSTEM
    assert not any(row["id"] == "consult" for row in out["beats"])
    act = next(body for body in gate_rig.bodies if is_act(body))
    assert act["model"] == settings.anthropic_model_fast
    assert "[RULING" not in text_of(act["messages"][-1]["content"])
    assert all(
        body["model"] == settings.anthropic_model_fast
        for body in gate_rig.bodies
        if body is not gate_rig.strong[0]
    )
    assert "ConsultBeat: strong call failed: provider 529: provider_unavailable" in caplog.text
    print("PASS strong consult 529: Haiku completes with no consult beat and no ruling")


def test_blocked_consult_never_reaches_dmact(selfhost, gate_rig):
    from fastapi.encoders import jsonable_encoder

    gate_rig.consult_reply = "OUTCOME: zzpackcanary."
    gs, doc = make_save(), {}
    token = consent_for(gs, gate_rig, doc)
    with pytest.raises(moderation.OutputBlocked):
        attempt(gs, gate_rig, doc, consent_token=token)
    assert len(gate_rig.strong) == 1
    assert not any(is_act(body) for body in gate_rig.bodies)

    gate_rig.bodies.clear()
    save = jsonable_encoder(save_to_doc(make_save()))
    save.update({"items": [], "memory": [], "physics_cache": []})
    first = selfhost.post("/api/game/turn", json={"save": save, "player_input": MOVE})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["kind"] == "escalation_consent_needed"
    blocked = selfhost.post(
        "/api/game/turn",
        json={
            "save": body["save"],
            "player_input": MOVE,
            "consent_token": body["consent_token"],
        },
    )
    assert blocked.status_code == 451, blocked.text
    assert blocked.json()["detail"]["error"] == "output_blocked"
    assert len(gate_rig.strong) == 1
    assert not any(is_act(body) for body in gate_rig.bodies)
    print("PASS blocked ruling is output_blocked and DMAct is never sent")


def test_oversized_rules_make_no_strong_call(gate_rig, monkeypatch, caplog):
    monkeypatch.setattr(builtin, "_ability_block", lambda pack, turn_class: "r" * 12000)
    gs, doc = make_save(), {}
    out = attempt(gs, gate_rig, doc)
    assert not gate_rig.strong and out["narrative"]
    assert not any(row["id"] == "consult" for row in out["beats"])
    act = next(body for body in gate_rig.bodies if is_act(body))
    assert act["model"] == settings.anthropic_model_fast
    assert "ConsultBeat: mandatory rules and move exceed input budget" in caplog.text
    print("PASS 12000-character mandatory rules: no consent asked, zero strong bodies")


def consult_state(gs, player_input):
    pack = lore.load_pack("testpack")
    schema = lore.state_field_defs(pack)
    return TurnState(
        gs=gs,
        save_id="local",
        player_input=player_input,
        pre_state=gs.state.to_dict(),
        pack=pack,
        pack_schema=schema,
        valid_keys={field["name"] for field in schema},
        turn_class="ability",
        turn_model=MODEL_HAIKU,
        monster_level=0,
        has_npcs=False,
        stage=gs.state.get("tutorial_stage", 0),
        insight_dice=[],
        memory_block="",
        recalled=[],
        ctx=ContextEngine(),
        consult=True,
    )


def test_large_optional_restrictions_fit_budget(gate_rig, monkeypatch):
    rules = "Rules must remain whole."
    monkeypatch.setattr(builtin, "_ability_block", lambda pack, turn_class: rules)
    gs = make_save()
    ts = consult_state(gs, "m" * 2000)
    ts.physicalisation_block = "".join(f"Restriction {n}: " + "r" * 3000 for n in range(5))
    ts.world_systems_block = "World systems must yield to physicalisation."
    with turn_bindings(gate_rig, {}):
        ConsultBeat().run(ts)
    assert len(gate_rig.strong) == 1
    body = gate_rig.strong[0]
    message = text_of(body["messages"][0]["content"])
    assert len(CONSULT_SYSTEM) + len(message) <= 9000
    assert message.startswith(rules + "\n\n") and message.endswith(ts.player_input)
    assert "Restriction 0:" in message
    assert ts.world_systems_block not in message
    assert ts.consult_ruling == gate_rig.consult_reply
    print("PASS five 3000-character restrictions plus a 2000-character move fit 9000 characters")


def test_optional_priority_and_last_narrative_cap(gate_rig, monkeypatch):
    rules = "Whole rules."
    monkeypatch.setattr(builtin, "_ability_block", lambda pack, turn_class: rules)
    gs = make_save()
    last = "".join(f"Narrative line {n:04d}. " for n in range(150))
    gs.history[-1]["content"] = "locale-only narrative"
    gs.history[-1]["content_en"] = last
    ts = consult_state(gs, MOVE)
    ts.physicalisation_block = "Physicalisation first."
    ts.world_systems_block = "World systems second."
    with turn_bindings(gate_rig, {}):
        ConsultBeat().run(ts)
    message = text_of(gate_rig.strong[0]["messages"][0]["content"])
    state = gs.state.to_dict()
    state.pop("pending_input", None)
    world = (
        "[WORLD STATE]\n"
        + json.dumps(state, sort_keys=True, separators=(",", ":"))
        + "\n[/WORLD STATE]"
    )
    assert message == "\n\n".join(
        [rules, ts.physicalisation_block, ts.world_systems_block, world, last[:1500], MOVE]
    )
    assert "locale-only narrative" not in message
    assert len(CONSULT_SYSTEM) + len(message) <= 9000
    print(
        "PASS optional priority: physics, world systems, world state, canonical last narrative capped at 1500"
    )


def test_consult_includes_world_state_without_pending_input(gate_rig):
    gs = make_save()
    gs.state.location = "Mist courtyard"
    gs.state.pending_input = "zz-pending"
    ts = consult_state(gs, MOVE)
    with turn_bindings(gate_rig, {}):
        ConsultBeat().run(ts)
    assert len(gate_rig.strong) == 1
    message = text_of(gate_rig.strong[0]["messages"][0]["content"])
    assert "[WORLD STATE]" in message and gs.state.location in message
    assert "zz-pending" not in message
    assert len(CONSULT_SYSTEM) + len(message) <= 9000
    assert gs.state.pending_input == "zz-pending"
    print("PASS bounded consult includes world state and location without pending input")


@pytest.mark.parametrize("declined,policy", [(True, "ask"), (False, "never")])
def test_decline_or_never_ability_has_no_consult(gate_rig, declined, policy):
    gs = make_save(policy)
    out = attempt(gs, gate_rig, {}, decline_escalation=declined)
    assert not gate_rig.strong
    assert all(row["id"] != "consult" for row in out["beats"])
    assert out["narrative"] and out["model"] == MODEL_HAIKU
    print("PASS decline or never through run_turn: zero strong bodies and no consult beat")


def test_consult_applies_is_a_second_guard(gate_rig):
    ts = consult_state(make_save(), MOVE)
    ts.consult = False
    assert not ConsultBeat().applies(ts)
    pipeline = build_pipeline_for("ability", consult=True)
    with turn_bindings(gate_rig, {}):
        pipeline.run(ts)
    assert not gate_rig.strong and ts.narrative
    assert not any(row["id"] == "consult" for row in ts.by_beat)
    print("PASS ts.consult=False skips a structurally present consult")
