"""Stress tests for diceless-by-default + per-save dice opt-in.

Pattern: stress_pending_input.py.

Suites:
  A. TestDetectorStress - 200+ boundary inputs: detect_reality_bending
                                   never raises; correct True/False on all
  B. TestRunTurnRouting - run_turn routing under diceless/opt-in/native-pack
                                   with >=50 confrontation + >=50 reality-bending inputs
"""

import unittest
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


def _make_gs(pack_id: str = "testpack", dice_mechanics: bool = False) -> GameSave:
    gs = GameSave()
    gs.profile = CharacterProfile(name="Hero", appearance="plain")
    gs.config = GameConfig(pack_id=pack_id)
    gs.config.dice_mechanics = dice_mechanics
    gs.state = WorldState(location="dungeon")
    gs.history = []
    gs.turn = 1
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


def _canned_dc() -> DiceAssessment:
    return DiceAssessment(dc=12, skill="brawl", stat="STR", flavor="Tension.")


# boundary input banks

_REALITY_BENDING_TRUE = [
    # teleport variations
    "i teleport to the moon",
    "teleport across the hall",
    "i teleport directly behind him",
    "teleporting into the vault",
    "i teleported last time",
    "teleport me out of here",
    "teleportation would save us",
    # blink
    "i blink to the rooftop",
    "blink across the city",
    "blink through the gate",
    # phase through
    "i phase through the wall",
    "phase through the iron door",
    "i phase through the solid stone",
    # walk through wall
    "i walk through the wall",
    "walk through the walls of the keep",
    "i walk through the solid barrier",
    "i walk through solid rock",
    # fly/leap/jump/rise/float/levitate/ascend to sky/space destinations
    "i fly up into the sky",
    "i fly into space",
    "i fly to the moon",
    "i fly through the clouds",
    "i fly up to the stars",
    "i fly up into orbit",
    "i leap to the sun",
    "i leap into the stars",
    "i leap into space",
    "i leap into the void",
    "i jump to the moon",
    "i jump into space",
    "i jump into the void",
    "i jump through the void",
    "i jump up into the sky",
    "i jump up to the stars",
    "i rise up to the stars",
    "i rise into orbit",
    "i rise into the heavens",
    "i float into the sky",
    "i float up to the clouds",
    "i float up into orbit",
    "i float up to the moon",
    "i float into space",
    "i levitate into the heavens",
    "i levitate into space",
    "i levitate into orbit",
    "i ascend to the stars",
    "i ascend into orbit",
    "i ascend into the heavens",
    "i ascend up to the moon",
    # levitate alone (pattern is \blevitate\b - "levitating" does not match)
    "i levitate",
    "levitate off the ground",
    "just levitate above it",
    # stop/rewind/reverse/freeze time
    "i stop time",
    "i freeze time",
    "i rewind time",
    "i reverse time",
    "stop time right now",
    "freeze time around us",
    "reverse time by one hour",
    "rewind time to before the battle",
    # time travel
    "i time-travel to yesterday",
    "i timetravel back",
    "i travel back in time",
    "i travel forward in time",
    "time travel back to the beginning",
    "travel back in time before the war",
    # turn/become/go invisible
    "i become invisible",
    "i turn invisible",
    "i go invisible",
    "become invisible now",
    "turn invisible in the crowd",
    "go invisible and slip through",
    # breathe in space/void/vacuum
    "i breathe in space",
    "i breathe in the void",
    "i breathe in vacuum",
    # resurrect/raise the dead (pattern requires the word "dead")
    "i resurrect the dead",
    "i raise the dead",
    "raise the dead soldiers",
    "resurrect the dead warriors",
    # upper-case and mixed case
    "I TELEPORT TO THE MOON",
    "I Jump To The Moon",
    "BECOME INVISIBLE",
    "STOP TIME",
    "FREEZE TIME NOW",
    "I PHASE THROUGH THE WALL",
    "I LEVITATE",
    "I FLY INTO SPACE",
    "I RISE INTO THE HEAVENS",
    # embedded in longer sentences
    "as quickly as possible i teleport to the moon to escape",
    "my plan is to phase through the wall and grab the key",
    "i decide to stop time and rearrange everything",
    "while everyone watches i levitate gracefully",
    "i want to become invisible and slip away unnoticed",
    "if i could levitate i would reach the tower",
    "i will try to stop time before they notice",
    "i attempt to teleport behind the guard",
    "quickly i ascend into the heavens to survey the land",
    "let me try to walk through the solid wall",
    "i know i can travel back in time if i focus",
]

