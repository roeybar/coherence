"""Tests for the call_llm_plain unpack fix in dice_gate and craft_gate.

stdlib unittest, zero real LLM calls - call_llm_plain is monkeypatched.
Runs in the test container.

Suites:
  TestDiceGateLLMUnpack - assess_dice_dc returns computed DC on valid JSON;
                            falls back to default 12 on malformed response.
  TestCraftGateLLMUnpack - assess_craft_dc returns computed DC on valid JSON;
                            falls back to default 12 on malformed response.
"""

import unittest
from unittest.mock import patch

import app.game.dice_gate as dg
import app.game.craft_gate as cg


# ---------------------------------------------------------------------------
# Minimal state stand-ins (dict interface only; both gates use .get)
# ---------------------------------------------------------------------------

_DICE_STATE = {
    "location": "dark alley",
    "player_conditions": "none",
    "scene_aggression": 40,
}

_CRAFT_STATE = {
    "tech_level": "early medieval",
    "location": "smithy",
}

_ITEM_VIEW = {
    "scene": [{"name": "iron ingot"}, {"name": "hammer"}],
    "carried": [{"name": "tongs"}],
}


# ---------------------------------------------------------------------------
# Suite 1 - dice_gate.assess_dice_dc
# ---------------------------------------------------------------------------


class TestDiceGateLLMUnpack(unittest.TestCase):
    def test_valid_json_returns_computed_dc(self):
        """call_llm_plain returns a 2-tuple with a valid JSON response ->
        assess_dice_dc must return a DiceAssessment whose .dc equals the
        value from the LLM, not the hard-coded fallback of 12."""
        llm_response = (
            '{"dc":18,"skill":"ambush","stat":"DEX","flavor":"The guard tenses."}',
            object(),  # usage sentinel - must be ignored
        )

        with patch.object(dg, "call_llm_plain", return_value=llm_response) as mock_fn:
            result = dg.assess_dice_dc("attack the guard", _DICE_STATE)

        # The fix: the call site unpacks the 2-tuple and uses text.strip
        self.assertEqual(
            result.dc,
            18,
            f"expected dc=18 (from LLM JSON), got {result.dc}; unpack fix may be missing",
        )
        self.assertEqual(result.skill, "ambush")
        self.assertEqual(result.stat, "DEX")
        self.assertEqual(result.flavor, "The guard tenses.")
        self.assertEqual(result.difficulty_label, "hard")

        # Verify the call used correct positional + keyword args
        args, kwargs = mock_fn.call_args
        self.assertEqual(args[0], "", "first arg (system) must be empty string, not MODEL_HAIKU")
        self.assertIsInstance(args[1], list, "second arg (messages) must be a list")
        self.assertEqual(args[1][0]["role"], "user")
        self.assertIn("attack the guard", args[1][0]["content"])
        self.assertIn("model", kwargs, "MODEL_HAIKU must be passed as keyword 'model='")
        self.assertEqual(kwargs["max_tokens"], 100)
        print(
            f"PASS dice valid JSON: dc={result.dc} skill={result.skill!r} "
            f"stat={result.stat!r} label={result.difficulty_label!r}"
        )

    def test_fallback_on_malformed_response(self):
        """call_llm_plain returns a 2-tuple with non-JSON text ->
        the try/except catches the parse error and the fallback defaults fire."""
        llm_response = ("not valid json at all {{", object())

        with patch.object(dg, "call_llm_plain", return_value=llm_response):
            result = dg.assess_dice_dc("punch the door", _DICE_STATE)

        self.assertEqual(result.dc, 12, f"expected fallback dc=12, got {result.dc}")
        self.assertEqual(result.skill, "check")
        self.assertEqual(result.stat, "none")
        self.assertEqual(result.flavor, "The outcome is uncertain.")
        print(f"PASS dice fallback: dc={result.dc} skill={result.skill!r} stat={result.stat!r}")

    def test_fallback_on_missing_dc_field(self):
        """JSON returned but 'dc' key absent -> default dc=12."""
        llm_response = (
            '{"skill":"dodge","stat":"DEX","flavor":"Quick move."}',
            object(),
        )
        with patch.object(dg, "call_llm_plain", return_value=llm_response):
            result = dg.assess_dice_dc("dodge the blow", _DICE_STATE)

        # data.get("dc", 12) -> 12
        self.assertEqual(result.dc, 12)
        self.assertEqual(result.skill, "dodge")
        print(f"PASS dice missing dc field: dc={result.dc} (default)")

    def test_dc_clamped_to_1_25(self):
        """DC values outside [1,25] are clamped."""
        llm_response = ('{"dc":99,"skill":"leap","stat":"STR","flavor":"Far."}', object())
        with patch.object(dg, "call_llm_plain", return_value=llm_response):
            result = dg.assess_dice_dc("leap the chasm", _DICE_STATE)
        self.assertEqual(result.dc, 25)
        print(f"PASS dice dc clamped: dc=99 -> {result.dc}")


# ---------------------------------------------------------------------------
# Suite 2 - craft_gate.assess_craft_dc
# ---------------------------------------------------------------------------


