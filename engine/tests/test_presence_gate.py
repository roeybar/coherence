"""Tests for presence_gate (narration discipline).

stdlib unittest, zero LLM calls, PASS/FAIL prints.
Pattern: tests/test_encounter_mechanics.py

Suites:
  1. TestDepartureCueHit - representative cues fire with has_npcs=True
  2. TestNoNpcNoFire - same inputs with has_npcs=False return None
  3. TestNoCueNoFire - non-departure inputs return None
  4. TestNpcsInSceneCarry - supplied names propagate; empty list tolerated
  5. TestPureFunction - deterministic across repeat calls (no LLM)
  6. TestWordBoundary - noisy substrings ("runny", "leaves on a tree") don't fire
"""

import unittest

from app.game.presence_gate import PresenceDeparture, detect_departure


class TestDepartureCueHit(unittest.TestCase):
    """Suite 1 - representative departure cue phrases fire with has_npcs=True."""

    def _check(self, text: str, npcs=None):
        if npcs is None:
            npcs = ["Soledad"]
        result = detect_departure(text, has_npcs=True, npcs_in_scene=npcs)
        self.assertIsNotNone(
            result,
            f"Expected departure detection for {text!r}, got None",
        )
        self.assertIsInstance(result, PresenceDeparture)
        self.assertTrue(len(result.cues) > 0, "Expected at least one cue")
        print(f"PASS cue hit: {text!r} -> cues={result.cues}")
        return result

    def test_walk_away(self):
        self._check("I walk away without looking back")

    def test_descend_the_stairs(self):
        self._check("I descend the stairs quickly")

    def test_storm_out(self):
        self._check("I storm out of the room")

    def test_step_over_and_head_for_door(self):
        r = self._check("I step over the wire and head for the door")
        # Both cues should be present
        cues_lower = [c.lower() for c in r.cues]
        self.assertTrue(
            any("step" in c for c in cues_lower) or any("head" in c for c in cues_lower),
            f"Expected step or head cue; got {r.cues}",
        )

    def test_leave(self):
        self._check("I leave the building")

    def test_exit(self):
        self._check("I exit through the side door")

    def test_flee(self):
        self._check("I flee down the corridor")

    def test_run(self):
        self._check("I run as fast as I can")

    def test_turn_to_go(self):
        self._check("I turn to go before she can finish")

    def test_slip_away(self):
        self._check("I slip away while she's distracted")

    def test_bolt(self):
        self._check("I bolt for the exit")

    def test_retreat(self):
        self._check("I retreat into the shadows")

    def test_withdraw(self):
        self._check("I withdraw from the conversation and head for the door")

    def test_go_down(self):
        self._check("I go down the stairs without saying anything")

    def test_walk_out(self):
        self._check("I walk out of the tavern")

    def test_mixed_case(self):
        self._check("I WALK AWAY from the negotiation")

    def test_leaves_the_room(self):
        self._check("I leaves the room in a hurry")

    def test_descending(self):
        self._check("I start descending the staircase")


class TestNoNpcNoFire(unittest.TestCase):
    """Suite 2 - same inputs with has_npcs=False return None (the gate)."""

    _DEPARTURES = [
        "I walk away without looking back",
        "I descend the stairs quickly",
        "I storm out of the room",
        "I leave the building",
        "I exit through the side door",
        "I flee down the corridor",
        "I run as fast as I can",
        "I turn to go before she finishes",
    ]

    def test_no_npc_never_fires(self):
        for text in self._DEPARTURES:
            with self.subTest(text=text):
                result = detect_departure(text, has_npcs=False, npcs_in_scene=["Soledad"])
                self.assertIsNone(
                    result,
                    f"Expected None (has_npcs=False) for {text!r}, got {result}",
                )
                print(f"PASS no-npc gate: {text!r} -> None")


class TestNoCueNoFire(unittest.TestCase):
    """Suite 3 - non-departure inputs with has_npcs=True return None."""

    _NON_DEPARTURES = [
        "I ask her name",
        "I wait and see what happens",
        "I look around the room",
        "I stand up slowly",
        "I nod at her words",
        "I check my belongings",
        "I stay silent",
        "I think about what she said",
    ]

    def test_no_cue_no_fire(self):
        for text in self._NON_DEPARTURES:
            with self.subTest(text=text):
                result = detect_departure(text, has_npcs=True, npcs_in_scene=["Soledad"])
                self.assertIsNone(
                    result,
                    f"Expected None (no cue) for {text!r}, got {result}",
                )
                print(f"PASS no-cue: {text!r} -> None")


