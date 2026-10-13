"""Built-in Beat catalog.

DMWorldStepBeat: monolithic single-call DM (used for
  standard turns; preserves pre-pipeline behaviour).
DMActBeat + GroundingBeat: ability turns split into
  (a) render the act + immediate consequence and (b) ground the
  player in their new position with sensory observation.
NpcReactBeat: conditional reaction beat for NPCs in scene.

Beats can produce rough lego pieces; the frontend covers
seams with status-overlay animation. The composed narrative is used
as written; there is no translation step.
"""

from __future__ import annotations

import logging
import re as _re

from app.game.beats.base import PROFILES, STAKES_TO_PROFILE, reply_max_tokens
from app.game.beats.state import TurnState
from app.game.lore import DEFAULT_PACK_ID
from app.game.llm import (
    call_llm_reply,
    call_llm_plain,
    MODEL_HAIKU,
    MODEL_SONNET,
)
from app.llm_client import ProviderError
from app.game import npcs as npc_mod
from app.game.parser import ParseResult, ReplyIncomplete, guard_delta, parse_response
from app.game import moderation
from app.game import context as context_mod
from app.game.prompts import (
    CONSULT_SYSTEM,
    HOOK_LAST,
    _ability_block,
    build_system_blocks,
    warn_if_below_cache_floor,
)
from app.game.state import WorldState


log = logging.getLogger(__name__)

MAX_TOKENS = 1500


# Hardcoded post-process - moved out of the system prompt to save
# tokens. Belt-and-braces vs the model regressing to old patterns.
_TRAILING_PROMPT_PATTERNS = (
    "what do you do?",
    "what will you do?",
    "what do you do",
    "what will you do",
    "what's your move?",
    "what's your move",
    "your move?",
    "your move",
)


def _strip_trailing_prompt(narrative: str) -> str:
    if not narrative:
        return narrative
    s = narrative.rstrip()
    nl = s.rfind("\n")
    last = s[nl + 1 :] if nl >= 0 else s
    last_lower = last.strip().rstrip("?.!").lower()
    if last_lower in _TRAILING_PROMPT_PATTERNS:
        return s[:nl].rstrip() if nl >= 0 else ""
    return narrative


# Em-dash drama -> newline. Cheaper to enforce in code than to spend
# system-prompt tokens telling the model "no em-dash drama" - the
# model ignores the rule. Only fires when the dash sits between
# whitespace on both sides ("you stand - alone" -> "you stand\nalone");
# compound words ("tax-collector", "10D-native") and numeric ranges
# ("10-12") are not touched.
_DASH_DRAMA_RE = _re.compile(r"\s+[\u2014\u2013-]+\s+")


def _replace_em_dashes(narrative: str) -> str:
    if not narrative:
        return narrative
    return _DASH_DRAMA_RE.sub("\n", narrative)


# Metagame-bracket strip: the model sometimes leaks internal scaffolding
# tags into player-visible narrative. Players should never see these.
#
# Pattern: a bracket whose first non-space characters are two or more
# uppercase ASCII letters, e.g. [INSIGHT DICE ...], [DICE RESOLUTION],
# [DM only ...], [NARRATIVE]. The match is case-sensitive: a tag that
# starts in lowercase, such as [token monster ...], is not stripped,
# and neither is a status tag such as "[Year 1 | Spring | Arc 1]".
#
# Multi-line content inside brackets is fine - [^\]] matches newlines.
# After stripping, collapse 3+ consecutive newlines to 2 so we don't
# leave gaps where the leak used to be.
_METAGAME_TAG_RE = _re.compile(r"\[\s*[A-Z]{2,}[^\]]*\]")
_BLANK_LINES_RE = _re.compile(r"\n{3,}")


def _strip_metagame_brackets(narrative: str) -> str:
    if not narrative:
        return narrative
    out = _METAGAME_TAG_RE.sub("", narrative)
    out = _BLANK_LINES_RE.sub("\n\n", out)
    return out.strip()


#
# One retry per move: a reply the provider cut
# (a TRUNCATION_STOPS reason, or no stop reason at all) is asked again at
# RETRY_CEILING_FACTOR times the ceiling; a reply with empty narration at the
# same ceiling. A second failure in the move refuses it; run_turn restores the
# save. Grounding and NpcReact drop their text instead (their delta is
# discarded anyway).
RETRY_CEILING_FACTOR = 2

