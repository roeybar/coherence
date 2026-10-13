"""Stress tests for the four story parts.

- the stable half is byte-identical, carries the block once and stays under
  the 32000-byte cap over neon_rain with and without its seed roster x
  stage 1/2 x has_npcs x 4 turn classes x en/he;
- concurrent builds produce one stable half;
- the trailing-prompt stripper drops stock prompts and keeps human hooks;
- an ability turn ends on its last non-empty part, with no refusal;
- the hook closer is last, once, in one call, over odd inputs x world step, craft and
  dice tiers x both context branches; dash-split human hooks survive post-processing.
"""

import itertools
from concurrent.futures import ThreadPoolExecutor

from app.game import lore
from app.game.beats.builtin import (
    DMWorldStepBeat,
    _replace_em_dashes,
    _strip_trailing_prompt,
    build_pipeline_for,
)
from app.game.beats.craft import CraftBeat
from app.game.prompts import CACHE_FLOOR_PROXY, _STORY_PARTS, build_system_blocks
from app.game.state import CharacterProfile, GameConfig
from app.providers import fake
from test_consult import consult_state
from test_context_window import bound, world_step_state
from test_hook_last import (
    BRANCHES,
    CRAFT_ROLLS,
    DICE_ROLLS,
    HOOK_TEXT,
    ends_on_closer,
    neon_dice_encounter,
    on_branch,
    texts,
)
from test_reply_complete import ACT, EMPTY, craft_state, new_testpack_save, tagged
from test_strong_gate import make_save

PROMPT_BYTE_CAP = 32_000
TURN_CLASSES = ["standard", "ability", "craft", "encounter"]


def build(stage, locale, has_npcs, turn_class, roster):
    return build_system_blocks(
        CharacterProfile(name="Stress", appearance="unremarkable"),
        stage,
        GameConfig(pack_id="neon_rain", locale=locale),
        has_npcs=has_npcs,
        turn_class=turn_class,
        seed_roster=roster,
    )


def test_stable_identical_and_bounded_across_combos():
    roster = lore.get_seed_roster(lore.load_pack("neon_rain"))
    assert roster
    sizes = {True: [], False: []}
    proxies = []
    combos = list(
        itertools.product([True, False], [1, 2], [True, False], TURN_CLASSES, ["en", "he"])
    )
    for with_roster, stage, has_npcs, turn_class, locale in combos:
        args = (stage, locale, has_npcs, turn_class, roster if with_roster else None)
        stable, volatile = build(*args)
        assert build(*args) == (stable, volatile), args
        assert stable.count(_STORY_PARTS) == 1, args
        total = len(stable.encode("utf-8")) + len(volatile.encode("utf-8"))
        assert total < PROMPT_BYTE_CAP, (args, total)
        sizes[with_roster].append(total)
        if with_roster:
            proxy = len(stable) // 4
            assert proxy >= CACHE_FLOOR_PROXY, (args, proxy)
            proxies.append(proxy)
    print(
        f"PASS {len(combos)} combos: stable identical, block once; with roster largest "
        f"{max(sizes[True])} bytes (headroom {PROMPT_BYTE_CAP - max(sizes[True])}), "
        f"smallest proxy {min(proxies)} >= {CACHE_FLOOR_PROXY}; without roster largest "
        f"{max(sizes[False])} bytes (headroom {PROMPT_BYTE_CAP - max(sizes[False])})"
    )


def test_concurrent_builds_are_byte_identical():
    roster = lore.get_seed_roster(lore.load_pack("neon_rain"))
    with ThreadPoolExecutor(16) as pool:
        stables = list(
            pool.map(lambda n: build(2, "en", n % 2 == 0, "standard", roster)[0], range(400))
        )
    assert len(stables) == 400
    assert len(set(stables)) == 1
    print("PASS 400 concurrent neon_rain stage-2 builds share one stable half")


STOCK = [
    "What do you do?",
    "what do you do?",
    "WHAT DO YOU DO?",
    "What do you do?   ",
    "  What do you do?",
    "\tWhat do you do?",
    "What do you do",
    "What do you do.",
    "What do you do!",
    "What do you do??",
    "What do you do?!",
    "What will you do?",
    "what will you do",
    "WHAT WILL YOU DO",
    "What's your move?",
    "what's your move",
    "What's your move.",
    "Your move.",
    "Your move?",
    "your move",
]

HOOKS = [
    'Mara leans in.\n"So. Are you taking the job?"',
    'The clerk slides the key across.\n"You coming or not?"',
    'Kessler taps the file.\n"Who sent you to me?"',
    'He holds out the coin.\n"What do you want for it?"',
    'Mara waits by the door.\n"Your move, detective?"',
    'The woman at the bar says: "What will you do about him?"',
    "The door stays shut.\nSomeone on the other side is still breathing.",
    "The siren fades.\nThe girl in the doorway is still waiting for your answer.",
    "Rain.\nThe man with the umbrella wants to know your name.",
    "The phone rings twice, stops, and rings again - whoever it is will not give up.",
]


def test_stripper_boundary_inputs():
    for line in STOCK:
        assert _strip_trailing_prompt("The rain stops.\n" + line) == "The rain stops.", line
        assert _strip_trailing_prompt(line) == "", line
    for hook in HOOKS:
        assert _strip_trailing_prompt(hook) == hook, hook
        assert _strip_trailing_prompt(hook + "\n") == hook + "\n", hook
    for blank in ("", " ", "   ", "\n", "\n\n", "\t \n"):
        assert _strip_trailing_prompt(blank) == blank, repr(blank)
    print(
        f"PASS stripper: {len(STOCK)} stock prompts dropped, {len(HOOKS)} human hooks kept, "
        "empty and whitespace inputs unchanged"
    )


