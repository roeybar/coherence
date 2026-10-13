"""DMWorldStep, Craft and Grounding end their user message on the hook check, once, last.
NpcReact writes the turn's hook from its own system prompt and carries no hook check;
DMAct writes no hook. The pack's dice directives all name the hook. On OpenAI the hook
check comes last, then the format reminder."""

import json
from pathlib import Path

import pytest

from app.config import settings
from app.game import lore, prompts
from app.game.beats.base import PROFILES
from app.game.beats.builtin import DMWorldStepBeat, build_pipeline_for
from app.game.beats.craft import CraftBeat
from app.game.beats.encounter import EncounterBeat
from app.game.dice_gate import DiceAssessment
from app.game.llm import MODEL_SONNET
from app.providers import fake
from app.providers.openai_compat import FORMAT_REMINDER
from test_consult import consult_state
from test_context_window import bound, world_step_state
from test_reply_complete import ACT, EMPTY, FULL, craft_state, new_testpack_save, openai_beat
from test_strong_gate import make_save

NEON_LORE_ROOT = lore.LORE_ROOT  # the pack fixture repoints it at the test packs
PACK_JSON = Path(__file__).resolve().parents[1] / "lore" / "neon_rain" / "pack.json"
# A literal copy, so a change to the closer is a deliberate change here too.
HOOK_TEXT = (
    "[HOOK CHECK] Your last sentence is the HOOK: someone in this scene wants something "
    "from the player, a situation will not wait, or a question is put to the player. "
    "Weather, light, a sound or a mood on its own, or a person who is only present, "
    "is not a hook. Nothing follows the hook. If someone speaks last, their closing quote "
    "ends the reply, with no gesture or description after it, and that line is a question "
    "or a demand to the player; a statement or a refusal is not a hook. "
    "The development comes in its own sentence before the hook. "
    "Before you answer, check the last sentence; if it is not a hook, end on one. "
    'Never a list of options or a stock "What do you do?".'
)
GROUND_HOOK = (
    '[NARRATIVE]\nRain on the bell. The observer steps closer: "Was that you?"\n[/NARRATIVE]'
)
DICE_ROLLS = {"critical": 20, "success": 15, "partial": 11, "fail": 6, "fumble": 1}
CRAFT_ROLLS = {"critical": 17, "success": 12, "partial": 8, "fail": 7}
STAKES_PROFILE = {"mundane": "short", "normal": "medium", "dramatic": "long"}
BRANCHES = ["no_ceiling", "ceiling"]