# Stop reasons that mean the reply was cut, or carried none. Any other reason
# (stop, end_turn, tool_use, stop_sequence, other) plays like a natural stop.
TRUNCATION_STOPS = ("length", "max_tokens", "model_context_window_exceeded", "")
# The provider's filter stopped the reply: refused as output_blocked, never shown.
BLOCKED_STOPS = ("content_filter", "refusal")


def _tagged_reply(
    ts, system, messages, *, model, max_tokens, need_delta=True, droppable=False, empty_ok=False
):
    """Call, record usage and parse one tagged reply.
    Complete: a reply that was not cut (no TRUNCATION_STOPS reason)
    and narration that is not empty; a missing [STATE_DELTA] is then an empty
    delta. A filtered reply refuses the
    move as output_blocked. need_delta=False: a missing delta is not logged.
    droppable=True: once this move's retry is spent, an incomplete reply comes
    back as an empty result instead of refusing. empty_ok=True: an empty
    narration goes back to the beat, which handles it itself.
    Returns (result, in, out, cache_read, cache_creation) summed over the calls made."""
    totals = [0, 0, 0, 0]
    ceiling = max_tokens
    while True:
        raw, usage, stop_reason, truncated = call_llm_reply(
            system, messages, model=model, max_tokens=ceiling
        )
        ts.ctx.record(usage)
        if usage:
            totals[0] += usage.input_tokens
            totals[1] += usage.output_tokens
            totals[2] += getattr(usage, "cache_read_input_tokens", 0)
            totals[3] += getattr(usage, "cache_creation_input_tokens", 0)
        if stop_reason in BLOCKED_STOPS:
            log.warning(
                "tagged reply stopped by the provider filter (%s); refusing the move", stop_reason
            )
            raise moderation.OutputBlocked("provider_filter")
        result = parse_response(raw, ts.valid_keys)
        cut = bool(truncated) or stop_reason in TRUNCATION_STOPS
        empty = not (result.narrative or "").strip()
        if not cut and (empty_ok or not empty):
            if need_delta and not result.found_delta:
                log.info("tagged reply ended without a state delta; applying an empty delta")
            ts.reply_ceiling = ceiling
            return (result, *totals)
        if ts.reply_retried:
            if droppable:
                log.warning(
                    "tagged reply incomplete at max_tokens=%d after this move's retry; "
                    "dropping this beat's text",
                    ceiling,
                )
                return (
                    ParseResult(
                        narrative="",
                        delta={},
                        raw="",
                        signals={},
                        found_narrative=False,
                        found_delta=False,
                    ),
                    *totals,
                )
            log.warning(
                "tagged reply incomplete at max_tokens=%d after this move's retry; "
                "refusing the move",
                ceiling,
            )
            raise ReplyIncomplete()
        ts.reply_retried = True
        retry_ceiling = max_tokens * RETRY_CEILING_FACTOR if cut else ceiling
        log.warning(
            "tagged reply %s at max_tokens=%d; asking once more at max_tokens=%d",
            "cut" if cut else "empty",
            ceiling,
            retry_ceiling,
        )
        ceiling = retry_ceiling