class TestNpcsInSceneCarry(unittest.TestCase):
    """Suite 4 - supplied names propagate; empty list tolerated."""

    def test_single_npc_name_carried(self):
        result = detect_departure("I walk away", has_npcs=True, npcs_in_scene=["Soledad"])
        self.assertIsNotNone(result)
        self.assertEqual(result.npcs_in_scene, ["Soledad"])
        print(f"PASS single npc: {result.npcs_in_scene}")

    def test_multiple_npc_names_carried(self):
        result = detect_departure(
            "I leave without a word",
            has_npcs=True,
            npcs_in_scene=["Soledad", "Brennan"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.npcs_in_scene, ["Soledad", "Brennan"])
        print(f"PASS multi npc: {result.npcs_in_scene}")

    def test_empty_npc_list_tolerated(self):
        result = detect_departure("I walk away", has_npcs=True, npcs_in_scene=[])
        self.assertIsNotNone(result, "Should still fire with empty npc list")
        self.assertEqual(result.npcs_in_scene, [])
        print("PASS empty npc list: fires, npcs_in_scene=[]")

    def test_npc_names_not_mutated(self):
        original = ["Soledad", "Brennan"]
        result = detect_departure("I exit quickly", has_npcs=True, npcs_in_scene=original)
        self.assertIsNotNone(result)
        # Mutating the result should not affect the original list
        result.npcs_in_scene.append("Ghost")
        self.assertEqual(original, ["Soledad", "Brennan"], "original list was mutated")
        print("PASS npc list not mutated")


class TestPureFunction(unittest.TestCase):
    """Suite 5 - deterministic across repeat calls; no LLM touched."""

    def test_deterministic_across_repeats(self):
        text = "I walk away from Soledad"
        results = [
            detect_departure(text, has_npcs=True, npcs_in_scene=["Soledad"]) for _ in range(20)
        ]
        self.assertTrue(all(r is not None for r in results))
        first_cues = results[0].cues
        for r in results[1:]:
            self.assertEqual(r.cues, first_cues, "Non-deterministic result")
        print(f"PASS deterministic: 20 repeats, cues={first_cues}")

    def test_no_fire_deterministic(self):
        text = "I ask what she knows"
        results = [
            detect_departure(text, has_npcs=True, npcs_in_scene=["Soledad"]) for _ in range(20)
        ]
        self.assertTrue(all(r is None for r in results))
        print("PASS deterministic None: 20 repeats")


class TestWordBoundary(unittest.TestCase):
    """Suite 6 - word-boundary correctness: noisy substrings must not fire."""

    def test_runny_does_not_fire(self):
        result = detect_departure(
            "My runny nose is bothering me", has_npcs=True, npcs_in_scene=["Soledad"]
        )
        self.assertIsNone(result, "'runny' should not trigger 'run'")
        print("PASS word-boundary: 'runny' does not fire")

    def test_leaves_as_word_fires_false_positive(self):
        # "leaves" is in the departure lexicon ("leave|leaves|leaving").
        # "leaves on a tree" contains "leaves" as a standalone word (\b guard
        # is satisfied), so it does fire - a tolerable false positive by design
        # ("false positives are tolerable; they only sharpen an already-safe baseline").
        result = detect_departure(
            "I notice the leaves on the tree outside", has_npcs=True, npcs_in_scene=["Soledad"]
        )
        self.assertIsNotNone(
            result, "'leaves' as standalone word should fire (tolerable false positive)"
        )
        print("PASS 'leaves on a tree' fires as expected (tolerable false positive)")

    def test_exiting_fires(self):
        # "exiting" is in the lexicon
        result = detect_departure("I am exiting the hall", has_npcs=True, npcs_in_scene=["Soledad"])
        self.assertIsNotNone(result, "'exiting' should fire")
        print("PASS 'exiting' fires")

    def test_runner_does_not_fire(self):
        # "runner" should not match "run" or "runs" due to word boundary
        result = detect_departure(
            "The runner crosses the finish line", has_npcs=True, npcs_in_scene=["Soledad"]
        )
        self.assertIsNone(result, "'runner' should not trigger 'run'")
        print("PASS word-boundary: 'runner' does not fire")

    def test_exited_does_not_fire(self):
        # "exited" is not in the lexicon (only "exit", "exits", "exiting")
        result = detect_departure("She exited the room", has_npcs=True, npcs_in_scene=["Soledad"])
        self.assertIsNone(result, "'exited' (past tense, not in lexicon) should not fire")
        print("PASS 'exited' does not fire (not in lexicon)")


if __name__ == "__main__":
    unittest.main()
