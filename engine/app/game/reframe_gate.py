"""Wholesale-state-reframe gate.

When a player declares a wholesale change to the world state in a
single turn ('instead of tanks we'll now make cars', 'we transition
to peace', 'all territories must adopt the 10% tax') - the DM
would otherwise resolve years of work in one beat with no
confirmation. Surface a modal so the player sees the projected
state diff before committing.

Architecture mirrors ability-intent gate:
  - detector regex (broad, prefers false positive over miss)
  - ReframeAmbiguous exception raised by engine.run_turn
  - API handler returns 200 with the modal payload
  - Player POSTs back with reframe_confirm=True | False

The state-diff preview is lightweight: it surfaces the player's input
pattern and a list of state keys likely affected.
"""

from __future__ import annotations

import re
from typing import Optional
from app.game.gate_base import TurnGate


# detection patterns


# Patterns that signal a wholesale-state-reframe turn.
_PATTERN_INSTEAD_OF = re.compile(
    r"\binstead\s+of\s+\w+(?:\s+\w+){0,3}\s+(?:we'?ll|we\s+will|we\s+now|i'?ll|i\s+will|we\s+begin|we\s+(?:transition|switch)\s+to)\b",
    re.IGNORECASE,
)
_PATTERN_TRANSITION = re.compile(
    r"\b(?:"
    r"(?:we|i)\s+(?:transition|pivot|shift|convert|switch)\s+(?:to|from|into|all)|"
    r"(?:the\s+war\s+is\s+over)|"
    r"(?:end\s+the\s+war|peacetime\s+begins)|"
    r"from\s+now\s+on\s+(?:we|everyone|all)\s+(?:will|must|shall)|"
    r"complete\s+(?:development|conquest|unification|integration)\s+of\s+(?:all|every)|"
    r"all\s+territories?\s+(?:must|will|shall)"
    r")\b",
    re.IGNORECASE,
)


# Heuristic: which top-level state keys are likely affected by a
# wholesale reframe? Used for the diff preview.
_LIKELY_AFFECTED_KEYS = (
    "active_threats",
    "active_quests",
    "npc_dispositions",
    "tech_level",
    "stability",
    "treasury",
    "food",
    "year",
    "season",
    "arc",
)


def detect_reframe(player_input: str) -> Optional[str]:
    """Return a short label identifying the reframe pattern that
    matched, or None when no reframe is detected. Labels are stable
    so the modal can show a relevant header."""
    if not player_input:
        return None
    if _PATTERN_INSTEAD_OF.search(player_input):
        return "instead_of_substitution"
    if _PATTERN_TRANSITION.search(player_input):
        return "transition_or_finalisation"
    return None


# exception + payload


class ReframeAmbiguous(TurnGate):
    """Raised by run_turn when a wholesale-reframe pattern is detected
    and the player hasn't yet confirmed. API catches this and returns a
    200 with the modal payload. The save is not mutated on the gate
    path - re-posting with reframe_confirm=True re-runs the turn cleanly."""

    def __init__(self, label: str, likely_affected: list[str]):
        super().__init__("reframe_confirmation_needed")
        self.label = label
        self.likely_affected = likely_affected

    def to_payload(self) -> dict:
        return {
            "kind": "reframe_confirmation_needed",
            "label": self.label,
            "likely_affected": self.likely_affected,
            "message": (
                "You're declaring a sweeping change that will "
                "reshape your world in one turn. Confirm before "
                "committing? The DM will narrate years of "
                "consequence in a single response."
            ),
            "options": [
                {
                    "value": "confirm",
                    "label": "Yes, commit the change",
                    "hint": "The DM will narrate the transition. Old state may obsolete.",
                },
                {
                    "value": "cancel",
                    "label": "No, let me rephrase",
                    "hint": "The turn is discarded; revise your input and try again.",
                },
            ],
        }


# helpers consumed by engine.run_turn


def likely_affected_keys(state) -> list[str]:
    """Filter _LIKELY_AFFECTED_KEYS down to those actually present in
    the current state - keeps the preview honest."""
    return [k for k in _LIKELY_AFFECTED_KEYS if state.get(k) is not None]
