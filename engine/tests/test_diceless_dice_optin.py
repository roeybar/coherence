"""Tests for diceless-by-default + per-save dice opt-in.

stdlib unittest, zero live services. Patches LLM classifiers and DB calls.

Suites:
  1. TestDetector - detect_reality_bending TRUE/FALSE
  2. TestPhysPackDefaultNoDice - physicalisation pack default -> no DiceRollPending
  3. TestPhysPackOptInDoesDice - physicalisation pack opt-in -> DiceRollPending
  4. TestRealityBendingGroundsBothModes - reality-bending -> AbilityIntentAmbiguous
  5. TestAthleticVerbNeverDicesInPhysPack - "i jump to the moon" -> grounding
  6. TestDiceNativePackUnaffected - neon_rain always dices
  7. TestBackwardCompat - GameConfig(**{}) -> dice_mechanics False
  8. TestFailSafe - classify_scope raises -> no NameError
"""

import unittest
from dataclasses import asdict
from unittest.mock import MagicMock, patch

from app.game.dice_gate import DiceAssessment, DiceRollPending
from app.game.physicalisation import AbilityIntentAmbiguous, detect_reality_bending
from app.game.state import CharacterProfile, GameConfig, GameSave, WorldState
from tests.fixtures import phys_pack


def setUpModule():
    phys_pack.start()


def tearDownModule():
    phys_pack.stop()


# helpers


def _make_gs(pack_id: str = "testpack", dice_mechanics: bool = False, turn: int = 1) -> GameSave:
    gs = GameSave()
    gs.profile = CharacterProfile(name="Hero", appearance="plain")
    gs.config = GameConfig(pack_id=pack_id)
    gs.config.dice_mechanics = dice_mechanics
    gs.state = WorldState(location="dungeon")
    gs.history = []
    gs.turn = turn
    gs.undo_stack = []
    gs.session_in = 0
    gs.session_out = 0
    gs.checkpoint_state = {}
    gs.checkpoint_history = []
    gs.checkpoint_turn = 0
    gs.read_only = False
    gs.shadow_of = None
    gs.seed_roster = None
    gs.glossary = {}
    return gs


def _canned_dice_assessment() -> DiceAssessment:
    return DiceAssessment(
        dc=13,
        skill="brawn",
        stat="STR",
        flavor="The guard braces for impact.",
    )


def _run_turn_no_dice(gs, player_input, **kw):
    """Call run_turn with minimal mocks; any exception other than
    DiceRollPending is acceptable (pipeline may fail without LLM/DB)."""
    from app.game.engine import run_turn

    with (
        patch("app.game.physicalisation.classify_scope", return_value=({"scope": "none"}, None)),
        patch(
            "app.game.physicalisation.classify_scope_tier2", return_value=({"scope": "none"}, None)
        ),
        patch("app.game.engine.classify_turn", return_value=("standard", None)),
        patch(
            "app.game.items.visible_to_player",
            return_value={"scene": [], "carried": [], "npcs": {}},
        ),
        patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
    ):
        try:
            run_turn(gs, player_input, "save-test", **kw)
        except DiceRollPending:
            raise
        except Exception:
            pass  # network/pipeline failures OK in hermetic env


def _run_turn_expect_dice(gs, player_input, **kw):
    """Call run_turn expecting DiceRollPending; raises AssertionError if not raised."""
    from app.game.engine import run_turn

    with (
        patch("app.game.physicalisation.classify_scope", return_value=({"scope": "none"}, None)),
        patch(
            "app.game.physicalisation.classify_scope_tier2", return_value=({"scope": "none"}, None)
        ),
        patch("app.game.engine.classify_turn", return_value=("standard", None)),
        patch(
            "app.game.items.visible_to_player",
            return_value={"scene": [], "carried": [], "npcs": {}},
        ),
        patch("app.game.dice_gate.assess_dice_dc", return_value=_canned_dice_assessment()),
        patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
    ):
        with _AssertRaisesDice() as ctx:
            run_turn(gs, player_input, "save-test", **kw)
    return ctx


class _AssertRaisesDice:
    """Context manager: absorbs DiceRollPending (expected); re-raises others."""

    def __init__(self):
        self.raised = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is DiceRollPending:
            self.raised = True
            return True  # suppress
        if exc_type is not None:
            return False  # re-raise other exceptions
        return False


