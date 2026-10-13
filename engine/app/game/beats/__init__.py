"""Beat-based turn pipeline.

A turn is a sequence of Beats run in order. Each Beat reads/mutates a
shared TurnState and may emit a narrative chunk + state delta + signals.
The engine composes the chunks into the final assistant message.

The simplest pipeline is a single beat (DMWorldStepBeat) that wraps the
monolithic DM call. Other pipelines split the DM call into focused beats
(act / grounding / npc_react / ...) and let lore packs declare their own
beat sequences via pack.json.

Turn boundaries (in run_turn) work on the text as written; the pipeline
does no translation step. Frontend covers any narrative-fragmentation
seams with status overlays.
"""

from app.game.beats.state import TurnState
from app.game.beats.base import Beat, BeatPipeline
from app.game.beats.builtin import (
    DMWorldStepBeat,
    DMActBeat,
    GroundingBeat,
    NpcReactBeat,
    build_pipeline_for,
)
from app.game.beats.craft import CraftBeat
from app.game.beats.encounter import EncounterBeat

__all__ = [
    "TurnState",
    "Beat",
    "BeatPipeline",
    "DMWorldStepBeat",
    "DMActBeat",
    "GroundingBeat",
    "NpcReactBeat",
    "CraftBeat",
    "EncounterBeat",
    "build_pipeline_for",
]
