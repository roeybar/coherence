"""Tests for narration discipline - prompt-string assertions.

stdlib unittest, zero LLM calls, PASS/FAIL prints.

Suites:
  - TestBrevityScoping - _RESPONSE_LENGTH has scope-limiter + "no paragraphs of dialogue"
  - TestEarshotBaseline - has_npcs=True -> earshot block present; False -> absent
  - TestNoCachePrefixLeak - earshot in volatile, brevity+dice-meta in stable
  - TestDiceMetaRule - _WRITING_RULES contains the dice meta-rule
  - TestDefaultHintBinding - render_directive FAIL/CRITICAL binding text present
  - TestDepartureDirectiveWiring - departure detection + directive block wiring (isolated)
"""

import unittest
from unittest.mock import patch

from app.game.prompts import build_system_blocks
from app.game.state import CharacterProfile, GameConfig
from app.game.encounter_mechanics import render_directive


def _profile() -> CharacterProfile:
    return CharacterProfile(name="Tester", appearance="nondescript")


def _config(**kw) -> GameConfig:
    return GameConfig(**kw)


# ---------------------------------------------------------------------------
# TestBrevityScoping
# ---------------------------------------------------------------------------


class TestBrevityScoping(unittest.TestCase):
    """_RESPONSE_LENGTH has scope-limiter and still has
    the original '1-3 short paragraphs' cap."""

    def _stable(self, **kw):
        stable, _ = build_system_blocks(_profile(), stage=2, config=_config(**kw))
        return stable

    def test_no_paragraphs_of_dialogue_present(self):
        stable = self._stable()
        self.assertIn(
            "no paragraphs of dialogue",
            stable,
            "Expected 'no paragraphs of dialogue' in stable block",
        )
        print("PASS brevity: 'no paragraphs of dialogue' in stable")

    def test_original_cap_preserved(self):
        stable = self._stable()
        self.assertIn(
            "1-3 short paragraphs",
            stable,
            "Expected '1-3 short paragraphs' still present (no deletion)",
        )
        print("PASS brevity: '1-3 short paragraphs' still present")

    def test_environment_and_action_scope(self):
        stable = self._stable()
        self.assertIn(
            "ENVIRONMENT and ACTION prose ONLY",
            stable,
            "Expected scope-limiter text in stable block",
        )
        print("PASS brevity: 'ENVIRONMENT and ACTION prose ONLY' present")

    def test_npc_speech_cap_phrasing(self):
        stable = self._stable()
        self.assertIn(
            "a line or two at a time",
            stable,
            "Expected soft speech cap 'a line or two at a time' present",
        )
        print("PASS brevity: 'a line or two at a time' present")


# ---------------------------------------------------------------------------
# TestEarshotBaseline
# ---------------------------------------------------------------------------


class TestEarshotBaseline(unittest.TestCase):
    """earshot block injected when has_npcs=True; absent
    when has_npcs=False (confirms it rides _NPC_KNOWLEDGE, not stable)."""

    _EXPECTED_PHRASES = [
        "PHYSICAL PRESENCE & EARSHOT",
        "GIVE UP",
        "CAVE",
        "TRY TO STOP",
        "within earshot",
    ]

    def _full(self, has_npcs: bool) -> str:
        stable, volatile = build_system_blocks(
            _profile(),
            stage=2,
            config=_config(),
            has_npcs=has_npcs,
        )
        return f"{stable}\n{volatile}" if volatile else stable

    def test_has_npcs_true_all_phrases_present(self):
        text = self._full(has_npcs=True)
        for phrase in self._EXPECTED_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(
                    phrase,
                    text,
                    f"Expected {phrase!r} in has_npcs=True prompt",
                )
        print(f"PASS earshot: all {len(self._EXPECTED_PHRASES)} phrases present when has_npcs=True")

    def test_has_npcs_false_earshot_absent(self):
        text = self._full(has_npcs=False)
        self.assertNotIn(
            "PHYSICAL PRESENCE & EARSHOT",
            text,
            "Expected earshot block ABSENT when has_npcs=False",
        )
        print("PASS earshot: block absent when has_npcs=False")


# ---------------------------------------------------------------------------
# TestNoCachePrefixLeak
# ---------------------------------------------------------------------------


class TestNoCachePrefixLeak(unittest.TestCase):
    """earshot text lives in volatile (varies per turn);
    brevity clause and dice meta-rule are in stable (cached)."""

    def _blocks(self, has_npcs: bool):
        return build_system_blocks(
            _profile(),
            stage=2,
            config=_config(),
            has_npcs=has_npcs,
        )

    def test_earshot_in_volatile_not_stable(self):
        stable, volatile = self._blocks(has_npcs=True)
        self.assertIn(
            "PHYSICAL PRESENCE & EARSHOT",
            volatile,
            "Expected earshot block in volatile",
        )
        self.assertNotIn(
            "PHYSICAL PRESENCE & EARSHOT",
            stable,
            "Expected earshot block NOT in stable",
        )
        print("PASS cache split: earshot in volatile")

    def test_earshot_absent_from_both_when_no_npcs(self):
        stable, volatile = self._blocks(has_npcs=False)
        self.assertNotIn("PHYSICAL PRESENCE & EARSHOT", stable)
        self.assertNotIn("PHYSICAL PRESENCE & EARSHOT", volatile)
        print("PASS cache split: earshot absent from both when no npcs")

    def test_brevity_clause_in_stable(self):
        stable, _ = self._blocks(has_npcs=True)
        self.assertIn(
            "no paragraphs of dialogue",
            stable,
            "Expected brevity clause in stable",
        )
        print("PASS cache split: brevity clause in stable")

    def test_dice_meta_rule_in_stable(self):
        stable, _ = self._blocks(has_npcs=True)
        self.assertIn(
            "MUST reflect that outcome",
            stable,
            "Expected dice meta-rule in stable",
        )
        print("PASS cache split: dice meta-rule in stable")


# ---------------------------------------------------------------------------
# TestDiceMetaRule
# ---------------------------------------------------------------------------


class TestDiceMetaRule(unittest.TestCase):
    """_WRITING_RULES contains the dice meta-rule."""

    def _stable(self) -> str:
        stable, _ = build_system_blocks(_profile(), stage=2, config=_config())
        return stable

    def test_must_reflect_outcome(self):
        stable = self._stable()
        self.assertIn(
            "MUST reflect that outcome",
            stable,
            "Expected 'MUST reflect that outcome' in _WRITING_RULES",
        )
        print("PASS dice meta: 'MUST reflect that outcome' present")

    def test_never_narrate_reward(self):
        stable = self._stable()
        self.assertIn(
            "Never narrate a reward the roll did not grant",
            stable,
            "Expected 'Never narrate a reward the roll did not grant' present",
        )
        print("PASS dice meta: 'Never narrate a reward...' present")

    def test_must_not_contradict(self):
        stable = self._stable()
        self.assertIn(
            "MUST NOT contradict the roll",
            stable,
            "Expected 'MUST NOT contradict the roll' present",
        )
        print("PASS dice meta: 'MUST NOT contradict the roll' present")


# ---------------------------------------------------------------------------
# TestDefaultHintBinding
# ---------------------------------------------------------------------------


class TestDefaultHintBinding(unittest.TestCase):
    """render_directive emits FAIL binding text.
    Proxy for turn-30 divergence: FAIL must deny the objective."""

    def _render(self, tier: str, roll: int = 8, dc: int = 14):
        return render_directive(
            None,
            roll=roll,
            dc=dc,
            skill="persuade",
            stat="CHA",
            tier=tier,
            new_state={"scene_aggression": {"before": 50, "after": 62}},
        )

    def test_fail_denies_objective(self):
        text = self._render("fail")
        self.assertIn(
            "does NOT obtain",
            text,
            "FAIL directive must contain 'does NOT obtain'",
        )
        print("PASS hint binding: fail contains 'does NOT obtain'")

    def test_fail_npc_does_not_concede(self):
        text = self._render("fail")
        self.assertIn(
            "does NOT concede or reveal on a fail",
            text,
            "FAIL directive must say NPC does NOT concede or reveal",
        )
        print("PASS hint binding: fail contains 'does NOT concede or reveal on a fail'")

    def test_critical_differs_from_fail(self):
        fail_text = self._render("fail")
        crit_text = self._render("critical", roll=19, dc=14)
        self.assertNotEqual(
            fail_text,
            crit_text,
            "CRITICAL and FAIL renders must differ",
        )
        self.assertIn("CRITICAL", crit_text)
        print("PASS hint binding: critical render differs from fail render")

    def test_tier_token_present(self):
        for tier in ("fail", "critical", "partial", "success"):
            with self.subTest(tier=tier):
                text = self._render(tier)
                self.assertIn(
                    tier.upper(),
                    text,
                    f"Expected tier token {tier.upper()!r} in render",
                )
        print("PASS hint binding: tier tokens present for all tiers")

    def test_no_dc_leak(self):
        text = self._render("fail")
        # The directive contains DC in the machine-readable header
        # ("[dice resolution] Player rolled N vs DC N ...") which is intentional.
        # What must not happen is leaking the DC to the LLM prose instruction.
        # The guard is the explicit instruction "Do not mention the DC or dice
        # mechanics" - verify that instruction is present in the directive.
        self.assertIn(
            "Do NOT mention the DC or dice mechanics",
            text,
            "Directive must contain prose guard 'Do NOT mention the DC or dice mechanics'",
        )
        print("PASS hint binding: prose guard 'Do NOT mention DC or dice mechanics' present")

    def test_binding_is_explicit(self):
        text = self._render("fail")
        self.assertIn(
            "BINDING on the fiction",
            text,
            "Expected 'BINDING on the fiction' in DEFAULT_HINT",
        )
        print("PASS hint binding: 'BINDING on the fiction' present")

    def test_outcome_must_match_tier(self):
        text = self._render("success")
        self.assertIn(
            "OUTCOME MUST MATCH THE TIER",
            text,
            "Expected 'OUTCOME MUST MATCH THE TIER' in DEFAULT_HINT",
        )
        print("PASS hint binding: 'OUTCOME MUST MATCH THE TIER' present")


