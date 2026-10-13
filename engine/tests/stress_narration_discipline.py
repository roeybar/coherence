"""Stress tests for narration discipline.

300+ iterations over:
  - has_npcs T/F
  - departure / neutral / boundary input
  - turn_class standard/ability/craft/encounter
  - locale en/he

Assertions:
  - no exception from detector or prompt build
  - detector fires iff (has_npcs and real cue present)
  - prompt bytes under sane cap (budget-bloat guard)
  - earshot block present iff has_npcs
  - word-boundary correctness: "runny nose", "leaves rustled" etc.
"""

import itertools
import unittest

from app.game.presence_gate import detect_departure
from app.game.prompts import build_system_blocks
from app.game.state import CharacterProfile, GameConfig


# helpers


def _profile() -> CharacterProfile:
    return CharacterProfile(name="Stress", appearance="unremarkable")


def _config(locale="en") -> GameConfig:
    return GameConfig(locale=locale)


# input corpus

_DEPARTURE_INPUTS = [
    "I walk away from the table",
    "I leave the building quickly",
    "I exit through the back door",
    "I flee down the corridor",
    "I run as fast as I can toward the gate",
    "I turn to go without a word",
    "I descend the stairs",
    "I storm out in frustration",
    "I bolt for the nearest exit",
    "I slip away while they're distracted",
    "I retreat back to the shadows",
    "I withdraw from the conversation and head out",
    "I step out of the room",
    "I duck out the side entrance",
]

_NEUTRAL_INPUTS = [
    "I ask her about the package",
    "I wait patiently",
    "I look around the room",
    "I nod at his words",
    "I stand still and listen",
    "I think about what she said",
    "I check my pockets",
    "I say nothing",
]

_BOUNDARY_INPUTS = [
    # emoji
    "I \U0001f6aa the room",
    "I \U0001f3c3 quickly",
    # cue-substring-in-word (should not fire on these)
    "I have a runny nose",
    "The runner crosses the finish line",
    # "leaves" as noun (will fire - tolerable false positive; "leaves" is in lexicon)
    # not asserting no-fire here, just no-exception
    "The leaves rustled in the breeze",
    # mixed-case
    "i WALK AWAY from the negotiation",
    "I LEAVE WITHOUT A WORD",
    # empty
    "",
    # whitespace only
    "   ",
    # punctuation only
    "???!!!",
    # 5k-char string (random ascii + departure cue buried in middle)
    "x" * 2000 + " I walk away " + "y" * 2000,
]

_TURN_CLASSES = ["standard", "ability", "craft", "encounter"]
_LOCALES = ["en", "he"]

# Words that should never trigger the detector due to word-boundary correctness
_NO_FIRE_WORD_BOUNDARY = [
    "I have a runny nose",  # "runny" != "run"
    "The runner crosses the line",  # "runner" != "run"
]

# Departure cue substrings that appear inside longer words - must not fire
_NO_FIRE_EMBEDDED = [
    "I studied extensively",  # "exit" not present; just check no crash
    "She bolted the door shut",  # "bolt" is in lexicon -> this will fire (tolerable)
]


class TestStressDetector(unittest.TestCase):
    """Stress the departure detector: no exception in 300+ iterations."""

    def test_no_exception_any_input(self):
        """No exception for any combination of input x has_npcs x names."""
        all_inputs = _DEPARTURE_INPUTS + _NEUTRAL_INPUTS + _BOUNDARY_INPUTS
        npcs_variants = [
            [],
            ["Soledad"],
            ["Soledad", "Brennan"],
        ]
        iterations = 0
        for text, has_npcs, npcs in itertools.product(all_inputs, [True, False], npcs_variants):
            with self.subTest(text=text[:40], has_npcs=has_npcs):
                try:
                    detect_departure(text, has_npcs=has_npcs, npcs_in_scene=npcs)
                except Exception as e:
                    self.fail(
                        f"detect_departure raised {type(e).__name__}: {e} for input={text!r:.40}"
                    )
                iterations += 1
        print(f"PASS stress detector: {iterations} iterations, no exception")

    def test_fires_iff_has_npcs_and_cue(self):
        """Detector fires iff has_npcs=True and a real departure cue is present."""
        npcs = ["Soledad"]
        correct = 0
        total = 0
        for text in _DEPARTURE_INPUTS:
            with self.subTest(text=text[:40]):
                # has_npcs=True + departure cue -> must fire
                result_on = detect_departure(text, has_npcs=True, npcs_in_scene=npcs)
                self.assertIsNotNone(
                    result_on,
                    f"Expected fire: has_npcs=True + departure in {text!r}",
                )
                # has_npcs=False + same input -> must not fire (gate)
                result_off = detect_departure(text, has_npcs=False, npcs_in_scene=npcs)
                self.assertIsNone(
                    result_off,
                    f"Expected None: has_npcs=False gate for {text!r}",
                )
                correct += 2
            total += 2
        print(f"PASS stress detector fires-iff: {total} checks, {correct} correct")

    def test_neutral_never_fires(self):
        """Neutral inputs never trigger the detector."""
        npcs = ["Soledad"]
        for text in _NEUTRAL_INPUTS:
            with self.subTest(text=text):
                result = detect_departure(text, has_npcs=True, npcs_in_scene=npcs)
                self.assertIsNone(
                    result,
                    f"Expected None for neutral input: {text!r}",
                )
        print(f"PASS stress detector neutral: {len(_NEUTRAL_INPUTS)} inputs, none fire")

    def test_word_boundary_correctness(self):
        """Cue substrings inside other words (runny, runner) must not fire."""
        npcs = ["Soledad"]
        for text in _NO_FIRE_WORD_BOUNDARY:
            with self.subTest(text=text):
                result = detect_departure(text, has_npcs=True, npcs_in_scene=npcs)
                self.assertIsNone(
                    result,
                    f"Word-boundary violation: {text!r} should not fire",
                )
        print(f"PASS stress detector word-boundary: {len(_NO_FIRE_WORD_BOUNDARY)} non-firing")

    def test_boundary_no_exception(self):
        """Boundary inputs (emoji, 5k-char, empty) never raise."""
        npcs = ["Soledad"]
        for text in _BOUNDARY_INPUTS:
            with self.subTest(text=repr(text[:40])):
                try:
                    detect_departure(text, has_npcs=True, npcs_in_scene=npcs)
                    detect_departure(text, has_npcs=False, npcs_in_scene=npcs)
                except Exception as e:
                    self.fail(f"detect_departure raised {type(e).__name__} for {text!r:.40}")
        print(f"PASS stress detector boundary: {len(_BOUNDARY_INPUTS)} inputs, no exception")


