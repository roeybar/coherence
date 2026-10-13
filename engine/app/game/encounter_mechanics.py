"""encounter_mechanics - pure, LLM-free dice-outcome resolver.

Provides the data defaults and resolver functions used by EncounterBeat
(beats/encounter.py) and optionally overridden by a pack's
`encounter_mechanics` section in pack.json.

All functions are deterministic and free of I/O. Packs that declare no
`encounter_mechanics` key get the engine's own defaults.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# default tier table
# (tier_name, min_margin | None) - None = floor, must be last.
# margin = roll - dc.
DEFAULT_TIERS: list[tuple[str, Optional[int]]] = [
    ("critical", 5),
    ("success", 0),
    ("partial", -4),
    ("fail", -9),
    ("fumble", None),
]

# default aggression deltas
# Mirrors _AGGRESSION_DELTA in beats/encounter.py.
# Structure: {tier_name: {field: delta}}.
DEFAULT_EFFECTS: dict[str, dict[str, int]] = {
    "critical": {"scene_aggression": -15},
    "success": {"scene_aggression": -8},
    "partial": {"scene_aggression": 0},
    "fail": {"scene_aggression": 12},
    "fumble": {"scene_aggression": 25},
}

# default clamps
# Mirrors _AGGRESSION_MIN/_MAX in beats/encounter.py:36.
DEFAULT_CLAMPS: dict[str, tuple[int, int]] = {
    "scene_aggression": (0, 100),
}

# default hint template
# Mirrors the f-string at beats/encounter.py:67-74.
# {tier_upper} = tier.upper; {tier} = lowercase tier;
# {stat_part} is pre-computed by render_directive exactly as
# encounter.py:66.
DEFAULT_HINT = (
    "[DICE RESOLUTION] Player rolled {roll} vs DC {dc} "
    "for {skill}{stat_part}. "
    "Outcome: {tier_upper}. "
    "Scene aggression: {new_agg}/100. "
    "This verdict is BINDING on the fiction and the OUTCOME MUST MATCH THE TIER. "
    "critical: the attempt fully succeeds - the player gets what the action was "
    "reaching for, at its best. success: it works. partial: they get it only at "
    "a real cost or incompletely. fail/fumble: the attempt FAILS - the player "
    "does NOT obtain what the action was reaching for (the concession, the "
    "information, the escape, the advantage), and an NPC does NOT concede or "
    "reveal on a fail; a complication lands instead. Never narrate a fail as "
    "though it succeeded, and never grant on a fail the same reward a success "
    "would have given. Do NOT mention the DC or dice mechanics - let the fiction "
    "carry the result."
)

# prompt-budget constant
# Mirrors pack_beats.MAX_TEMPLATE_BYTES.
MAX_TEMPLATE_BYTES = 8_192


# resolver functions


def resolve_tier(
    margin: int,
    tiers: list[tuple[str, Optional[int]]] = DEFAULT_TIERS,
) -> str:
    """Map a roll-minus-DC margin to an outcome tier name.

    Iterates tiers in order; returns the first whose min_margin is None
    (floor) or whose min_margin <= margin.  By convention the None entry
    is last, so every margin gets a result.
    """
    for name, min_margin in tiers:
        if min_margin is None or margin >= min_margin:
            return name
    # Unreachable when tiers is valid (None floor present), but
    # fall through to the last tier name as a defensive measure.
    return tiers[-1][0]


def apply_effects(
    state: Any,
    tier: str,
    effects: dict[str, dict[str, int]],
    clamps: dict[str, Any],
) -> dict[str, dict[str, int]]:
    """Apply the tier's state deltas to state in-place.

    Returns {field: {before: int, after: int}} for each mutated field.

    state - a WorldState (any object with .get(f) and setattr).
    effects - {tier_name: {field: delta}}.
    clamps - {field: (lo, hi)} or {field: [lo, hi]}.
    Writes via setattr, matching beats/encounter.py:63.
    """
    applied: dict[str, dict[str, int]] = {}
    tier_delta = effects.get(tier, {})
    for f, delta in tier_delta.items():
        before = int(state.get(f) or 0)
        after = before + int(delta)
        if f in clamps:
            lo, hi = int(clamps[f][0]), int(clamps[f][1])
            after = max(lo, min(hi, after))
        setattr(state, f, after)
        applied[f] = {"before": before, "after": after}
    return applied


def render_directive(
    spec: Optional[dict],
    *,
    roll: int,
    dc: int,
    skill: str,
    stat: str,
    tier: str,
    new_state: dict[str, dict[str, int]],
) -> str:
    """Render the per-turn outcome directive (prompt injection string).

    If spec declares a per-tier directive, return it (truncated at
    MAX_TEMPLATE_BYTES with a warning if over-cap).
    Otherwise render DEFAULT_HINT with the roll parameters.

    stat_part is computed exactly as beats/encounter.py:66.
    """
    stat_part = f" ({stat})" if stat and stat.lower() not in ("none", "") else ""

    # Pack-declared per-tier directive takes precedence.
    if spec is not None:
        directive = (spec.get("effects") or {}).get(tier, {}).get("directive")
        if directive:
            if len(directive) > MAX_TEMPLATE_BYTES:
                log.warning(
                    "encounter_mechanics: directive for tier %r exceeds %d bytes  - truncating",
                    tier,
                    MAX_TEMPLATE_BYTES,
                )
                directive = directive[:MAX_TEMPLATE_BYTES]
            return directive

    # Default engine hint.
    new_agg = (new_state.get("scene_aggression") or {}).get("after", 0)
    rendered = DEFAULT_HINT.format(
        roll=roll,
        dc=dc,
        skill=skill,
        stat_part=stat_part,
        tier=tier,
        tier_upper=tier.upper(),
        new_agg=new_agg,
    )
    if len(rendered) > MAX_TEMPLATE_BYTES:
        log.warning(
            "encounter_mechanics: DEFAULT_HINT render exceeds %d bytes - truncating",
            MAX_TEMPLATE_BYTES,
        )
        rendered = rendered[:MAX_TEMPLATE_BYTES]
    return rendered


def mechanics_for(pack: Optional[dict]) -> Optional[dict]:
    """Extract the validated encounter_mechanics spec from a loaded pack dict.

    Returns None when the pack has no encounter_mechanics section.
    """
    return ((pack or {}).get("pack_beats") or {}).get("encounter_mechanics")
