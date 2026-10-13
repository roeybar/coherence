"""Social systems - the bluff post-turn pass and the insight-roll block.

  - bluff with delayed verification
    A deterministic post-turn pass that spends no LLM tokens. When a
    player input contains an unverifiable authority-claim that
    resolves a scene, append it to active_threats with
    verification_pending=True so future turns can challenge it.

  - recent NPC insight rolls
    state.last_insight_rolls is a field in the pack's state_schema.json,
    so the DM's state delta can write it. The block shows the latest
    rolls so the DM keeps continuity on what NPCs noticed.

Both are best-effort - engine.py swallows failures with a log
warning so a buggy detector never blocks a turn.
"""

from __future__ import annotations

import re
from typing import Optional


# bluff verification


# Phrases that suggest an unverifiable authority/relationship claim.
# Intentionally broad - the rule is "any unprovable claim
# that resolves a scene seeds a future threat".
_PATTERN_BLUFF_CLAIM = re.compile(
    r"\b(?:"
    # Authority invocation: "i am the X", "by order of the X"
    r"i\s+am\s+(?:the\s+|sir\s+|lord\s+|lady\s+|captain\s+|baron\s+|"
    r"baroness\s+|duke\s+|duchess\s+|king\s+|queen\s+|prince\s+|princess\s+|"
    r"general\s+|admiral\s+|colonel\s+|magistrate\s+|inquisitor\s+)|"
    r"by\s+(?:order\s+of|the\s+authority\s+of|decree\s+of)\s+(?:the\s+|sir\s+|lord\s+|lady\s+|king\s+|queen\s+|duke\s+)|"
    # Relationship invocation: "the X sent me", "my X will be furious"
    r"(?:the\s+)?(?:king|queen|duke|baron|magistrate|inquisitor|captain|general|admiral|abbot|guildmaster|elder)\s+(?:sent\s+me|will\s+hear|will\s+be|knows)|"
    r"my\s+(?:patron|cousin|brother|uncle|father|mother|sister|husband|wife)\s+is\s+(?:the\s+|sir\s+|lord\s+|lady\s+|captain\s+|baron\s+|magistrate\s+|inquisitor\s+)|"
    # Threat invocation: "you wouldn't dare - I serve X"
    r"i\s+serve\s+(?:the\s+|lord\s+|lady\s+|king\s+|queen\s+|duke\s+|baron\s+|inquisitor\s+|magistrate\s+)|"
    r"you\s+wouldn'?t\s+dare\b"
    r")\b",
    re.IGNORECASE,
)


def detect_bluff_claim(player_input: str) -> Optional[str]:
    """If the player input contains an unverifiable authority/relationship
    claim, return the relevant snippet for use as the threat summary.
    None otherwise. Tolerant of common phrasings; tuned to be generous - false positives just log a non-load-bearing threat entry."""
    if not player_input:
        return None
    m = _PATTERN_BLUFF_CLAIM.search(player_input)
    if not m:
        return None
    start = max(0, player_input.rfind(".", 0, m.start()) + 1)
    end = player_input.find(".", m.end())
    if end < 0:
        end = len(player_input)
    return player_input[start:end].strip()[:200]


def maybe_record_bluff(state, player_input: str, *, scene_resolved: bool = True) -> bool:
    """If the player made a bluff-shaped claim and the scene resolved
    around it, append a verification-pending threat. Returns True iff
    appended. Caller decides what 'scene_resolved' means - typically:
    the turn produced a narrative without a hostile response."""
    if not scene_resolved:
        return False
    snippet = detect_bluff_claim(player_input)
    if not snippet:
        return False
    from app.game.world_systems import add_verification_pending_threat

    add_verification_pending_threat(state, snippet)
    return True


# NPC insight/perception rolls with explicit DC


def render_insight_rolls_block(state) -> str:
    """Show the most recent N insight rolls to the DM beat so it
    can keep narrative continuity (NPCs remember what they noticed)."""
    rolls = state.get("last_insight_rolls") or []
    if not rolls:
        return ""
    parts = [
        "RECENT NPC INSIGHT ROLLS (the DM rolled these last few turns "
        "- honour the outcomes when narrating what NPCs know / suspect):",
    ]
    for r in rolls[-5:]:
        npc = r.get("npc_id") or "?"
        kind = r.get("kind") or "insight"
        dc = r.get("dc")
        total = r.get("total") or r.get("roll") or 0
        out = r.get("outcome") or ""
        turn = r.get("turn") or "?"
        parts.append(f"  - T{turn}  {npc} {kind} (roll {total} vs DC {dc}) -> {out}")
    return "\n".join(parts)


# unified composition


def render_social_systems_block(state) -> str:
    """One block the DM beat can append. Empty when nothing active."""
    sections = [
        render_insight_rolls_block(state),
    ]
    nonempty = [s for s in sections if s]
    return "\n\n".join(nonempty)
