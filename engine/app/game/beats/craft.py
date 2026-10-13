"""CraftBeat - dice-driven craft resolution.

Runs as the sole beat in the 'craft' pipeline. Identical flow to
DMWorldStepBeat but injects a [craft result] block that tells the DM the
d20 roll, the DC, the outcome tier (critical/success/partial/fail), and
a focused narration instruction so the model renders the right quality of
result without second-guessing the roll.
"""

from __future__ import annotations

import json as _json
import logging

from app.game.beats.state import TurnState
from app.game.lore import DEFAULT_PACK_ID
from app.game.craft_gate import craft_outcome_tier
from app.game.llm import MAX_TOKENS
from app.game import npcs as npc_mod
from app.game import moderation
from app.game import context as context_mod
from app.game.parser import guard_delta
from app.game.prompts import HOOK_LAST, build_system_blocks, warn_if_below_cache_floor
from app.game.state import WorldState

log = logging.getLogger("beats.craft")

_TIER_GUIDANCE = {
    "critical": (
        "BEAT FOCUS - craft critical success: the item came together "
        "better than planned. Narrate 3-4 sentences in the four STORY PARTS: "
        "the player's work, the satisfying moment of completion with one small "
        "bonus property (sharper edge, brighter flame, more doses, unusual "
        "sheen, etc.), and a hook. Apply the item via items delta."
    ),
    "success": (
        "BEAT FOCUS - craft success: the item was completed exactly as "
        "intended. Narrate 3-4 sentences in the four STORY PARTS: the "
        "player's work, the clean completion, what it changes, and a hook. "
        "Apply the item via items delta."
    ),
    "partial": (
        "BEAT FOCUS - craft partial success: the item exists but has a "
        "flaw - weaker, rough, unstable, or inferior to what was intended. "
        "Narrate 3-4 sentences in the four STORY PARTS: the player's work, "
        "the compromise, what the flaw will cost, and a hook. "
        "Apply a clearly inferior version of the item via items delta."
    ),
    "fail": (
        "BEAT FOCUS - craft failure: the attempt failed. The materials "
        "were damaged, misaligned, or the process broke down. "
        "Narrate 3-4 sentences in the four STORY PARTS: the player's work, "
        "what went wrong, what it costs, and a hook. "
        "Do NOT add the intended item to state; remove any consumed materials."
    ),
}