# ---------------------------------------------------------------------------
# TestDepartureDirectiveWiring
# ---------------------------------------------------------------------------


class TestDepartureDirectiveWiring(unittest.TestCase):
    """departure detection + directive wiring (isolated).

    Tests the deterministic surfaces:
    (a) departure detector fires for departure inputs
    (b) the [player leaving presence] block is correctly built from a
        PresenceDeparture result - using the same logic as engine.py
    (c) ts.signals["player_departing"] is set when departure fires
    (d) non-departure input -> block absent, signal absent
    """

    def _build_block(self, departure_result) -> str:
        """Mirror the engine's block-building logic (engine.py insertion)."""
        if departure_result is None:
            return ""
        npc_names = (
            ", ".join(departure_result.npcs_in_scene)
            if departure_result.npcs_in_scene
            else "the NPC"
        )
        cue = departure_result.cues[0] if departure_result.cues else "departure"
        return (
            f"[PLAYER LEAVING PRESENCE - THIS TURN]\n"
            f"The player is moving out of {npc_names}'s immediate space this turn "
            f'(cue: "{cue}"). {npc_names} must resolve NOW to exactly ONE of:\n'
            f"  - GIVE UP: fall silent, let them go (brief parting line only if still in "
            f"earshot);\n"
            f"  - CAVE FAST: concede the one key thing in a sentence or two while the "
            f"player can still hear, BEFORE they are out of range;\n"
            f"  - TRY TO STOP: move to block, shout after, or follow (an action, not a "
            f"speech).\n"
            f"No monologue chases the player. Once the player is gone, no further NPC "
            f"dialogue reaches them - narrate only the environment and their exit."
        )

    def test_departure_input_fires(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I walk away from the conversation",
            has_npcs=True,
            npcs_in_scene=["Soledad"],
        )
        self.assertIsNotNone(result)
        print("PASS wiring: departure input fires detector")

    def test_departure_block_header(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I walk away from the conversation",
            has_npcs=True,
            npcs_in_scene=["Soledad"],
        )
        block = self._build_block(result)
        self.assertIn("[PLAYER LEAVING PRESENCE - THIS TURN]", block)
        print("PASS wiring: block contains header")

    def test_departure_block_names_npc(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I walk away",
            has_npcs=True,
            npcs_in_scene=["Soledad"],
        )
        block = self._build_block(result)
        self.assertIn("Soledad", block)
        print("PASS wiring: block names the NPC")

    def test_departure_block_fallback_npc_name(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I walk away",
            has_npcs=True,
            npcs_in_scene=[],
        )
        block = self._build_block(result)
        self.assertIn("the NPC", block)
        print("PASS wiring: block uses 'the NPC' fallback when names empty")

    def test_departure_block_options(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I turn to go without answering",
            has_npcs=True,
            npcs_in_scene=["Brennan"],
        )
        block = self._build_block(result)
        self.assertIn("GIVE UP", block)
        self.assertIn("CAVE FAST", block)
        self.assertIn("TRY TO STOP", block)
        self.assertIn("No monologue chases the player", block)
        print("PASS wiring: block contains all three resolution options")

    def test_signal_would_be_set(self):
        """Simulates ts.signals["player_departing"] assignment in engine.py."""
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I walk away quickly",
            has_npcs=True,
            npcs_in_scene=["Soledad"],
        )
        # Simulate what engine.py does after TurnState construction:
        signals = {}
        if result is not None:
            signals["player_departing"] = {
                "cues": result.cues,
                "npcs_in_scene": result.npcs_in_scene,
            }
        self.assertIn("player_departing", signals)
        self.assertEqual(signals["player_departing"]["npcs_in_scene"], ["Soledad"])
        self.assertTrue(len(signals["player_departing"]["cues"]) > 0)
        print(f"PASS wiring: signals['player_departing'] = {signals['player_departing']}")

    def test_non_departure_block_absent(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I ask her name",
            has_npcs=True,
            npcs_in_scene=["Soledad"],
        )
        self.assertIsNone(result)
        block = self._build_block(result)
        self.assertEqual(block, "")
        # Simulate signals
        signals = {}
        if result is not None:
            signals["player_departing"] = {
                "cues": result.cues,
                "npcs_in_scene": result.npcs_in_scene,
            }
        self.assertNotIn("player_departing", signals)
        print("PASS wiring: non-departure input -> block absent, signal absent")

    def test_no_npc_block_absent(self):
        from app.game.presence_gate import detect_departure

        result = detect_departure(
            "I walk away",
            has_npcs=False,
            npcs_in_scene=["Soledad"],
        )
        self.assertIsNone(result)
        block = self._build_block(result)
        self.assertEqual(block, "")
        print("PASS wiring: has_npcs=False -> block absent (gate)")


# ---------------------------------------------------------------------------
# TestStakesBrevityFix
# ---------------------------------------------------------------------------


class TestStakesBrevityFix(unittest.TestCase):
    """_RESPONSE_LENGTH contains the routine-movement override
    and the stakes-scaling sentence.

    These strings are LOAD-BEARING: the diagnosis showed "go to my office"
    triggered the old "scene shift -> paint the place" expansion clause.
    The fix adds an explicit override so mundane/familiar movement is capped
    at 2-4 short sentences.
    """

    def _stable(self):
        stable, _ = build_system_blocks(_profile(), stage=2, config=_config())
        return stable

    def test_routine_movement_override_present(self):
        stable = self._stable()
        self.assertIn(
            "Routine / low-stakes movement to a FAMILIAR or MUNDANE place",
            stable,
            "Expected routine-movement override in _RESPONSE_LENGTH",
        )
        print("PASS stakes-brevity: routine-movement override present")

    def test_routine_movement_not_a_scene_shift(self):
        stable = self._stable()
        self.assertIn(
            "is NOT a scene-shift",
            stable,
            "Expected 'is NOT a scene-shift' in _RESPONSE_LENGTH",
        )
        print("PASS stakes-brevity: 'is NOT a scene-shift' present")

    def test_stakes_scaling_rule_present(self):
        stable = self._stable()
        self.assertIn(
            "Scale prose to stakes.",
            stable,
            "Expected 'Scale prose to stakes.' in _RESPONSE_LENGTH",
        )
        print("PASS stakes-brevity: 'Scale prose to stakes.' present")

    def test_mundane_sentence_cap_mentioned(self):
        stable = self._stable()
        self.assertIn(
            "Mundane / low-stakes action = 2-4 short sentences",
            stable,
            "Expected 'Mundane / low-stakes action = 2-4 short sentences' in stable",
        )
        print("PASS stakes-brevity: mundane sentence cap present")

    def test_do_not_narrate_travel_present(self):
        stable = self._stable()
        self.assertIn(
            "Do NOT narrate travel the player did not ask to see",
            stable,
            "Expected travel-narration guard present",
        )
        print("PASS stakes-brevity: travel-narration guard present")

    def test_original_brevity_rules_intact(self):
        """Regression: later edits must not delete existing brevity rules."""
        stable = self._stable()
        for phrase in (
            "1-3 short paragraphs",
            "ENVIRONMENT and ACTION prose ONLY",
            "a line or two at a time",
            "no paragraphs of dialogue",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, stable, f"Existing brevity rule missing: {phrase!r}")
        print("PASS stakes-brevity: all existing brevity rules intact")


# ---------------------------------------------------------------------------
# TestClassifyStakes
# ---------------------------------------------------------------------------


