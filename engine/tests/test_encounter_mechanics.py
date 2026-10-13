"""Tests for encounter_mechanics.

stdlib unittest, zero LLM calls, PASS/FAIL prints.
Pattern: tests/test_pack_maker.py

Suites:
  1. TestDefaultTierMatchesLegacy - full dc 1..25 x roll 1..30 grid
  2. TestDefaultEffectsMatchLegacy - delta + clamp edges
  4. TestEncounterBeatDefault - bit-identical signals + pack_blocks
  5. TestPackDeclaredBandsEffects - pack-declared tiers/effects/directive
  6. TestValidation - ~20 malformed specs each raise PackBeatsError
  7. TestEncounterFuzz - 20 boundary specs x 7 combos, never a 500
  8. TestFailOpen - monkeypatched render raises, turn still completes
  9. TestDryRun - real pack load + end-to-end, LLM never touched
 10. TestDirectiveTruncation - runtime truncate + warn
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# App imports
# ---------------------------------------------------------------------------

from app.game.encounter_mechanics import (
    DEFAULT_CLAMPS,
    DEFAULT_EFFECTS,
    DEFAULT_TIERS,
    apply_effects,
    mechanics_for,
    render_directive,
    resolve_tier,
)
from app.game.pack_beats import (
    MAX_TEMPLATE_BYTES,
    MAX_TIERS,
    PackBeatsError,
    validate_encounter_mechanics,
)
from app.game.beats.encounter import (
    EncounterBeat,
    _AGGRESSION_DELTA,
    _AGGRESSION_MIN,
    _AGGRESSION_MAX,
)
from app.game.state import WorldState


# ---------------------------------------------------------------------------
# Fake TurnState helpers (no LLM, no full dataclass deps)
# ---------------------------------------------------------------------------


class FakeDiceCtx:
    def __init__(self, dc, skill, stat):
        self.dc = dc
        self.skill = skill
        self.stat = stat


class FakeGS:
    def __init__(self, state):
        self.state = state


class FakeTurnState:
    def __init__(self, gs, pack=None):
        self.gs = gs
        self.pack = pack if pack is not None else {}
        self.dice_roll = None
        self.dice_context = None
        self.pack_blocks = []
        self.signals = {}
        self.by_beat = []
        self.turn_tokens_in = 0
        self.turn_tokens_out = 0

    def record_beat(self, *, beat_id, tokens_in=0, tokens_out=0, model=""):
        self.by_beat.append({"id": beat_id})


def _reference_tier(roll: int, dc: int) -> str:
    """The shipped d20 tier thresholds, written out independently of
    DEFAULT_TIERS so the grid test compares two separate encodings."""
    if roll >= dc + 5:
        return "critical"
    if roll >= dc:
        return "success"
    if roll >= dc - 4:
        return "partial"
    if roll >= dc - 9:
        return "fail"
    return "fumble"


# ---------------------------------------------------------------------------
# Suite 1 - tier math invariance (full dc 1..25 x roll 1..30)
# ---------------------------------------------------------------------------


class TestDefaultTierMatchesLegacy(unittest.TestCase):
    """resolve_tier(roll-dc, DEFAULT_TIERS) == _reference_tier(roll, dc)
    for every cell in the 25x30 grid."""

    def test_full_grid(self):
        mismatches = []
        for dc in range(1, 26):
            for roll in range(1, 31):
                margin = roll - dc
                got = resolve_tier(margin, DEFAULT_TIERS)
                want = _reference_tier(roll, dc)
                if got != want:
                    mismatches.append(
                        f"dc={dc} roll={roll} margin={margin}: got={got!r} want={want!r}"
                    )
        total = 25 * 30
        ok = total - len(mismatches)
        print(f"\n{'OK' if not mismatches else 'FAIL'}- {ok}/{total} cells matched")
        for m in mismatches:
            print(f"  MISMATCH {m}")
        self.assertEqual(
            mismatches,
            [],
            f"{len(mismatches)} mismatches:\n" + "\n".join(mismatches),
        )


# ---------------------------------------------------------------------------
# Suite 2 - default effects match legacy _AGGRESSION_DELTA logic
# ---------------------------------------------------------------------------


class TestDefaultEffectsMatchLegacy(unittest.TestCase):
    """apply_effects(DEFAULT_EFFECTS, DEFAULT_CLAMPS) produces the same
    before/after values as the legacy _AGGRESSION_DELTA + clamp logic."""

    def _legacy(self, current_agg: int, tier: str) -> int:
        delta = _AGGRESSION_DELTA.get(tier, 0)
        return max(_AGGRESSION_MIN, min(_AGGRESSION_MAX, current_agg + delta))

    def _apply(self, current_agg: int, tier: str) -> int:
        state = WorldState(scene_aggression=current_agg)
        applied = apply_effects(state, tier, DEFAULT_EFFECTS, DEFAULT_CLAMPS)
        return applied["scene_aggression"]["after"]

    def test_all_tiers_mid_value(self):
        for tier in ("critical", "success", "partial", "fail", "fumble"):
            with self.subTest(tier=tier):
                got = self._apply(50, tier)
                want = self._legacy(50, tier)
                print(
                    f"PASS tier={tier} agg=50: {got}"
                    if got == want
                    else f"FAIL tier={tier} agg=50: got={got} want={want}"
                )
                self.assertEqual(got, want)

    def test_clamp_lower_bound(self):
        # critical at 5 -> 5-15=-10 -> clamped to 0
        got = self._apply(5, "critical")
        want = self._legacy(5, "critical")
        self.assertEqual(got, 0)
        self.assertEqual(got, want)
        print(f"PASS clamp lower: got={got}")

    def test_clamp_upper_bound(self):
        # fumble at 80 -> 80+25=105 -> clamped to 100
        got = self._apply(80, "fumble")
        want = self._legacy(80, "fumble")
        self.assertEqual(got, 100)
        self.assertEqual(got, want)
        print(f"PASS clamp upper: got={got}")

    def test_clamp_already_at_zero(self):
        got = self._apply(0, "critical")
        want = self._legacy(0, "critical")
        self.assertEqual(got, 0)
        self.assertEqual(got, want)
        print(f"PASS already at 0: got={got}")

    def test_clamp_already_at_hundred(self):
        got = self._apply(100, "fumble")
        want = self._legacy(100, "fumble")
        self.assertEqual(got, 100)
        self.assertEqual(got, want)
        print(f"PASS already at 100: got={got}")

    def test_missing_field_treated_as_zero(self):
        # scene_aggression absent in state -> treated as 0
        state = WorldState()
        applied = apply_effects(state, "fail", DEFAULT_EFFECTS, DEFAULT_CLAMPS)
        want = self._legacy(0, "fail")
        got = applied["scene_aggression"]["after"]
        self.assertEqual(got, want)
        print(f"PASS missing field: got={got}")

    def test_before_field_preserved(self):
        state = WorldState(scene_aggression=60)
        applied = apply_effects(state, "success", DEFAULT_EFFECTS, DEFAULT_CLAMPS)
        self.assertEqual(applied["scene_aggression"]["before"], 60)
        print(f"PASS before preserved: {applied['scene_aggression']}")


# ---------------------------------------------------------------------------
# Suite 4 - bit-identical signals + pack_blocks on default path
# ---------------------------------------------------------------------------


class TestEncounterBeatDefault(unittest.TestCase):
    """default (spec-less) encounter turn appends exactly one block to
    ts.pack_blocks, byte-for-byte equal to the shipped DEFAULT_HINT render
    (post- elaborated roll-binding directive, kept by design).
    Non-encounter turns leave pack_blocks untouched."""

    def _legacy_hint(self, roll, dc, skill, stat, tier, new_agg):
        stat_part = f" ({stat})" if stat and stat.lower() not in ("none", "") else ""
        return (
            f"[DICE RESOLUTION] Player rolled {roll} vs DC {dc} "
            f"for {skill}{stat_part}. "
            f"Outcome: {tier.upper()}. "
            f"Scene aggression: {new_agg}/100. "
            f"This verdict is BINDING on the fiction and the OUTCOME MUST MATCH THE TIER. "
            f"critical: the attempt fully succeeds - the player gets what the action was "
            f"reaching for, at its best. success: it works. partial: they get it only at "
            f"a real cost or incompletely. fail/fumble: the attempt FAILS - the player "
            f"does NOT obtain what the action was reaching for (the concession, the "
            f"information, the escape, the advantage), and an NPC does NOT concede or "
            f"reveal on a fail; a complication lands instead. Never narrate a fail as "
            f"though it succeeded, and never grant on a fail the same reward a success "
            f"would have given. Do NOT mention the DC or dice mechanics - let the fiction "
            f"carry the result."
        )

    def _run(self, roll, dc, skill, stat, scene_agg, pack=None):
        state = WorldState(scene_aggression=scene_agg)
        gs = FakeGS(state)
        ts = FakeTurnState(gs=gs, pack=pack or {})
        ts.dice_roll = roll
        ts.dice_context = FakeDiceCtx(dc=dc, skill=skill, stat=stat)
        EncounterBeat().run(ts)
        return ts

    def test_default_table(self):
        """Pins the shipped DEFAULT_HINT: signals + pack_blocks byte-for-byte."""
        table = [
            (20, 10, "attack", "STR", 50),
            (5, 15, "stealth", "DEX", 30),
            (1, 25, "persuade", "CHA", 80),
            (25, 10, "jump", "none", 0),
            (10, 10, "lift", "STR", 100),
            (15, 15, "dodge", "", 50),
        ]
        for roll, dc, skill, stat, scene_agg in table:
            with self.subTest(roll=roll, dc=dc):
                ts = self._run(roll, dc, skill, stat, scene_agg)
                tier = _reference_tier(roll, dc)
                legacy_new_agg = max(
                    _AGGRESSION_MIN,
                    min(_AGGRESSION_MAX, scene_agg + _AGGRESSION_DELTA.get(tier, 0)),
                )
                expected_hint = self._legacy_hint(roll, dc, skill, stat, tier, legacy_new_agg)

                # signals match legacy
                self.assertEqual(ts.signals["dice_tier"], tier)
                self.assertEqual(ts.signals["scene_aggression"], legacy_new_agg)

                # pack_blocks: exactly one, byte-for-byte identical
                self.assertEqual(
                    len(ts.pack_blocks), 1, f"expected 1 block, got {len(ts.pack_blocks)}"
                )
                self.assertEqual(
                    ts.pack_blocks[0],
                    expected_hint,
                    f"block mismatch for roll={roll} dc={dc}",
                )
                print(f"PASS roll={roll} dc={dc} tier={tier}: block matches")

    def test_non_encounter_turn_pack_blocks_untouched(self):
        """applies returns False; pack_blocks stays unchanged."""
        beat = EncounterBeat()
        state = WorldState(scene_aggression=50)
        gs = FakeGS(state)
        ts = FakeTurnState(gs=gs)
        ts.dice_roll = None
        ts.dice_context = None
        ts.pack_blocks = ["existing_block"]

        # applies must be False
        self.assertFalse(beat.applies(ts))
        # pack_blocks unchanged (run never called)
        self.assertEqual(ts.pack_blocks, ["existing_block"])
        print("PASS non-encounter: pack_blocks untouched")


# ---------------------------------------------------------------------------
# Suite 5 - pack-declared tiers, effects, and directives
# ---------------------------------------------------------------------------


class TestPackDeclaredBandsEffects(unittest.TestCase):
    def test_custom_two_tier_win_loss(self):
        spec = {
            "outcome_tiers": [
                {"tier": "win", "min_margin": 0},
                {"tier": "loss", "min_margin": None},
            ],
            "effects": {
                "win": {"state_delta": {"scene_aggression": -20}},
                "loss": {"state_delta": {"scene_aggression": 20}},
            },
            "clamps": {"scene_aggression": [0, 100]},
        }
        tiers = [(t["tier"], t["min_margin"]) for t in spec["outcome_tiers"]]

        # roll=15, dc=10 -> margin=5 -> win
        self.assertEqual(resolve_tier(5, tiers), "win")
        # roll=5, dc=10 -> margin=-5 -> loss
        self.assertEqual(resolve_tier(-5, tiers), "loss")

        # apply win effect: 50-20=30
        state = WorldState(scene_aggression=50)
        effects = {n: v.get("state_delta", {}) for n, v in spec["effects"].items()}
        clamps = {f: tuple(v) for f, v in spec["clamps"].items()}
        applied = apply_effects(state, "win", effects, clamps)
        self.assertEqual(applied["scene_aggression"]["before"], 50)
        self.assertEqual(applied["scene_aggression"]["after"], 30)
        print("PASS custom win/loss tiers + effects")

    def test_per_tier_directive_returned(self):
        spec = {
            "outcome_tiers": [
                {"tier": "hit", "min_margin": 0},
                {"tier": "miss", "min_margin": None},
            ],
            "effects": {
                "hit": {"state_delta": {}, "directive": "You strike true!"},
            },
        }
        directive = render_directive(
            spec,
            roll=15,
            dc=10,
            skill="slash",
            stat="STR",
            tier="hit",
            new_state={"scene_aggression": {"before": 50, "after": 50}},
        )
        self.assertEqual(directive, "You strike true!")
        print(f"PASS per-tier directive: {directive!r}")

    def test_no_directive_falls_back_to_default_hint(self):
        spec = {
            "outcome_tiers": [
                {"tier": "hit", "min_margin": 0},
                {"tier": "miss", "min_margin": None},
            ],
            "effects": {"hit": {"state_delta": {}}},  # no directive
        }
        directive = render_directive(
            spec,
            roll=15,
            dc=10,
            skill="slash",
            stat="STR",
            tier="hit",
            new_state={"scene_aggression": {"before": 50, "after": 50}},
        )
        # Falls through to DEFAULT_HINT
        self.assertIn("[DICE RESOLUTION]", directive)
        print("PASS no directive -> DEFAULT_HINT")

    def test_encounter_beat_with_pack_mechanics(self):
        spec = {
            "outcome_tiers": [
                {"tier": "win", "min_margin": 0},
                {"tier": "loss", "min_margin": None},
            ],
            "effects": {
                "win": {"state_delta": {"scene_aggression": -10}, "directive": "Victory!"},
                "loss": {"state_delta": {"scene_aggression": 10}},
            },
            "clamps": {"scene_aggression": [0, 100]},
        }
        pack = {"pack_beats": {"encounter_mechanics": spec}}
        state = WorldState(scene_aggression=50)
        gs = FakeGS(state)
        ts = FakeTurnState(gs=gs, pack=pack)
        ts.dice_roll = 15
        ts.dice_context = FakeDiceCtx(dc=10, skill="strike", stat="STR")
        EncounterBeat().run(ts)
        self.assertEqual(ts.signals["dice_tier"], "win")
        self.assertEqual(ts.signals["scene_aggression"], 40)
        self.assertEqual(len(ts.pack_blocks), 1)
        self.assertEqual(ts.pack_blocks[0], "Victory!")
        print("PASS encounter beat with pack mechanics")


# ---------------------------------------------------------------------------
# Suite 6 - validation: ~20 malformed specs each raise PackBeatsError
# ---------------------------------------------------------------------------


class TestValidation(unittest.TestCase):
    """Each malformed encounter_mechanics section must raise PackBeatsError."""

    def _validate(self, em_section):
        manifest = {
            "schema_version": 1,
            "id": "test_v",
            "encounter_mechanics": em_section,
        }
        return validate_encounter_mechanics(manifest, Path("/tmp"))

    def test_absent_returns_none(self):
        result = validate_encounter_mechanics({"schema_version": 1, "id": "x"}, Path("/tmp"))
        self.assertIsNone(result)
        print("PASS absent -> None")

    def test_valid_minimal_loads(self):
        result = self._validate({"dice_notation": "1d20"})
        self.assertIsNotNone(result)
        print("PASS minimal valid spec loads")

    def test_valid_full_spec_loads(self):
        result = self._validate(
            {
                "dice_notation": "2d6+1",
                "engine_rolls": False,
                "outcome_tiers": [
                    {"tier": "hit", "min_margin": 5},
                    {"tier": "graze", "min_margin": 0},
                    {"tier": "miss", "min_margin": None},
                ],
                "effects": {
                    "hit": {"state_delta": {"scene_aggression": -10}},
                    "graze": {"state_delta": {"scene_aggression": 0}},
                    "miss": {
                        "state_delta": {"scene_aggression": 10},
                        "directive": "You miss - describe the near-failure.",
                    },
                },
                "clamps": {"scene_aggression": [0, 100]},
            }
        )
        self.assertIsNotNone(result)
        print("PASS full spec loads")

    def test_bad_dice_notation_word(self):
        with self.assertRaises(PackBeatsError):
            self._validate({"dice_notation": "twenty"})
        print("PASS bad dice notation (word)")

    def test_bad_dice_notation_too_many_side_digits(self):
        # d1000 has 4 digits - exceeds \d{1,3}
        with self.assertRaises(PackBeatsError):
            self._validate({"dice_notation": "1d1000"})
        print("PASS bad dice notation (4-digit sides)")

    def test_bad_dice_notation_no_number_before_d(self):
        with self.assertRaises(PackBeatsError):
            self._validate({"dice_notation": "d20"})
        print("PASS bad dice notation (no count)")

    def test_engine_rolls_non_bool(self):
        with self.assertRaises(PackBeatsError):
            self._validate({"engine_rolls": "yes"})
        print("PASS engine_rolls non-bool")

    def test_outcome_tiers_not_list(self):
        with self.assertRaises(PackBeatsError):
            self._validate({"outcome_tiers": {"tier": "x", "min_margin": None}})
        print("PASS outcome_tiers not list")

    def test_outcome_tiers_too_many(self):
        # MAX_TIERS + 1 entries (even without proper null floor, count check fires first)
        tiers = [{"tier": f"t{i}", "min_margin": i} for i in range(MAX_TIERS + 1)]
        with self.assertRaises(PackBeatsError):
            self._validate({"outcome_tiers": tiers})
        print("PASS outcome_tiers too many")

    def test_tier_not_snake_case(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "CamelCase", "min_margin": 0},
                        {"tier": "floor", "min_margin": None},
                    ]
                }
            )
        print("PASS tier not snake_case")

    def test_duplicate_tier_name(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 5},
                        {"tier": "hit", "min_margin": None},
                    ]
                }
            )
        print("PASS duplicate tier name")

    def test_min_margin_out_of_range_high(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 30},  # > 25
                        {"tier": "miss", "min_margin": None},
                    ]
                }
            )
        print("PASS min_margin out of range (high)")

    def test_min_margin_out_of_range_low(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": -30},  # < -25
                        {"tier": "miss", "min_margin": None},
                    ]
                }
            )
        print("PASS min_margin out of range (low)")

    def test_two_nulls(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "a", "min_margin": None},
                        {"tier": "b", "min_margin": None},
                    ]
                }
            )
        print("PASS two nulls")

    def test_null_not_last(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "a", "min_margin": None},
                        {"tier": "b", "min_margin": 0},
                    ]
                }
            )
        print("PASS null not last")

    def test_missing_null_floor(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 5},
                        {"tier": "miss", "min_margin": 0},
                        # no null floor
                    ]
                }
            )
        print("PASS missing null floor")

    def test_effect_key_not_declared_tier(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 0},
                        {"tier": "miss", "min_margin": None},
                    ],
                    "effects": {
                        "crit": {"state_delta": {"scene_aggression": -10}},
                    },
                }
            )
        print("PASS effect key not declared tier")

    def test_delta_out_of_range_high(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 0},
                        {"tier": "miss", "min_margin": None},
                    ],
                    "effects": {
                        "hit": {"state_delta": {"scene_aggression": 999}},
                    },
                }
            )
        print("PASS delta out of range (high)")

    def test_delta_out_of_range_low(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 0},
                        {"tier": "miss", "min_margin": None},
                    ],
                    "effects": {
                        "hit": {"state_delta": {"scene_aggression": -999}},
                    },
                }
            )
        print("PASS delta out of range (low)")

    def test_clamp_lo_greater_than_hi(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [{"tier": "x", "min_margin": None}],
                    "clamps": {"scene_aggression": [100, 0]},
                }
            )
        print("PASS clamp lo > hi")

    def test_clamp_not_list_of_two(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "clamps": {"scene_aggression": [0]},
                }
            )
        print("PASS clamp not list of two")

    def test_effects_not_dict(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [{"tier": "x", "min_margin": None}],
                    "effects": ["list", "not", "dict"],
                }
            )
        print("PASS effects not dict")

    def test_state_delta_not_dict(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 0},
                        {"tier": "miss", "min_margin": None},
                    ],
                    "effects": {"hit": {"state_delta": "not_a_dict"}},
                }
            )
        print("PASS state_delta not dict")

    def test_directive_too_long(self):
        with self.assertRaises(PackBeatsError):
            self._validate(
                {
                    "outcome_tiers": [
                        {"tier": "hit", "min_margin": 0},
                        {"tier": "miss", "min_margin": None},
                    ],
                    "effects": {
                        "hit": {"state_delta": {}, "directive": "x" * (MAX_TEMPLATE_BYTES + 1)},
                    },
                }
            )
        print("PASS directive too long")


# ---------------------------------------------------------------------------
# Suite 7 - boundary fuzz: 20 specs x 7 combos, never a 500
# ---------------------------------------------------------------------------


class TestEncounterFuzz(unittest.TestCase):
    """20 boundary specs x 7 (roll,dc,state) combos through the turn-time
    guard path - never raises, always valid tier."""

    # 20 boundary specs - valid or edge-of-schema structures
    _SPECS = [
        # 1. no spec (pure default path)
        None,
        # 2. empty spec
        {},
        # 3. empty outcome_tiers (falsy -> use engine defaults)
        {"outcome_tiers": []},
        # 4. single-tier (always floor)
        {"outcome_tiers": [{"tier": "only", "min_margin": None}]},
        # 5. effects only, no tiers declared
        {"effects": {"critical": {"state_delta": {"scene_aggression": -15}}}},
        # 6. empty effects
        {"effects": {}},
        # 7. empty clamps
        {"clamps": {}},
        # 8. clamps only
        {"clamps": {"scene_aggression": [0, 100]}},
        # 9. four-tier ladder
        {
            "outcome_tiers": [
                {"tier": "a", "min_margin": 5},
                {"tier": "b", "min_margin": 0},
                {"tier": "c", "min_margin": -5},
                {"tier": "d", "min_margin": None},
            ]
        },
        # 10. effect references a tier that is not in outcome_tiers
        {"effects": {"unknown_tier": {"state_delta": {}}}},
        # 11. valid two-tier pack
        {
            "outcome_tiers": [
                {"tier": "win", "min_margin": 0},
                {"tier": "loss", "min_margin": None},
            ],
            "effects": {
                "win": {"state_delta": {}},
                "loss": {"state_delta": {}},
            },
        },
        # 12. high min_margin threshold (only roll >> dc triggers)
        {
            "outcome_tiers": [
                {"tier": "crit", "min_margin": 10},
                {"tier": "floor", "min_margin": None},
            ]
        },
        # 13. minimum valid (one floor tier)
        {"outcome_tiers": [{"tier": "single_floor", "min_margin": None}]},
        # 14. zero delta in effects
        {"effects": {"fumble": {"state_delta": {"x": 0}}}},
        # 15. tight clamp (lo==hi)
        {"clamps": {"x": [0, 0]}},
        # 16. extreme positive margin threshold
        {
            "outcome_tiers": [
                {"tier": "ultra", "min_margin": 25},
                {"tier": "rest", "min_margin": None},
            ]
        },
        # 17. extreme negative margin threshold
        {
            "outcome_tiers": [
                {"tier": "low", "min_margin": -25},
                {"tier": "floor", "min_margin": None},
            ]
        },
        # 18. zero delta for a default-tier name
        {"effects": {"critical": {"state_delta": {"scene_aggression": 0}}}},
        # 19. two-tier with per-tier directive
        {
            "outcome_tiers": [
                {"tier": "mid", "min_margin": 0},
                {"tier": "low", "min_margin": None},
            ],
            "effects": {
                "mid": {"state_delta": {"scene_aggression": -5}, "directive": "Good move."},
            },
        },
        # 20. long-but-valid directive (under MAX_TEMPLATE_BYTES)
        {
            "outcome_tiers": [{"tier": "x", "min_margin": None}],
            "effects": {"x": {"state_delta": {}, "directive": "D" * 100}},
        },
    ]

    # 7 (roll, dc, state_dict) combos
    _COMBOS = [
        (1, 25, {}),
        (30, 1, {}),
        (10, 10, {"scene_aggression": 50}),
        (15, 10, {"scene_aggression": 0}),
        (5, 20, {"scene_aggression": 100}),
        (20, 20, {}),
        (1, 1, {"scene_aggression": 50}),
    ]

    def test_boundary_never_raises(self):
        beat = EncounterBeat()
        total = 0
        failed = []

        for i, spec in enumerate(self._SPECS):
            if spec is None:
                pack = {}
            else:
                pack = {"pack_beats": {"encounter_mechanics": spec}}

            for roll, dc, state_dict in self._COMBOS:
                total += 1
                label = f"spec#{i + 1} roll={roll} dc={dc}"
                state = WorldState(**state_dict)
                gs = FakeGS(state)
                ts = FakeTurnState(gs=gs, pack=pack)
                ts.dice_roll = roll
                ts.dice_context = FakeDiceCtx(dc=dc, skill="test", stat="STR")

                try:
                    result = beat.run(ts)
                    tier = result.signals.get("dice_tier")
                    if tier is None:
                        msg = f"FAIL [{label}]: dice_tier missing from signals"
                        failed.append(msg)
                        print(msg)
                    else:
                        print(f"PASS [{label}]: tier={tier}")
                except Exception as exc:
                    msg = f"FAIL [{label}]: raised {type(exc).__name__}: {exc}"
                    failed.append(msg)
                    print(msg)

        summary = f"{total - len(failed)}/{total} passed"
        print(f"\n{'OK' if not failed else 'FAIL'}- {summary}")
        self.assertEqual(
            failed,
            [],
            "Boundary cases failed:\n" + "\n".join(failed),
        )


# ---------------------------------------------------------------------------
# Suite 8 - fail-open: render raises, turn completes, no block, tier present
# ---------------------------------------------------------------------------


class TestFailOpen(unittest.TestCase):
    """monkeypatch render_directive to raise - turn must complete, no
    block appended to pack_blocks, dice_tier still in signals."""

    def test_render_raises_turn_completes(self):
        state = WorldState(scene_aggression=50)
        gs = FakeGS(state)
        ts = FakeTurnState(gs=gs, pack={})
        ts.dice_roll = 15
        ts.dice_context = FakeDiceCtx(dc=10, skill="climb", stat="STR")

        with patch(
            "app.game.beats.encounter.render_directive",
            side_effect=RuntimeError("injected failure"),
        ):
            result = EncounterBeat().run(ts)

        # Turn completed - no exception propagated.
        # No block injected.
        self.assertEqual(result.pack_blocks, [])
        # dice_tier must be present (it's written before the try block).
        self.assertIn("dice_tier", result.signals)
        print(f"PASS fail-open: turn complete, no block, dice_tier={result.signals['dice_tier']!r}")


# ---------------------------------------------------------------------------
# Suite 9 - dry-run: real pack load + end-to-end, LLM never touched
# ---------------------------------------------------------------------------


class TestDryRun(unittest.TestCase):
    """Load a temp pack with encounter_mechanics, run end-to-end, verify
    dice_gate.call_llm_plain is never called (the resolver is LLM-free)."""

    def test_encounter_mechanics_round_trip(self):
        import app.game.lore as lore_mod
        import app.game.dice_gate as dg

        em_spec = {
            "dice_notation": "1d20",
            "engine_rolls": False,
            "outcome_tiers": [
                {"tier": "win", "min_margin": 0},
                {"tier": "loss", "min_margin": None},
            ],
            "effects": {
                "win": {"state_delta": {"scene_aggression": -10}},
                "loss": {"state_delta": {"scene_aggression": 10}},
            },
            "clamps": {"scene_aggression": [0, 100]},
        }

        with tempfile.TemporaryDirectory() as tmproot:
            pack_dir = Path(tmproot) / "enc_test_pack"
            pack_dir.mkdir()

            manifest = {
                "id": "enc_test_pack",
                "schema_version": 1,
                "name": "EncTest",
                "version": "0.1",
                "description": "encounter_mechanics dry-run test",
                "encounter_mechanics": em_spec,
            }
            (pack_dir / "pack.json").write_text(json.dumps(manifest))
            (pack_dir / "state_schema.json").write_text(
                json.dumps({"fields": [{"name": "scene_aggression", "type": "int", "default": 50}]})
            )

            orig_root = lore_mod.LORE_ROOT
            lore_mod.LORE_ROOT = Path(tmproot)
            lore_mod.clear_cache()

            orig_call = dg.call_llm_plain
            llm_calls = []

            def _no_llm(*a, **kw):
                llm_calls.append((a, kw))
                raise AssertionError("LLM called during encounter_mechanics resolution")

            dg.call_llm_plain = _no_llm
            try:
                pack = lore_mod.load_pack("enc_test_pack")

                # Assert pack_beats.encounter_mechanics populated
                enc = pack["pack_beats"].get("encounter_mechanics")
                self.assertIsNotNone(enc, "encounter_mechanics not in pack_beats")
                self.assertEqual(enc["dice_notation"], "1d20")
                self.assertEqual(enc["engine_rolls"], False)
                self.assertEqual(len(enc["outcome_tiers"]), 2)

                # End-to-end encounter beat
                state = WorldState(scene_aggression=50)
                gs = FakeGS(state)
                ts = FakeTurnState(gs=gs, pack=pack)
                ts.dice_roll = 15
                ts.dice_context = FakeDiceCtx(dc=10, skill="strike", stat="STR")

                result = EncounterBeat().run(ts)

                # No LLM called
                self.assertEqual(llm_calls, [], "LLM called during encounter resolution")

                # roll=15, dc=10, margin=5; win needs margin>=0 -> "win"
                self.assertEqual(result.signals.get("dice_tier"), "win")
                # 50 - 10 = 40
                self.assertEqual(result.signals.get("scene_aggression"), 40)
                # pack_blocks: exactly one block (directive from render_directive)
                self.assertEqual(len(result.pack_blocks), 1)

                # Verify mechanics_for returns the loaded spec
                self.assertIsNotNone(mechanics_for(pack))

                print(
                    f"PASS dry-run: tier={result.signals['dice_tier']} "
                    f"agg={result.signals['scene_aggression']} "
                    f"blocks={len(result.pack_blocks)} "
                    f"llm_calls={len(llm_calls)}"
                )
            finally:
                dg.call_llm_plain = orig_call
                lore_mod.LORE_ROOT = orig_root
                lore_mod.clear_cache()


# ---------------------------------------------------------------------------
# Suite 10 - runtime truncation: over-cap directive truncates + warns
# ---------------------------------------------------------------------------


class TestDirectiveTruncation(unittest.TestCase):
    """runtime over-cap directives are truncated at a char boundary to
    MAX_TEMPLATE_BYTES, a warning is logged (never an error/raise), and the
    turn completes with exactly one pack_blocks block (fail-open)."""

    def test_pack_directive_over_cap_truncates(self):
        """render_directive: over-cap pack-tier directive -> truncate + warn."""
        spec = {
            "effects": {
                "success": {
                    "directive": "x" * (MAX_TEMPLATE_BYTES + 50),
                }
            }
        }
        with self.assertLogs("app.game.encounter_mechanics", level="WARNING") as cm:
            result = render_directive(
                spec,
                roll=10,
                dc=5,
                skill="slash",
                stat="STR",
                tier="success",
                new_state={"scene_aggression": {"before": 50, "after": 42}},
            )
        self.assertEqual(len(result), MAX_TEMPLATE_BYTES)
        self.assertTrue(
            any("truncat" in m for m in cm.output),
            f"Expected 'truncat' in log output; got: {cm.output}",
        )
        print(
            f"PASS pack directive over-cap truncates: len={len(result)} (cap={MAX_TEMPLATE_BYTES})"
        )

    def test_default_hint_over_cap_truncates(self):
        """render_directive(None, ...): over-cap DEFAULT_HINT -> truncate + warn."""
        import app.game.encounter_mechanics as em_mod

        # A format string longer than cap; only {roll} used so .format succeeds
        # with the extra kwargs that render_directive always passes.
        oversized_hint = "y" * (MAX_TEMPLATE_BYTES + 50) + "{roll}"
        original_hint = em_mod.DEFAULT_HINT
        em_mod.DEFAULT_HINT = oversized_hint
        self.addCleanup(setattr, em_mod, "DEFAULT_HINT", original_hint)

        with self.assertLogs("app.game.encounter_mechanics", level="WARNING") as cm:
            result = render_directive(
                None,
                roll=15,
                dc=10,
                skill="slash",
                stat="STR",
                tier="success",
                new_state={"scene_aggression": {"before": 50, "after": 40}},
            )
        self.assertEqual(len(result), MAX_TEMPLATE_BYTES)
        self.assertTrue(
            any("truncat" in m for m in cm.output),
            f"Expected 'truncat' in log output; got: {cm.output}",
        )
        print(f"PASS DEFAULT_HINT over-cap truncates: len={len(result)} (cap={MAX_TEMPLATE_BYTES})")

    def test_beat_double_guard_truncates_and_completes(self):
        """EncounterBeat: render returns over-cap string -> double-guard truncates,
        pack_blocks[0] capped, dice_tier present, warning on encounter_beat."""
        state = WorldState(scene_aggression=50)
        gs = FakeGS(state)
        ts = FakeTurnState(gs=gs, pack={})
        ts.dice_roll = 15
        ts.dice_context = FakeDiceCtx(dc=10, skill="test", stat="STR")

        oversized = "Z" * (MAX_TEMPLATE_BYTES + 100)

        with patch(
            "app.game.beats.encounter.render_directive",
            return_value=oversized,
        ):
            with self.assertLogs("encounter_beat", level="WARNING") as cm:
                result = EncounterBeat().run(ts)

        # Exactly one block, capped at MAX_TEMPLATE_BYTES
        self.assertEqual(len(result.pack_blocks), 1)
        self.assertEqual(len(result.pack_blocks[0]), MAX_TEMPLATE_BYTES)
        # Core signal still present
        self.assertIn("dice_tier", result.signals)
        self.assertTrue(
            any("truncat" in m for m in cm.output),
            f"Expected 'truncat' in encounter_beat log; got: {cm.output}",
        )
        print(
            f"PASS beat double-guard truncates: "
            f"block_len={len(result.pack_blocks[0])} "
            f"dice_tier={result.signals['dice_tier']!r}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