class DMWorldStepBeat:
    """The current monolithic DM call wrapped as a Beat. Reads recall
    + dice + history from TurnState, calls the LLM, parses, applies the
    state delta, and handles the two engine-level signals
    (checkpoint_worthy, tier5_reset). Appends one narrative chunk."""

    id = "builtin:dm_world_step"

    def applies(self, ts: TurnState) -> bool:
        return True

    def run(self, ts: TurnState) -> TurnState:
        gs = ts.gs

        # System prompt - built per turn from pack + engine pieces +
        # gates (has_npcs / turn_class) computed by run_turn before us.
        # Split at the prompt-cache boundary: everything per-turn
        # goes in the volatile half so the stable pack prefix stays
        # byte-identical and actually hits the cache.
        # seed_roster injected into stable to keep it above the cache floor.
        # Floor guard warns (never raises) when under proxy.
        stable, volatile = build_system_blocks(
            gs.profile,
            ts.stage,
            gs.config,
            monster_level=ts.monster_level,
            has_npcs=ts.has_npcs,
            turn_class=ts.turn_class,
            seed_roster=gs.seed_roster,
        )
        warn_if_below_cache_floor(
            gs.config.pack_id or DEFAULT_PACK_ID,
            ts.stage,
            stable,
        )
        # addressee disambiguation block, when present, is appended
        # so the DM beat knows who the player is talking to without
        # having to guess.
        if ts.addressee_block:
            volatile = f"{volatile}\n\n{ts.addressee_block}"
        # physicalisation block - when the scope classifier
        # detected an alteration attempt, this carries the omnipotent-
        # narrate-it-happening rules + player self-restrictions + entropy
        # hint level. Empty when scope == "none".
        if getattr(ts, "physicalisation_block", ""):
            volatile = f"{volatile}\n\n{ts.physicalisation_block}"
        # world-systems block - composed by
        # world_systems.render_world_systems_block in run_turn. Surfaces
        # active quests/threats/conditions/anomalies/world_rules/day_segment
        # so the DM beat has the continuity context. Empty when none of
        # those fields carry content.
        if getattr(ts, "world_systems_block", ""):
            volatile = f"{volatile}\n\n{ts.world_systems_block}"
        # pack-declared directive blocks - appended by any
        # PackTriggerBeat that fired earlier in this pipeline. The pack
        # speaks with engine authority here: these are mechanics, not
        # flavor suggestions.
        if getattr(ts, "pack_blocks", None):
            volatile = f"{volatile}\n\n" + "\n\n".join(ts.pack_blocks)
        # DM-private notes - player-hidden canonical track.
        _dm_notes_block = npc_mod.render_dm_notes_block(ts.gs.state)
        if _dm_notes_block:
            volatile = f"{volatile}\n\n{_dm_notes_block}"
        system = f"{stable}\n{volatile}" if volatile else stable

        # select length profile from stakes tag. DMWorldStepBeat
        # maps ts.stakes -> profile via STAKES_TO_PROFILE. If ts.length_profile
        # is already set (by an engine pre-pass), it wins.
        # Ability beats have their own fixed profiles and do not touch this field.
        if not ts.length_profile:
            ts.length_profile = STAKES_TO_PROFILE.get(ts.stakes, "")

        # User-side context: insight dice + memory recall + (optionally)
        # state JSON + the player's English-canonical input.
        import json as _json

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
        # prefix contract: per-beat-varying content (profile directive)
        # goes in the user message, never in the system volatile half. This
        # positions it after the history breakpoint, so the cached prefix
        # stays byte-identical across beats.
        if ts.length_profile in PROFILES:
            tail.append(PROFILES[ts.length_profile].directive)
        # The hook closer is the last narration instruction in this request.
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

        # Health snapshot for telemetry - same number the existing
        # monolith reports.
        ts.health_at_send = ts.ctx.health_score(system, messages)

        # canonical ceiling resolver. Precedence: max_tokens_override
        # (custom-world explicit raise) > ts.length_profile (stakes-driven) >
        # beat_default (MAX_TOKENS). The stakes ceiling flows only via
        # ts.length_profile -> resolver.
        eff_max = reply_max_tokens(ts, beat_default=MAX_TOKENS)
        result, beat_in, beat_out, beat_cri, beat_cci = _tagged_reply(
            ts, system_blocks, messages, model=ts.turn_model, max_tokens=eff_max
        )

        # Hardcoded post-process - keeps "what do you do?" out of player
        # view, turns spaced dashes into line breaks and strips metagame
        # brackets before the output check.
        result.narrative = _replace_em_dashes(result.narrative)
        result.narrative = _strip_trailing_prompt(result.narrative)
        result.narrative = _strip_metagame_brackets(result.narrative)
        moderation.check_decoded(result.narrative)

        # Engine-level signals - these touch durable save state, not
        # just the narrative.
        if result.signals.get("checkpoint_worthy") is True:
            gs.checkpoint_state = gs.state.to_dict()
            gs.checkpoint_len = len(gs.history)
            gs.checkpoint_history = []
            gs.checkpoint_turn = gs.turn

        moderation.check_decoded(result.delta)
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

        # Outputs - beat appends to TurnState.
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


#
# ability-turn split: dm_act + grounding
#