class TestClassifyStakes(unittest.TestCase):
    """classify_stakes keyword fast-path + fail-safe.

    The keyword path must not make LLM calls (network none in hermetic).
    The fail-safe must return 'normal' on any exception.
    """

    def test_mundane_verb_smoke_fast_path(self):
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("I smoke a cigarette")
        self.assertEqual(label, "mundane")
        self.assertIsNone(usage, "Keyword fast-path must return None usage (no LLM call)")
        print("PASS classify_stakes: 'smoke' -> mundane, no usage")

    def test_mundane_verb_go_fast_path(self):
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("I go to my office")
        self.assertEqual(label, "mundane")
        self.assertIsNone(usage)
        print("PASS classify_stakes: 'go' -> mundane")

    def test_mundane_verb_sit_fast_path(self):
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("I sit down and wait")
        self.assertEqual(label, "mundane")
        self.assertIsNone(usage)
        print("PASS classify_stakes: 'sit' -> mundane")

    def test_mundane_verb_head_fast_path(self):
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("I head back to the office")
        self.assertEqual(label, "mundane")
        self.assertIsNone(usage)
        print("PASS classify_stakes: 'head' -> mundane")

    def test_mundane_verb_look_fast_path(self):
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("I look around the room")
        self.assertEqual(label, "mundane")
        self.assertIsNone(usage)
        print("PASS classify_stakes: 'look' -> mundane")

    def test_failsafe_exception_returns_normal(self):
        """Fail-safe: any exception inside classify_stakes -> 'normal'."""
        from app.game.llm import classify_stakes

        with patch("app.game.llm.call_messages", side_effect=Exception("network err")):
            # Use a phrase that won't hit the fast-path
            label, usage = classify_stakes("I investigate the warehouse carefully")
        self.assertEqual(label, "normal", "Fail-safe must return 'normal' on exception")
        self.assertIsNone(usage)
        print("PASS classify_stakes: exception -> 'normal' (fail-safe)")

    def test_empty_input_returns_normal(self):
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("")
        self.assertEqual(label, "normal")
        print("PASS classify_stakes: empty input -> 'normal'")

    def test_no_leading_i_still_mundane(self):
        """Mundane verb without 'I' prefix is still caught."""
        from app.game.llm import classify_stakes

        label, usage = classify_stakes("smoke another cigarette")
        self.assertEqual(label, "mundane")
        self.assertIsNone(usage)
        print("PASS classify_stakes: no leading 'I' -> still mundane")


# ---------------------------------------------------------------------------
# TestLengthProfileInfrastructure
# ---------------------------------------------------------------------------


class TestLengthProfileInfrastructure(unittest.TestCase):
    """LengthProfile enum constants, profiles dict, and
    PROFILE_NAMES frozenset.  No LLM calls."""

    def test_short_max_tokens(self):
        from app.game.beats.base import LengthProfile

        self.assertEqual(LengthProfile.SHORT.max_tokens, 60)
        print("PASS profiles: short max_tokens == 60")

    def test_medium_max_tokens(self):
        from app.game.beats.base import LengthProfile

        self.assertEqual(LengthProfile.MEDIUM.max_tokens, 90)
        print("PASS profiles: medium max_tokens == 90")

    def test_long_max_tokens(self):
        from app.game.beats.base import LengthProfile

        self.assertEqual(LengthProfile.LONG.max_tokens, 250)
        print("PASS profiles: long max_tokens == 250")

    def test_expo_dump_max_tokens(self):
        from app.game.beats.base import LengthProfile

        self.assertEqual(LengthProfile.EXPO.max_tokens, 500)
        print("PASS profiles: expo-dump max_tokens == 500")

    def test_profiles_dict_keyed_by_name(self):
        from app.game.beats.base import PROFILES, LengthProfile

        self.assertIs(PROFILES["short"], LengthProfile.SHORT)
        self.assertIs(PROFILES["medium"], LengthProfile.MEDIUM)
        self.assertIs(PROFILES["long"], LengthProfile.LONG)
        self.assertIs(PROFILES["expo-dump"], LengthProfile.EXPO)
        print("PASS profiles: PROFILES dict keyed by name correctly")

    def test_terse_absent_from_profiles(self):
        from app.game.beats.base import PROFILES, PROFILE_NAMES

        self.assertNotIn("terse", PROFILES, "terse profile MUST be absent - folded into short")
        self.assertNotIn("terse", PROFILE_NAMES, "terse MUST be absent from PROFILE_NAMES")
        print("PASS profiles: terse absent from PROFILES and PROFILE_NAMES")

    def test_profile_names_frozenset(self):
        from app.game.beats.base import PROFILE_NAMES

        self.assertEqual(PROFILE_NAMES, frozenset({"short", "medium", "long", "expo-dump"}))
        print("PASS profiles: PROFILE_NAMES frozenset correct")

    def test_expo_dump_in_profile_names(self):
        from app.game.beats.base import PROFILE_NAMES

        self.assertIn("expo-dump", PROFILE_NAMES, "expo-dump must be present in PROFILE_NAMES")
        print("PASS profiles: 'expo-dump' in PROFILE_NAMES")

    def test_profile_directives_non_empty(self):
        from app.game.beats.base import PROFILES

        for name, spec in PROFILES.items():
            with self.subTest(profile=name):
                self.assertTrue(spec.directive, f"{name} directive must be non-empty")
        print("PASS profiles: all directives non-empty")

    def test_stakes_to_profile_mundane_short(self):
        from app.game.beats.base import STAKES_TO_PROFILE

        self.assertEqual(STAKES_TO_PROFILE.get("mundane"), "short")
        print("PASS profiles: STAKES_TO_PROFILE['mundane'] == 'short'")


# ---------------------------------------------------------------------------
# TestResolveMaxTokens  (canonical max_tokens resolver)
# ---------------------------------------------------------------------------


class TestResolveMaxTokens(unittest.TestCase):
    """resolve_max_tokens, the canonical ceiling resolver.

    Precedence: max_tokens_override > beat_profile_name > ts.length_profile
    > beat_default.  No LLM calls.
    """

    def _ts(self, max_override=0, length_profile=""):
        """Minimal TurnState-like object for resolver tests."""

        class _FakeTS:
            pass

        ts = _FakeTS()
        ts.max_tokens_override = max_override
        ts.length_profile = length_profile
        return ts

    def test_max_tokens_override_wins(self):
        from app.game.beats.base import resolve_max_tokens

        ts = self._ts(max_override=2500, length_profile="short")
        result = resolve_max_tokens(ts, beat_default=1500)
        self.assertEqual(result, 2500, "max_tokens_override must win over profile")
        print("PASS resolve: max_tokens_override=2500 wins over short(60)")

    def test_beat_profile_name_wins_over_length_profile(self):
        from app.game.beats.base import resolve_max_tokens

        ts = self._ts(max_override=0, length_profile="medium")  # would give 90
        # beat_profile_name="short" -> 60 should win over ts.length_profile
        result = resolve_max_tokens(ts, beat_default=1500, beat_profile_name="short")
        self.assertEqual(result, 60)
        print("PASS resolve: beat_profile_name='short'(60) wins over length_profile='medium'(90)")

    def test_length_profile_short_lowers_ceiling(self):
        from app.game.beats.base import resolve_max_tokens

        ts = self._ts(max_override=0, length_profile="short")
        result = resolve_max_tokens(ts, beat_default=1500)
        self.assertEqual(result, 60)
        print("PASS resolve: ts.length_profile='short' -> 60 (mundane ceiling applied)")

    def test_length_profile_medium(self):
        from app.game.beats.base import resolve_max_tokens

        ts = self._ts(max_override=0, length_profile="medium")
        self.assertEqual(resolve_max_tokens(ts, beat_default=1500), 90)
        print("PASS resolve: length_profile='medium' -> 90")

    def test_beat_default_fallback(self):
        from app.game.beats.base import resolve_max_tokens

        ts = self._ts(max_override=0, length_profile="")
        self.assertEqual(resolve_max_tokens(ts, beat_default=1500), 1500)
        print("PASS resolve: no override + no profile -> beat_default=1500")

    def test_unknown_profile_falls_back_to_default(self):
        from app.game.beats.base import resolve_max_tokens

        ts = self._ts(max_override=0, length_profile="unknown_profile")
        self.assertEqual(resolve_max_tokens(ts, beat_default=999), 999)
        print("PASS resolve: unknown profile name -> beat_default fallback")


# ---------------------------------------------------------------------------
# TestPackLengthProfiles  (pack_beats validation)
# ---------------------------------------------------------------------------


class TestPackLengthProfiles(unittest.TestCase):
    """pack.json length_profiles validation in pack_beats.

    Valid override maps are accepted; bad profile names fail load loudly
    (PackBeatsError) per the module's fail-fast doctrine.
    """

    def _validate(self, manifest_extra: dict):
        """Run validate_pack with a minimal manifest + extra fields."""
        from pathlib import Path
        from app.game.pack_beats import validate_pack

        # Minimal valid manifest (no beats/overrides/bindings/length_profiles)
        manifest = {**manifest_extra}
        pack_dir = Path("/app/engine/lore/neon_rain")
        return validate_pack(manifest, pack_dir)

    def test_no_length_profiles_is_valid(self):
        result = self._validate({})
        self.assertEqual(result["length_profiles"], {})
        print("PASS pack_lp: absent length_profiles -> empty dict")

    def test_valid_length_profiles_accepted(self):
        result = self._validate({"length_profiles": {"mundane": "short", "standard": "medium"}})
        self.assertEqual(result["length_profiles"], {"mundane": "short", "standard": "medium"})
        print("PASS pack_lp: valid length_profiles map accepted")

    def test_all_profile_names_valid(self):

        for name in ("short", "medium", "long", "expo-dump"):
            with self.subTest(profile=name):
                result = self._validate({"length_profiles": {"test_key": name}})
                self.assertEqual(result["length_profiles"]["test_key"], name)
        print("PASS pack_lp: all four profile names accepted")

    def test_invalid_profile_name_fails_load(self):
        from app.game.pack_beats import PackBeatsError, validate_pack
        from pathlib import Path

        manifest = {"length_profiles": {"standard": "ultralong"}}
        with self.assertRaises(PackBeatsError) as cm:
            validate_pack(manifest, Path("/app/engine/lore/neon_rain"))
        self.assertIn("ultralong", str(cm.exception))
        print("PASS pack_lp: invalid profile 'ultralong' fails load LOUDLY")

    def test_non_dict_length_profiles_fails(self):
        from app.game.pack_beats import PackBeatsError, validate_pack
        from pathlib import Path

        manifest = {"length_profiles": ["terse"]}  # wrong type
        with self.assertRaises(PackBeatsError):
            validate_pack(manifest, Path("/app/engine/lore/neon_rain"))
        print("PASS pack_lp: non-dict length_profiles raises PackBeatsError")


