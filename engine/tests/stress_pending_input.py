"""Stress tests for pending_input persistence and suggested_moves.

Pattern: test_narration_discipline.py.

Suites:
  A. TestBoundaryDeltaInjection - 500 iterations: guarded fields never change,
     legit field always applies, no cross-field bleed.
  B. TestVolumeRoundtrip - 1000 saves with varied pending_input through
     save_to_doc/doc_to_save; integrity + no state-field mutation.
  C. TestMalformedLlmFuzz - 200 malformed call_messages returns; never
     raises, suggested_moves always list <=3 strings or [], never stale.
"""

import random
import string
import unittest
from unittest.mock import patch

from app.game.campaign_clock import estimate_event_duration
from app.game.state import (
    CharacterProfile,
    GameConfig,
    GameSave,
    WorldState,
    doc_to_save,
    save_to_doc,
)


# helpers


def _rnd_str(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _make_gs(**state_kw) -> GameSave:
    gs = GameSave()
    gs.profile = CharacterProfile()
    gs.config = GameConfig()
    gs.state = WorldState(location="stress_loc", **state_kw)
    gs.history = []
    gs.turn = 0
    gs.undo_stack = []
    gs.session_in = 0
    gs.session_out = 0
    gs.checkpoint_state = {}
    gs.checkpoint_history = []
    gs.checkpoint_turn = 0
    gs.read_only = False
    gs.shadow_of = None
    return gs


# A. Boundary delta injection

_PROTECTED_GUARD = [
    "pending_input",
    "awaiting_reply",
    "npcs",
]

_LEGIT_LOCATIONS = [
    "forest",
    "dungeon",
    "cave",
    "market",
    "castle",
    "port",
    "tower",
    "ruins",
    "temple",
    "sewers",
    "vault",
    "mine",
    "keep",
]


class TestBoundaryDeltaInjection(unittest.TestCase):
    """500 iterations: guarded fields never changed by apply_delta, legit field always applies."""

    def test_guarded_fields_never_change_legit_always_applies(self):
        original_pending = {"text": "original_pending", "gate_kind": "dice_roll"}
        original_awaiting = {"npc_id": "guard", "question": "Who are you?", "attempts": 0}
        original_npcs = {"guard": {"id": "guard", "name": "Guard"}}

        injections_tried = 0
        legit_applied = 0

        for i in range(500):
            loc = random.choice(_LEGIT_LOCATIONS) + f"_{i}"
            state = WorldState(
                location="start",
                pending_input=dict(original_pending),
                awaiting_reply=dict(original_awaiting),
                npcs=dict(original_npcs),
            )

            # Build boundary delta: try to overwrite all three guarded fields
            # plus a legit field
            delta = {
                "pending_input": {"text": f"INJECTED_{i}", "gate_kind": "hack"},
                "awaiting_reply": {"npc_id": "INJECTED", "question": "hacked"},
                "npcs": {"guard": "REPLACED"},
                "location": loc,
            }

            with self.subTest(i=i, loc=loc):
                state.apply_delta(delta)

                # Guarded fields must be unchanged
                self.assertEqual(
                    state.get("pending_input"),
                    original_pending,
                    f"[iter {i}] pending_input was mutated by apply_delta",
                )
                self.assertEqual(
                    state.get("awaiting_reply"),
                    original_awaiting,
                    f"[iter {i}] awaiting_reply was mutated by apply_delta",
                )
                self.assertEqual(
                    state.get("npcs"),
                    original_npcs,
                    f"[iter {i}] npcs was mutated by apply_delta",
                )

                # Legit field must have been applied
                self.assertEqual(
                    state.location,
                    loc,
                    f"[iter {i}] location was not applied by apply_delta",
                )
                legit_applied += 1

            injections_tried += 1

        print(
            f"PASS stress-A: {injections_tried} boundary deltas - "
            f"guarded fields intact, {legit_applied} legit fields applied"
        )

    def test_guarded_fields_with_schema(self):
        """With a schema that includes pending_input, guard still prevents write."""
        schema = [
            {"name": "location", "type": "str"},
            {"name": "pending_input", "type": "dict"},
        ]
        original = {"text": "original"}
        failures = 0

        for i in range(100):
            state = WorldState(location="base", pending_input=dict(original))
            state.apply_delta(
                {
                    "pending_input": {"text": "INJECTED"},
                    "location": f"loc_{i}",
                },
                schema=schema,
            )
            if state.get("pending_input") != original:
                failures += 1

        self.assertEqual(
            failures, 0, f"{failures} schema iterations allowed pending_input mutation"
        )
        print("PASS stress-A2: schema guard blocks pending_input in 100 iters")


# B. Volume round-trip

_GATE_KINDS = [
    "moderation",
    "addressee_ambiguous",
    "ability_intent",
    "reframe",
    "world_rule",
    "craft_roll",
    "dice_roll",
    "escalation",
]


class TestVolumeRoundtrip(unittest.TestCase):
    """1000 saves with varied pending_input through save_to_doc/doc_to_save."""

    def test_pending_input_integrity_1000_saves(self):
        integrity_failures = 0
        mutation_failures = 0

        for i in range(1000):
            gate_kind = random.choice(_GATE_KINDS)
            # Vary the pending_input shape (None, short text, long text, extra keys)
            variant = i % 5
            if variant == 0:
                pending = None
            elif variant == 1:
                pending = {
                    "text": f"action_{i}",
                    "gate_kind": gate_kind,
                    "at_turn": i % 50,
                    "created_at": "2026-07-12T00:00:00+00:00",
                }
            elif variant == 2:
                pending = {
                    "text": "x" * 4000,
                    "gate_kind": gate_kind,
                    "at_turn": 0,
                    "created_at": "2026-07-12T00:00:00+00:00",
                }
            elif variant == 3:
                pending = {
                    "text": "",
                    "gate_kind": gate_kind,
                    "at_turn": 999,
                    "created_at": "2026-07-12T00:00:00+00:00",
                    "extra_key": "extra_value",
                }
            else:
                # Nested structure
                pending = {
                    "text": "nested",
                    "gate_kind": gate_kind,
                    "at_turn": i,
                    "created_at": "2026-07-12T00:00:00+00:00",
                    "meta": {"source": "test"},
                }

            loc = f"loc_{i}"
            year = i % 100
            state_kw = {"year": year}
            if pending is not None:
                state_kw["pending_input"] = pending

            gs = _make_gs(**state_kw)
            gs.state.location = loc

            doc = save_to_doc(gs)
            restored = doc_to_save(doc)

            # Verify pending_input integrity
            expected_pi = pending  # None if variant == 0
            actual_pi = restored.state.get("pending_input")
            if expected_pi is None:
                if actual_pi is not None:
                    integrity_failures += 1
            else:
                if actual_pi != expected_pi:
                    integrity_failures += 1

            # Verify other state fields were not mutated
            if restored.state.location != loc:
                mutation_failures += 1
            if restored.state.get("year") != year:
                mutation_failures += 1

        self.assertEqual(
            integrity_failures,
            0,
            f"{integrity_failures} round-trip integrity failures in 1000 saves",
        )
        self.assertEqual(
            mutation_failures, 0, f"{mutation_failures} cross-field mutation failures in 1000 saves"
        )
        print(
            "PASS stress-B: 1000 round-trips - "
            "pending_input integrity intact, no cross-field mutation"
        )


# C. Malformed-LLM fuzz


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeResp:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


_MALFORMED_RESPONSES = [
    # truncated JSON (parse fails -> clear-first [] wins)
    '{"suggested_moves": ["a", "b"',
    # JSON with non-list suggested_moves (string)
    '{"suggested_moves": "just a string"}',
    # >3 items (truncated by slice in the code)
    '{"suggested_moves": ["a", "b", "c", "d", "e"]}',
    # empty list
    '{"suggested_moves": []}',
    # null / None value
    '{"suggested_moves": null}',
    # ```json fences (stripped and parsed)
    '```json\n{"suggested_moves": ["x", "y", "z"]}\n```',
    # non-strings in list (filtered out, result is [])
    '{"suggested_moves": [1, 2, 3]}',
    # completely invalid JSON
    "not json at all",
    # empty response
    "",
    # bare integer (parse succeeds but no .get -> except -> [])
    "42",
]


class TestMalformedLlmFuzz(unittest.TestCase):
    """200 malformed LLM responses: never raises, result always valid list."""

    def test_malformed_llm_never_raises_and_result_always_valid(self):
        """estimate_event_duration must never raise and always produce a valid list."""
        iterations = 0
        for resp_text in _MALFORMED_RESPONSES * 20:  # 10 x 20 = 200
            state = WorldState(location="fuzz_loc")
            state.suggested_moves = ["prior_stale_move"]  # pre-seed stale

            with self.subTest(resp=resp_text[:40]):
                with patch("app.llm_client.call_messages", return_value=_FakeResp(resp_text)):
                    try:
                        estimate_event_duration(
                            "Something happened in the scene.",
                            clock_before={},
                            turn=1,
                            state=state,
                            turn_class="standard",
                        )
                    except Exception as e:
                        self.fail(
                            f"estimate_event_duration raised {type(e).__name__}: {e} "
                            f"for response: {resp_text!r:.50}"
                        )

                moves = state.suggested_moves

                # Must always be a list
                self.assertIsInstance(
                    moves, list, f"suggested_moves must be a list, got {type(moves).__name__}"
                )
                # Must never have more than 3 items
                self.assertLessEqual(
                    len(moves), 3, f"suggested_moves must have <=3 items, got {len(moves)}: {moves}"
                )
                # Each item must be a string
                for m in moves:
                    self.assertIsInstance(
                        m, str, f"each move must be str, got {type(m).__name__}: {m!r}"
                    )
                # Must not carry the stale value from before the call
                self.assertNotIn(
                    "prior_stale_move",
                    moves,
                    "stale move must never survive a call to estimate_event_duration",
                )

            iterations += 1

        print(
            f"PASS stress-C: {iterations} malformed-LLM iterations - "
            "never raises, always valid list <=3 strings, never stale"
        )

    def test_malformed_llm_never_raises_ability_turns(self):
        """Malformed responses on ability turns also behave correctly (no stale)."""
        for resp_text in _MALFORMED_RESPONSES * 2:  # quick subset
            state = WorldState(location="ability_loc")
            state.suggested_moves = ["old_ability_move"]

            with patch("app.llm_client.call_messages", return_value=_FakeResp(resp_text)):
                try:
                    estimate_event_duration(
                        "You activate your ability.",
                        clock_before={},
                        turn=2,
                        state=state,
                        turn_class="ability",
                    )
                except Exception as e:
                    self.fail(
                        f"estimate_event_duration raised on ability turn: {type(e).__name__}: {e}"
                    )

            moves = state.suggested_moves
            self.assertIsInstance(moves, list)
            self.assertLessEqual(len(moves), 3)
            self.assertNotIn("old_ability_move", moves, "ability turn must never carry stale moves")

        print("PASS stress-C2: malformed responses on ability turns - no stale, no raise")


if __name__ == "__main__":
    unittest.main()