_ACT_FOCUS = (
    "\nBEAT FOCUS - act: render what the player just attempted. The body, "
    "the matter, the world's response. Immediate sensory consequence "
    "(sight, sound, weight, pressure). This call writes ONLY the first two "
    "STORY PARTS: ACKNOWLEDGE (the player's act, as theirs) and REACT (the "
    "world's immediate response). Your first sentence has the player as its subject, "
    'doing the act ("You ..."); the world\'s response comes after it. The state '
    "delta records what changed in the world (location if they moved, "
    "active_threats if attention shifted, energy_tier if escalated, etc.). "
    "DO NOT yet describe where they end up afterward or what they observe "
    "from a new vantage - a separate call writes DEVELOP and HOOK. End when "
    "the act resolves, without a hook or a question to the player."
)

_NPC_REACT_LENGTH = "[LENGTH DIRECTIVE] 1-2 short sentences, ~15-40 tokens."


def consult_fits(pack, player_input) -> bool:
    fits = (
        len(CONSULT_SYSTEM) + len(_ability_block(pack, "ability")) + 2 + len(player_input) <= 9000
    )
    if not fits:
        log.warning("ConsultBeat: mandatory rules and move exceed input budget")
    return fits


class ConsultBeat:
    """Resolve the rules on the strong tier before fast-tier narration.
    If the narration then asks for a roll, the move re-runs as an encounter
    and this ruling is not reused.
    """

    id = "consult"

    def applies(self, ts: TurnState) -> bool:
        return ts.consult

    def run(self, ts: TurnState) -> TurnState:
        if not consult_fits(ts.pack, ts.player_input):
            return ts
        rules = _ability_block(ts.pack, "ability")
        mandatory = len(CONSULT_SYSTEM) + len(rules) + 2 + len(ts.player_input)
        prior = next(
            (
                row.get("content_en") or row.get("content", "")
                for row in reversed(ts.gs.history)
                if row.get("role") == "assistant"
            ),
            "",
        )
        parts = [rules]
        room = 9000 - mandatory
        import json

        state = ts.gs.state.to_dict()
        state.pop("pending_input", None)
        world = (
            "[WORLD STATE]\n"
            + json.dumps(state, sort_keys=True, separators=(",", ":"))
            + "\n[/WORLD STATE]"
        )
        for text in (ts.physicalisation_block, ts.world_systems_block, world, prior[:1500]):
            if text and room > 2:
                part = text[: room - 2]
                parts.append(part)
                room -= len(part) + 2
        parts.append(ts.player_input)
        try:
            raw, usage = call_llm_plain(
                CONSULT_SYSTEM,
                [{"role": "user", "content": "\n\n".join(parts)}],
                model=MODEL_SONNET,
                max_tokens=2000,
            )
        except ProviderError as e:
            log.warning("ConsultBeat: strong call failed: %s", e)
            return ts
        ts.ctx.record(usage)
        beat_in = usage.input_tokens if usage else 0
        beat_out = usage.output_tokens if usage else 0
        beat_cri = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
        beat_cci = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
        ts.record_beat(
            beat_id=self.id,
            model=MODEL_SONNET,
            tokens_in=beat_in,
            tokens_out=beat_out,
            cache_read=beat_cri,
            cache_creation=beat_cci,
        )
        ts.consult_ruling = (raw or "").strip()
        moderation.check_decoded(ts.consult_ruling)
        if not ts.consult_ruling:
            log.warning("ConsultBeat: empty rules ruling")
        return ts