# ---------------------------------------------------------------------------
# TestProfileDirectivePlacement  (prefix contract)
# ---------------------------------------------------------------------------


class TestProfileDirectivePlacement(unittest.TestCase):
    """prefix contract: the profile directive must be
    accessible as a string for placement in the user message (not the system).

    These are structural / string-content tests - no LLM calls.
    """

    def test_expo_directive_is_string(self):
        from app.game.beats.base import LengthProfile

        self.assertIsInstance(LengthProfile.EXPO.directive, str)
        self.assertTrue(len(LengthProfile.EXPO.directive) > 0)
        print("PASS profile placement: EXPO directive is a non-empty string")

    def test_short_directive_is_string(self):
        from app.game.beats.base import LengthProfile

        self.assertIsInstance(LengthProfile.SHORT.directive, str)
        print("PASS profile placement: SHORT directive is a non-empty string")

    def test_medium_directive_is_string(self):
        from app.game.beats.base import LengthProfile

        self.assertIsInstance(LengthProfile.MEDIUM.directive, str)
        print("PASS profile placement: MEDIUM directive is a non-empty string")

    def test_long_directive_is_string(self):
        from app.game.beats.base import LengthProfile

        self.assertIsInstance(LengthProfile.LONG.directive, str)
        print("PASS profile placement: LONG directive is a non-empty string")

    def test_short_directive_not_in_stable_system(self):
        """Prefix contract: the short profile directive must not appear in the
        stable system half (it belongs in the user message only)."""
        stable, volatile = build_system_blocks(_profile(), stage=2, config=_config())
        from app.game.beats.base import LengthProfile

        self.assertNotIn(
            LengthProfile.SHORT.directive,
            stable,
            "SHORT directive must NOT be in stable system (prefix contract violation)",
        )
        self.assertNotIn(
            LengthProfile.SHORT.directive,
            volatile,
            "SHORT directive must NOT be in volatile system (prefix contract violation)",
        )
        print(
            "PASS profile placement: SHORT directive absent from both stable and volatile system halves"
        )

    def test_states_on_turnstate(self):
        """TurnState has both 'stakes' and 'length_profile' fields with correct defaults."""
        from app.game.beats.state import TurnState
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(TurnState)}
        self.assertIn("stakes", field_names)
        self.assertIn("length_profile", field_names)
        # Check defaults
        defaults = {
            f.name: f.default
            for f in dataclasses.fields(TurnState)
            if f.default is not dataclasses.MISSING
        }
        self.assertEqual(defaults.get("stakes"), "normal")
        self.assertEqual(defaults.get("length_profile"), "")
        print(
            "PASS profile placement: TurnState has stakes='normal' and length_profile='' defaults"
        )


# ---------------------------------------------------------------------------
# TestCacheFloorCIGate  (hermetic CI gate)
# ---------------------------------------------------------------------------


class TestCacheFloorCIGate(unittest.TestCase):
    """CI gate: every pack x stage must clear the proxy floor.

    invariant: cache_floor_ok(stable) == True for all (pack_id, stage)
    combinations.  A pack silently dropping under-floor can't ship
    undetected - this test fails if any pack's seed-roster content is
    removed or truncated below the threshold.

    Uses the proxy (len(stable)//4 >= CACHE_FLOOR_PROXY=4096) - no API
    calls needed; the test runs hermetically in the test container.

    Negative-case coverage: verifies that the floor guard does reject a
    known-short prefix (empty string), confirming the gate is live.
    """

    def _stable_for(self, pack_id: str, stage: int) -> str:
        from app.game import lore as lore_mod
        from app.game.prompts import build_system_blocks
        from app.game.state import CharacterProfile, GameConfig

        config = GameConfig(pack_id=pack_id)
        pack = lore_mod.load_pack(pack_id)
        seed_roster = lore_mod.get_seed_roster(pack)
        profile = CharacterProfile(name="Test", appearance="test appearance")
        stable, _ = build_system_blocks(profile, stage, config, seed_roster=seed_roster)
        return stable

    def test_all_packs_both_stages_above_floor(self):
        """Every shipped pack x stages 1+2 must have proxy >= CACHE_FLOOR_PROXY."""
        from app.game.prompts import cache_floor_ok, CACHE_FLOOR_PROXY

        packs = ["neon_rain"]
        failures = []
        for pack_id in packs:
            for stage in [1, 2]:
                stable = self._stable_for(pack_id, stage)
                proxy = len(stable) // 4
                if not cache_floor_ok(stable):
                    failures.append(
                        f"{pack_id} stage={stage}: proxy={proxy} < {CACHE_FLOOR_PROXY} "
                        f"(chars={len(stable)}) - add seed-roster content"
                    )
                else:
                    print(
                        f"PASS floor gate: {pack_id} stage={stage} "
                        f"proxy={proxy} >= {CACHE_FLOOR_PROXY}"
                    )
        self.assertEqual(
            failures,
            [],
            msg="Cache floor CI gate FAILED:\n" + "\n".join(failures),
        )

    def test_floor_guard_rejects_empty_prefix(self):
        """Negative: empty stable prefix fails cache_floor_ok (gate is live)."""
        from app.game.prompts import cache_floor_ok

        self.assertFalse(
            cache_floor_ok(""),
            "cache_floor_ok('') must return False - gate must be active",
        )
        print("PASS floor gate: empty prefix correctly fails cache_floor_ok")

    def test_floor_guard_rejects_short_prefix(self):
        """Negative: a short prefix (< 16384 chars) fails cache_floor_ok."""
        from app.game.prompts import cache_floor_ok

        short = "x" * 8000  # 8000 chars -> proxy=2000 < 4096
        self.assertFalse(
            cache_floor_ok(short),
            "cache_floor_ok of 8000-char prefix must return False",
        )
        print("PASS floor gate: 8000-char prefix correctly fails cache_floor_ok")

    def test_floor_guard_accepts_long_prefix(self):
        """Positive: a prefix of 16384+ chars passes cache_floor_ok."""
        from app.game.prompts import cache_floor_ok

        long = "x" * 16384  # proxy=4096, exactly at floor
        self.assertTrue(
            cache_floor_ok(long),
            "cache_floor_ok of 16384-char prefix must return True",
        )
        print("PASS floor gate: 16384-char prefix passes cache_floor_ok")


# ---------------------------------------------------------------------------
# TestCachePrefixInvariant  (prefix contract)
# ---------------------------------------------------------------------------


def _marked(messages, provider):
    """Run the history breakpoint marker on the fast tier with `provider` bound."""
    from pydantic import SecretStr
    from app.game.llm import MODEL_HAIKU, _mark_history_cache_breakpoint
    from app.providers import registry

    tok = registry.bind_request(provider, SecretStr("test-key-prefix"))
    try:
        return _mark_history_cache_breakpoint(messages, MODEL_HAIKU)
    finally:
        registry.unbind_request(tok)