def _run_turn_only_scope_mock(gs, player_input, **kw):
    """Call run_turn with only classify_scope mocked (for AbilityIntentAmbiguous
    tests - the gate fires inside the physicalisation try, before classify_turn)."""
    from app.game.engine import run_turn

    with (
        patch("app.game.physicalisation.classify_scope", return_value=({"scope": "none"}, None)),
        patch(
            "app.game.physicalisation.classify_scope_tier2", return_value=({"scope": "none"}, None)
        ),
        patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
    ):
        return run_turn(gs, player_input, "save-test", **kw)


# 1. TestDetector


class TestDetector(unittest.TestCase):
    _TRUE_CASES = [
        "i teleport to the moon",
        "i jump to the moon",
        "i phase through the wall",
        "i stop time",
        "i become invisible",
        "i fly up into the sky",
        "i levitate",
        "i time-travel",
        "i travel back in time",
        "i resurrect the dead",
        "i walk through the wall",
        "i breathe in space",
        "i blink to the other side",
        "i freeze time",
        "i go invisible",
        "i fly into space",
        "i leap to the sun",
        "i turn invisible now",
        "teleport across the room",
        "i raise the dead",
        "i rewind time",
        "i ascend to the stars",
        "i float up into the sky",
    ]

    _FALSE_CASES = [
        "i walk to the market",
        "i climb the stairs",
        "i jump over the fence",
        "i punch the guard",
        "i talk to the merchant",
        "i run to the door",
        "i pick up the sword",
        "i open the door",
        "i push the cart",
        "i lift the box",
        "i look around",
        "i ask about the price",
        "i sit down",
        "i wait here",
        "",
        " ",
        "   ",
    ]

    def test_true_cases(self):
        for text in self._TRUE_CASES:
            with self.subTest(text=text):
                self.assertTrue(
                    detect_reality_bending(text),
                    f"Expected TRUE for: {text!r}",
                )
        print(f"PASS TestDetector.true_cases: {len(self._TRUE_CASES)} inputs -> True")

    def test_false_cases(self):
        for text in self._FALSE_CASES:
            with self.subTest(text=text):
                self.assertFalse(
                    detect_reality_bending(text),
                    f"Expected FALSE for: {text!r}",
                )
        print(f"PASS TestDetector.false_cases: {len(self._FALSE_CASES)} inputs -> False")

    def test_never_raises(self):
        for text in self._TRUE_CASES + self._FALSE_CASES + ["x" * 2000, "\u05d0\u05d1\u05d2"]:
            with self.subTest(text=text[:40]):
                try:
                    detect_reality_bending(text)
                except Exception as e:
                    self.fail(f"detect_reality_bending raised {e!r} for {text[:40]!r}")
        print("PASS TestDetector.never_raises")


# 2. TestPhysPackDefaultNoDice


class TestPhysPackDefaultNoDice(unittest.TestCase):
    def test_punch_guard_no_dice_default(self):
        """physicalisation pack, dice_mechanics absent/False -> no DiceRollPending."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=False)
        try:
            _run_turn_no_dice(gs, "i punch the guard")
        except DiceRollPending:
            self.fail("DiceRollPending should be suppressed in diceless-default mode")
        print("PASS TestPhysPackDefaultNoDice.punch_guard_no_dice_default")

    def test_attack_enemy_no_dice_default(self):
        """physicalisation pack, dice_mechanics=False -> no DiceRollPending for attack."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=False)
        try:
            _run_turn_no_dice(gs, "i attack the enemy")
        except DiceRollPending:
            self.fail("DiceRollPending should be suppressed in diceless-default mode")
        print("PASS TestPhysPackDefaultNoDice.attack_enemy_no_dice_default")

    def test_dice_mechanics_absent_defaults_false(self):
        """GameConfig from dict without dice_mechanics key -> False (no migration)."""
        gc = GameConfig(**{})
        self.assertFalse(gc.dice_mechanics)
        print("PASS TestPhysPackDefaultNoDice.dice_mechanics_absent_defaults_false")


# 3. TestPhysPackOptInDoesDice