def on_branch(monkeypatch, branch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(
        settings, "engine_fake_context_tokens", None if branch == "no_ceiling" else 3000
    )


def texts(call):
    return fake._text(call["system"]), fake._text(call["messages"][-1]["content"])


def ends_on_closer(call):
    """The call's last user text ends on the hook check, which appears once overall."""
    system, last = texts(call)
    assert last.rstrip().endswith(HOOK_TEXT), last[-300:]
    assert (system + "\n" + last).count(HOOK_TEXT) == 1
    return system, last


def neon_dice_encounter(ts, monkeypatch, tier):
    """Run the real EncounterBeat on neon_rain with the roll forced to a tier, so
    ts.pack_blocks carries the pack's own directive for it."""
    with monkeypatch.context() as m:
        m.setattr(lore, "LORE_ROOT", NEON_LORE_ROOT)
        neon = lore.load_pack("neon_rain")
    testpack, ts.pack = ts.pack, neon
    ts.dice_roll = DICE_ROLLS[tier]
    ts.dice_context = DiceAssessment(dc=15, skill="Intimidation", stat="CHA", flavor="A standoff.")
    EncounterBeat().run(ts)
    ts.pack = testpack
    assert ts.signals["dice_tier"] == tier
    assert any(f"DICE RESOLUTION - {tier.upper()}" in block for block in ts.pack_blocks)


def test_closer_constant():
    assert prompts.HOOK_LAST == HOOK_TEXT
    assert HOOK_TEXT.isascii()
    for anchor in (
        "[HOOK CHECK]",
        "only present",
        "Before you answer",
        'a stock "What do you do?"',
        "Nothing follows the hook",
        "its own sentence before the hook",
        "their closing quote ends the reply",
        "a statement or a refusal is not a hook",
    ):
        assert anchor in HOOK_TEXT, anchor
    assert "You ..." not in HOOK_TEXT
    print("PASS the hook closer is the pinned ASCII text and names the not-a-hook cases")


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("stakes", ["mundane", "normal", "dramatic"])
def test_world_step_ends_on_the_closer(pack, monkeypatch, calls, stakes, branch):
    on_branch(monkeypatch, branch)
    ts = world_step_state(new_testpack_save())
    ts.stakes = stakes
    ts.length_profile = ""
    with bound("fake"):
        DMWorldStepBeat().run(ts)
    assert len(calls) == 1
    _, last = ends_on_closer(calls[0])
    assert last.index(PROFILES[STAKES_PROFILE[stakes]].directive) < last.index(HOOK_TEXT)
    print(f"PASS world step ({stakes}, {branch}): one call, the length directive then the closer")


@pytest.mark.parametrize("branch", BRANCHES)
def test_dialogue_ends_on_the_closer(pack, monkeypatch, calls, branch):
    on_branch(monkeypatch, branch)
    ts = world_step_state(new_testpack_save())
    ts.player_input = 'I say "Who sent you?"'
    ts.addressee_block = "[ADDRESSEE] The player speaks to Mara."
    with bound("fake"):
        DMWorldStepBeat().run(ts)
    assert len(calls) == 1
    system, last = ends_on_closer(calls[0])
    assert "only words to one person, with no other act" in system
    assert 'I say "Who sent you?"' in last
    print(f"PASS dialogue ({branch}): the quoted line is kept and the closer is still last")


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("tier", list(DICE_ROLLS))
def test_every_dice_tier_ends_on_the_closer(pack, monkeypatch, calls, tier, branch):
    on_branch(monkeypatch, branch)
    ts = world_step_state(new_testpack_save())
    with bound("fake"):
        neon_dice_encounter(ts, monkeypatch, tier)
        DMWorldStepBeat().run(ts)
    assert len(calls) == 1
    system, last = ends_on_closer(calls[0])
    marker = f"DICE RESOLUTION - {tier.upper()}"
    assert marker in system + "\n" + last
    if branch == "ceiling":
        assert last.index(marker) < last.index(HOOK_TEXT)
    print(f"PASS dice {tier} ({branch}): the directive is in the call and the closer is last")


def test_every_dice_directive_names_the_hook():
    effects = json.loads(PACK_JSON.read_text(encoding="ascii"))["encounter_mechanics"]["effects"]
    assert list(effects) == list(DICE_ROLLS)
    for tier, spec in effects.items():
        assert "the hook" in spec["directive"], tier
    assert "No recovery line" in effects["fumble"]["directive"]
    print("PASS all five dice directives name the hook; the fumble still has no recovery line")


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("tier", list(CRAFT_ROLLS))
def test_every_craft_tier_ends_on_the_closer(pack, monkeypatch, calls, tier, branch):
    on_branch(monkeypatch, branch)
    ts = craft_state(world_step_state(new_testpack_save()))
    ts.craft_roll = CRAFT_ROLLS[tier]
    with bound("fake"):
        CraftBeat().run(ts)
    assert len(calls) == 1
    system, last = ends_on_closer(calls[0])
    assert "in the four STORY PARTS" in system + "\n" + last
    print(f"PASS craft {tier} ({branch}): one call, the guidance is there and the closer is last")


@pytest.mark.parametrize("variant", ["fast", "strong_act", "consult"])
def test_ability_turn_hook_comes_from_grounding(pack, monkeypatch, calls, variant):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = variant == "consult"
    ts.has_npcs = True
    if variant == "strong_act":
        ts.turn_model = MODEL_SONNET
    monkeypatch.setattr(
        fake, "SCRIPT", [(ACT, "end_turn"), (GROUND_HOOK, "end_turn"), (EMPTY, "end_turn")]
    )
    with bound("fake"):
        build_pipeline_for("ability", consult=ts.consult).run(ts)
    assert fake.SCRIPT == []
    grounding = [c for c in calls if "continuing a turn already in progress" in texts(c)[0]]
    assert len(grounding) == 1
    ends_on_closer(grounding[0])
    act = [c for c in calls if "BEAT FOCUS - act" in "\n".join(texts(c))]
    assert len(act) == 1
    assert "\n".join(texts(act[0])).count(HOOK_TEXT) == 0
    react = [c for c in calls if "Your reaction ends the turn" in texts(c)[0]]
    assert len(react) == 1
    assert "\n".join(texts(react[0])).count(HOOK_TEXT) == 0
    print(f"PASS ability turn ({variant}): grounding closes on the hook check; act and NPC do not")


def test_openai_body_closes_with_the_hook_then_the_format_reminder(pack, monkeypatch):
    bodies = openai_beat(monkeypatch, [(FULL, "stop")])
    ts = world_step_state(new_testpack_save())
    with bound("openai"):
        DMWorldStepBeat().run(ts)
    assert len(bodies) == 1
    assert bodies[0]["messages"][-1]["content"].endswith(HOOK_TEXT + "\n\n" + FORMAT_REMINDER)
    print("PASS the OpenAI body ends with the hook check, then the format reminder")
