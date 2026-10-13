"""EncounterBeat - resolves a dice roll and updates scene aggression.

Fires when TurnState.dice_roll is set (i.e. the player submitted a roll
via the inline dice gate). The beat:

  1. Derives the outcome tier (critical / success / partial / fail / fumble)
     from the roll vs. the DC in dice_context.
  2. Builds a one-paragraph "resolution hint" injected into the DM context
     so the DM beat narrates knowing the outcome.
  3. Updates state.scene_aggression: rises on fail/fumble, falls on
     critical/success, stays on partial.

The beat does not generate the final narrative itself - that remains
DMWorldStepBeat / DMActBeat. The outcome hint is a context string, not
player-facing prose.

Resolution logic is delegated to encounter_mechanics so pack authors can
override tiers, effects, and clamps via the encounter_mechanics section of
pack.json; a pack without one gets the engine's defaults. The rendered
directive is also appended to ts.pack_blocks so the DM beat sees the outcome
inline. ts.signals["dice_outcome_hint"] is still written, for telemetry.
"""

from __future__ import annotations

import logging

from app.game.beats.base import Beat
from app.game.beats.state import TurnState
from app.game.encounter_mechanics import (
    DEFAULT_CLAMPS,
    DEFAULT_EFFECTS,
    DEFAULT_TIERS,
    MAX_TEMPLATE_BYTES,
    apply_effects,
    mechanics_for,
    render_directive,
    resolve_tier,
)

log = logging.getLogger("encounter_beat")

# re-exports for external importers
# Derived from the resolver module's defaults so that any code
# importing these constants from this module continues to work.
_AGGRESSION_DELTA: dict[str, int] = {k: v["scene_aggression"] for k, v in DEFAULT_EFFECTS.items()}
_AGGRESSION_MIN, _AGGRESSION_MAX = DEFAULT_CLAMPS["scene_aggression"]


class EncounterBeat(Beat):
    """Resolve the dice roll and inject outcome context for the DM beat."""

    beat_id = "encounter"

    def applies(self, ts: TurnState) -> bool:
        return ts.dice_roll is not None and ts.dice_context is not None

    def run(self, ts: TurnState) -> TurnState:
        roll = int(ts.dice_roll)
        ctx = ts.dice_context  # DiceAssessment
        dc = ctx.dc
        skill = ctx.skill
        stat = ctx.stat

        # resolve tier from pack spec or engine defaults
        spec = mechanics_for(ts.pack)
        if spec and spec.get("outcome_tiers"):
            tiers = [(t["tier"], t["min_margin"]) for t in spec["outcome_tiers"]]
        else:
            tiers = DEFAULT_TIERS
        tier = resolve_tier(roll - dc, tiers)

        # apply state effects
        if spec and spec.get("effects"):
            effects = {tname: v.get("state_delta", {}) for tname, v in spec["effects"].items()}
        else:
            effects = DEFAULT_EFFECTS

        if spec and spec.get("clamps"):
            clamps = {f: tuple(v) for f, v in spec["clamps"].items()}
        else:
            clamps = DEFAULT_CLAMPS

        current_agg = int(ts.gs.state.get("scene_aggression") or 0)
        applied = apply_effects(ts.gs.state, tier, effects, clamps)
        new_agg = applied.get("scene_aggression", {}).get("after", current_agg)

        # Always write core signals (these survive even if render raises).
        ts.signals["dice_tier"] = tier
        ts.signals["scene_aggression"] = new_agg

        # fail-open: render directive + inject into the live DM channel.
        # Any exception must not eat the player's turn.
        try:
            directive = render_directive(
                spec,
                roll=roll,
                dc=dc,
                skill=skill,
                stat=stat,
                tier=tier,
                new_state=applied,
            )
            # cap at MAX_TEMPLATE_BYTES (render_directive already caps,
            # but guard here too for defensive depth).
            if len(directive) > MAX_TEMPLATE_BYTES:
                log.warning(
                    "EncounterBeat: directive exceeds %d bytes - truncating",
                    MAX_TEMPLATE_BYTES,
                )
                directive = directive[:MAX_TEMPLATE_BYTES]
            # Also written to signals, for telemetry.
            ts.signals["dice_outcome_hint"] = directive
            # Append to the live DM-injection channel (DMActBeat and DMWorldStepBeat read it).
            ts.pack_blocks.append(directive)
        except Exception:
            log.error(
                "EncounterBeat: render_directive raised - skipping pack_blocks injection",
                exc_info=True,
            )

        ts.record_beat(beat_id=self.beat_id)
        log.info(
            "EncounterBeat: roll=%d dc=%d skill=%s tier=%s agg=%d->%d",
            roll,
            dc,
            skill,
            tier,
            current_agg,
            new_agg,
        )
        return ts