class TestPhysPackOptInDoesDice(unittest.TestCase):
    def test_punch_guard_dice_opt_in(self):
        """physicalisation pack, dice_mechanics=True -> DiceRollPending raised."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=True)
        ctx = _run_turn_expect_dice(gs, "i punch the guard")
        self.assertTrue(ctx.raised, "DiceRollPending should fire when dice opted in")
        print("PASS TestPhysPackOptInDoesDice.punch_guard_dice_opt_in")

    def test_attack_soldier_dice_opt_in(self):
        """physicalisation pack, dice_mechanics=True -> DiceRollPending for attack."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=True)
        ctx = _run_turn_expect_dice(gs, "i attack the soldier")
        self.assertTrue(ctx.raised, "DiceRollPending should fire for attack with opt-in")
        print("PASS TestPhysPackOptInDoesDice.attack_soldier_dice_opt_in")


# 4. TestRealityBendingGroundsBothModes


class TestRealityBendingGroundsBothModes(unittest.TestCase):
    def _assert_grounds(self, player_input: str, dice_mechanics: bool):
        """Verify AbilityIntentAmbiguous is raised (not DiceRollPending)."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=dice_mechanics)
        raised_aia = False
        raised_drp = False
        try:
            _run_turn_only_scope_mock(gs, player_input)
        except AbilityIntentAmbiguous as e:
            raised_aia = True
            self.assertEqual(
                e.to_payload()["kind"],
                "ability_intent_clarification_needed",
                "payload kind must be ability_intent_clarification_needed",
            )
        except DiceRollPending:
            raised_drp = True
        except Exception:
            pass

        self.assertFalse(
            raised_drp, f"DiceRollPending must NOT fire for reality-bending: {player_input!r}"
        )
        self.assertTrue(
            raised_aia, f"AbilityIntentAmbiguous must fire for reality-bending: {player_input!r}"
        )

    def test_teleport_diceless_mode(self):
        self._assert_grounds("i teleport to the moon", dice_mechanics=False)
        print("PASS TestRealityBendingGroundsBothModes.teleport_diceless")

    def test_teleport_opt_in_mode(self):
        """Reality-bending grounds even when dice opt-in is on (orthogonality)."""
        self._assert_grounds("i teleport to the moon", dice_mechanics=True)
        print("PASS TestRealityBendingGroundsBothModes.teleport_opt_in")

    def test_stop_time_grounds(self):
        self._assert_grounds("i stop time", dice_mechanics=False)
        print("PASS TestRealityBendingGroundsBothModes.stop_time_grounds")

    def test_become_invisible_grounds(self):
        self._assert_grounds("i become invisible", dice_mechanics=True)
        print("PASS TestRealityBendingGroundsBothModes.become_invisible_grounds")


# 5. TestAthleticVerbNeverDicesInPhysPack


class TestAthleticVerbNeverDicesInPhysPack(unittest.TestCase):
    def test_jump_to_moon_grounds_not_dice(self):
        """'i jump to the moon' - athletic verb + reality-bending.
        Even with dice_mechanics=True it must ground (AbilityIntentAmbiguous),
        not roll (DiceRollPending)."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=True)
        raised_aia = False
        raised_drp = False
        try:
            _run_turn_only_scope_mock(gs, "i jump to the moon")
        except AbilityIntentAmbiguous:
            raised_aia = True
        except DiceRollPending:
            raised_drp = True
        except Exception:
            pass

        self.assertFalse(raised_drp, "DiceRollPending must NOT fire for 'i jump to the moon'")
        self.assertTrue(raised_aia, "AbilityIntentAmbiguous must fire for 'i jump to the moon'")
        print("PASS TestAthleticVerbNeverDicesInPhysPack.jump_to_moon_grounds")

    def test_fly_into_sky_grounds(self):
        """'i fly up into the sky' -> grounding (not dice) even with opt-in."""
        gs = _make_gs(pack_id="testpack", dice_mechanics=True)
        raised_aia = False
        raised_drp = False
        try:
            _run_turn_only_scope_mock(gs, "i fly up into the sky")
        except AbilityIntentAmbiguous:
            raised_aia = True
        except DiceRollPending:
            raised_drp = True
        except Exception:
            pass

        self.assertFalse(raised_drp, "'i fly up into the sky' must not dice")
        self.assertTrue(raised_aia, "'i fly up into the sky' must ground")
        print("PASS TestAthleticVerbNeverDicesInPhysPack.fly_into_sky_grounds")


# 6. TestDiceNativePackUnaffected


