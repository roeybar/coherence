"""Dice-roll gate.

Two detection paths:

  1. Pre-turn (proactive) - detect_dice_request classifies whether the
     player's action warrants a roll before the DM runs. Engine raises
     DiceRollPending; the web client shows an inline dice gate. On re-POST the roll
     result is injected into the DM context so the DM narrates the outcome.

  2. Post-DM (reactive) - detect_dice_in_narrative scans the DM's
     finished text for a [ROLL:skill:dc] structural marker. If found the
     engine strips the marker, attaches the pre-roll narrative to the gate
     payload, and raises DiceRollPending without committing state. On re-POST
     the engine re-runs with the roll result and the pending narrative as
     additional context.

DiceRollPending - raised when a roll is required but not yet provided.
Save is not mutated on either gate path.
"""

from __future__ import annotations

import json
import logging
import re

from app.game.llm import MODEL_HAIKU, call_llm_plain
from app.game import moderation
from app.game.gate_base import TurnGate, record_aux_call

log = logging.getLogger("dice_gate")

# confrontational lexicon
_CONFRONT_RE = re.compile(
    r"\b(attack|fight|strike|punch|stab|shoot|slash|tackle|charge|dodge|"
    r"parry|block|evade|sneak|hide|steal|pickpocket|climb|jump|leap|swim|"
    r"sprint|force|break|kick|throw|wrestle|grapple|disarm|intimidate|"
    r"threaten|bribe|lie|deceive|bluff|seduce|persuade|hack|bypass|lock|"
    r"disable|dismantle|repair)\b",
    re.IGNORECASE,
)

# post-DM marker
# DM embeds [ROLL:SkillName:DC] in narrative to request a roll.
_ROLL_MARKER_RE = re.compile(
    r"\[ROLL:([^:\]]{1,40}):(\d{1,2})\]",
    re.IGNORECASE,
)


# helpers


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


class DiceAssessment:
    __slots__ = ("dc", "difficulty_label", "skill", "stat", "flavor", "pending_narrative")

    def __init__(self, dc: int, skill: str, stat: str, flavor: str, pending_narrative: str = ""):
        self.dc = dc
        self.difficulty_label = _difficulty_label(dc)
        self.skill = skill
        self.stat = stat
        self.flavor = flavor
        self.pending_narrative = pending_narrative  # only set on post-DM path


# path 1: pre-turn detection


def detect_dice_request(player_input: str, state) -> bool:
    """True when the player input contains a confrontational verb and
    the scene has enough tension to warrant a roll.

    Pure lexical so it's fast and free. The haiku DC-assessment only
    runs when this returns True.
    """
    if not _CONFRONT_RE.search(player_input):
        return False
    # Don't double-gate on turns that are already gated as crafting.
    # (Engine handles ordering; this guard is belt-and-braces.)
    return True


def assess_dice_dc(player_input: str, state, aux_llm_rows: list | None = None) -> DiceAssessment:
    """Single haiku call to determine skill, stat, and DC for a roll."""
    prompt = (
        f'Player action: "{player_input}"\n'
        f"Scene: {state.get('location') or 'unknown'}\n"
        f"Player conditions: {state.get('player_conditions') or 'none'}\n"
        f"Scene aggression: {state.get('scene_aggression') or 0}/100\n\n"
        "Determine what dice roll this action requires. Reply JSON only, no markdown:\n"
        '{"dc":<int 1-25>,"skill":"<verb, max 3 words>",'
        '"stat":"<STR|DEX|INT|WIS|CHA|CON|none>","flavor":"<one terse sentence, max 20 words>"}\n'
        "DC guide: trivial 1-8, moderate 9-12, difficult 13-16, hard 17-20, legendary 21+.\n"
        "stat: the primary ability this tests (none if pure skill).\n"
        "flavor: brief atmospheric cue for the player (e.g. 'The guard glances your way.')."
    )
    try:
        text, _usage = call_llm_plain(
            "",
            [{"role": "user", "content": prompt}],
            model=MODEL_HAIKU,
            max_tokens=100,
        )
        # this call completes just before the gate raises; record its token
        # counts with the other aux rows.
        record_aux_call(aux_llm_rows, "aux:dice_dc", MODEL_HAIKU, _usage)
        data = json.loads(text.strip())
        moderation.check_decoded(data)
        dc = max(1, min(25, int(data.get("dc", 12))))
        skill = str(data.get("skill") or "check")[:40]
        stat = str(data.get("stat") or "none")[:10]
        flavor = str(data.get("flavor") or "")[:140]
        moderation.check_decoded({"skill": skill, "stat": stat, "flavor": flavor})
    except Exception:
        log.warning("assess_dice_dc parse failed; using defaults", exc_info=True)
        dc, skill, stat, flavor = 12, "check", "none", "The outcome is uncertain."
    return DiceAssessment(dc=dc, skill=skill, stat=stat, flavor=flavor)


# path 2: post-DM detection


def detect_dice_in_narrative(narrative: str) -> DiceAssessment | None:
    """Scan a finished DM narrative for a [ROLL:skill:dc] marker.

    If found, returns a DiceAssessment with the pending_narrative set
    (marker stripped from the text). Returns None if no marker present.

    The DM embeds the marker at the natural pause where the player
    should roll, e.g.:
        "The guard eyes you coldly. [ROLL:Persuasion:13] He reaches
        for his whistle - your next words will decide this."

    On gate path the engine includes everything before the marker as
    pending_narrative so the frontend can display context.
    """
    m = _ROLL_MARKER_RE.search(narrative)
    if not m:
        return None
    skill = m.group(1).strip()
    dc = max(1, min(25, int(m.group(2))))
    # Text before the marker = pending context for the player
    before = narrative[: m.start()].strip()
    # Flavor = whatever follows the marker (first sentence only)
    after = narrative[m.end() :].strip()
    flavor = after.split(".")[0].strip()[:140] if after else ""
    return DiceAssessment(
        dc=dc,
        skill=skill,
        stat="none",
        flavor=flavor,
        pending_narrative=before,
    )


# exception


class DiceRollPending(TurnGate):
    """Raised when a dice roll is required but not yet submitted.

    The web client shows an inline dice gate; the player rolls; re-POST
    carries dice_roll. Save is not mutated on this path.
    """

    __slots__ = ("dc", "difficulty_label", "skill", "stat", "flavor", "pending_narrative")

    def __init__(self, assessment: DiceAssessment):
        self.dc = assessment.dc
        self.difficulty_label = assessment.difficulty_label
        self.skill = assessment.skill
        self.stat = assessment.stat
        self.flavor = assessment.flavor
        self.pending_narrative = assessment.pending_narrative

    def to_payload(self) -> dict:
        p: dict = {
            "kind": "dice_roll_needed",
            "dc": self.dc,
            "difficulty_label": self.difficulty_label,
            "skill": self.skill,
            "stat": self.stat,
            "flavor": self.flavor,
            "dice_sides": 20,
        }
        if self.pending_narrative:
            p["pending_narrative"] = self.pending_narrative
        return p