class TestCraftGateLLMUnpack(unittest.TestCase):
    def test_valid_json_returns_computed_dc(self):
        """call_llm_plain returns a 2-tuple with a valid JSON response ->
        assess_craft_dc must return a CraftAssessment whose .dc equals the
        value from the LLM, not the hard-coded fallback of 12."""
        llm_response = (
            '{"dc":7,"target":"crude iron blade","inputs_committed":["iron ingot","hammer"],'
            '"flavor":"The forge glows hot."}',
            object(),
        )

        with patch.object(cg, "call_llm_plain", return_value=llm_response) as mock_fn:
            result = cg.assess_craft_dc("forge a blade", _ITEM_VIEW, _CRAFT_STATE)

        self.assertEqual(
            result.dc,
            7,
            f"expected dc=7 (from LLM JSON), got {result.dc}; unpack fix may be missing",
        )
        self.assertEqual(result.target, "crude iron blade")
        self.assertEqual(result.inputs_committed, ["iron ingot", "hammer"])
        self.assertEqual(result.flavor, "The forge glows hot.")
        self.assertEqual(result.difficulty_label, "trivial")

        args, kwargs = mock_fn.call_args
        self.assertEqual(args[0], "", "first arg (system) must be empty string, not MODEL_HAIKU")
        self.assertIsInstance(args[1], list, "second arg (messages) must be a list")
        self.assertEqual(args[1][0]["role"], "user")
        self.assertIn("model", kwargs, "MODEL_HAIKU must be passed as keyword 'model='")
        self.assertEqual(kwargs["max_tokens"], 120)
        print(
            f"PASS craft valid JSON: dc={result.dc} target={result.target!r} "
            f"inputs={result.inputs_committed} label={result.difficulty_label!r}"
        )

    def test_fallback_on_malformed_response(self):
        """call_llm_plain returns a 2-tuple with non-JSON text ->
        fallback defaults fire."""
        llm_response = (">>> BROKEN <<<", object())

        with patch.object(cg, "call_llm_plain", return_value=llm_response):
            result = cg.assess_craft_dc("craft a sword", _ITEM_VIEW, _CRAFT_STATE)

        self.assertEqual(result.dc, 12, f"expected fallback dc=12, got {result.dc}")
        self.assertEqual(result.target, "crafted item")
        self.assertEqual(result.flavor, "The work requires care and skill.")
        print(f"PASS craft fallback: dc={result.dc} target={result.target!r}")

    def test_fallback_inputs_from_available_items(self):
        """On parse failure, inputs_committed defaults to first 2 available items."""
        llm_response = ("not json", object())

        with patch.object(cg, "call_llm_plain", return_value=llm_response):
            result = cg.assess_craft_dc("combine items", _ITEM_VIEW, _CRAFT_STATE)

        # Fallback: names[:2] from all_items = ["iron ingot", "hammer"]
        self.assertEqual(result.inputs_committed, ["iron ingot", "hammer"])
        print(f"PASS craft fallback inputs: {result.inputs_committed}")

    def test_inputs_committed_capped_at_four(self):
        """inputs_committed is capped at 4 items even if LLM returns more."""
        llm_response = (
            '{"dc":10,"target":"bundle","inputs_committed":'
            '["a","b","c","d","e","f"],"flavor":"Many parts."}',
            object(),
        )
        with patch.object(cg, "call_llm_plain", return_value=llm_response):
            result = cg.assess_craft_dc("assemble parts", _ITEM_VIEW, _CRAFT_STATE)
        self.assertLessEqual(len(result.inputs_committed), 4)
        print(f"PASS craft inputs cap: {len(result.inputs_committed)} items (from 6 returned)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()


class TestGateDcCallsAreBilled(unittest.TestCase):
    """a gate's own DC-assessment call is real spend that lands
    immediately before the gate raises. fixed the tuple-unpack so the call
    works; it stayed free because the usage was dropped on the floor."""

    def _usage(self):
        class _U:
            input_tokens = 800
            output_tokens = 40
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        return _U()

    def test_dice_dc_call_lands_in_aux_rows(self):
        from app.game import dice_gate

        rows = []
        with patch.object(
            dice_gate,
            "call_llm_plain",
            return_value=('{"dc":14,"skill":"climb","stat":"STR","flavor":"sheer"}', self._usage()),
        ):
            dice_gate.assess_dice_dc("climb the wall", {}, rows)
        self.assertEqual(len(rows), 1, "dice DC call was not accumulated - free tokens")
        self.assertEqual(rows[0]["id"], "aux:dice_dc")
        self.assertEqual(rows[0]["tokens_in"], 800)

    def test_craft_dc_call_lands_in_aux_rows(self):
        from app.game import craft_gate

        rows = []
        with patch.object(
            craft_gate,
            "call_llm_plain",
            return_value=(
                '{"dc":11,"target":"dagger","inputs_committed":[],"flavor":"forge"}',
                self._usage(),
            ),
        ):
            craft_gate.assess_craft_dc("forge a dagger", {}, {}, rows)
        self.assertEqual(len(rows), 1, "craft DC call was not accumulated - free tokens")
        self.assertEqual(rows[0]["id"], "aux:craft_dc")

    def test_no_aux_list_is_a_no_op(self):
        """Callers that don't bill (tools, tests) must still work."""
        from app.game import dice_gate

        with patch.object(
            dice_gate,
            "call_llm_plain",
            return_value=('{"dc":9,"skill":"x","stat":"none","flavor":"y"}', self._usage()),
        ):
            a = dice_gate.assess_dice_dc("x", {})  # no rows arg
        self.assertEqual(a.dc, 9)
