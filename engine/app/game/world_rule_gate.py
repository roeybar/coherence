"""World-rule shift gate.

When a player declares a new permanent rule for their world, we stop
and confirm before mutating state. The existing parse_world_rule regex
is the cheap pre-filter; if it fires we escalate to a haiku classifier
that distinguishes genuine paradigm-shift declarations from false
positives (e.g. "no one saw the fight" passing the regex).

Flow:
  1. Regex pre-filter (parse_world_rule) - already in engine.py.
  2. classify_rule_shift - haiku binary classifier. Returns None if
     this is not a paradigm shift (turn proceeds normally). Returns a
     dict {rule_text, scope, projected_effects} when it is one.
  3. Engine raises WorldRuleShiftPending; API returns 200 modal payload.
  4. Player posts back with world_rule_confirm="confirm".
  5. Engine writes to state.world_rules + injects "world reacts" beat
     instruction into the world_systems_block for this turn only.

Failure policy: if the haiku call fails, default to treating the input
as a paradigm shift (gate fires). This prevents silent writes on
classifier outage.
"""

from __future__ import annotations

import json
import logging
from typing import Optional
from app.game.gate_base import TurnGate
from app.game import moderation

log = logging.getLogger(__name__)

_CLASSIFIER_SYSTEM = """\
You are a game-master assistant. Determine whether the player's input
declares a new PERMANENT world rule, law, or policy - a paradigm shift
that will reshape how the world operates going forward.

Paradigm shifts include:
  - Banning or outlawing a technology / practice / group for years
  - Decreeing a mandatory policy for all subjects / citizens
  - Declaring a tax, tithe, or tribute that changes the economy
  - Enacting a constitutional rule ("from now on X is forbidden")

NOT paradigm shifts:
  - Describing a past event ("no one saw the fight")
  - Commanding a specific NPC ("no, I told you not to enter")
  - Expressing the player character's personal preference
  - Rhetorical / figurative language

Respond ONLY with a JSON object - no prose, no markdown fences:
{
  "is_paradigm_shift": true | false,
  "rule_text": "< <=80-char canonical rule text, or empty string>",
  "scope": "economic | political | technological | social | military | other",
  "projected_effects": ["<<=60-char effect>", ...]
}
projected_effects: 2-4 short bullets describing what changes in-world.
If is_paradigm_shift is false, set rule_text="" and projected_effects=[].
"""


def classify_rule_shift(player_input: str) -> Optional[dict]:
    """Run haiku classifier. Returns None (not a paradigm shift) or
    {rule_text, scope, projected_effects} (gate should fire).

    Raises nothing - failure defaults to treating as paradigm_shift=True
    so callers can safely do:
        result = classify_rule_shift(inp)
        if result is not None:
            raise WorldRuleShiftPending(...)
    """
    if not player_input:
        return None
    try:
        from app.game.llm import MODEL_HAIKU, call_llm_plain

        raw = call_llm_plain(
            system=_CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": player_input[:500]}],
            model=MODEL_HAIKU,
            max_tokens=200,
        )
        data = json.loads(raw.strip())
        moderation.check_decoded(data)
        if not data.get("is_paradigm_shift"):
            return None
        return {
            "rule_text": (data.get("rule_text") or "").strip()[:120],
            "scope": data.get("scope") or "other",
            "projected_effects": [e[:80] for e in (data.get("projected_effects") or [])[:4]],
        }
    except Exception as exc:
        log.warning("world_rule_gate classifier failed (%s) - defaulting to paradigm_shift", exc)
        # Safe default: treat as paradigm shift so the turn is gated.
        return {
            "rule_text": player_input[:120],
            "scope": "other",
            "projected_effects": [],
        }


class WorldRuleShiftPending(TurnGate):
    """Raised by engine.run_turn when a world-rule paradigm shift is
    detected and the player hasn't confirmed yet. The save is not
    mutated - re-posting with world_rule_confirm='confirm' retries cleanly."""

    def __init__(self, rule_text: str, scope: str, projected_effects: list[str]):
        super().__init__("world_rule_shift_pending")
        self.rule_text = rule_text
        self.scope = scope
        self.projected_effects = projected_effects

    def to_payload(self) -> dict:
        return {
            "kind": "world_rule_shift_pending",
            "rule_text": self.rule_text,
            "scope": self.scope,
            "projected_effects": self.projected_effects,
            "message": (
                "You're enacting a permanent world rule. "
                "Once confirmed, the DM will narrate the world's "
                "reaction and carry this constraint forward in every "
                "subsequent turn."
            ),
            "options": [
                {
                    "value": "confirm",
                    "label": "Enact the rule",
                    "hint": "The world reacts this turn; the rule persists.",
                },
                {
                    "value": "cancel",
                    "label": "Cancel, rephrase",
                    "hint": "The turn is discarded; no state is changed.",
                },
            ],
        }


def world_reacts_beat(rule_text: str, scope: str) -> str:
    """Compose the DM-beat injection for a newly confirmed world rule.
    Returned string is appended to the world_systems_block for this
    turn only - instructs the DM to narrate immediate consequences."""
    return (
        "WORLD-RULE JUST ENACTED (player confirmed this turn):\n"
        f'  Rule: "{rule_text}"\n'
        f"  Scope: {scope}\n"
        "  The DM MUST narrate an immediate world reaction this turn - "
        "how NPCs, factions, or systems present in the scene respond to "
        "this new edict. Show tangible in-scene consequences, not passive "
        "acknowledgement. The rule now persists into all future turns."
    )
