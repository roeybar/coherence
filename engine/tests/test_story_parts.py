"""Every game-master narration runs four story parts in order (acknowledge, react,
develop, human hook); SHORT is 2-4 short sentences; the openings end on a human hook."""

import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.game import engine, hook_repair, lore, quoted_speech, timeskip
from app.game.beats import base, builtin, craft
from app.game.beats.builtin import (
    DMActBeat,
    DMWorldStepBeat,
    GroundingBeat,
    NpcReactBeat,
    _replace_em_dashes,
    _strip_metagame_brackets,
    _strip_trailing_prompt,
)
from app.game.beats.craft import CraftBeat
from app.game.beats.encounter import EncounterBeat
from app.game.dice_gate import DiceAssessment
from app.game.prompts import HOOK_LAST, build_system_blocks
from app.game.state import CharacterProfile, GameConfig
from app.providers import fake
from test_context_window import bound, world_step_state
from test_reply_complete import craft_state

NEON_LORE_ROOT = lore.LORE_ROOT  # the pack fixture repoints it at the test packs
PROMPTS = Path(__file__).resolve().parents[1] / "lore" / "neon_rain" / "prompts"
PACK_JSON = Path(__file__).resolve().parents[1] / "lore" / "neon_rain" / "pack.json"
OPENINGS = {
    "opening.md": (
        "A voice through the glass, low and tired: \"I know it's late. "
        'They said you still take the ones nobody else will. Will you hear me out?"'
    ),
    "opening_freeform.md": (
        'A voice through the glass, quiet and quick: "Please - I need someone '
        'who still opens the door this late. Will you let me in?"'
    ),
}
PARTS = ("1. ACKNOWLEDGE", "2. REACT", "3. DEVELOP", "4. HOOK")
SHORT_DIRECTIVE = (
    "[LENGTH DIRECTIVE] 2-4 short sentences, ~30-60 tokens. Routine/low-stakes. "
    "Do NOT expand, recap unchanged state, or narrate travel the player "
    "didn't ask to see."
)


def in_order(text, *marks):
    for mark in marks:
        assert mark in text, f"missing {mark!r}"
    at = [text.index(mark) for mark in marks]
    assert at == sorted(at), f"out of order: {list(zip(marks, at))}"


def neon_stable(stage, locale, has_npcs=False, turn_class="standard"):
    pack = lore.load_pack("neon_rain")
    stable, _ = build_system_blocks(
        CharacterProfile(name="Test", appearance="test appearance"),
        stage,
        GameConfig(pack_id="neon_rain", locale=locale),
        has_npcs=has_npcs,
        turn_class=turn_class,
        seed_roster=lore.get_seed_roster(pack),
    )
    return stable


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize("locale", ["en", "he"])
def test_stable_prompt_carries_the_parts_in_order(stage, locale):
    from app.game.prompts import _STORY_PARTS

    stable = neon_stable(stage, locale)
    assert stable.count(_STORY_PARTS) == 1
    in_order(stable, *PARTS)
    print(
        f"PASS neon_rain stage {stage} {locale}: the stable half carries the parts once, in order"
    )


def test_hook_rules():
    from app.game.prompts import _STORY_PARTS

    for anchor in (
        "whole turn the player reads",
        "human hook",
        "The hook is the last sentence",
        "an NPC question to the player is a good one",
        "Never end on a list of options",
        'a stock "What do you do?"',
        "anywhere in this request",
        "the first sentence has the player as its subject",
        "before the hook, never inside the hook sentence",
        "The hook is the last sentence and nothing follows it",
        "the speech ends on the question or demand",
        "with the development and the hook each in a sentence of its own",
        "Pure speech on a short turn (length directive: short sentences)",
        "only words to one person, with no other act",
        "may be two parts and need not open on the player",
        "straight double quotes",
        "nothing after the closing one",
        "Any other move keeps all four",
    ):
        assert anchor in _STORY_PARTS, anchor
    assert "short sentences" in base.LengthProfile.SHORT.directive
    assert "short sentences" not in base.LengthProfile.MEDIUM.directive
    assert "short sentences" not in base.LengthProfile.LONG.directive
    assert "one sentence may carry two" not in _STORY_PARTS
    assert "in at least three sentences" not in _STORY_PARTS
    assert not re.search(r"\d\s*(-\s*\d+\s*)?sentences", _STORY_PARTS)
    in_order(_STORY_PARTS, *PARTS)
    print("PASS the story-parts block names the four parts in order and the hook rules")


