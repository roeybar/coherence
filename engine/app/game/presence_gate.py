"""Deterministic departure detector.

Pre-turn, LLM-free gate that fires when the player's input contains a
departure/exit cue and NPCs are present in the scene. Mirrors the structure
of game/dice_gate.py (pure-lexical, no I/O, fast).

The detector is LOSSY-TOLERANT by design: false negatives are acceptable
because the baseline physical presence & earshot rule in _NPC_KNOWLEDGE
always fires on has_npcs turns. False positives are also tolerable - they
only sharpen a prompt directive that is already directionally safe.
"""

from __future__ import annotations

import re
from typing import Optional


# departure lexicon (word-boundary guards)
# Each phrase is matched with \b...\b. Multi-word phrases use \s+ between
# tokens. The list is intentionally curated rather than exhaustive.
_DEPART_PHRASES = [
    # Multi-word first (longest alternative first, so the longer phrase matches)
    r"walk\s+away",
    r"walks\s+away",
    r"walk\s+off",
    r"walks\s+off",
    r"walk\s+out",
    r"walks\s+out",
    r"turn\s+to\s+go",
    r"turn\s+to\s+leave",
    r"turn\s+away",
    r"turns\s+away",
    r"go\s+down",
    r"goes\s+down",
    r"down\s+the\s+stairs",
    r"downstairs",
    r"head\s+for\s+the\s+door",
    r"head\s+out",
    r"heads\s+out",
    r"step\s+over",
    r"step\s+past",
    r"step\s+out",
    r"steps\s+out",
    r"storm\s+out",
    r"storm\s+off",
    r"storms\s+off",
    r"back\s+out",
    r"back\s+away",
    r"back\s+off",
    r"backs\s+away",
    r"dash\s+off",
    r"take\s+off",
    r"get\s+out",
    r"march\s+off",
    r"stalk\s+off",
    r"slip\s+away",
    r"slip\s+out",
    r"duck\s+out",
    r"break\s+off",
    r"breaks\s+off",
    r"break\s+away",
    r"move\s+off",
    r"move\s+away",
    r"wander\s+off",
    # Single-word (after multi-word to avoid shadowing)
    r"leave",
    r"leaves",
    r"leaving",
    r"exit",
    r"exits",
    r"exiting",
    r"descend",
    r"descends",
    r"descending",
    r"flee",
    r"flees",
    r"fleeing",
    r"bolt",
    r"bolts",
    r"retreat",
    r"retreats",
    r"withdraw",
    r"withdraws",
    r"run",
    r"runs",
]

# Build a single compiled pattern with word-boundary guards around each phrase.
_DEPART_RE = re.compile(
    r"\b(?:" + "|".join(_DEPART_PHRASES) + r")\b",
    re.IGNORECASE,
)


class PresenceDeparture:
    """Result of detect_departure when a departure cue is found.

    Attributes:
        cues - matched cue substrings, deduped, order-preserved.
        npcs_in_scene - NPC names supplied by the caller (may be empty).
    """

    __slots__ = ("cues", "npcs_in_scene")

    def __init__(self, cues: list[str], npcs_in_scene: list[str]) -> None:
        self.cues: list[str] = cues
        self.npcs_in_scene: list[str] = npcs_in_scene

    def __repr__(self) -> str:  # pragma: no cover
        return f"PresenceDeparture(cues={self.cues!r}, npcs_in_scene={self.npcs_in_scene!r})"


def detect_departure(
    player_input: str,
    has_npcs: bool,
    npcs_in_scene: list[str],
) -> Optional[PresenceDeparture]:
    """Return a PresenceDeparture when the player is moving out of NPC space.

    Returns None when:
      - has_npcs is False (gate: never fire without NPCs present), or
      - _DEPART_RE finds no departure cue in player_input.

    On a hit:
      cues         = deduped matched cue substrings (order-preserved).
      npcs_in_scene = the caller-supplied NPC names (may be empty list).
    """
    if not has_npcs:
        return None
    text = player_input or ""
    matches = _DEPART_RE.findall(text)
    if not matches:
        return None
    # Deduplicate while preserving order.
    seen: set[str] = set()
    cues: list[str] = []
    for m in matches:
        key = m.lower()
        if key not in seen:
            seen.add(key)
            cues.append(m)
    return PresenceDeparture(cues=cues, npcs_in_scene=list(npcs_in_scene))