class DMActBeat:
    """Ability-turn beat A: narrate the act + immediate consequence +
    state delta. Drops the grounding/observation work to the next beat
    so the model focuses tightly on rendering the supernatural moment."""

    id = "builtin:dm_act"

    def applies(self, ts: TurnState) -> bool:
        return ts.turn_class == "ability"

    def run(self, ts: TurnState) -> TurnState:
        gs = ts.gs

        # Cache split: _ACT_FOCUS + per-turn blocks live in the
        # volatile half so the stable pack prefix caches across beats
        # and turns.
        # seed_roster injected into stable; floor guard warns when
        # under proxy (same invariant as DMWorldStepBeat - same stable bytes).
        stable, volatile = build_system_blocks(
            gs.profile,
            ts.stage,
            gs.config,
            monster_level=ts.monster_level,
            has_npcs=ts.has_npcs,
            turn_class=ts.turn_class,
            seed_roster=gs.seed_roster,
        )
        warn_if_below_cache_floor(
            gs.config.pack_id or DEFAULT_PACK_ID,
            ts.stage,
            stable,
        )
        volatile = f"{volatile}{_ACT_FOCUS}"
        # addressee disambiguation block (same logic as DMWorldStep)
        if ts.addressee_block:
            volatile = f"{volatile}\n\n{ts.addressee_block}"
        # pack-declared directive blocks (same logic as DMWorldStep)
        if getattr(ts, "pack_blocks", None):
            volatile = f"{volatile}\n\n" + "\n\n".join(ts.pack_blocks)
        # DM-private notes
        _dm_notes_block = npc_mod.render_dm_notes_block(ts.gs.state)
        if _dm_notes_block:
            volatile = f"{volatile}\n\n{_dm_notes_block}"
        system = f"{stable}\n{volatile}"

        import json as _json

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
        if ts.consult_ruling:
            tail.append(
                f"[RULING - DM only, from the rules consult]\n{ts.consult_ruling}\n[/RULING]"
            )
        tail.append(ts.player_input)
        # prefix contract: DMActBeat is fixed at "short" profile.
        # Directive in user message, not system volatile.
        tail.append(PROFILES["short"].directive)

        def build_context(memory_block):
            return "\n\n".join([dice_block] + ([memory_block] if memory_block else []) + tail)

        from app.game.engine import _history_for_llm, MAX_HISTORY

        history = _history_for_llm(gs.history)
        system_blocks, messages, start, memory = context_mod.assemble(
            stable,
            volatile,
            history,
            build_context,
            context_mod.recall_for(ts),
            start=ts.window_start,
            memory_block=ts.memory_block,
            legacy_n=MAX_HISTORY,
        )

        ts.health_at_send = ts.ctx.health_score(system, messages)

        # The strong model occasionally returns empty narrative on ability turns
        # (~3-5% rate observed). Retry once with a sharper prompt rather
        # than ship a black hole to the player. Cost of one retry is
        # bounded; the fallback is a minimal stub if retry also fails.
        # haiku-large for custom worlds -> same fast-tier model with more
        # response budget; the strong model is reserved for rare escalations.
        # DMActBeat fixed at "short" profile (60 tok).
        eff_max = reply_max_tokens(ts, beat_default=1500, beat_profile_name="short")
        # The strong tier thinks within max_tokens; leave room for text.
        if ts.turn_model == MODEL_SONNET:
            eff_max = max(eff_max, 2000)
        result, beat_in, beat_out, beat_cri, beat_cci = self._call_and_parse(
            system_blocks,
            messages,
            ts,
            max_tokens=eff_max,
        )
        if not (result.narrative or "").strip():
            log.warning(
                "DMActBeat: empty narrative from %s - retrying with stricter directive",
                ts.turn_model,
            )
            stricter_volatile = volatile + (
                "\n\nCRITICAL: your previous attempt produced an empty narrative. "
                "You MUST write at least one short paragraph (2-4 sentences) "
                "describing what physically happens when the player attempts "
                "the act. Even if the attempt fails or produces an unexpected "
                "result, render the moment - never emit empty narrative."
            )
            try:
                retry_system, retry_messages, _s, _m = context_mod.assemble(
                    stable,
                    stricter_volatile,
                    history,
                    build_context,
                    context_mod.recall_for(ts),
                    start=start,
                    memory_block=memory,
                    legacy_n=MAX_HISTORY,
                )
            except context_mod.ContextTooLarge:
                log.warning("DMActBeat: retry exceeds context ceiling - falling back to stub.")
            else:
                result2, in2, out2, cri2, cci2 = self._call_and_parse(
                    retry_system,
                    retry_messages,
                    ts,
                    max_tokens=ts.reply_ceiling or eff_max,
                )
                beat_in += in2
                beat_out += out2
                beat_cri += cri2
                beat_cci += cci2
                if (result2.narrative or "").strip():
                    result = result2
                else:
                    log.warning("DMActBeat: retry ALSO empty - falling back to stub.")
            if not (result.narrative or "").strip():
                result.narrative = (
                    "You attempt the act. The world hesitates around you "
                    "for a moment, then settles. Something happened - what, "
                    "you can't quite tell yet."
                )

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

    def _call_and_parse(self, system, messages, ts, *, max_tokens):
        """One tagged LLM call (asked again once per move if incomplete) + parse + post-process.
        Returns (result, in, out, cache_read, cache_creation)."""
        result, in_, out_, cri_, cci_ = _tagged_reply(
            ts, system, messages, model=ts.turn_model, max_tokens=max_tokens, empty_ok=True
        )
        result.narrative = _replace_em_dashes(result.narrative)
        result.narrative = _strip_trailing_prompt(result.narrative)
        result.narrative = _strip_metagame_brackets(result.narrative)
        moderation.check_decoded(result.narrative)
        return result, in_, out_, cri_, cci_