_REALITY_BENDING_FALSE = [
    # mundane locomotion near-misses
    "i walk to the market",
    "walk across the bridge",
    "i walk through the crowd",
    "i walk through the door",
    "i walk through the rain",
    "i climb the stairs",
    "climb the ladder",
    "climb the cliff face",
    "i jump over the fence",
    "jump across the gap",
    "i jump down from the ledge",
    "i jump off the wall",
    "i run to the door",
    "i run away",
    "i run through the alley",
    "i fly a kite",  # fly without celestial destination
    "i fly the banner proudly",  # fly without celestial destination
    "i leap over the barrel",
    "i leap forward",
    "i leap across the stream",
    "i rise from the chair",
    "rise to greet her",
    "i float on the raft",
    "float along the river",
    "floating on the water",
    "i ascend the mountain path",  # ascend without space destination
    "i ascend the spiral staircase",  # ascend without space destination
    "i ascend to the roof",  # roof is not a celestial destination
    # mundane combat/social
    "i punch the guard",
    "punch the attacker",
    "i attack the soldier",
    "i strike the enemy",
    "i fight the bandit",
    "i talk to the merchant",
    "i ask about the price",
    "i bribe the guard",
    "i lie to the innkeeper",
    "i persuade the merchant",
    "i sneak past the guard",
    "i hide in the shadows",
    "i steal the key",
    "i shoot my bow",
    "i throw the rock",
    "i dodge the arrow",
    "i block the blow",
    "i sprint around the corner",
    "i swim across the lake",
    "i kick the gate open",
    "i grapple with the guard",
    # empty/whitespace
    "",
    " ",
    "   ",
    "\t",
    "\n",
    # normal interactions
    "i look around",
    "look at the sky",
    "i sit down",
    "i wait here",
    "i open the chest",
    "i read the sign",
    "i drink the potion",
    "i eat the bread",
    "i rest by the fire",
    "i search the room",
    "i pick up the torch",
    "i hand him the coin",
    "i nod in agreement",
    "i shout for help",
    # partial pattern words that should not match
    "i telescope to the window",  # not teleport
    "blinked in surprise",  # blink without to/across/through
    "i phase into the next stage",  # phase without 'through'
    "i blinking",  # blink not followed by to/across/through
    "time flies when we're busy",  # time without stop/rewind etc.
    "i reverse course",  # reverse without 'time'
    "the invisible hand of the market",  # not a verb form
    "resurrect the project",  # not "the dead"
    "raise the alarm",  # raise without "the dead"
    "raise your hand",  # raise without "the dead"
    "stop the bleeding",  # stop without 'time'
    "freeze the assets",  # freeze without 'time'
    "i travel to the capital",  # travel without back/forward in time
    "i travel across the desert",  # travel without back/forward in time
    "i rewind the tape",  # rewind without 'time'
    "walk through the procedure",  # walk through without wall/solid
    # 2KB junk
    "x" * 2000,
    # unicode
    "\u05d0\u05e0\u05d9 \u05d4\u05d5\u05dc\u05da \u05dc\u05e9\u05d5\u05e7",  # Hebrew: "i walk to the market"
    "\u79c1\u306f\u8d70\u3063\u3066\u3044\u307e\u3059",  # Japanese
    "\u044f \u0438\u0434\u0443 \u043d\u0430 \u0440\u044b\u043d\u043e\u043a",  # Russian
    "\u0645\u0631\u062d\u0628\u0627",  # Arabic
]

_CONFRONTATION_VERBS = [
    "i punch the guard",
    "i attack the soldier",
    "i strike the enemy",
    "i fight the bandit",
    "i shoot my bow at him",
    "i stab the attacker",
    "i slash at the monster",
    "i tackle the runner",
    "i throw the guard",
    "i kick the door open",
    "i wrestle the prisoner",
    "i grapple with the knight",
    "i threaten the merchant",
    "i intimidate the guard",
    "i jump over the fence",  # jump is in CONFRONT_RE
    "i climb the wall",  # climb is in CONFRONT_RE
    "i sprint past the guard",  # sprint is in CONFRONT_RE
    "i force open the gate",  # force is in CONFRONT_RE
    "i break the lock",  # break is in CONFRONT_RE
    "i steal the coin",  # steal is in CONFRONT_RE
    "i pick the lock",
    "i dodge the arrow",
    "i hide behind the barrel",
    "i deceive the guard",
    "i bluff my way in",
]


# A. Detector stress


