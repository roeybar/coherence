"""TurnState - the shared mutable context that flows through a Beat
pipeline. Beats read fields (player_input, history, pack, ...) and write
to the output buckets (narrative_chunks, signals, diff, by_beat).

GameSave is referenced (not copied) so beats can mutate state /
history / undo_stack / checkpoint_* on the durable object. The engine
post-pipeline reads narrative_chunks + diff + signals to compose the
response payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.game.context import ContextEngine
    from app.game.state import GameSave


@dataclass
class TurnState:
    # inputs (set by run_turn before pipeline.run)
    gs: "GameSave"
    save_id: str
    player_input: str
    pre_state: dict  # WorldState as-of turn start, frozen
    pack: dict  # loaded lore pack
    pack_schema: list  # state field defs
    valid_keys: set  # accepted state-delta keys
    turn_class: str  # "ability" / "standard" / pack-specific
    turn_model: str  # MODEL_HAIKU or MODEL_SONNET
    monster_level: int
    has_npcs: bool
    stage: int  # tutorial_stage
    insight_dice: list[int]
    memory_block: str  # formatted local recall (may be empty)
    recalled: list[dict]  # raw recall hits
    ctx: "ContextEngine"  # mutated by beats via .record
    consult: bool = False
    consult_ruling: str = ""
    recall_query: str = ""  # Query for recall before the sent history window.
    window_start: int = 0  # Base whole-turn history boundary for this turn.
    items_block: str = ""  # formatted item context (may be empty)
    items_ops_log: dict = field(default_factory=lambda: {"applied": [], "skipped": []})
    # Counter for: how many of the player's quoted-speech segments the
    # DM dropped/paraphrased and the post-processor had to splice back in.
    # Held on the turn state for this turn only; no per-turn record is kept.
    quoted_speech_repaired: int = 0
    # Per-turn override for the DMWorldStep / DMAct beat max_tokens. Engine
    # bumps this for custom-world turns ("haiku-large") since the system
    # prompt is fatter (premise + scene + tone) and benefits from longer
    # responses without escalating to the strong model. 0 = use the beat default.
    # important: only written by engine.run_turn for custom-world raises.
    # Stakes-driven mundane cap flows via ts.length_profile.
    max_tokens_override: int = 0
    # Set once a tagged reply in this move has been asked again because it came
    # back incomplete; a second incomplete reply in the move refuses the move
    # (or, for Grounding/NpcReact, drops that beat's text).
    reply_retried: bool = False
    # The ceiling of the last complete tagged reply (0 before any).
    reply_ceiling: int = 0
    # stakes classification tag from classify_stakes.
    # Set by engine.run_turn; carried through the beat pipeline.
    # Values: "mundane" | "normal" | "dramatic". Default "normal" when
    # the caller does not set it.
    stakes: str = "normal"
    # resolved length profile name for this turn.
    # DMWorldStepBeat sets this from ts.stakes via STAKES_TO_PROFILE before
    # its LLM call. Empty string = no profile override (use beat default).
    # Ability beats (DMActBeat/GroundingBeat/NpcReactBeat) use fixed per-
    # class profiles passed as beat_profile_name to resolve_max_tokens and
    # do not write this field (to avoid pipeline cross-contamination).
    length_profile: str = ""
    # addressee block - one-line context appended to the system
    # prompt so the DM beat knows who the player is talking to. Set by
    # run_turn from the addressee classifier (or from the player's
    # clarification pick). Empty string = no addressee context (action
    # turns, non-dialogue input).
    addressee_block: str = ""
    # physicalisation context block - populated by run_turn
    # when the scope classifier flags an alteration attempt. Composed
    # via app.game.physicalisation.render_physicalisation_block: lore-pack
    # rule block + current self_restrictions + entropy hint + scope
    # guidance. Empty string when scope == "none" (no injection).
    physicalisation_block: str = ""
    # world-systems context block - composed every
    # turn by world_systems.render_world_systems_block. Surfaces:
    # world_rules, active_quests, active_threats, player_conditions,
    # production_techniques, pc_only_anomalies, day_segment. Empty
    # when none of those fields are populated.
    world_systems_block: str = ""
    # pack-declared trigger beats append their directive blocks
    # here; the DM beats fold them into the system prompt alongside the
    # other context blocks. Empty for packs with no beats[] (coherence
    # today) - zero-cost on the default path.
    pack_blocks: list[str] = field(default_factory=list)
    # craft gate passback. craft_roll is the d20 value the player
    # rolled in the CraftRollModal; craft_context is the CraftAssessment
    # from assess_craft_dc. Both set by engine.run_turn when craft_roll
    # is provided in the request. None = not a craft turn.
    craft_roll: object = None
    craft_context: object = None
    # dice gate passback. dice_roll is the d20 value the player
    # rolled in the inline dice gate; dice_context is the DiceAssessment
    # from assess_dice_dc. Both set by engine.run_turn when dice_roll
    # is provided in the request. None = not a dice-gated turn.
    dice_roll: object = None
    dice_context: object = None

    # outputs (beats append)
    narrative_chunks: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    # Cumulative {field: {before, after}} produced by state.apply_delta -
    # the "what changed" view.
    diff: dict = field(default_factory=dict)
    # Raw delta dict the model emitted (pre-apply). The engine reads this
    # verbatim, so we keep the last-emitted version
    # rather than rebuilding it.
    delta_raw: dict = field(default_factory=dict)
    parse_ok: dict = field(
        default_factory=lambda: {
            "narrative": True,
            "delta": True,
            "delta_json": True,
        }
    )
    health_at_send: float | None = None

    # accounting (per-beat, plus totals for the turn)
    turn_tokens_in: int = 0
    turn_tokens_out: int = 0
    by_beat: list[dict] = field(default_factory=list)

    # Convenience views
    @property
    def narrative(self) -> str:
        """Composed assistant narrative - concatenation of chunks. Beats
        produce rough lego pieces; frontend smooths the seams."""
        return "\n\n".join(c for c in self.narrative_chunks if c).strip()

    def record_beat(
        self,
        *,
        beat_id: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str = "",
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        """Append a per-beat usage row to TurnState.by_beat.

        cache_read - Anthropic cache_read_input_tokens for this beat.
        cache_creation - Anthropic cache_creation_input_tokens for this beat.
        Both default to 0; callers that omit them are unaffected.
        """
        self.turn_tokens_in += int(tokens_in or 0)
        self.turn_tokens_out += int(tokens_out or 0)
        self.by_beat.append(
            {
                "id": beat_id,
                "model": model,
                "tokens_in": int(tokens_in or 0),
                "tokens_out": int(tokens_out or 0),
                "cost": 0.0,  # no prices are kept; the row keeps the field
                "cache_read": int(cache_read or 0),
                "cache_creation": int(cache_creation or 0),
            }
        )
