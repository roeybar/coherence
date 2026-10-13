"""Beat base + simple linear pipeline.

A Beat is a unit of work in a turn. The protocol is intentionally tiny:
  applies(ts) -> bool - should this beat run on this turn?
  run(ts)     -> ts - do the work; mutate ts; return it.

A BeatPipeline is an ordered list run linearly.

Beats may make zero, one, or many LLM calls; a beat with none only
mutates state and spends no tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.game.beats.state import TurnState


# engine-canonical length profiles
#
# Four named levels. max_tokens is a hard ceiling (truncation guard, not
# a length target - the directive shapes prose; the ceiling is belt-and-
# braces). The directive string is appended to the per-beat user message
# (never the system volatile half) per prefix contract: per-beat-
# varying content must sit after the history cache breakpoint.


@dataclass(frozen=True)
class _LengthProfileSpec:
    name: str
    max_tokens: int
    directive: str  # appended to user message, not system


class LengthProfile:
    """Engine-canonical narration length profiles.

    Usage: profiles[name] -> _LengthProfileSpec(name, max_tokens, directive).
    Beats that have a fixed profile (DMActBeat=short, GroundingBeat=medium,
    NpcReactBeat=short) reference these by name. DMWorldStepBeat selects
    dynamically from ts.stakes via the STAKES_TO_PROFILE mapping.
    expo-dump is opt-in for packs only and never fires by default.
    """

    SHORT = _LengthProfileSpec(
        "short",
        60,
        "[LENGTH DIRECTIVE] 2-4 short sentences, ~30-60 tokens. Routine/low-stakes. "
        "Do NOT expand, recap unchanged state, or narrate travel the player "
        "didn't ask to see.",
    )
    MEDIUM = _LengthProfileSpec(
        "medium",
        90,
        "[LENGTH DIRECTIVE] 2-4 sentences, ~40-90 tokens.",
    )
    LONG = _LengthProfileSpec(
        "long",
        250,
        "[LENGTH DIRECTIVE] One paragraph, ~100-250 tokens. Paint the moment - but once.",
    )
    EXPO = _LengthProfileSpec(
        "expo-dump",
        500,
        "[LENGTH DIRECTIVE] Full exposition/lore. 500 tokens is a HARD ceiling, "
        "not a target - stop when it's said.",
    )


# Name-indexed lookup - single source of truth for beats and validators.
PROFILES: dict[str, _LengthProfileSpec] = {
    "short": LengthProfile.SHORT,
    "medium": LengthProfile.MEDIUM,
    "long": LengthProfile.LONG,
    "expo-dump": LengthProfile.EXPO,
}

# Immutable set for fast membership tests (used in pack_beats validation).
PROFILE_NAMES: frozenset[str] = frozenset(PROFILES.keys())

# Stakes tag -> profile name for DMWorldStepBeat.
#  activation: all three levels mapped. mundane is also structurally
# enforced as N=1 in build_pipeline_for (no pack beat composition).
# normal/dramatic apply the profile ceiling on the single DMWorldStepBeat.
# expo-dump is never auto-mapped - opt-in for packs only.
STAKES_TO_PROFILE: dict[str, str] = {
    "mundane": "short",  # 60 tok - cigarette fast-path
    "normal": "medium",  # 90 tok - standard prose cap
    "dramatic": "long",  # 250 tok - richer stakes cap
}


def resolve_max_tokens(ts, beat_default: int, beat_profile_name: str = "") -> int:
    """Canonical ceiling resolver for all DM beats.

    Precedence (high -> low):
    1. ts.max_tokens_override - custom-world explicit raise set by
       engine.run_turn. only written for custom-world 'haiku-large' turns.
       Stakes-driven mundane cap flows via ts.length_profile, not here.
    2. beat_profile_name - fixed per-class profile for ability beats
       (DMActBeat='short', GroundingBeat='medium', NpcReactBeat='short').
    3. ts.length_profile - stakes-selected profile (set by DMWorldStepBeat
       from ts.stakes via STAKES_TO_PROFILE, or pre-set by the engine).
    4. beat_default - the beat's own int ceiling (fallback when no profile applies).

    Pack length_profiles are validated in pack_beats.py; this resolver
    does not read them.
    """
    # 1. Custom-world explicit raise.
    if getattr(ts, "max_tokens_override", 0):
        return ts.max_tokens_override
    # 2. Fixed per-class beat profile (ability beats).
    if beat_profile_name and beat_profile_name in PROFILES:
        return PROFILES[beat_profile_name].max_tokens
    # 3. Stakes-selected profile (DMWorldStepBeat standard turns).
    lp = getattr(ts, "length_profile", "")
    if lp and lp in PROFILES:
        return PROFILES[lp].max_tokens
    # 4. The beat's own default.
    return beat_default


# A tagged reply carries [/NARRATIVE] and a [STATE_DELTA] JSON block after the
# prose, so the API ceiling is the profile's prose ceiling plus this allowance.
# The directive the model sees is unchanged.
TAG_ALLOWANCE = 400


def reply_max_tokens(ts, beat_default: int, beat_profile_name: str = "") -> int:
    """The API max_tokens for a tagged reply: resolve_max_tokens, plus
    TAG_ALLOWANCE when the ceiling came from a length profile. A custom-world
    override and a beat's own default are sent as they are."""
    ceiling = resolve_max_tokens(ts, beat_default, beat_profile_name)
    if getattr(ts, "max_tokens_override", 0):
        return ceiling
    if beat_profile_name in PROFILES or getattr(ts, "length_profile", "") in PROFILES:
        return ceiling + TAG_ALLOWANCE
    return ceiling


log = logging.getLogger(__name__)


@runtime_checkable
class Beat(Protocol):
    id: str

    def applies(self, ts: TurnState) -> bool: ...
    def run(self, ts: TurnState) -> TurnState: ...


class BeatPipeline:
    """Linear, in-order beat execution. Returns the (mutated) TurnState
    so the caller can read narrative_chunks / signals / diff / by_beat.

    Failures inside a beat propagate; we do not swallow exceptions
    here. The caller wraps run in whatever telemetry / error-handling
    it wants."""

    def __init__(self, beats: list[Beat]):
        self.beats = list(beats)

    def run(self, ts: TurnState) -> TurnState:
        for beat in self.beats:
            try:
                applies = beat.applies(ts)
            except Exception as e:
                log.warning("beat %r applies() raised: %s - skipping", beat.id, e)
                continue
            if not applies:
                log.debug("beat %r skipped (applies=False)", beat.id)
                continue
            ts = beat.run(ts)
        return ts