class CraftBeat:
    """Dice-driven craft resolution beat."""

    id = "builtin:craft"

    def applies(self, ts: TurnState) -> bool:
        return ts.turn_class == "craft"

    def run(self, ts: TurnState) -> TurnState:
        gs = ts.gs
        craft_roll = getattr(ts, "craft_roll", None)
        craft_ctx = getattr(ts, "craft_context", None)

        if craft_roll is None or craft_ctx is None:
            log.warning("CraftBeat: missing craft_roll or craft_context - falling back")
            from app.game.beats.builtin import DMWorldStepBeat

            return DMWorldStepBeat().run(ts)

        outcome = craft_outcome_tier(craft_roll, craft_ctx.dc)
        guidance = _TIER_GUIDANCE.get(outcome, _TIER_GUIDANCE["success"])

        craft_block = (
            "[CRAFT RESULT - DM only, hidden from player]\n"
            f"Target: {craft_ctx.target}\n"
            f"Inputs: {', '.join(craft_ctx.inputs_committed) or 'none'}\n"
            f"DC: {craft_ctx.dc} ({craft_ctx.difficulty_label}) | "
            f"Roll: {craft_roll} | Outcome: {outcome.upper()}\n"
            "[/CRAFT RESULT]\n\n"
            f"{guidance}"
        )

        # Cache split: per-turn blocks + the craft result go in the
        # volatile half so the stable pack prefix caches.
        # seed_roster injected into stable; floor guard warns when under
        # proxy (same stable bytes as DMWorldStepBeat on the same save).
        stable, volatile = build_system_blocks(
            gs.profile,
            ts.stage,
            gs.config,
            monster_level=ts.monster_level,
            has_npcs=ts.has_npcs,
            turn_class="standard",
            seed_roster=gs.seed_roster,
        )
        warn_if_below_cache_floor(
            gs.config.pack_id or DEFAULT_PACK_ID,
            ts.stage,
            stable,
        )
        if ts.addressee_block:
            volatile = f"{volatile}\n\n{ts.addressee_block}"
        if getattr(ts, "world_systems_block", ""):
            volatile = f"{volatile}\n\n{ts.world_systems_block}"
        _dm_notes = npc_mod.render_dm_notes_block(ts.gs.state)
        if _dm_notes:
            volatile = f"{volatile}\n\n{_dm_notes}"
        volatile = f"{volatile}\n\n{craft_block}"
        system = f"{stable}\n{volatile}"

        dice_block = (
            "[INSIGHT DICE - DM only, hidden from player. "
            "Consume in order for NPC perception/insight checks: "
            f"{', '.join(str(d) for d in ts.insight_dice)}]"
        )
        tail = []
        if ts.items_block:
            tail.append(ts.items_block)
        # [WORLD STATE] at every tutorial stage: without it the narrator
        # invents values it must not change. Tutorial pacing stays on ts.stage.
        state_json = _json.dumps(gs.state.to_dict(), indent=2)
        tail.append(f"[WORLD STATE]\n{state_json}\n[/WORLD STATE]")
        tail.append(ts.player_input)
        tail.append(HOOK_LAST)

        def build_context(memory_block):
            return "\n\n".join([dice_block] + ([memory_block] if memory_block else []) + tail)

        from app.game.engine import _history_for_llm, MAX_HISTORY

        system_blocks, messages, _start, _memory = context_mod.assemble(
            stable,
            volatile,
            _history_for_llm(gs.history),
            build_context,
            context_mod.recall_for(ts),
            start=ts.window_start,
            memory_block=ts.memory_block,
            legacy_n=MAX_HISTORY,
        )

        ts.health_at_send = ts.ctx.health_score(system, messages)

        eff_max = ts.max_tokens_override or MAX_TOKENS
        from app.game.beats.builtin import _tagged_reply

        result, beat_in, beat_out, beat_cri, beat_cci = _tagged_reply(
            ts, system_blocks, messages, model=ts.turn_model, max_tokens=eff_max
        )

        from app.game.beats.builtin import (
            _strip_trailing_prompt,
            _replace_em_dashes,
            _strip_metagame_brackets,
        )

        result.narrative = _replace_em_dashes(result.narrative)
        result.narrative = _strip_trailing_prompt(result.narrative)
        result.narrative = _strip_metagame_brackets(result.narrative)
        moderation.check_decoded(result.narrative)

        if result.signals.get("checkpoint_worthy") is True:
            gs.checkpoint_state = gs.state.to_dict()
            gs.checkpoint_len = len(gs.history)
            gs.checkpoint_history = []
            gs.checkpoint_turn = gs.turn

        result.delta, _dropped = guard_delta(result.delta, gs.state.to_dict())
        if _dropped:
            log.info("state delta: ignored rewrites of %s", ",".join(sorted(_dropped)))
        diff = gs.state.apply_delta(result.delta, schema=ts.pack_schema)

        if result.signals.get("tier5_reset") is True:
            prev = gs.undo_stack[-1]["state"] if gs.undo_stack else gs.state.to_dict()
            gs.state = WorldState(**prev)
            if "active_threats" in result.delta:
                gs.state.active_threats = result.delta["active_threats"]
                diff["active_threats"] = {
                    "before": prev.get("active_threats", []),
                    "after": gs.state.active_threats,
                }

        ts.narrative_chunks.append(result.narrative or "")
        ts.diff.update(diff)
        ts.delta_raw = dict(result.delta or {})
        ts.signals.update(result.signals)
        ts.parse_ok = {
            "narrative": result.found_narrative,
            "delta": result.found_delta,
            "delta_json": result.delta_valid_json,
        }
        ts.record_beat(
            beat_id=self.id,
            model=ts.turn_model,
            tokens_in=beat_in,
            tokens_out=beat_out,
            cache_read=beat_cri,
            cache_creation=beat_cci,
        )
        return ts