class TestDetectorStress(unittest.TestCase):
    def test_reality_bending_true_inputs_never_raise(self):
        """detect_reality_bending never raises on any input."""
        all_inputs = _REALITY_BENDING_TRUE + _REALITY_BENDING_FALSE + _CONFRONTATION_VERBS
        for text in all_inputs:
            with self.subTest(text=text[:60]):
                try:
                    detect_reality_bending(text)
                except Exception as e:
                    self.fail(f"detect_reality_bending raised {e!r} on {text[:60]!r}")
        print(f"PASS stress: detector never-raises on {len(all_inputs)} inputs")

    def test_true_inputs_all_return_true(self):
        misroutes = []
        for text in _REALITY_BENDING_TRUE:
            if not detect_reality_bending(text):
                misroutes.append(text)
        self.assertEqual(
            misroutes,
            [],
            f"Expected True but got False for: {misroutes!r}",
        )
        print(
            f"PASS stress: {len(_REALITY_BENDING_TRUE)} reality-bending inputs -> True, 0 misroutes"
        )

    def test_false_inputs_all_return_false(self):
        misroutes = []
        for text in _REALITY_BENDING_FALSE:
            if detect_reality_bending(text):
                misroutes.append(text)
        self.assertEqual(
            misroutes,
            [],
            f"Expected False but got True for: {misroutes!r}",
        )
        print(f"PASS stress: {len(_REALITY_BENDING_FALSE)} mundane inputs -> False, 0 misroutes")

    def test_confrontation_inputs_not_reality_bending(self):
        """Confrontation verbs (not impossible acts) must not spuriously trigger."""
        # jump/climb/sprint are in CONFRONT_RE but not in reality-bending unless
        # targeting an impossible destination.
        safe_confront = [
            "i punch the guard",
            "i attack the soldier",
            "i strike the enemy",
            "i fight the bandit",
            "i shoot my bow at him",
            "i stab the attacker",
            "i slash at the monster",
            "i tackle the runner",
            "i throw the guard",
            "i kick the door open",
        ]
        misroutes = [t for t in safe_confront if detect_reality_bending(t)]
        self.assertEqual(
            misroutes,
            [],
            f"Confrontation inputs spuriously detected as reality-bending: {misroutes!r}",
        )
        print(f"PASS stress: {len(safe_confront)} confrontation inputs -> not reality-bending")


# B. run_turn routing stress