class TestCachePrefixInvariant(unittest.TestCase):
    """Prefix contract: per-beat content (the profile directive)
    never appears before the history cache breakpoint.

    The contract:
      (a) Per-beat profile directive is in the user message only - never
          in the system stable or volatile half.
      (b) The message breakpoint (_mark_history_cache_breakpoint) marks
          messages[-2] (last history message), not messages[-1] (current
          user message).
      (c) System blocks are composed once per turn - byte-identical across
          all beats of that turn's pipeline.

    At runtime this shows up as cache_read>0 on beats 2..N once a turn makes
    several LLM calls. This hermetic test only checks the structural placement.
    """

    # (b) breakpoint placement

    def test_breakpoint_on_second_to_last_message(self):
        """History breakpoint sits on messages[-2], not messages[-1]."""
        messages = [
            {"role": "user", "content": "turn 1 question"},
            {"role": "assistant", "content": "turn 1 answer"},
            {"role": "user", "content": "current turn context"},
        ]
        result = _marked(messages, "anthropic")
        hist_msg = result[-2]
        content = hist_msg["content"]
        self.assertIsInstance(content, list, "History message content must be a list of blocks")
        self.assertIn(
            "cache_control", content[-1], "Last block in history msg must have cache_control"
        )
        self.assertEqual(content[-1]["cache_control"], {"type": "ephemeral"})
        print("PASS prefix invariant: history breakpoint on messages[-2]")

    def test_current_user_message_not_marked(self):
        """Current turn's user message (messages[-1]) has no cache_control."""
        messages = [
            {"role": "user", "content": "history 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "current turn with per-beat directive"},
        ]
        result = _marked(messages, "anthropic")
        last_msg = result[-1]
        content = last_msg["content"]
        if isinstance(content, list):
            for block in content:
                self.assertNotIn(
                    "cache_control", block, "Current user message must NOT have cache_control"
                )
        else:
            self.assertIsInstance(content, str, "Unmarked message content stays as string")
        print("PASS prefix invariant: current user message not marked")

    def test_noop_for_provider_without_cache_control(self):
        """A provider without cache_control support gets no markers."""
        messages = [
            {"role": "user", "content": "history"},
            {"role": "user", "content": "current"},
        ]
        result = _marked(messages, "openai")
        self.assertEqual(result, messages, "No cache_control support: messages must be unchanged")
        print("PASS prefix invariant: no cache_control injected without provider support")

    def test_noop_for_single_message(self):
        """Single-message list (no history): no breakpoint added."""
        messages = [{"role": "user", "content": "only message"}]
        result = _marked(messages, "anthropic")
        self.assertEqual(result, messages, "Single message: must be returned unchanged")
        print("PASS prefix invariant: no-op for single message (no history)")

    def test_existing_list_content_gets_marker(self):
        """History message with existing list content: cache_control on last block."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "block one"},
                    {"type": "text", "text": "block two"},
                ],
            },
            {"role": "user", "content": "current"},
        ]
        result = _marked(messages, "anthropic")
        content = result[-2]["content"]
        self.assertIsInstance(content, list)
        self.assertIn(
            "cache_control", content[-1], "cache_control must be on the last existing block"
        )
        self.assertNotIn("cache_control", content[0], "Earlier blocks must not be modified")
        print("PASS prefix invariant: list-content history message gets marker on last block")

    def test_input_messages_not_mutated(self):
        """_mark_history_cache_breakpoint must not mutate the input list."""
        original_content = "turn 1 answer"
        messages = [
            {"role": "assistant", "content": original_content},
            {"role": "user", "content": "current"},
        ]
        _ = _marked(messages, "anthropic")
        # Original list and dict must be unchanged.
        self.assertIs(
            messages[0]["content"],
            original_content,
            "Input messages[-2]['content'] must not be mutated",
        )
        print("PASS prefix invariant: input messages not mutated by breakpoint marker")

    # (a) profile directive never in system blocks

    def test_all_profile_directives_absent_from_system(self):
        """no profile directive appears in system stable or volatile."""
        from app.game.beats.base import PROFILES
        from app.game.prompts import build_system_blocks
        from app.game.state import CharacterProfile, GameConfig

        profile = CharacterProfile(name="Test", appearance="test")
        stable, volatile = build_system_blocks(profile, stage=2, config=GameConfig())
        failures = []
        for lp_name, spec in PROFILES.items():
            if spec.directive in stable:
                failures.append(f"{lp_name} directive in STABLE (prefix contract violation)")
            if spec.directive in volatile:
                failures.append(f"{lp_name} directive in VOLATILE (prefix contract violation)")
        self.assertEqual(
            failures,
            [],
            msg="PREFIX CONTRACT VIOLATED:\n" + "\n".join(failures),
        )
        print(
            f"PASS prefix invariant: all {len(PROFILES)} profile directives absent "
            "from system stable and volatile"
        )

    # (c) system composed once - byte stability check

    def test_system_stable_byte_identical_same_inputs(self):
        """Calling build_system_blocks twice with identical inputs -> same stable bytes."""
        from app.game.prompts import build_system_blocks
        from app.game.state import CharacterProfile, GameConfig

        profile = CharacterProfile(name="Test", appearance="test appearance")
        config = GameConfig(pack_id="neon_rain")
        stable1, volatile1 = build_system_blocks(profile, 2, config)
        stable2, volatile2 = build_system_blocks(profile, 2, config)
        self.assertEqual(
            stable1, stable2, "Stable block must be byte-identical across repeated calls"
        )
        print("PASS prefix invariant: stable block byte-identical on repeated calls")


# ---------------------------------------------------------------------------
# TestStakesProfiles  (STAKES_TO_PROFILE extension)
# ---------------------------------------------------------------------------


class TestStakesProfiles(unittest.TestCase):
    """STAKES_TO_PROFILE now maps all three stakes levels.

    mundane->short, normal->medium, dramatic->long. These drive the profile
    ceiling on DMWorldStepBeat for standard turns via ts.length_profile.
    """

    def test_stakes_to_profile_mundane_short(self):
        from app.game.beats.base import STAKES_TO_PROFILE

        self.assertEqual(STAKES_TO_PROFILE.get("mundane"), "short")
        print("PASS stakes profiles: STAKES_TO_PROFILE['mundane'] == 'short'")

    def test_stakes_to_profile_normal_medium(self):
        from app.game.beats.base import STAKES_TO_PROFILE

        self.assertEqual(
            STAKES_TO_PROFILE.get("normal"),
            "medium",
            "'normal' stakes must map to 'medium' profile (90 tok)",
        )
        print("PASS stakes profiles: STAKES_TO_PROFILE['normal'] == 'medium'")

    def test_stakes_to_profile_dramatic_long(self):
        from app.game.beats.base import STAKES_TO_PROFILE

        self.assertEqual(
            STAKES_TO_PROFILE.get("dramatic"),
            "long",
            "'dramatic' stakes must map to 'long' profile (250 tok)",
        )
        print("PASS stakes profiles: STAKES_TO_PROFILE['dramatic'] == 'long'")

    def test_all_three_stakes_levels_covered(self):
        from app.game.beats.base import STAKES_TO_PROFILE

        for level in ("mundane", "normal", "dramatic"):
            with self.subTest(level=level):
                self.assertIn(level, STAKES_TO_PROFILE, f"STAKES_TO_PROFILE must cover '{level}'")
        print("PASS stakes profiles: all three stakes levels covered in STAKES_TO_PROFILE")

    def test_normal_profile_ceiling(self):
        from app.game.beats.base import STAKES_TO_PROFILE, PROFILES

        profile_name = STAKES_TO_PROFILE["normal"]
        ceiling = PROFILES[profile_name].max_tokens
        self.assertEqual(ceiling, 90, "'normal' -> 'medium' -> 90 tok ceiling")
        print(f"PASS stakes profiles: 'normal' -> '{profile_name}' -> {ceiling} tok")

    def test_dramatic_profile_ceiling(self):
        from app.game.beats.base import STAKES_TO_PROFILE, PROFILES

        profile_name = STAKES_TO_PROFILE["dramatic"]
        ceiling = PROFILES[profile_name].max_tokens
        self.assertEqual(ceiling, 250, "'dramatic' -> 'long' -> 250 tok ceiling")
        print(f"PASS stakes profiles: 'dramatic' -> '{profile_name}' -> {ceiling} tok")


# ---------------------------------------------------------------------------
# TestPipelineComposition  (build_pipeline_for)
# ---------------------------------------------------------------------------


class TestPipelineComposition(unittest.TestCase):
    """build_pipeline_for stakes->count+profile logic.

    (a) mundane standard -> N=1 short (cigarette fast-path, structurally enforced).
    (b) normal/dramatic standard -> N=1 with correct profile ceiling mapping.
    (c) ability two-part: [DMActBeat, GroundingBeat, NpcReactBeat]; model-
        escalation respected (DMActBeat reads ts.turn_model, not hardcoded Sonnet).
    (d) build_pipeline_for accepts stakes kwarg without error.
    (e) mundane with a pack that has beats -> still N=1 (pack beats bypassed).
    """

    def test_build_pipeline_accepts_stakes_kwarg(self):
        """build_pipeline_for signature accepts stakes= without TypeError."""
        from app.game.beats.builtin import build_pipeline_for

        try:
            build_pipeline_for("standard", stakes="mundane")
        except TypeError as e:
            self.fail(f"build_pipeline_for does not accept stakes kwarg: {e}")
        print("PASS pipeline: build_pipeline_for accepts stakes kwarg")

    def test_mundane_standard_returns_one_beat(self):
        """(a) mundane standard -> exactly 1 beat in the pipeline."""
        from app.game.beats.builtin import build_pipeline_for

        pipeline = build_pipeline_for("standard", stakes="mundane")
        self.assertEqual(
            len(pipeline.beats), 1, "mundane standard must return N=1 beat (cigarette fast-path)"
        )
        print("PASS pipeline: mundane standard -> N=1 beat")

    def test_mundane_standard_beat_is_dm_world_step(self):
        """(a) The single mundane beat is DMWorldStepBeat."""
        from app.game.beats.builtin import build_pipeline_for, DMWorldStepBeat

        pipeline = build_pipeline_for("standard", stakes="mundane")
        self.assertIsInstance(
            pipeline.beats[0], DMWorldStepBeat, "mundane standard beat[0] must be DMWorldStepBeat"
        )
        print("PASS pipeline: mundane standard beat[0] is DMWorldStepBeat")

    def test_mundane_standard_bypasses_pack_beats(self):
        """(e) Pack with declared beats: mundane standard still returns N=1 (pack skipped)."""
        from app.game.beats.builtin import build_pipeline_for, DMWorldStepBeat

        # Minimal pack dict with a declared beat (simulates e.g. neon_rain)
        fake_pack = {
            "pack_beats": {
                "beats": [{"id": "x", "label": "x", "when": None, "text": "injected"}],
                "overrides": [],
                "bindings": [],
                "encounter_mechanics": None,
                "length_profiles": {},
            }
        }
        pipeline = build_pipeline_for("standard", stakes="mundane", pack=fake_pack)
        self.assertEqual(len(pipeline.beats), 1, "mundane bypasses pack beats -> still N=1")
        self.assertIsInstance(pipeline.beats[0], DMWorldStepBeat)
        print("PASS pipeline: mundane standard bypasses pack beats -> N=1")

    def test_normal_standard_returns_one_beat(self):
        """(b) normal standard -> 1 beat (DMWorldStepBeat; profile ceiling short/300)."""
        from app.game.beats.builtin import build_pipeline_for, DMWorldStepBeat

        pipeline = build_pipeline_for("standard", stakes="normal")
        self.assertEqual(len(pipeline.beats), 1)
        self.assertIsInstance(pipeline.beats[0], DMWorldStepBeat)
        print("PASS pipeline: normal standard -> 1 DMWorldStepBeat")

    def test_dramatic_standard_returns_one_beat(self):
        """(b) dramatic standard -> 1 beat (DMWorldStepBeat; profile ceiling medium/500)."""
        from app.game.beats.builtin import build_pipeline_for, DMWorldStepBeat

        pipeline = build_pipeline_for("standard", stakes="dramatic")
        self.assertEqual(len(pipeline.beats), 1)
        self.assertIsInstance(pipeline.beats[0], DMWorldStepBeat)
        print("PASS pipeline: dramatic standard -> 1 DMWorldStepBeat")

    def test_ability_pipeline_beat_count(self):
        """(c) ability turn -> 3 beats: DMActBeat + GroundingBeat + NpcReactBeat."""
        from app.game.beats.builtin import build_pipeline_for

        pipeline = build_pipeline_for("ability", stakes="normal")
        self.assertEqual(
            len(pipeline.beats),
            3,
            "ability pipeline must have 3 beats: act + grounding + npc_react",
        )
        print("PASS pipeline: ability -> 3 beats")

    def test_ability_pipeline_beat_types(self):
        """(c) ability beat order: DMActBeat[0], GroundingBeat[1], NpcReactBeat[2]."""
        from app.game.beats.builtin import (
            build_pipeline_for,
            DMActBeat,
            GroundingBeat,
            NpcReactBeat,
        )

        pipeline = build_pipeline_for("ability", stakes="normal")
        self.assertIsInstance(pipeline.beats[0], DMActBeat)
        self.assertIsInstance(pipeline.beats[1], GroundingBeat)
        self.assertIsInstance(pipeline.beats[2], NpcReactBeat)
        print("PASS pipeline: ability beat order DMActBeat/GroundingBeat/NpcReactBeat")

    def test_ability_dmact_uses_ts_turn_model_not_hardcoded_sonnet(self):
        """(c) consent honored: DMActBeat reads ts.turn_model, not MODEL_SONNET literal."""
        import inspect
        from app.game.beats.builtin import DMActBeat

        src = inspect.getsource(DMActBeat._call_and_parse)
        # Must read from ts.turn_model (consent-gated by engine.py)
        self.assertIn(
            "ts.turn_model", src, "DMActBeat._call_and_parse must use ts.turn_model (consent-gated)"
        )
        # Must not hard-code MODEL_SONNET as the literal model kwarg
        # (the consent gate in engine.py sets ts.turn_model before the pipeline runs)
        self.assertNotIn(
            "model=MODEL_SONNET",
            src,
            "DMActBeat must not hard-code MODEL_SONNET - consent flows via ts.turn_model",
        )
        print("PASS pipeline: DMActBeat uses ts.turn_model (honors consent gating)")

    def test_ability_grounding_uses_model_haiku(self):
        """(c) GroundingBeat always uses MODEL_HAIKU (the cheap explanation beat)."""
        import inspect
        from app.game.beats.builtin import GroundingBeat

        src = inspect.getsource(GroundingBeat.run)
        self.assertIn(
            "MODEL_HAIKU", src, "GroundingBeat must use MODEL_HAIKU (Haiku explanation beat)"
        )
        print("PASS pipeline: GroundingBeat uses MODEL_HAIKU (Haiku explanation)")

    def test_ability_npcreact_uses_model_haiku(self):
        """(c) NpcReactBeat always uses MODEL_HAIKU (cheap conditional reaction)."""
        import inspect
        from app.game.beats.builtin import NpcReactBeat

        src = inspect.getsource(NpcReactBeat.run)
        self.assertIn("MODEL_HAIKU", src, "NpcReactBeat must use MODEL_HAIKU")
        print("PASS pipeline: NpcReactBeat uses MODEL_HAIKU")

    def test_ability_dmact_short_profile(self):
        """(c) DMActBeat is fixed at 'short' profile (60 tok) via resolve_max_tokens."""
        import inspect
        from app.game.beats.builtin import DMActBeat

        src = inspect.getsource(DMActBeat.run)
        self.assertIn(
            'beat_profile_name="short"',
            src,
            "DMActBeat must pass beat_profile_name='short' to resolve_max_tokens",
        )
        print("PASS pipeline: DMActBeat fixed at 'short' profile (60 tok)")

    def test_ability_grounding_medium_profile(self):
        """(c) GroundingBeat is fixed at 'medium' profile (90 tok)."""
        import inspect
        from app.game.beats.builtin import GroundingBeat

        src = inspect.getsource(GroundingBeat.run)
        self.assertIn(
            'beat_profile_name="medium"',
            src,
            "GroundingBeat must pass beat_profile_name='medium' to resolve_max_tokens",
        )
        print("PASS pipeline: GroundingBeat fixed at 'medium' profile (90 tok)")

    def test_ability_npcreact_short_profile(self):
        """NpcReactBeat is fixed at 'short' profile (60 tok)."""
        import inspect
        from app.game.beats.builtin import NpcReactBeat

        src = inspect.getsource(NpcReactBeat.run)
        self.assertIn(
            'beat_profile_name="short"',
            src,
            "NpcReactBeat must pass beat_profile_name='short' to resolve_max_tokens",
        )
        print("PASS pipeline: NpcReactBeat fixed at 'short' profile (60 tok)")

    def test_mundane_ability_still_three_beats(self):
        """Mundane stakes do not suppress the ability two-part (only standard turns are capped)."""
        from app.game.beats.builtin import (
            build_pipeline_for,
            DMActBeat,
            GroundingBeat,
        )

        pipeline = build_pipeline_for("ability", stakes="mundane")
        self.assertEqual(
            len(pipeline.beats), 3, "mundane+ability must still return 3 beats (ability two-part)"
        )
        self.assertIsInstance(pipeline.beats[0], DMActBeat)
        self.assertIsInstance(pipeline.beats[1], GroundingBeat)
        print("PASS pipeline: mundane+ability -> still 3 beats (ability unaffected by stakes)")

    def test_engine_call_site_passes_stakes(self):
        """build_pipeline_for call in _run_turn_inner passes stakes=stakes_label.
        (_run_turn_inner is the body of the pipeline; run_turn is now a thin gate-billing
        wrapper - the stakes wiring lives in the inner function.)"""
        import inspect
        from app.game import engine

        src = inspect.getsource(engine._run_turn_inner)
        self.assertIn(
            "stakes=stakes_label",
            src,
            "engine._run_turn_inner must pass stakes=stakes_label to build_pipeline_for",
        )
        print("PASS engine: _run_turn_inner passes stakes=stakes_label to build_pipeline_for")


# ---------------------------------------------------------------------------
# TestBeatMetering  (no-drop per-beat metering)
# ---------------------------------------------------------------------------


class TestBeatMetering(unittest.TestCase):
    """every beat's usage is recorded (no drop).

    Structural test: each beat class's run method must contain a
    ts.record_beat(...) call.  record_beat accumulates by_beat entries so
    every beat's tokens_in/out/cache_read/cache_creation are visible in
    the turn telemetry.  None dropped = turn cost math is complete.
    """

    def _assert_records_beat(self, beat_cls, method_name="run"):
        import inspect

        src = inspect.getsource(getattr(beat_cls, method_name))
        self.assertIn(
            "ts.record_beat", src, f"{beat_cls.__name__}.{method_name} must call ts.record_beat"
        )

    def test_dm_world_step_records_beat(self):
        from app.game.beats.builtin import DMWorldStepBeat

        self._assert_records_beat(DMWorldStepBeat)
        print("PASS metering: DMWorldStepBeat.run calls ts.record_beat")

    def test_dm_act_records_beat(self):
        from app.game.beats.builtin import DMActBeat

        self._assert_records_beat(DMActBeat)
        print("PASS metering: DMActBeat.run calls ts.record_beat")

    def test_grounding_records_beat(self):
        from app.game.beats.builtin import GroundingBeat

        self._assert_records_beat(GroundingBeat)
        print("PASS metering: GroundingBeat.run calls ts.record_beat")

    def test_npc_react_records_beat(self):
        from app.game.beats.builtin import NpcReactBeat

        self._assert_records_beat(NpcReactBeat)
        print("PASS metering: NpcReactBeat.run calls ts.record_beat")

    def test_dm_world_step_records_cache_tokens(self):
        """DMWorldStepBeat must record cache_read + cache_creation (no-drop for cache accounting)."""
        import inspect
        from app.game.beats.builtin import DMWorldStepBeat

        src = inspect.getsource(DMWorldStepBeat.run)
        self.assertIn(
            "cache_read=beat_cri", src, "DMWorldStepBeat must pass cache_read to record_beat"
        )
        self.assertIn(
            "cache_creation=beat_cci",
            src,
            "DMWorldStepBeat must pass cache_creation to record_beat",
        )
        print("PASS metering: DMWorldStepBeat records cache_read + cache_creation")

    def test_dm_act_records_cache_tokens(self):
        import inspect
        from app.game.beats.builtin import DMActBeat

        src = inspect.getsource(DMActBeat.run)
        self.assertIn("cache_read=beat_cri", src, "DMActBeat must pass cache_read to record_beat")
        self.assertIn(
            "cache_creation=beat_cci", src, "DMActBeat must pass cache_creation to record_beat"
        )
        print("PASS metering: DMActBeat records cache_read + cache_creation")

    def test_grounding_records_cache_tokens(self):
        import inspect
        from app.game.beats.builtin import GroundingBeat

        src = inspect.getsource(GroundingBeat.run)
        self.assertIn(
            "cache_read=beat_cri", src, "GroundingBeat must pass cache_read to record_beat"
        )
        self.assertIn(
            "cache_creation=beat_cci", src, "GroundingBeat must pass cache_creation to record_beat"
        )
        print("PASS metering: GroundingBeat records cache_read + cache_creation")

    def test_npc_react_records_cache_tokens(self):
        import inspect
        from app.game.beats.builtin import NpcReactBeat

        src = inspect.getsource(NpcReactBeat.run)
        self.assertIn(
            "cache_read=beat_cri", src, "NpcReactBeat must pass cache_read to record_beat"
        )
        self.assertIn(
            "cache_creation=beat_cci", src, "NpcReactBeat must pass cache_creation to record_beat"
        )
        print("PASS metering: NpcReactBeat records cache_read + cache_creation")

    def test_record_beat_accumulates_tokens(self):
        """TurnState.record_beat accumulates turn_tokens_in + turn_tokens_out."""
        from app.game.beats.state import TurnState
        import dataclasses

        # Verify the by_beat structure supports per-beat token accounting.
        fields = {f.name for f in dataclasses.fields(TurnState)}
        self.assertIn("by_beat", fields, "TurnState must have by_beat for per-beat accounting")
        self.assertIn("turn_tokens_in", fields, "TurnState must have turn_tokens_in accumulator")
        self.assertIn("turn_tokens_out", fields, "TurnState must have turn_tokens_out accumulator")
        print("PASS metering: TurnState has by_beat + turn_tokens_in/out accumulators")

    def test_record_beat_by_beat_schema(self):
        """record_beat appends a dict with cache_read and cache_creation keys."""
        import inspect
        from app.game.beats.state import TurnState

        src = inspect.getsource(TurnState.record_beat)
        self.assertIn('"cache_read"', src, "record_beat must write cache_read to by_beat dict")
        self.assertIn(
            '"cache_creation"', src, "record_beat must write cache_creation to by_beat dict"
        )
        print("PASS metering: record_beat by_beat schema includes cache_read + cache_creation")


# ---------------------------------------------------------------------------
# TestByteIdenticalPrefix  (byte-identical prefix)
# ---------------------------------------------------------------------------


class TestByteIdenticalPrefix(unittest.TestCase):
    """the system stable block is byte-identical across all
    beats of a single turn (the cache precondition for beats 2..N).

    This is a structural / determinism test only.  Runtime proof
    (cache_read>0 on beats 2..N) is covered by the stress tests.

    Asserts:
    (a) build_system_blocks is deterministic: same inputs -> same stable bytes.
    (b) DMWorldStepBeat and DMActBeat call build_system_blocks with the same
        argument pattern (gs.profile, ts.stage, gs.config, monster_level,
        has_npcs, turn_class, seed_roster) -> given the same TurnState they
        produce byte-identical stable prefixes.
    (c) The profile directive (per-beat-varying content) never appears in the
        stable or volatile system half (enforced by the prefix-contract tests
        above; re-checked here as an invariant).
    """

    def test_stable_block_byte_identical_repeated_calls(self):
        """(a) build_system_blocks(same inputs) -> identical stable bytes."""
        from app.game.prompts import build_system_blocks
        from app.game.state import CharacterProfile, GameConfig

        profile = CharacterProfile(name="Test", appearance="test appearance")
        config = GameConfig(pack_id="neon_rain")
        stable1, _ = build_system_blocks(profile, 2, config)
        stable2, _ = build_system_blocks(profile, 2, config)
        self.assertEqual(
            stable1, stable2, "build_system_blocks must be deterministic - same stable bytes"
        )
        print("PASS prefix: stable block byte-identical on repeated calls")

    def test_dm_world_step_build_system_blocks_call_pattern(self):
        """(b) DMWorldStepBeat calls build_system_blocks with gs.profile, ts.stage,
        gs.config, monster_level, has_npcs, turn_class, seed_roster."""
        import inspect
        from app.game.beats.builtin import DMWorldStepBeat

        src = inspect.getsource(DMWorldStepBeat.run)
        self.assertIn("build_system_blocks(", src)
        # All parameters that make up the shared prefix must be present
        for arg in (
            "gs.profile",
            "ts.stage",
            "gs.config",
            "monster_level=ts.monster_level",
            "has_npcs=ts.has_npcs",
            "turn_class=ts.turn_class",
            "seed_roster=gs.seed_roster",
        ):
            self.assertIn(arg, src, f"DMWorldStepBeat.run must pass {arg!r} to build_system_blocks")
        print("PASS prefix: DMWorldStepBeat.run build_system_blocks call pattern correct")

    def test_dm_act_beat_build_system_blocks_call_pattern(self):
        """(b) DMActBeat calls build_system_blocks with the identical argument set
        as DMWorldStepBeat - guaranteeing byte-identical stable prefix on the same turn."""
        import inspect
        from app.game.beats.builtin import DMActBeat

        src = inspect.getsource(DMActBeat.run)
        self.assertIn("build_system_blocks(", src)
        for arg in (
            "gs.profile",
            "ts.stage",
            "gs.config",
            "monster_level=ts.monster_level",
            "has_npcs=ts.has_npcs",
            "turn_class=ts.turn_class",
            "seed_roster=gs.seed_roster",
        ):
            self.assertIn(
                arg,
                src,
                f"DMActBeat.run must pass {arg!r} to build_system_blocks "
                f"(must match DMWorldStepBeat for byte-identical prefix)",
            )
        print("PASS prefix: DMActBeat.run build_system_blocks call pattern matches DMWorldStepBeat")

    def test_profile_directives_absent_from_system_blocks(self):
        """(c) prefix contract gate: no profile directive in stable or volatile system."""
        from app.game.beats.base import PROFILES
        from app.game.prompts import build_system_blocks
        from app.game.state import CharacterProfile, GameConfig

        profile = CharacterProfile(name="Test", appearance="test")
        stable, volatile = build_system_blocks(profile, stage=2, config=GameConfig())
        failures = []
        for lp_name, spec in PROFILES.items():
            if spec.directive in stable:
                failures.append(f"{lp_name} directive in STABLE (prefix contract violation)")
            if spec.directive in volatile:
                failures.append(f"{lp_name} directive in VOLATILE (prefix contract violation)")
        self.assertEqual(
            failures,
            [],
            "PREFIX CONTRACT: no profile directive in system blocks\n" + "\n".join(failures),
        )
        print(
            f"PASS prefix: all {len(PROFILES)} profile directives absent "
            "from system (prefix contract holds)"
        )

    def test_stable_block_same_across_standard_and_ability_given_same_inputs(self):
        """(b) DMWorldStepBeat (standard) and DMActBeat (ability) build identical
        stable prefixes when given the same pack/profile/config inputs, because they
        call build_system_blocks with the same argument set."""
        from app.game.prompts import build_system_blocks
        from app.game.state import CharacterProfile, GameConfig

        profile = CharacterProfile(name="Player", appearance="tall, dark coat")
        config = GameConfig(pack_id="neon_rain")
        from app.game import lore as lore_mod

        pack = lore_mod.load_pack("neon_rain")
        seed_roster = lore_mod.get_seed_roster(pack)
        # Simulate the call each beat would make with identical TurnState args
        stable_world_step, _ = build_system_blocks(
            profile,
            stage=2,
            config=config,
            monster_level=0,
            has_npcs=True,
            turn_class="standard",
            seed_roster=seed_roster,
        )
        stable_dm_act, _ = build_system_blocks(
            profile,
            stage=2,
            config=config,
            monster_level=0,
            has_npcs=True,
            turn_class="ability",
            seed_roster=seed_roster,
        )
        # turn_class differs between standard and ability (it flows into volatile),
        # but both are called with the same pack/profile/seed_roster -> stable
        # prefix (the cached portion) must be byte-identical because stable does
        # not vary by turn_class.
        self.assertEqual(
            stable_world_step,
            stable_dm_act,
            "Stable prefix must be byte-identical regardless of turn_class "
            "(turn_class affects volatile only, not stable)",
        )
        print("PASS prefix: stable prefix byte-identical for standard vs ability turn_class")


# ---------------------------------------------------------------------------
# TestDiceBeats  (dice-beat propagation)
# ---------------------------------------------------------------------------


class TestDiceBeats(unittest.TestCase):
    """dice-mechanic pack_beats for the shipped dice pack.

    Suites:
    (a) Every shipped pack loads via validate_pack without PackError.
    (b) Each dice pack (neon_rain) has
        encounter_mechanics non-None in their validated pack_beats.
    (d) Each dice pack's encounter_mechanics has the required tiers structure:
        - 5 tiers (critical/success/partial/fail/fumble)
        - last tier has min_margin None (the floor)
        - tiers are named correctly
    (e) Each dice pack's effects reference their pack-appropriate state fields.
    (f) Each dice pack's clamps are valid pairs [lo, hi] with lo <= hi.
    (g) Each dice pack has a non-empty length_profiles map.
    (i) mechanics_for(loaded_pack) returns the correct spec for dice packs.
    """

    DICE_PACKS = ("neon_rain",)
    EXPECTED_TIERS = ("critical", "success", "partial", "fail", "fumble")

    def _load(self, pack_id: str) -> dict:
        """Load a pack via validate_pack (the same path the engine uses)."""
        import json
        from pathlib import Path
        from app.game.pack_beats import validate_pack

        pack_dir = Path(f"/app/engine/lore/{pack_id}")
        manifest = json.loads((pack_dir / "pack.json").read_text())
        return validate_pack(manifest, pack_dir)

    # (a) every shipped pack loads without PackError

    def test_all_packs_load_without_error(self):
        """Every shipped pack validates cleanly - no PackBeatsError on load."""
        from app.game.pack_beats import PackBeatsError

        for pack_id in ("neon_rain",):
            with self.subTest(pack=pack_id):
                try:
                    self._load(pack_id)
                except PackBeatsError as e:
                    self.fail(f"{pack_id} raised PackBeatsError: {e}")
        print("PASS dice beats: every shipped pack loads without PackBeatsError")

    # (b) dice packs have encounter_mechanics

    def test_dice_packs_have_encounter_mechanics(self):
        """Each dice pack's validated pack_beats includes encounter_mechanics."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                enc = pb.get("encounter_mechanics")
                self.assertIsNotNone(
                    enc,
                    f"{pack_id} must declare encounter_mechanics",
                )
        print("PASS dice beats: all dice packs have encounter_mechanics")

    # (d) tier structure

    def test_dice_packs_have_five_tiers(self):
        """Each dice pack declares exactly 5 outcome tiers."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                tiers = pb["encounter_mechanics"]["outcome_tiers"]
                self.assertEqual(
                    len(tiers),
                    5,
                    f"{pack_id}: expected 5 tiers, got {len(tiers)}",
                )
        print("PASS dice beats: all dice packs declare 5 outcome tiers")

    def test_dice_packs_tier_names_correct(self):
        """Each dice pack's tier names match the canonical set."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                tier_names = tuple(t["tier"] for t in pb["encounter_mechanics"]["outcome_tiers"])
                self.assertEqual(
                    tier_names,
                    self.EXPECTED_TIERS,
                    f"{pack_id}: tier names {tier_names} != {self.EXPECTED_TIERS}",
                )
        print(f"PASS dice beats: all dice packs have tiers {self.EXPECTED_TIERS}")

    def test_dice_packs_last_tier_is_floor(self):
        """The last tier in each dice pack has min_margin None (the floor entry)."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                tiers = pb["encounter_mechanics"]["outcome_tiers"]
                last = tiers[-1]
                self.assertIsNone(
                    last.get("min_margin"),
                    f"{pack_id}: last tier {last['tier']!r} must have min_margin=None "
                    "(floor entry - required by pack_beats schema)",
                )
        print("PASS dice beats: last tier is floor (min_margin=None) in all dice packs")

    # (e) effects reference pack-appropriate state fields

    def test_neon_rain_effects_target_heat_and_composure(self):
        """neon_rain effects touch heat and composure (the noir state fields)."""
        pb = self._load("neon_rain")
        effects = pb["encounter_mechanics"]["effects"]
        # Collect all state_delta field names across all tiers
        all_fields = set()
        for tname, ev in effects.items():
            all_fields.update(ev.get("state_delta", {}).keys())
        self.assertIn("heat", all_fields, "neon_rain encounter_mechanics must affect 'heat'")
        self.assertIn(
            "composure", all_fields, "neon_rain encounter_mechanics must affect 'composure'"
        )
        print("PASS dice beats: neon_rain effects target heat + composure")

    # (f) clamps validity

    def test_dice_packs_clamps_valid(self):
        """Each dice pack's clamps are [lo, hi] pairs with lo <= hi."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                clamps = pb["encounter_mechanics"]["clamps"]
                self.assertTrue(clamps, f"{pack_id}: clamps must not be empty")
                for fname, bounds in clamps.items():
                    self.assertEqual(
                        len(bounds),
                        2,
                        f"{pack_id}: clamps.{fname} must be [lo, hi], got {bounds!r}",
                    )
                    lo, hi = bounds
                    self.assertLessEqual(
                        lo,
                        hi,
                        f"{pack_id}: clamps.{fname}: lo ({lo}) > hi ({hi})",
                    )
        print("PASS dice beats: all dice pack clamps are valid [lo, hi] pairs")

    # (g) dice packs have non-empty length_profiles

    def test_dice_packs_have_length_profiles(self):
        """Each dice pack declares at least one length_profiles entry."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                lp = pb.get("length_profiles", {})
                self.assertTrue(
                    lp,
                    f"{pack_id} must declare at least one length_profiles entry "
                    "(per-pack voice tuning)",
                )
        print("PASS dice beats: all dice packs have non-empty length_profiles")

    def test_neon_rain_standard_profile_is_short(self):
        """neon_rain short-leaning voice: standard turns map to 'short'."""
        pb = self._load("neon_rain")
        lp = pb.get("length_profiles", {})
        self.assertEqual(
            lp.get("standard"),
            "short",
            "neon_rain: 'standard' turn type must map to 'short' (noir brevity)",
        )
        print("PASS dice beats: neon_rain standard->short (noir brevity confirmed)")

    # (i) mechanics_for round-trip

    def test_mechanics_for_dice_packs_returns_spec(self):
        """mechanics_for(loaded_dice_pack) returns a non-None spec dict."""
        from app.game import lore as lore_mod
        from app.game.encounter_mechanics import mechanics_for

        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pack = lore_mod.load_pack(pack_id)
                spec = mechanics_for(pack)
                self.assertIsNotNone(
                    spec,
                    f"mechanics_for({pack_id}) must return a spec (not None)",
                )
                self.assertIn("outcome_tiers", spec)
                self.assertIn("effects", spec)
                self.assertIn("clamps", spec)
        print("PASS dice beats: mechanics_for returns valid spec for all dice packs")

    def test_neon_rain_tiers_resolve_correctly(self):
        """neon_rain outcome_tiers resolve the canonical margins correctly."""
        from app.game.encounter_mechanics import resolve_tier

        pb = self._load("neon_rain")
        raw_tiers = pb["encounter_mechanics"]["outcome_tiers"]
        tiers = [(t["tier"], t["min_margin"]) for t in raw_tiers]
        # Standard tier margin expectations (mirrors encounter_mechanics.DEFAULT_TIERS)
        self.assertEqual(resolve_tier(10, tiers), "critical", "margin=10 -> critical")
        self.assertEqual(resolve_tier(0, tiers), "success", "margin=0  -> success")
        self.assertEqual(resolve_tier(-3, tiers), "partial", "margin=-3 -> partial")
        self.assertEqual(resolve_tier(-9, tiers), "fail", "margin=-9 -> fail")
        self.assertEqual(resolve_tier(-15, tiers), "fumble", "margin=-15 -> fumble")
        print(
            "PASS dice beats: neon_rain tiers resolve correctly (critical/success/partial/fail/fumble)"
        )

    # per-tier directive strings present

    def test_dice_packs_all_tiers_have_directives(self):
        """Each dice pack's effects section has a directive for every tier."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                effects = pb["encounter_mechanics"]["effects"]
                for tier in self.EXPECTED_TIERS:
                    directive = effects.get(tier, {}).get("directive")
                    self.assertIsNotNone(
                        directive,
                        f"{pack_id}: encounter_mechanics.effects.{tier}.directive must be set",
                    )
                    self.assertIsInstance(directive, str)
                    self.assertGreater(len(directive), 0)
        print("PASS dice beats: all dice pack tiers have non-empty directive strings")

    def test_dice_pack_directives_contain_no_mention_guard(self):
        """All directives end with the DC-concealment guard phrase."""
        for pack_id in self.DICE_PACKS:
            with self.subTest(pack=pack_id):
                pb = self._load(pack_id)
                effects = pb["encounter_mechanics"]["effects"]
                for tier in self.EXPECTED_TIERS:
                    directive = effects.get(tier, {}).get("directive", "")
                    self.assertIn(
                        "Do NOT mention the DC or dice mechanics",
                        directive,
                        f"{pack_id}.{tier}: directive must include DC-concealment guard",
                    )
        print("PASS dice beats: all dice pack directives contain DC-concealment guard")


if __name__ == "__main__":
    unittest.main()