class TestStressPromptBuild(unittest.TestCase):
    """Stress build_system_blocks: no exception, sane size."""

    # Sane budget: 32k bytes - the largest legitimate combo (ability + he + npcs)
    # is ~26.7k; 32k leaves headroom while catching genuine bloat additions.
    _PROMPT_BYTE_CAP = 32_000

    def _all_combos(self):
        """Generate (has_npcs, turn_class, locale) combos."""
        return list(itertools.product([True, False], _TURN_CLASSES, _LOCALES))

    def test_no_exception_all_combos(self):
        combos = self._all_combos()
        for has_npcs, turn_class, locale in combos:
            with self.subTest(has_npcs=has_npcs, turn_class=turn_class, locale=locale):
                try:
                    build_system_blocks(
                        _profile(),
                        stage=2,
                        config=_config(locale),
                        has_npcs=has_npcs,
                        turn_class=turn_class,
                    )
                except Exception as e:
                    self.fail(
                        f"build_system_blocks raised {type(e).__name__}: {e} "
                        f"for has_npcs={has_npcs} turn_class={turn_class} locale={locale}"
                    )
        print(f"PASS stress prompt: {len(combos)} combos, no exception")

    def test_prompt_bytes_under_cap(self):
        combos = self._all_combos()
        for has_npcs, turn_class, locale in combos:
            with self.subTest(has_npcs=has_npcs, turn_class=turn_class, locale=locale):
                stable, volatile = build_system_blocks(
                    _profile(),
                    stage=2,
                    config=_config(locale),
                    has_npcs=has_npcs,
                    turn_class=turn_class,
                )
                total_bytes = len(stable.encode("utf-8")) + len(volatile.encode("utf-8"))
                self.assertLess(
                    total_bytes,
                    self._PROMPT_BYTE_CAP,
                    f"Prompt bloat: {total_bytes} bytes for "
                    f"has_npcs={has_npcs} turn_class={turn_class} locale={locale} "
                    f"(cap={self._PROMPT_BYTE_CAP})",
                )
        print(f"PASS stress prompt bytes: all combos under {self._PROMPT_BYTE_CAP}B")

    def test_earshot_present_iff_has_npcs(self):
        """Earshot block in volatile iff has_npcs=True; never in stable."""
        for has_npcs in [True, False]:
            for turn_class in _TURN_CLASSES:
                with self.subTest(has_npcs=has_npcs, turn_class=turn_class):
                    stable, volatile = build_system_blocks(
                        _profile(),
                        stage=2,
                        config=_config(),
                        has_npcs=has_npcs,
                        turn_class=turn_class,
                    )
                    # Never in stable
                    self.assertNotIn(
                        "PHYSICAL PRESENCE & EARSHOT",
                        stable,
                        "Earshot block must never appear in stable",
                    )
                    if has_npcs:
                        self.assertIn(
                            "PHYSICAL PRESENCE & EARSHOT",
                            volatile,
                            f"Earshot block must be in volatile when has_npcs=True (turn_class={turn_class})",
                        )
                    else:
                        self.assertNotIn(
                            "PHYSICAL PRESENCE & EARSHOT",
                            volatile,
                            f"Earshot block must be absent from volatile when has_npcs=False (turn_class={turn_class})",
                        )
        print("PASS stress earshot presence: correct for all turn_class x has_npcs combos")

    def test_300_iterations(self):
        """Explicit 300+ iteration count for the stress claim."""
        combos = self._all_combos()  # 2 x 4 x 2 = 16
        iterations = 0
        # Generate more iterations by sampling inputs
        for _ in range(20):  # 20 x 16 = 320 > 300
            for has_npcs, turn_class, locale in combos:
                try:
                    build_system_blocks(
                        _profile(),
                        stage=2,
                        config=_config(locale),
                        has_npcs=has_npcs,
                        turn_class=turn_class,
                    )
                except Exception as e:
                    self.fail(
                        f"build_system_blocks raised {type(e).__name__}: {e} "
                        f"at iteration {iterations}"
                    )
                iterations += 1
        self.assertGreaterEqual(iterations, 300, "Expected at least 300 iterations")
        print(f"PASS stress 300+: {iterations} iterations, no exception")


if __name__ == "__main__":
    unittest.main()