class GroundingBeat:
    """Ability-turn beat B: paint where the player now is + what they
    observe. Reads the just-mutated worldstate (post dm_act) so the
    model describes the new vantage. Pure description - no state
    changes, no signals. Cheap haiku call.

    Skipped when dm_act produced an empty narrative (e.g. parser
    failure) so we don't ground the player in nothing.
    Skipped on tier5_reset turns (the act narrative already snaps the
    player back; grounding would contradict)."""

    id = "builtin:grounding"

    def applies(self, ts: TurnState) -> bool:
        if ts.turn_class != "ability":
            return False
        if ts.signals.get("tier5_reset") is True:
            return False
        # Always run on ability turns - even if DMActBeat's prior chunk
        # was the empty-fallback stub, grounding gives the player a
        # usable scene description from the (possibly updated) state.
        return True

    def run(self, ts: TurnState) -> TurnState:
        gs = ts.gs
        prior_act = "\n\n".join(c for c in ts.narrative_chunks if c).strip()

        # Minimal system prompt - no abilities block, no full NPC block,
        # no insight dice. Just enough to render position + observation.
        sys = (
            f"You are the DM, continuing a turn already in progress.\n\n"
            f"The player ({gs.profile.name}) just acted. The act has resolved "
            f"and worldstate is updated. The text so far already acknowledges "
            f"the act and shows the world's first reaction. Your job: in 1-2 "
            f"short paragraphs, write the last two story parts of this turn, "
            f"in order:\n"
            f"- DEVELOP: ground them in their NEW POSITION and describe what "
            f"they OBSERVE from this vantage - what they see, hear, smell - and "
            f"let one thing move the story forward, in its own sentence before the hook, never inside the hook sentence. Use only the updated "
            f"worldstate below.\n"
            f"- HOOK: end on a human hook for the next move - a person who "
            f"wants something, a situation that will not wait, or a question "
            f"someone puts to them, specific to this place. The hook is the "
            f"last sentence and nothing follows it; if someone speaks last, the line ends on the question or demand.\n\n"
            f"DO NOT re-narrate the act. DO NOT introduce new ability "
            f"consequences. Never end on a list of options.\n\n"
            f"FORMAT - every response, no exceptions:\n"
            f"[NARRATIVE]\n"
            f"... your 1-2 short paragraphs: development, then the hook ...\n"
            f"[/NARRATIVE]\n"
            f"[STATE_DELTA]\n"
            f"{{}}\n"
            f"[/STATE_DELTA]\n\n"
            f"WRITING - terse journalist voice; short, active sentences; "
            f"describe seen/heard/smelled; no em-dash drama; no editorial "
            f'parentheticals; no stock trailing prompts such as "What do you do?" '
            f"- the hook is a person, a situation or a question from the scene."
        )

        import json as _json

        state_json = _json.dumps(gs.state.to_dict(), indent=2)
        # prefix contract: GroundingBeat is fixed at "medium"
        # profile (2-4 sentences / 90 tok). Directive in user message.
        ctx_msg = (
            f"[WORLD STATE - updated]\n{state_json}\n[/WORLD STATE]\n\n"
            f"[PRIOR ACT - what just happened, do not repeat]\n"
            f"{prior_act}\n[/PRIOR ACT]\n\n"
            f"{PROFILES['medium'].directive}\n\n"
            f"Render the grounding.\n\n{HOOK_LAST}"
        )
        messages = [{"role": "user", "content": ctx_msg}]

        # GroundingBeat fixed at "medium" (90 tok).
        eff_max = reply_max_tokens(ts, beat_default=300, beat_profile_name="medium")
        result, beat_in, beat_out, beat_cri, beat_cci = _tagged_reply(
            ts,
            sys,
            messages,
            model=MODEL_HAIKU,
            max_tokens=eff_max,
            need_delta=False,
            droppable=True,
        )
        result.narrative = _replace_em_dashes(result.narrative)
        result.narrative = _strip_trailing_prompt(result.narrative)
        result.narrative = _strip_metagame_brackets(result.narrative)
        moderation.check_decoded(result.narrative)

        # Grounding is pure description - ignore any state delta the
        # model accidentally emits.
        ts.narrative_chunks.append(result.narrative or "")

        ts.record_beat(
            beat_id=self.id,
            model=MODEL_HAIKU,
            tokens_in=beat_in,
            tokens_out=beat_out,
            cache_read=beat_cri,
            cache_creation=beat_cci,
        )
        return ts