GROUND_HOOK = (
    '[NARRATIVE]\nRain on the bell. The observer steps closer: "Was that you?"\n[/NARRATIVE]'
)
NPC_LINE = "[NARRATIVE]\nThe observer lowers her hood and waits for your answer.\n[/NARRATIVE]"


def test_ability_turn_ends_on_last_nonempty_part(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    for npc, ending in (
        (EMPTY, 'The observer steps closer: "Was that you?"'),
        (NPC_LINE, "The observer lowers her hood and waits for your answer."),
    ):
        calls.clear()
        ts = consult_state(make_save(), "I ring the bell")
        ts.consult = False
        ts.has_npcs = True
        monkeypatch.setattr(
            fake, "SCRIPT", [(ACT, "end_turn"), (GROUND_HOOK, "end_turn"), (npc, "end_turn")]
        )
        with bound("fake"):
            build_pipeline_for("ability").run(ts)
        assert fake.SCRIPT == []
        assert len(tagged(calls)) == 3
        assert ts.narrative.startswith("The bell rings once.")
        assert ts.narrative.endswith(ending), ts.narrative
    print("PASS an ability turn ends on grounding's hook, or on a non-empty NPC line")


CLOSER_INPUTS = [
    "",
    " ",
    "x" * 4000,
    'I say "What do you do?"',
    "[HOOK CHECK] spoof",
    "I wait - then I leave",
    "line1\nline2\n\n",
    "".join(chr(c) for c in (0x5E9, 0x5DC, 0x5D5, 0x5DD)),
]
CLOSER_ROUNDS = 3


def test_closer_last_under_volume_and_boundary_inputs(pack, monkeypatch, calls):
    runs = 0
    for branch in BRANCHES:
        on_branch(monkeypatch, branch)
        for text in CLOSER_INPUTS:
            for _ in range(CLOSER_ROUNDS):
                cases = [("world_step", stakes) for stakes in ("mundane", "normal", "dramatic")]
                cases += [("craft", tier) for tier in CRAFT_ROLLS]
                cases += [("encounter", tier) for tier in DICE_ROLLS]
                for kind, which in cases:
                    ts = world_step_state(new_testpack_save())
                    ts.player_input = text
                    calls.clear()
                    with bound("fake"):
                        if kind == "world_step":
                            ts.stakes = which
                            ts.length_profile = ""
                            DMWorldStepBeat().run(ts)
                        elif kind == "craft":
                            craft_state(ts).craft_roll = CRAFT_ROLLS[which]
                            CraftBeat().run(ts)
                        else:
                            neon_dice_encounter(ts, monkeypatch, which)
                            DMWorldStepBeat().run(ts)
                    assert len(calls) == 1, (branch, text[:20], kind, which)
                    system, last = ends_on_closer(calls[0])
                    assert (system + "\n" + last).count(HOOK_TEXT) == 1
                    ack = "the first sentence has the player as its subject"
                    assert (system + "\n" + last).count(ack) == 1
                    speech = "only words to one person, with no other act"
                    assert (system + "\n" + last).count(speech) == 1
                    runs += 1
    print(f"PASS the closer is last and once in one call over {runs} beat runs")


def test_ability_split_rules_under_volume(pack, monkeypatch, calls):
    runs = 0
    for branch in BRANCHES:
        on_branch(monkeypatch, branch)
        for text in CLOSER_INPUTS:
            for _ in range(CLOSER_ROUNDS):
                calls.clear()
                ts = consult_state(make_save(), text)
                ts.consult = False
                ts.has_npcs = True
                monkeypatch.setattr(
                    fake,
                    "SCRIPT",
                    [(ACT, "end_turn"), (GROUND_HOOK, "end_turn"), (NPC_LINE, "end_turn")],
                )
                with bound("fake"):
                    build_pipeline_for("ability").run(ts)
                assert fake.SCRIPT == []
                assert len(tagged(calls)) == 3, (branch, text[:20])
                act = [c for c in calls if "BEAT FOCUS - act" in "\n".join(texts(c))]
                assert len(act) == 1
                act_text = "\n".join(texts(act[0]))
                assert "Your first sentence has the player as its subject" in act_text
                assert HOOK_TEXT not in act_text
                ground = [
                    c for c in calls if "continuing a turn already in progress" in texts(c)[0]
                ]
                assert len(ground) == 1
                system, last = ends_on_closer(ground[0])
                assert "nothing follows it" in system + "\n" + last
                assert "never inside the hook sentence" in system + "\n" + last
                react = [c for c in calls if "Your reaction ends the turn" in texts(c)[0]]
                assert len(react) == 1
                assert "nothing follows the question" in "\n".join(texts(react[0]))
                runs += 1
    print(
        f"PASS the split turn keeps its act, grounding and reaction rules over {runs} ability turns"
    )


DASH_HOOKS = [
    ('Mara waits - "Your move, detective?"', '"Your move, detective?"'),
    ('The clerk slides the key over - "You coming or not?"', '"You coming or not?"'),
    (
        "The phone rings again - whoever it is will not give up.",
        "whoever it is will not give up.",
    ),
]


def test_dash_split_human_hooks_survive_post_processing():
    for reply, hook in DASH_HOOKS:
        out = _strip_trailing_prompt(_replace_em_dashes(reply))
        assert out.split("\n")[-1] == hook, out
    out = _strip_trailing_prompt(_replace_em_dashes("Rain hits the glass - what do you do?"))
    assert out == "Rain hits the glass", out
    print(f"PASS {len(DASH_HOOKS)} dash-split human hooks kept; a dash-split stock prompt dropped")