def test_npc_last_line_rule():
    from app.game.prompts import _RESPONSE_LENGTH

    assert "the NPC's last line can be the hook, and nothing follows it" in _RESPONSE_LENGTH
    print("PASS the dialogue length rule puts the cue before the NPC line and nothing after it")


def neon_critical_encounter(ts, monkeypatch):
    """Run the real EncounterBeat on neon_rain with the roll forced to a critical,
    so ts.pack_blocks carries the pack's own CRITICAL directive."""
    with monkeypatch.context() as m:
        m.setattr(lore, "LORE_ROOT", NEON_LORE_ROOT)
        neon = lore.load_pack("neon_rain")
    testpack, ts.pack = ts.pack, neon
    ts.dice_roll = 20
    ts.dice_context = DiceAssessment(dc=10, skill="Intimidation", stat="CHA", flavor="A standoff.")
    EncounterBeat().run(ts)
    ts.pack = testpack
    assert ts.signals["dice_tier"] == "critical"
    assert any("DICE RESOLUTION - CRITICAL" in block for block in ts.pack_blocks)


KINDS = ["standard", "encounter", "act", "grounding", "npc_react", "craft", "timeskip"]


@pytest.mark.parametrize("branch", ["no_ceiling", "ceiling"])
@pytest.mark.parametrize("kind", KINDS)
def test_every_narrating_call_carries_the_parts(pack, monkeypatch, calls, kind, branch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(
        settings, "engine_fake_context_tokens", None if branch == "no_ceiling" else 3000
    )
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    ts = world_step_state(gs)
    with bound("fake"):
        if kind == "encounter":
            ts.turn_class = kind
            neon_critical_encounter(ts, monkeypatch)
            DMWorldStepBeat().run(ts)
        elif kind == "standard":
            ts.turn_class = kind
            DMWorldStepBeat().run(ts)
        elif kind == "act":
            ts.turn_class = "ability"
            DMActBeat().run(ts)
        elif kind in ("grounding", "npc_react"):
            ts.turn_class = "ability"
            ts.has_npcs = True
            ts.narrative_chunks = ["The bell rings once."]
            (GroundingBeat() if kind == "grounding" else NpcReactBeat()).run(ts)
        elif kind == "craft":
            CraftBeat().run(craft_state(ts))
        else:
            timeskip.call_period_advance({}, "year", 0, 1)
    call = calls[-1]
    system = fake._text(call["system"])
    last = fake._text(call["messages"][-1]["content"])
    text = system + "\n" + last
    if kind in ("standard", "encounter", "act", "craft"):
        in_order(text, *PARTS)
    if kind in ("standard", "encounter", "craft"):
        assert "the first sentence has the player as its subject" in text
    if kind in ("standard", "encounter", "act", "craft"):
        assert "only words to one person, with no other act" in text
        assert "Any other move keeps all four" in text
    if kind == "encounter":
        assert "DICE RESOLUTION - CRITICAL" in text
        assert "let the scene breathe" not in text
    elif kind == "craft":
        assert "in the four STORY PARTS" in text
    if kind == "act":
        assert "ONLY the first two" in text
        assert "Your first sentence has the player as its subject" in text
    elif kind == "grounding":
        in_order(text, "DEVELOP:", "HOOK:")
        assert "The hook is the last sentence" in text
        assert "never inside the hook sentence" in text
        assert "never inside the hook sentence" not in HOOK_LAST
        assert "nothing follows it" in text
    elif kind == "npc_react":
        assert "it is the turn's HOOK" in text
        assert "nothing follows the question" in text
        assert "straight double quotes" in text
    elif kind == "timeskip":
        in_order(text, "ACKNOWLEDGE", "REACT", "DEVELOP", "HOOK")
    if kind == "act":
        from app.game.prompts import _STORY_PARTS

        assert _STORY_PARTS in system
        if branch == "ceiling":
            assert "BEAT FOCUS - act" not in system
            assert "BEAT FOCUS - act" in last
        else:
            assert "BEAT FOCUS - act" in system
    print(f"PASS {kind} ({branch}): the call carries its story parts in order")


def test_old_conflicting_phrases_are_gone():
    for stage in (1, 2):
        for locale in ("en", "he"):
            stable = neon_stable(stage, locale)
            for phrase in ("then stop", "a beat or two", "= 1-3 sentences"):
                assert phrase not in stable, (stage, locale, phrase)
            assert "one sentence may carry two" not in stable, (stage, locale)
    assert "Stop after the act resolves" not in builtin._ACT_FOCUS
    grounding = inspect.getsource(GroundingBeat.run)
    assert 'no "What do you do?" trailing prompts' not in grounding
    assert "Pure description of place" not in grounding
    assert "Do not invite player action" not in timeskip._PERIOD_ADVANCE_SYSTEM
    assert "director's summary" not in timeskip._PERIOD_ADVANCE_SYSTEM
    for tier, guidance in craft._TIER_GUIDANCE.items():
        assert "STORY PARTS" in guidance and "hook" in guidance, tier
        assert "2-3 sentences" not in guidance, tier
    print("PASS the old stop-early, menu-free and director's-summary phrases are gone")


def test_short_profile_numbers():
    short = base.LengthProfile.SHORT
    assert (short.name, short.max_tokens, short.directive) == ("short", 60, SHORT_DIRECTIVE)
    medium, long_, expo = (
        base.LengthProfile.MEDIUM,
        base.LengthProfile.LONG,
        base.LengthProfile.EXPO,
    )
    assert (medium.name, medium.max_tokens, medium.directive) == (
        "medium",
        90,
        "[LENGTH DIRECTIVE] 2-4 sentences, ~40-90 tokens.",
    )
    assert (long_.name, long_.max_tokens, long_.directive) == (
        "long",
        250,
        "[LENGTH DIRECTIVE] One paragraph, ~100-250 tokens. Paint the moment - but once.",
    )
    assert (expo.name, expo.max_tokens, expo.directive) == (
        "expo-dump",
        500,
        "[LENGTH DIRECTIVE] Full exposition/lore. 500 tokens is a HARD ceiling, "
        "not a target - stop when it's said.",
    )
    assert base.reply_max_tokens(SimpleNamespace(), 200, "short") == 460
    source = inspect.getsource(NpcReactBeat.run)
    assert "_NPC_REACT_LENGTH" in source
    assert "PROFILES['short'].directive" not in source
    print("PASS SHORT is 60 tokens (API 460) with its 2-4 sentence directive; others unchanged")


def test_openings_end_on_a_human_hook():
    for name, hook in OPENINGS.items():
        text = (PROMPTS / name).read_text(encoding="ascii")
        assert "What do you do?" not in text, name
        assert text.rstrip("\n").split("\n")[-1] == hook, name
        assert _strip_trailing_prompt(text) == text, name
    print("PASS both openings end on a spoken human hook the stripper leaves alone")


def test_openings_end_on_a_spoken_hook():
    for name in OPENINGS:
        text = (PROMPTS / name).read_text(encoding="ascii")
        shape = hook_repair.check_hook(text)
        assert shape in hook_repair.HOOK_SHAPES, (name, shape)
        assert shape == "question", (name, shape)
    print("PASS both openings end on a spoken question the hook check accepts")


def test_pure_speech_reply_keeps_balanced_quotes():
    reply = 'Mara\'s jaw tightens. "Nobody sent me - I came on my own. Will you take it or not?"'
    out = _strip_metagame_brackets(_strip_trailing_prompt(_replace_em_dashes(reply)))
    assert out.count('"') % 2 == 0, out
    assert out.endswith('?"'), out
    assert out.endswith(re.findall(r'"[^"]*"', out)[-1]), out
    fixed, repaired = quoted_speech.ensure_verbatim(
        'I say "Who sent you?"', 'Mara stiffens. "Who wants to know?"'
    )
    assert repaired == 1, fixed
    assert fixed.count('"') % 2 == 0, fixed
    assert fixed.endswith('"Who wants to know?"'), fixed
    print("PASS a pure-speech reply keeps its quotes balanced through the post-processing")


def test_stripper_keeps_a_human_hook():
    hook = 'Mara leans in.\n"So. Are you taking the job?"'
    assert _strip_trailing_prompt(hook) == hook
    assert _strip_trailing_prompt("Rain on the glass.\nWhat do you do?") == "Rain on the glass."
    print("PASS the stripper keeps an NPC question and still drops a stock prompt")


def test_a_reply_without_the_parts_still_plays(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    ts = world_step_state(gs)
    with bound("fake"):
        DMWorldStepBeat().run(ts)
    assert len(calls) == 1
    assert ts.narrative == fake.DEFAULT_REPLY
    print("PASS a reply without the four parts plays as written in one call")


def test_story_prompts_are_ascii():
    from app.game.prompts import _RESPONSE_LENGTH, _STORY_PARTS

    texts = [
        _STORY_PARTS,
        _RESPONSE_LENGTH,
        base.LengthProfile.SHORT.directive,
        builtin._NPC_REACT_LENGTH,
        builtin._ACT_FOCUS,
        inspect.getsource(GroundingBeat.run),
        inspect.getsource(NpcReactBeat.run),
        timeskip._PERIOD_ADVANCE_SYSTEM,
        *craft._TIER_GUIDANCE.values(),
    ]
    texts += [(PROMPTS / name).read_bytes().decode("latin-1") for name in OPENINGS]
    for text in texts:
        assert text.isascii(), text[:60]
    print(f"PASS {len(texts)} new and amended story strings are ASCII")


def test_period_prompt_hooks_only_the_final_period():
    system = timeskip._PERIOD_ADVANCE_SYSTEM
    for phrase in (
        "If N is less than M",
        "No hook, and do not address the player",
        "If N equals M (the final period)",
        "Do not put the hook in the EVENTS: line",
    ):
        assert phrase in system, phrase
    assert "Do not invite player action" not in system
    assert "director's summary" not in system
    assert "(period 1 of 3)" in timeskip.build_period_prompt({}, "year", 0, 3)
    assert "(period 3 of 3)" in timeskip.build_period_prompt({}, "year", 2, 3)
    print("PASS the period prompt hooks only the final period, keyed on '(period N of M)'")


def test_final_period_hook_stays_out_of_the_classifier(monkeypatch):
    reply = "The harvest fails.\nMara waits at your door with a sealed letter.\nEVENTS: harvest fails, election"
    seen = []

    def classify(summary, conditions):
        seen.append(summary)
        return {"interrupt": False, "reason": ""}, None

    monkeypatch.setattr(timeskip, "call_period_advance", lambda *args: (reply, {}))
    monkeypatch.setattr(timeskip, "classify_interrupt", classify)
    skip = {"span_n": 2, "span_unit": "year"}
    r = timeskip.run_timeskip({}, skip=skip)
    assert seen == ["harvest fails, election"] * 2
    out = timeskip.compose_timeskip_response(skip, r)
    assert out.endswith("Mara waits at your door with a sealed letter.")
    assert "attention needed" not in out
    print("PASS the classifier reads the EVENTS line, so the final hook ends the skip")


DASH_REPLY = "Rain hits the glass - what do you do?"


@pytest.mark.parametrize("kind", ["world_step", "act", "grounding", "npc_react", "craft"])
def test_dash_split_stock_prompt_is_stripped(pack, monkeypatch, calls, kind):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(fake, "DEFAULT_REPLY", DASH_REPLY)
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    ts = world_step_state(gs)
    with bound("fake"):
        if kind == "world_step":
            DMWorldStepBeat().run(ts)
        elif kind == "act":
            ts.turn_class = "ability"
            DMActBeat().run(ts)
        elif kind in ("grounding", "npc_react"):
            ts.turn_class = "ability"
            ts.has_npcs = True
            ts.narrative_chunks = ["The bell rings once."]
            (GroundingBeat() if kind == "grounding" else NpcReactBeat()).run(ts)
        else:
            CraftBeat().run(craft_state(ts))
    assert len(calls) == 1
    final = ts.narrative_chunks[-1]
    assert "Rain hits the glass" in final, final
    assert "what do you do" not in final.lower(), final
    print(f"PASS {kind}: a dash-split stock prompt does not survive as the last line")


def test_critical_encounter_directive_keeps_the_hook():
    spec = json.loads(PACK_JSON.read_text(encoding="ascii"))["encounter_mechanics"]
    directive = spec["effects"]["critical"]["directive"]
    assert directive.startswith("DICE RESOLUTION - CRITICAL")
    assert "breathe" not in directive
    assert "then stop" not in directive
    assert "the hook" in directive
    print("PASS the CRITICAL encounter directive ends on the hook, not on a quiet beat")


def test_deferral_clause_names_the_parts():
    from app.game.prompts import _STORY_PARTS

    assert "names the STORY PARTS this call writes" in _STORY_PARTS
    assert "or other engine instruction" not in _STORY_PARTS
    print("PASS only a BEAT FOCUS that names the story parts can narrow the call")