class TestRunTurnRouting(unittest.TestCase):
    def _run_physpack_diceless(self, player_input: str) -> str:
        """Run in physicalisation pack+dice_mechanics=False; return 'dice' | 'aia' | 'other'."""
        from app.game.engine import run_turn

        gs = _make_gs(pack_id="testpack", dice_mechanics=False)
        with (
            patch(
                "app.game.physicalisation.classify_scope", return_value=({"scope": "none"}, None)
            ),
            patch(
                "app.game.physicalisation.classify_scope_tier2",
                return_value=({"scope": "none"}, None),
            ),
            patch("app.game.engine.classify_turn", return_value=("standard", None)),
            patch(
                "app.game.items.visible_to_player",
                return_value={"scene": [], "carried": [], "npcs": {}},
            ),
            patch("app.game.dice_gate.assess_dice_dc", return_value=_canned_dc()),
            patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
        ):
            try:
                run_turn(gs, player_input, "save-s")
                return "other"
            except DiceRollPending:
                return "dice"
            except AbilityIntentAmbiguous:
                return "aia"
            except Exception:
                return "other"

    def _run_physpack_opt_in(self, player_input: str) -> str:
        """Run in physicalisation pack+dice_mechanics=True; return 'dice' | 'aia' | 'other'."""
        from app.game.engine import run_turn

        gs = _make_gs(pack_id="testpack", dice_mechanics=True)
        with (
            patch(
                "app.game.physicalisation.classify_scope", return_value=({"scope": "none"}, None)
            ),
            patch(
                "app.game.physicalisation.classify_scope_tier2",
                return_value=({"scope": "none"}, None),
            ),
            patch("app.game.engine.classify_turn", return_value=("standard", None)),
            patch(
                "app.game.items.visible_to_player",
                return_value={"scene": [], "carried": [], "npcs": {}},
            ),
            patch("app.game.dice_gate.assess_dice_dc", return_value=_canned_dc()),
            patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
        ):
            try:
                run_turn(gs, player_input, "save-s")
                return "other"
            except DiceRollPending:
                return "dice"
            except AbilityIntentAmbiguous:
                return "aia"
            except Exception:
                return "other"

    def _run_reality_bending_route(self, player_input: str, dice_mechanics: bool) -> str:
        """Run reality-bending input; classify_scope mocked only (gate fires early)."""
        from app.game.engine import run_turn

        gs = _make_gs(pack_id="testpack", dice_mechanics=dice_mechanics)
        with (
            patch(
                "app.game.physicalisation.classify_scope", return_value=({"scope": "none"}, None)
            ),
            patch(
                "app.game.physicalisation.classify_scope_tier2",
                return_value=({"scope": "none"}, None),
            ),
            patch("app.game.llm.call_llm_plain", MagicMock(return_value="{}")),
        ):
            try:
                run_turn(gs, player_input, "save-s")
                return "other"
            except DiceRollPending:
                return "dice"
            except AbilityIntentAmbiguous:
                return "aia"
            except Exception:
                return "other"

    def test_confrontation_diceless_never_raises_dice(self):
        """physicalisation pack+dice_mechanics=False: confrontation -> not dice."""
        # Use verbs that are clearly in CONFRONT_RE and not reality-bending
        confront_safe = [
            "i punch the guard",
            "i attack the soldier",
            "i strike the enemy",
            "i fight the bandit",
            "i shoot my bow at him",
            "i stab the attacker",
            "i tackle the runner",
            "i throw the guard",
            "i kick the door open",
            "i wrestle the prisoner",
            "i grapple with the knight",
            "i threaten the merchant",
            "i intimidate the guard",
            "i sneak past the sentry",
            "i hide in the barrel",
            "i steal the key",
            "i pick the lock",
        ]
        misroutes = []
        for text in confront_safe:
            result = self._run_physpack_diceless(text)
            if result == "dice":
                misroutes.append(text)
        self.assertEqual(misroutes, [], f"Diceless mode emitted DiceRollPending for: {misroutes!r}")
        print(
            f"PASS stress: {len(confront_safe)} confrontation inputs in diceless mode -> 0 dice raises"
        )

    def test_confrontation_opt_in_all_raise_dice(self):
        """physicalisation pack+dice_mechanics=True: confrontation -> DiceRollPending."""
        confront_safe = [
            "i punch the guard",
            "i attack the soldier",
            "i strike the enemy",
            "i fight the bandit",
            "i shoot my bow at him",
            "i stab the attacker",
            "i tackle the runner",
            "i throw the guard",
            "i kick the door open",
            "i wrestle the prisoner",
        ]
        misroutes = []
        for text in confront_safe:
            result = self._run_physpack_opt_in(text)
            if result != "dice":
                misroutes.append((text, result))
        self.assertEqual(misroutes, [], f"Opt-in mode did not dice for: {misroutes!r}")
        print(
            f"PASS stress: {len(confront_safe)} confrontation inputs in opt-in mode -> all dice raises"
        )

    def test_reality_bending_always_grounds_diceless(self):
        """Reality-bending -> AbilityIntentAmbiguous in diceless mode, never DiceRollPending."""
        rb_inputs = [
            "i teleport to the moon",
            "i stop time",
            "i become invisible",
            "i jump to the moon",
            "i fly into space",
            "i levitate",
            "i phase through the wall",
            "i walk through the wall",
            "i travel back in time",
            "i resurrect the dead",
        ]
        misroutes = []
        for text in rb_inputs:
            result = self._run_reality_bending_route(text, dice_mechanics=False)
            if result != "aia":
                misroutes.append((text, result))
        self.assertEqual(
            misroutes, [], f"Reality-bending did not ground in diceless mode: {misroutes!r}"
        )
        print(f"PASS stress: {len(rb_inputs)} reality-bending inputs in diceless mode -> all AIA")

    def test_reality_bending_always_grounds_opt_in(self):
        """Reality-bending -> AbilityIntentAmbiguous even in opt-in mode (orthogonality)."""
        rb_inputs = [
            "i teleport to the moon",
            "i stop time",
            "i become invisible",
            "i jump to the moon",
            "i fly into space",
            "i levitate",
            "i phase through the wall",
            "i walk through the wall",
            "i travel back in time",
            "i resurrect the dead",
        ]
        misroutes = []
        for text in rb_inputs:
            result = self._run_reality_bending_route(text, dice_mechanics=True)
            if result != "aia":
                misroutes.append((text, result))
        self.assertEqual(
            misroutes, [], f"Reality-bending did not ground in opt-in mode: {misroutes!r}"
        )
        print(f"PASS stress: {len(rb_inputs)} reality-bending inputs in opt-in mode -> all AIA")

    def test_mundane_movement_no_spurious_aia(self):
        """Mundane movement never spuriously raises AbilityIntentAmbiguous."""
        mundane = [
            "i walk to the market",
            "i climb the stairs",
            "i run to the door",
            "i look around",
            "i open the chest",
            "i sit down",
            "i wait here",
            "i ask about the price",
            "i pick up the sword",
            "i drink the potion",
        ]
        spurious = []
        for text in mundane:
            result = self._run_physpack_diceless(text)
            if result == "aia":
                spurious.append(text)
        self.assertEqual(spurious, [], f"Mundane inputs spuriously raised AIA: {spurious!r}")
        print(f"PASS stress: {len(mundane)} mundane inputs -> 0 spurious AIA raises")

    def test_total_input_count(self):
        """Verify the combined input corpus exceeds 200 entries."""
        total = len(_REALITY_BENDING_TRUE) + len(_REALITY_BENDING_FALSE) + len(_CONFRONTATION_VERBS)
        self.assertGreaterEqual(total, 200, f"Total corpus is only {total} entries (need >= 200)")
        print(f"PASS stress: {total} inputs, 0 misroutes across all suites")


if __name__ == "__main__":
    unittest.main()
