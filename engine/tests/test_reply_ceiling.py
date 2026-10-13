import json

import httpx
import pytest

from app.game import engine
from app.game.beats.base import PROFILES, TAG_ALLOWANCE
from app.game.beats.builtin import DMActBeat, DMWorldStepBeat, GroundingBeat, NpcReactBeat
from app.game.beats.craft import CraftBeat
from app.game.craft_gate import CraftAssessment
from app.game.state import CharacterProfile, GameConfig
from app.providers import registry
from test_context_window import bound, world_step_state


TAGGED = "[NARRATIVE]Rain.[/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]"
EXPECTED = {"short": 460, "medium": 490, "long": 650, "expo-dump": 900}


@pytest.fixture(params=["anthropic", "openai"])
def rig(pack, monkeypatch, request):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    name = request.param
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if name == "anthropic":
            payload = {
                "content": [{"type": "text", "text": TAGGED}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            }
        else:
            payload = {
                "choices": [{"message": {"content": TAGGED}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(registry.get_provider(name), "transport", httpx.MockTransport(respond))
    return name, bodies


def test_profile_ceilings(rig):
    provider, bodies = rig
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    assert TAG_ALLOWANCE == 400
    for name in PROFILES:
        ts = world_step_state(gs)
        ts.length_profile = name
        before = len(bodies)
        with bound(provider):
            DMWorldStepBeat().run(ts)
        assert len(bodies) == before + 1
        assert bodies[-1]["max_tokens"] == EXPECTED[name] == PROFILES[name].max_tokens + 400
    for stakes, ceiling in [("mundane", 460), ("normal", 490), ("dramatic", 650)]:
        ts = world_step_state(gs)
        ts.length_profile = ""
        ts.stakes = stakes
        before = len(bodies)
        with bound(provider):
            DMWorldStepBeat().run(ts)
        assert len(bodies) == before + 1
        assert bodies[-1]["max_tokens"] == ceiling
    print(f"PASS {provider} recorded profile ceilings: 460, 490, 650, 900; stakes: 460, 490, 650")


def test_ability_beat_ceilings(rig):
    provider, bodies = rig
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    for beat, ceiling in [(DMActBeat(), 460), (GroundingBeat(), 490), (NpcReactBeat(), 460)]:
        ts = world_step_state(gs)
        ts.turn_class = "ability"
        before = len(bodies)
        with bound(provider):
            beat.run(ts)
        assert len(bodies) == before + 1
        assert bodies[-1]["max_tokens"] == ceiling
    print(f"PASS {provider} recorded ability ceilings: act 460, grounding 490, NPC reaction 460")


def test_unprofiled_ceilings_unchanged(rig):
    provider, bodies = rig
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    for beat in (DMWorldStepBeat(), DMActBeat(), GroundingBeat(), NpcReactBeat()):
        ts = world_step_state(gs)
        ts.turn_class = "ability" if not isinstance(beat, DMWorldStepBeat) else "standard"
        ts.max_tokens_override = 2500
        before = len(bodies)
        with bound(provider):
            beat.run(ts)
        assert len(bodies) == before + 1
        assert bodies[-1]["max_tokens"] == 2500

    craft_ts = world_step_state(gs)
    craft_ts.turn_class = "craft"
    craft_ts.items_block = "[ITEMS]iron[/ITEMS]"
    craft_ts.craft_roll = 12
    craft_ts.craft_context = CraftAssessment(
        dc=12,
        target="bell",
        inputs_committed=["iron"],
        flavor="The work needs care.",
    )
    before = len(bodies)
    with bound(provider):
        CraftBeat().run(craft_ts)
    assert len(bodies) == before + 1
    assert bodies[-1]["max_tokens"] == 1024
    print(
        f"PASS {provider} recorded unprofiled ceilings unchanged: override 2500 for four beats; craft 1024"
    )
