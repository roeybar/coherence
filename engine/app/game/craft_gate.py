"""Craft detection + DC assessment.

detect_craft(player_input, item_view) -> bool
    Lexical: craft verb and >=1 item visible in scene or carried.
    Pure item-presence check prevents false positives on social actions
    ('make peace', 'create a distraction') that have no physical inputs.

assess_craft_dc(player_input, item_view, state) -> CraftAssessment
    Single haiku call -> dc, target, inputs_committed, difficulty_label, flavor.

craft_outcome_tier(roll, dc) -> str
    Maps a d20 roll vs DC to 'critical'/'success'/'partial'/'fail'.

CraftRollPending - raised by engine.run_turn when craft detected but no
    craft_roll passback. Save is not mutated on gate path.
"""

from __future__ import annotations

import json
import logging
import re

from app.game.llm import MODEL_HAIKU, call_llm_plain
from app.game import moderation
from app.game.gate_base import TurnGate, record_aux_call

log = logging.getLogger("craft_gate")

_CRAFT_RE = re.compile(
    r"\b(craft|make|forge|build|combine|assemble|brew|cook|mix|fashion"
    r"|construct|create|produce|fabricate|shape|carve|weave|bind)\b",
    re.IGNORECASE,
)

# Shaping one thing into another without a craft verb ("twist the wire into a
# lockpick"): one of these verbs, one to four object words (no comma, semicolon,
# "and" or "then" between), then "into".
_CRAFT_INTO_RE = re.compile(
    r"\b(?:twist|bend|file|whittle|sharpen|fold|braid|splice|solder|knot|rig|cobble"
    r"|hammer|melt|wrap|wind|cut|tear|strip)\s+(?:(?!(?:and|then|through|across|past|along|around|over|toward|towards|way)\b)[\w'\-]+\s+){1,4}?into\b",
    re.IGNORECASE,
)


def _difficulty_label(dc: int) -> str:
    if dc <= 8:
        return "trivial"
    if dc <= 12:
        return "moderate"
    if dc <= 16:
        return "difficult"
    if dc <= 20:
        return "hard"
    return "legendary"


def detect_craft(player_input: str, item_view: dict) -> bool:
    """True when the player input contains a craft verb and >=1 item is in scope."""
    if not (_CRAFT_RE.search(player_input) or _CRAFT_INTO_RE.search(player_input)):
        return False
    all_items = list(item_view.get("scene", [])) + list(item_view.get("carried", []))
    return bool(all_items)


class CraftAssessment:
    __slots__ = ("dc", "difficulty_label", "target", "inputs_committed", "flavor")

    def __init__(self, dc: int, target: str, inputs_committed: list, flavor: str):
        self.dc = dc
        self.difficulty_label = _difficulty_label(dc)
        self.target = target
        self.inputs_committed = inputs_committed
        self.flavor = flavor


def assess_craft_dc(
    player_input: str, item_view: dict, state, aux_llm_rows: list | None = None
) -> CraftAssessment:
    """Single haiku call to assess craft difficulty and parameters."""
    all_items = list(item_view.get("scene", [])) + list(item_view.get("carried", []))
    names = [it.get("name") or "item" for it in all_items]

    prompt = (
        f'Player action: "{player_input}"\n'
        f"Available items: {', '.join(names)}\n"
        f"Tech level: {state.get('tech_level') or 'early medieval'}\n"
        f"Location: {state.get('location') or 'unknown'}\n\n"
        "Assess the crafting attempt. Reply JSON only, no markdown:\n"
        '{"dc":<int 1-25>,"target":"<what is made, max 8 words>",'
        '"inputs_committed":["<item>",...],"flavor":"<one terse sentence, max 20 words>"}\n'
        "DC guide: trivial 1-8, moderate 9-12, difficult 13-16, hard 17-20, legendary 21+.\n"
        "inputs_committed: items actually consumed (subset of available, max 4 items).\n"
        "If the craft makes no sense with available items, dc=25, explain briefly in flavor."
    )

    try:
        text, _usage = call_llm_plain(
            "",
            [{"role": "user", "content": prompt}],
            model=MODEL_HAIKU,
            max_tokens=120,
        )
        # real spend immediately before the gate raises - accumulate it.
        record_aux_call(aux_llm_rows, "aux:craft_dc", MODEL_HAIKU, _usage)
        data = json.loads(text.strip())
        moderation.check_decoded(data)
        dc = max(1, min(25, int(data.get("dc", 12))))
        target = str(data.get("target") or "crafted item")[:60]
        inputs = [str(x)[:40] for x in (data.get("inputs_committed") or [])[:4]]
        flavor = str(data.get("flavor") or "")[:140]
        moderation.check_decoded({"target": target, "inputs": inputs, "flavor": flavor})
    except Exception:
        log.warning("assess_craft_dc parse failed; using defaults", exc_info=True)
        dc, target, inputs = 12, "crafted item", names[:2]
        flavor = "The work requires care and skill."

    return CraftAssessment(dc=dc, target=target, inputs_committed=inputs, flavor=flavor)


def craft_outcome_tier(roll: int, dc: int) -> str:
    """Map a d20 roll vs DC to an outcome label.

    critical - roll >= dc+5  (exceptional, bonus property)
    success - roll >= dc    (clean success)
    partial - roll >= dc-4  (success with complication / reduced quality)
    fail - roll < dc-4  (failure, inputs consumed)
    """
    if roll >= dc + 5:
        return "critical"
    if roll >= dc:
        return "success"
    if roll >= dc - 4:
        return "partial"
    return "fail"


class CraftRollPending(TurnGate):
    """Raised when a craft attempt is detected but no roll has been submitted.
    The frontend pops CraftRollModal; the player rolls; re-POST with craft_roll.
    Save is not mutated on this path."""

    __slots__ = ("dc", "difficulty_label", "target", "inputs_committed", "flavor")

    def __init__(self, assessment: CraftAssessment):
        self.dc = assessment.dc
        self.difficulty_label = assessment.difficulty_label
        self.target = assessment.target
        self.inputs_committed = assessment.inputs_committed
        self.flavor = assessment.flavor

    def to_payload(self) -> dict:
        return {
            "kind": "craft_roll_needed",
            "dc": self.dc,
            "difficulty_label": self.difficulty_label,
            "target": self.target,
            "inputs_committed": self.inputs_committed,
            "flavor": self.flavor,
        }
