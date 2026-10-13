"""Tests for the automated-client detector.

Suites S1-S5.  stdlib unittest, zero LLM calls, zero live services.
Runs in the test container (network: none).

S1 - locked invariant: human-pace x boundary-input grid
     x every hard-exemption -> level==0, flagged==False on all.
S2 - bot signals: repetition + ua active -> flagged=True, level=MAX_LEVEL;
     one signal alone -> not flagged.
S3 - per-turn reversible: no latch after signals clear.
S4 - durable-state integrity: gs.state/history unchanged by assess.
S5 - malformed input / missing fields -> no exception (no-500).
"""

import sys
import unittest

from app.game.anti_automation import assess, Assessment, K, MAX_LEVEL

# ---------------------------------------------------------------------------
# Minimal fake GameSave so we do not need the full app bootstrap.
# ---------------------------------------------------------------------------


class _FakeConfig:
    locale = "en"
    tier = "premium"


class _FakeState:
    def to_dict(self):
        return {"location": "forest", "turn": 0}


class _FakeGS:
    def __init__(self, history=None, shadow_of=None):
        self.history = history or []
        self.shadow_of = shadow_of
        self.config = _FakeConfig()
        self.state = _FakeState()
        self.session_in = 0
        self.session_out = 0


# ---------------------------------------------------------------------------
# S1 - human-never-flagged invariant
# ---------------------------------------------------------------------------


class TestS1HumanNeverFlagged(unittest.TestCase):
    """The locked invariant: human sessions + boundary inputs +
    every hard-exemption must always return level==0, flagged==False."""

    def _human_assess(self, player_input, gs=None, **kwargs):
        """Call assess with a browser user-agent (ua signal never fires)."""
        if gs is None:
            gs = _FakeGS()
        return assess(
            gs,
            player_input,
            ua="Mozilla/5.0 (compatible)",
            **kwargs,
        )

    # boundary input grid

    def test_repeat_input_no_flag(self):
        """Repeated player_input from a browser -> no flag."""
        history = [
            {"role": "user", "content": "go north"},
            {"role": "assistant", "content": "you walk north"},
            {"role": "user", "content": "go north"},
        ]
        gs = _FakeGS(history=history)
        r = self._human_assess("go north", gs=gs)
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_one_word_input_no_flag(self):
        r = self._human_assess("north")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_unicode_he_input_no_flag(self):
        r = self._human_assess("\u05dc\u05da \u05e6\u05e4\u05d5\u05e0\u05d4")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_long_input_2000_chars_no_flag(self):
        r = self._human_assess("A" * 2000)
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_undo_spam_then_turn_no_flag(self):
        """Undo operations don't touch assess; subsequent normal turn fine."""
        r = self._human_assess("look around")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    # hard-exemption grid

    def _all_signals_assess(self, **kwargs):
        """Call with both signals active but also a hard-exemption."""
        history = [{"role": "user", "content": "go north"}]
        gs = _FakeGS(history=history)
        return assess(
            gs,
            "go north",
            ua="python-httpx/0.24",
            **kwargs,
        )

    def test_consent_token_exemption(self):
        r = self._all_signals_assess(consent_token="tok-abc")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_dice_roll_exemption(self):
        r = self._all_signals_assess(dice_roll=17)
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_craft_roll_exemption(self):
        r = self._all_signals_assess(craft_roll=12)
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_addressee_exemption(self):
        r = self._all_signals_assess(addressee="npc_guard")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_reframe_confirm_exemption(self):
        r = self._all_signals_assess(reframe_confirm="confirm")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_world_rule_confirm_exemption(self):
        r = self._all_signals_assess(world_rule_confirm="cancel")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    def test_suggestion_index_exemption(self):
        r = self._all_signals_assess(suggestion_index=0)
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)

    # normal play without any signals

    def test_normal_session_no_flag(self):
        r = self._human_assess("I open the heavy iron door and step inside.")
        self.assertEqual(r.level, 0)
        self.assertFalse(r.flagged)
        self.assertEqual(r.signals, [])


# ---------------------------------------------------------------------------
# S2 - bot signals: repetition + ua -> max level + flagged
# ---------------------------------------------------------------------------