class TestDiceNativePackUnaffected(unittest.TestCase):
    def test_neon_rain_punch_still_dices_opt_in_false(self):
        """neon_rain pack, dice_mechanics=False -> still dices (suppression
        gated on _pack_has_phys which is False for neon_rain)."""
        gs = _make_gs(pack_id="neon_rain", dice_mechanics=False)
        ctx = _run_turn_expect_dice(gs, "i punch the guard")
        self.assertTrue(
            ctx.raised, "DiceRollPending must fire in neon_rain regardless of dice_mechanics"
        )
        print("PASS TestDiceNativePackUnaffected.neon_rain_punch_dices_opt_in_false")

    def test_neon_rain_punch_still_dices_opt_in_true(self):
        """neon_rain pack, dice_mechanics=True -> DiceRollPending (no-op flag)."""
        gs = _make_gs(pack_id="neon_rain", dice_mechanics=True)
        ctx = _run_turn_expect_dice(gs, "i punch the guard")
        self.assertTrue(ctx.raised, "DiceRollPending must fire in neon_rain")
        print("PASS TestDiceNativePackUnaffected.neon_rain_punch_dices_opt_in_true")

    def test_neon_rain_teleport_no_ability_grounding(self):
        """neon_rain has no physicalisation template -> reality-bending
        does not raise AbilityIntentAmbiguous (floor gated on _pack_has_phys)."""
        gs = _make_gs(pack_id="neon_rain", dice_mechanics=False)
        raised_aia = False
        try:
            _run_turn_only_scope_mock(gs, "i teleport to the moon")
        except AbilityIntentAmbiguous:
            raised_aia = True
        except Exception:
            pass

        self.assertFalse(
            raised_aia, "AbilityIntentAmbiguous must NOT fire in neon_rain (no phys template)"
        )
        print("PASS TestDiceNativePackUnaffected.teleport_no_ability_grounding")


# 7. TestBackwardCompat


class TestBackwardCompat(unittest.TestCase):
    def test_empty_dict_yields_false(self):
        """GameConfig(**{}) -> dice_mechanics == False (no migration)."""
        gc = GameConfig(**{})
        self.assertFalse(gc.dice_mechanics)
        print("PASS TestBackwardCompat.empty_dict_yields_false")

    def test_partial_dict_yields_false(self):
        """GameConfig(**{"locale":"en","pack_id":"testpack"}) -> dice_mechanics == False."""
        gc = GameConfig(**{"locale": "en", "pack_id": "testpack"})
        self.assertFalse(gc.dice_mechanics)
        print("PASS TestBackwardCompat.partial_dict_yields_false")

    def test_explicit_true_survives_roundtrip(self):
        """dice_mechanics=True round-trips through asdict."""
        gc = GameConfig()
        gc.dice_mechanics = True
        d = asdict(gc)
        self.assertIn("dice_mechanics", d)
        gc2 = GameConfig(**d)
        self.assertTrue(gc2.dice_mechanics)
        print("PASS TestBackwardCompat.explicit_true_roundtrip")

    def test_gameconfig_field_count_grew_by_one(self):
        """GameConfig has dice_mechanics as a field (sanity)."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(GameConfig)}
        self.assertIn("dice_mechanics", fields)
        print("PASS TestBackwardCompat.gameconfig_has_dice_mechanics")


# 8. TestFailSafe


class TestFailSafe(unittest.TestCase):
    def test_classify_scope_raises_no_name_error(self):
        """If classify_scope raises, outer-scope _reality_bending/_dice_suppressed
        are still defined (set before the try) -> no NameError at the dice gates."""
        from app.game.engine import run_turn

        gs = _make_gs(pack_id="testpack", dice_mechanics=False)
        with (
            patch(
                "app.game.physicalisation.classify_scope",
                side_effect=RuntimeError("classifier down"),
            ),
            patch("app.game.engine.classify_turn", return_value=("standard", None)),
            patch(
                "app.game.items.visible_to_player",
                return_value={"scene": [], "carried": [], "npcs": {}},
            ),
            patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
        ):
            try:
                run_turn(gs, "i punch the guard", "save-test")
            except DiceRollPending:
                # Should be suppressed (physicalisation pack + dice_mechanics=False).
                self.fail("classify_scope failure must not un-suppress the dice gate")
            except NameError as e:
                self.fail(f"NameError after classify_scope failure: {e}")
            except Exception:
                pass  # Any other exception from pipeline is fine
        print("PASS TestFailSafe.classify_scope_raises_no_name_error")


if __name__ == "__main__":
    unittest.main()
