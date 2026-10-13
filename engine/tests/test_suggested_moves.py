"""Tests for suggested_moves emitted every turn.

stdlib unittest, mock app.llm_client.call_messages; zero live services.
Pattern: test_narration_discipline.py.

Tests:
  1. TestAbilityTurnNowEmits - ability turns now emit (old early-clear removed)
  2. TestStandardTruncatesToThree - LLM returns 5 long moves -> <=3, each <=10 words
  3. TestEmptyNarrativeClears - pre-seed stale; empty narrative -> []
  4. TestLlmFailureNeverStale - LLM raises -> clear-first leaves []
  5. TestCraftAndEncounterEmit - craft/encounter/standard all emit moves
  6. TestTimeskipRefreshes - _run_timeskip_turn calls estimate_event_duration
                                       with turn_class="timeskip"
"""

import json
import unittest
from unittest.mock import patch

from app.game.campaign_clock import estimate_event_duration
from app.game.state import (
    CharacterProfile,
    GameConfig,
    GameSave,
    WorldState,
)


# helpers


def _state(**kw) -> WorldState:
    """Minimal WorldState with a location field (no clock -> simpler prompt)."""
    return WorldState(location="tavern", **kw)


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeResp:
    def __init__(self, payload: dict):
        self.content = [_FakeBlock(json.dumps(payload))]


def _llm_resp(moves: list) -> _FakeResp:
    return _FakeResp({"suggested_moves": moves})


def _make_gs() -> GameSave:
    """Minimal GameSave suitable for _run_timeskip_turn."""
    gs = GameSave()
    gs.profile = CharacterProfile(name="Tester", appearance="plain")
    gs.config = GameConfig()
    gs.state = WorldState(location="city", year=1)
    gs.history = []
    gs.turn = 0
    gs.undo_stack = []
    gs.session_in = 0
    gs.session_out = 0
    gs.checkpoint_state = {}
    gs.checkpoint_history = []
    gs.checkpoint_turn = 0
    return gs


_FAKE_TS_RESULT = {
    "completed_periods": 2,
    "interrupted": False,
    "interrupt_reason": "",
    "events_lines": [],
    "total_input_tokens": 100,
    "total_output_tokens": 50,
}


# 1. Ability turns now emit


class TestAbilityTurnNowEmits(unittest.TestCase):
    def test_ability_turn_now_emits(self):
        """ability turns must now emit suggested_moves (old early-clear removed)."""
        state = _state()
        with patch("app.llm_client.call_messages", return_value=_llm_resp(["a", "b", "c"])):
            estimate_event_duration(
                "You charge at the enemy.",
                clock_before={},
                turn=1,
                state=state,
                turn_class="ability",
            )
        self.assertEqual(
            state.suggested_moves, ["a", "b", "c"], "ability turn must emit moves, not clear them"
        )
        print("PASS test1: ability turn emits suggested_moves")


# 2. Standard turn truncates to <=3, each <=10 words


class TestStandardTruncatesToThree(unittest.TestCase):
    def test_standard_truncates_to_three(self):
        """LLM returning 5 long moves -> result is <=3, each <=10 words."""
        long_moves = [
            "one two three four five six seven eight nine ten eleven",
            "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda",
            " ".join(["word"] * 20),
            "short move",
            "another extra move",
        ]
        state = _state()
        with patch("app.llm_client.call_messages", return_value=_llm_resp(long_moves)):
            estimate_event_duration(
                "Something happened in the kingdom.",
                clock_before={},
                turn=2,
                state=state,
                turn_class="standard",
            )
        self.assertLessEqual(len(state.suggested_moves), 3, "must never emit more than 3 moves")
        for m in state.suggested_moves:
            word_count = len(m.split())
            self.assertLessEqual(
                word_count, 10, f"move {m!r} exceeds 10-word cap ({word_count} words)"
            )
        print("PASS test2: standard turn <=3 moves, each <=10 words")


# 3. Empty narrative clears