#
# NPC reaction (conditional)
#


class NpcReactBeat:
    """When NPCs are in scene and something significant just happened,
    render a brief reaction (glance, step back, half-formed question).
    Reads gs.state.npc_dispositions to pick which NPC reacts. Cheap
    haiku call. 1-2 sentences."""

    id = "builtin:npc_react"

    def applies(self, ts: TurnState) -> bool:
        if not ts.has_npcs:
            return False
        # Only fire after a substantive prior chunk
        if not any(c.strip() for c in ts.narrative_chunks):
            return False
        # Skip tier5_reset (everything snapped back; NPCs at the new
        # vantage haven't actually witnessed the cataclysm)
        if ts.signals.get("tier5_reset") is True:
            return False
        # On standard turns (no DMActBeat) we leave NPC reactions inside
        # the monolith. Only run when we've already had >=1 ability-side
        # beat produce content.
        return ts.turn_class == "ability"

    def run(self, ts: TurnState) -> TurnState:
        gs = ts.gs
        prior = "\n\n".join(c for c in ts.narrative_chunks if c).strip()
        npcs = gs.state.npc_dispositions or {}

        # npc_dispositions values were historically short strings
        # ("hostile", "trusting") but newer LLM passes write a dict
        # shape ({name, disposition, role, seen, knows}). Render either.
        def _disp_str(v):
            if isinstance(v, dict):
                s = v.get("disposition") or v.get("name") or ""
                return str(s)[:40]
            return str(v)[:40]

        npc_summary = (
            ", ".join(f"{name} ({_disp_str(disp)})" for name, disp in list(npcs.items())[:4])
            if npcs
            else "(no named NPCs in dispositions)"
        )

        sys = (
            "You are the DM. NPCs in the scene have just witnessed what "
            "happened. Pick ONE NPC from the dispositions list and render "
            "a brief reaction - a glance, a step, a half-question, body "
            "language. 1-2 short sentences. Your reaction ends the turn, so "
            "it is the turn's HOOK: aim it at the player - a look that wants "
            "an answer, a step toward them, or a short line that ends on a "
            "question to them. Any spoken line goes in straight double quotes; any gesture comes before it, and nothing follows the question. Stay on the moment the text ends on; do not "
            "open a second, unrelated question, and never offer a list of "
            "options. NEVER coin metagame terms ('Higher "
            "Plane', '10D entity', tier numbers). NEVER diagnose the "
            "player's hidden status. Use the NPC's world vocabulary.\n\n"
            "If no NPC plausibly witnessed (out of sight / too far), output "
            "empty NARRATIVE and empty STATE_DELTA - that's allowed and "
            "correct; the turn then ends on the hook already written.\n\n"
            "FORMAT:\n"
            "[NARRATIVE]\n"
            "... 1-2 short sentences, or empty if none witnessed ...\n"
            "[/NARRATIVE]\n"
            "[STATE_DELTA]\n"
            "{}\n"
            "[/STATE_DELTA]"
        )

        # prefix contract: NpcReactBeat is fixed at "short"
        # profile (ceiling 60 tok); its own 1-2 sentence directive in the user message.
        ctx_msg = (
            f"[NPCS IN SCENE]\n{npc_summary}\n[/NPCS IN SCENE]\n\n"
            f"[WHAT JUST HAPPENED]\n{prior}\n[/WHAT JUST HAPPENED]\n\n"
            f"{_NPC_REACT_LENGTH}\n\n"
            f"Render the NPC reaction (or empty if none plausibly witnessed)."
        )
        messages = [{"role": "user", "content": ctx_msg}]

        # NpcReactBeat fixed at "short" (60 tok).
        eff_max = reply_max_tokens(ts, beat_default=200, beat_profile_name="short")
        result, beat_in, beat_out, beat_cri, beat_cci = _tagged_reply(
            ts,
            sys,
            messages,
            model=MODEL_HAIKU,
            max_tokens=eff_max,
            need_delta=False,
            droppable=True,
            empty_ok=True,
        )
        result.narrative = _replace_em_dashes(result.narrative)
        result.narrative = _strip_trailing_prompt(result.narrative)
        result.narrative = _strip_metagame_brackets(result.narrative)
        moderation.check_decoded(result.narrative)

        # Only append if non-trivial - empty/whitespace skipped so we
        # don't add stray paragraph breaks.
        if (result.narrative or "").strip():
            ts.narrative_chunks.append(result.narrative)

        ts.record_beat(
            beat_id=self.id,
            model=MODEL_HAIKU,
            tokens_in=beat_in,
            tokens_out=beat_out,
            cache_read=beat_cri,
            cache_creation=beat_cci,
        )
        return ts