class TestS2BotSignalsFlagged(unittest.TestCase):
    def test_repetition_and_ua_flagged_max(self):
        """Repetition + automation UA -> flagged=True, level=MAX_LEVEL."""
        self.assertEqual(K, 2)
        history = [{"role": "user", "content": "go north"}]
        gs = _FakeGS(history=history)
        r = assess(gs, "go north", ua="python-requests/2.31")
        self.assertTrue(r.flagged)
        self.assertEqual(r.level, MAX_LEVEL)
        self.assertEqual(r.signals, ["repetition", "ua"])

    def test_one_signal_alone_not_flagged(self):
        """Repetition alone or automation UA alone - below K=2."""
        cases = (
            ("repetition", "go north", "Mozilla/5.0 (compatible)"),
            ("ua", "a fresh unique input", "python-requests/2.31"),
        )
        for name, text, ua in cases:
            with self.subTest(signal=name):
                gs = _FakeGS(history=[{"role": "user", "content": "go north"}])
                r = assess(gs, text, ua=ua)
                self.assertEqual(r.signals, [name])
                self.assertFalse(r.flagged)
                self.assertEqual(r.level, 0)

    def test_signal_names_in_result(self):
        """Active signals are named in the assessment."""
        history = [{"role": "user", "content": "attack"}]
        gs = _FakeGS(history=history)
        r = assess(gs, "attack", ua="python-httpx/0.24")
        self.assertIn("repetition", r.signals)
        self.assertIn("ua", r.signals)


# ---------------------------------------------------------------------------
# S3 - per-turn reversible, no latch
# ---------------------------------------------------------------------------


class TestS3PerTurnReversible(unittest.TestCase):
    def test_flag_then_clear(self):
        """After signals clear, the next call returns level 0 / not flagged."""
        history = [{"role": "user", "content": "go north"}]
        gs = _FakeGS(history=history)

        # Bot turn: flagged
        r_bot = assess(gs, "go north", ua="python-requests/2.31")
        self.assertTrue(r_bot.flagged)

        # Human turn: new input, normal UA -> level drops
        gs2 = _FakeGS()
        r_human = assess(
            gs2,
            "look around carefully at the room",
            ua="Mozilla/5.0 (compatible)",
        )
        self.assertFalse(r_human.flagged)
        self.assertEqual(r_human.level, 0)

    def test_no_state_mutation_between_calls(self):
        """A flagged call does not mutate anything that causes the next call
        to be flagged when signals are absent."""
        gs = _FakeGS()
        for _ in range(5):
            assess(gs, "probe", ua="python-requests/2.31")
        # Now call with no signals
        r = assess(
            gs,
            "a totally fresh unique prompt here 2026",
            ua="Mozilla/5.0",
        )
        self.assertFalse(r.flagged)


# ---------------------------------------------------------------------------
# S4 - durable-state integrity
# ---------------------------------------------------------------------------


class TestS4DurableStateIntegrity(unittest.TestCase):
    def test_state_unchanged_after_flag(self):
        """assess must not mutate gs.state, gs.history, or session tokens."""
        history = [{"role": "user", "content": "go north"}]
        gs = _FakeGS(history=history)
        state_before = gs.state.to_dict().copy()
        history_before = list(gs.history)
        si_before = gs.session_in
        so_before = gs.session_out

        assess(gs, "go north", ua="python-requests/2.31")

        self.assertEqual(gs.state.to_dict(), state_before)
        self.assertEqual(gs.history, history_before)
        self.assertEqual(gs.session_in, si_before)
        self.assertEqual(gs.session_out, so_before)

    def test_non_flagged_state_unchanged(self):
        """Human turn: assess doesn't mutate anything."""
        gs = _FakeGS()
        state_before = gs.state.to_dict().copy()

        assess(gs, "open the door", ua="Mozilla/5.0")

        self.assertEqual(gs.state.to_dict(), state_before)


# ---------------------------------------------------------------------------
# S5 - malformed pack / missing fields -> no exception
# ---------------------------------------------------------------------------


class TestS5MalformedNoException(unittest.TestCase):
    def test_none_ip_ua(self):
        gs = _FakeGS()
        r = assess(gs, "turn", ua=None)
        self.assertIsInstance(r, Assessment)

    def test_empty_string_ua(self):
        gs = _FakeGS()
        r = assess(gs, "turn", ua="")
        self.assertIsInstance(r, Assessment)

    def test_gs_missing_history_attr(self):
        """gs without history doesn't crash assess."""

        class _Bare:
            shadow_of = None
            config = _FakeConfig()
            state = _FakeState()
            session_in = 0
            session_out = 0
            # no .history attribute

        r = assess(_Bare(), "turn", ua="")
        self.assertIsInstance(r, Assessment)

    def test_very_long_ua(self):
        gs = _FakeGS()
        r = assess(gs, "turn", ua="x" * 500)
        self.assertIsInstance(r, Assessment)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestS1HumanNeverFlagged,
        TestS2BotSignalsFlagged,
        TestS3PerTurnReversible,
        TestS4DurableStateIntegrity,
        TestS5MalformedNoException,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