class TestEmptyNarrativeClears(unittest.TestCase):
    def test_empty_narrative_clears(self):
        """Empty narrative string must clear stale suggested_moves to []."""
        state = _state()
        state.suggested_moves = ["stale_move_1", "stale_move_2"]
        estimate_event_duration("", clock_before={}, turn=3, state=state)
        self.assertEqual(state.suggested_moves, [], "empty narrative must clear suggested_moves")
        print("PASS test3: empty narrative clears suggested_moves")

    def test_whitespace_only_clears(self):
        """Whitespace-only narrative must also clear suggested_moves."""
        state = _state()
        state.suggested_moves = ["stale"]
        estimate_event_duration("   \n\t  ", clock_before={}, turn=3, state=state)
        self.assertEqual(
            state.suggested_moves, [], "whitespace narrative must clear suggested_moves"
        )
        print("PASS test3b: whitespace-only narrative clears suggested_moves")


# 4. LLM failure leaves [] not stale (clear-first)


class TestLlmFailureNeverStale(unittest.TestCase):
    def test_llm_failure_never_stale(self):
        """Pre-seeded stale moves must be replaced by [] when the LLM raises."""
        state = _state()
        state.suggested_moves = ["old_stale_move"]
        with patch("app.llm_client.call_messages", side_effect=RuntimeError("LLM unavailable")):
            estimate_event_duration(
                "The dragon roars and breathes fire.",
                clock_before={},
                turn=4,
                state=state,
                turn_class="standard",
            )
        self.assertEqual(
            state.suggested_moves, [], "clear-first must leave [] on LLM failure, not stale"
        )
        print("PASS test4: LLM failure leaves [] not stale")

    def test_parse_failure_never_stale(self):
        """Malformed JSON from LLM must leave [] not stale."""

        class _BadResp:
            content = [type("B", (), {"text": "NOT JSON {{{"})]

        state = _state()
        state.suggested_moves = ["stale_from_prior_turn"]
        with patch("app.llm_client.call_messages", return_value=_BadResp()):
            estimate_event_duration(
                "The battle rages on.",
                clock_before={},
                turn=4,
                state=state,
                turn_class="standard",
            )
        self.assertEqual(state.suggested_moves, [], "JSON parse failure must leave [], not stale")
        print("PASS test4b: JSON parse failure leaves [] not stale")


# 5. craft / encounter / standard all emit moves


class TestCraftAndEncounterEmit(unittest.TestCase):
    def _run(self, turn_class: str) -> list:
        state = _state()
        with patch(
            "app.llm_client.call_messages", return_value=_llm_resp(["do X", "do Y", "do Z"])
        ):
            estimate_event_duration(
                "An event unfolds before you.",
                clock_before={},
                turn=5,
                state=state,
                turn_class=turn_class,
            )
        return state.suggested_moves

    def test_craft_emits(self):
        moves = self._run("craft")
        self.assertGreater(len(moves), 0, "craft turn must emit suggested_moves")
        print("PASS test5: craft turn emits suggested_moves")

    def test_encounter_emits(self):
        moves = self._run("encounter")
        self.assertGreater(len(moves), 0, "encounter turn must emit suggested_moves")
        print("PASS test5: encounter turn emits suggested_moves")

    def test_standard_emits(self):
        moves = self._run("standard")
        self.assertGreater(len(moves), 0, "standard turn must emit suggested_moves")
        print("PASS test5: standard turn emits suggested_moves")


# 6. Timeskip turn refreshes suggested_moves


class TestTimeskipRefreshes(unittest.TestCase):
    def test_timeskip_refreshes(self):
        """_run_timeskip_turn must call estimate_event_duration with turn_class='timeskip'."""
        from app.game.engine import _run_timeskip_turn

        gs = _make_gs()
        skip = {"span_n": 2, "span_unit": "year"}

        with (
            patch("app.game.timeskip.run_timeskip", return_value=_FAKE_TS_RESULT),
            patch(
                "app.game.timeskip.compose_timeskip_response",
                return_value="Two years passed quietly.",
            ),
            patch("app.game.campaign_clock.estimate_event_duration") as mock_eed,
        ):
            _run_timeskip_turn(gs, "save_test", "skip 2 years", skip)

        mock_eed.assert_called_once()
        _, kwargs = mock_eed.call_args
        self.assertEqual(
            kwargs.get("turn_class"), "timeskip", f"Expected turn_class='timeskip', got: {kwargs}"
        )
        print("PASS test6: timeskip calls estimate_event_duration(turn_class='timeskip')")


if __name__ == "__main__":
    unittest.main()