#
# Pipeline factory
#


def build_pipeline_for(
    turn_class: str,
    pack: dict | None = None,
    forced_beat_ids: set | frozenset = frozenset(),
    stakes: str = "normal",
    consult: bool = False,
):
    """Engine-default pipeline by turn class + stakes, composed with the
    pack's declared beats + overrides. Pack trigger beats run
    first (they inject context the DM beats consume); pipeline_overrides
    may gate grounding / npc_react / encounter off or per-condition.
    A pack with no beats[] passes through unchanged.

    stakes drives count + per-beat profile.

    * stakes=="mundane" + standard turn -> N=1 short (DMWorldStepBeat
      only, no pack beat composition). Structurally enforced: trigger
      beats add context suited to normal/dramatic prose; skipping them
      on a short 2-4 sentence cap prevents unrequested grounding/recap.
      The profile ceiling (60 tok) is applied by STAKES_TO_PROFILE via
      DMWorldStepBeat -> ts.length_profile = "short".

    * stakes in {"normal","dramatic"} + standard -> today's beat shape
      (single DMWorldStepBeat + pack beats composed normally) with the
      profile ceiling from STAKES_TO_PROFILE: "normal"->medium(90),
      "dramatic"->long(250).

    * "ability" turns -> [ConsultBeat (only when consult=True), DMActBeat, GroundingBeat, NpcReactBeat]:
      the consented rules consult precedes fast-tier narration.
      DMActBeat is the Haiku action beat (short/60 tok); GroundingBeat is the Haiku
      EXPLANATION/grounding beat (medium/90 tok); NpcReactBeat is a
      conditional Haiku reaction (short/60 tok). consent gating
      sets ts.consult before the pipeline runs. DMActBeat reads
      ts.turn_model, which stays on the fast tier.
    """
    from app.game.beats.base import BeatPipeline
    from app.game.beats.craft import CraftBeat

    #  mundane fast-path: structurally enforce N=1 for standard
    # turns so the cigarette case is a single capped DM call. Pack beat
    # composition is intentionally skipped - mundane turns don't warrant
    # context-injection trigger beats or NPC grounding recaps.
    if turn_class not in ("ability", "craft", "encounter") and stakes == "mundane":
        return BeatPipeline([DMWorldStepBeat()])

    if turn_class == "ability":
        beats = ([ConsultBeat()] if consult else []) + [
            DMActBeat(),
            GroundingBeat(),
            NpcReactBeat(),
        ]
    elif turn_class == "craft":
        beats = [CraftBeat()]
    elif turn_class == "encounter":
        from app.game.beats.encounter import EncounterBeat

        beats = [EncounterBeat(), DMWorldStepBeat()]
    else:
        beats = [DMWorldStepBeat()]
    pb = (pack or {}).get("pack_beats") or {}
    if pb.get("beats") or pb.get("overrides"):
        from app.game.pack_beats import compose_pipeline

        beats = compose_pipeline(beats, pack, set(forced_beat_ids))
    return BeatPipeline(beats)
