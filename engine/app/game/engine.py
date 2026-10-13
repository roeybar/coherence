"""Game orchestration - turn pipeline, undo, rewind, meta, save creation.

Pure functions of GameSave + inputs -> mutated GameSave + response payload.
The API layer is responsible for persistence (load -> engine -> save).
"""

import copy
import hashlib
import json
import random
import re
import time
from datetime import datetime, timezone
from dataclasses import asdict

from app.game.context import (
    ContextEngine,
    ContextTooLarge,
    check_floor,
    context_ceiling,
    recall_block,
    window_start,
)
from app.game.llm import (
    MODEL_HAIKU,
    MODEL_SONNET,
    classify_turn,
    classify_stakes,
    generate_world_premise,
    rewind_debrief,
    call_llm_plain,
)
from app import store
from app.providers import registry
from app.game import lore as lore_pack
from app.game import moderation as moderation_mod
from app.game.memory import format_recall_block, index_turn, recall
from app.game.parser import ReplyIncomplete
from app.game.prompts import (
    meta_system,
)
from app.game.state import (
    UNDO_CAP,
    CharacterProfile,
    GameConfig,
    GameSave,
    WorldState,
    make_world_state,
)
from app.game import telemetry
from app.game.turn_zero import normalise_turn_zero
from app.game.addressee import (
    AddresseeAmbiguous,
    AddresseeKind,
    AddresseeResult,
    classify_addressee,
    looks_like_dialogue,
    resolve_from_pick,
    system_prompt_block as addressee_block,
)
from app.game import npcs as npc_mod
from app.game.gate_base import TurnGate
from app.game.escalation import (
    StrongGate,
    default_policy_for_tier,
    normalise_policy,
    tier_allows_sonnet,
)


def _npc_block_for_turn(gs, addr_result, player_input: str) -> str:
    """render NPC record blocks for the system prompt based on
    which NPCs the addressee classifier resolved to. Empty string for
    none / broadcast / crowd / AMBIGUOUS (the last never reaches here)."""
    names: list[str] = []
    k = getattr(addr_result, "kind", None)
    if k == AddresseeKind.NAMED:
        names = [addr_result.name] if addr_result.name else []
    elif k == AddresseeKind.NAMED_MULTI:
        names = list(addr_result.names or [])
    elif k == AddresseeKind.UNIQUE:
        names = [addr_result.name] if addr_result.name else []
    if not names:
        return ""
    block = npc_mod.render_in_scope(
        gs.state,
        names,
        player_input,
        gs.state.location,
    )
    return ("\n\n" + block) if block else ""


MAX_HISTORY = 6


# Quick lexical signals that the player intends to interact with an NPC.
# Used to flip on the full NPC_KNOWLEDGE block when state.npc_dispositions
# is still empty (first-encounter case). Lowercase substring match.
_DIALOGUE_CUES = (
    "ask ",
    "asks ",
    "tell ",
    "say ",
    "says ",
    "speak ",
    "talk ",
    "greet",
    "address",
    "shout ",
    "whisper",
    "reply",
    "answer ",
    '"',
    "'",
)


def _detect_npcs_in_play(gs, player_input: str) -> bool:
    """True if the model needs the full NPC plausibility block this turn:
    - state.npc_dispositions has any entry (the durable signal), or
    - the player's message contains a dialogue cue (covers the
      first-encounter case where state hasn't been populated yet)."""
    try:
        if gs.state.npc_dispositions:
            return True
    except Exception:
        pass
    pi = (player_input or "").lower()
    return any(c in pi for c in _DIALOGUE_CUES)


def _detect_npc_question(narrative: str, state) -> "tuple[str, str] | None":
    """Heuristic: find a `?` in the narrative and check whether a known
    NPC name appears within ~100 chars before it.  Returns
    (npc_name, question_text) for the first matching question, or None.

    Intentionally conservative - the NPC name must be in
    state.npc_dispositions so we only react to established characters.
    Does not modify state; read-only."""
    if not narrative or "?" not in narrative:
        return None
    try:
        known_names: list[str] = []
        disp = getattr(state, "npc_dispositions", None) or {}
        for k, v in disp.items():
            if isinstance(v, dict) and v.get("name"):
                known_names.append(str(v["name"]))
            elif isinstance(k, str) and k and ":" not in k:
                known_names.append(k)
        if not known_names:
            return None
        for m in re.finditer(r"\?", narrative):
            pos = m.start()
            window = narrative[max(0, pos - 100) : pos + 1]
            for name in known_names:
                if re.search(rf"\b{re.escape(name)}\b", window, re.IGNORECASE):
                    # Extract the sentence containing the question mark
                    prev_end = max(
                        narrative.rfind(".", 0, pos),
                        narrative.rfind("!", 0, pos),
                        narrative.rfind("?", 0, pos - 1) if pos > 0 else -1,
                        pos - 150,
                    )
                    sent_start = max(0, prev_end + 1)
                    question = narrative[sent_start : pos + 1].strip()[:200]
                    return (name, question)
    except Exception:
        pass
    return None


def _history_for_llm(history: list) -> list:
    """Project history to {role, content} with English content. New entries
    use content_en; legacy/English-only entries use content."""
    out = []
    for m in history:
        c = m.get("content_en") or m.get("content") or ""
        out.append({"role": m["role"], "content": c})
    return out


def _history_for_display(history: list, locale: str) -> list:
    """Project history for the player. Prefer locale-specific content, fall
    back through content_en, content."""
    out = []
    key = f"content_{locale}"
    for m in history:
        if locale != "en" and m.get(key):
            c = m[key]
        else:
            c = m.get("content_en") or m.get("content") or ""
        out.append({"role": m["role"], "content": c})
    return out


def _new_history_entry(
    role: str, content_en: str, locale: str = "en", content_locale: str | None = None
) -> dict:
    """Build a uniform history dict. Always carries content_en. For non-en
    locales also carries content_<locale>. The `content` key is read when
    present but never written."""
    e = {"role": role, "content_en": content_en}
    if locale != "en" and content_locale is not None:
        e[f"content_{locale}"] = content_locale
    return e


def _parse_world_premise(raw: str) -> dict:
    """Parse a world_premise JSON blob, tolerant of legacy ```json fences
    and dict-coerced values. Returns {} on any failure so callers can
    treat it as 'no overrides'."""
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s).strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    i = s.find("{")
    j = s.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return {}
    try:
        v = json.loads(s[i : j + 1])
        return v if isinstance(v, dict) else {}
    except Exception:
        # Trailing-comma cleanup as a last resort.
        try:
            cleaned = re.sub(r",(\s*[}\]])", r"\1", s[i : j + 1])
            v = json.loads(cleaned)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}


def create_save(
    name: str, profile: CharacterProfile, config: GameConfig, world_hint: str = ""
) -> GameSave:
    """Build a new save: generate world (if custom), seed state, insert opening line.

    `world_type` is one of:
      - "canon" - the pack's canonical content
      - "custom" - player-built structured world ( form -> haiku premise)
      - "freeform" - no world setup; sparse opening, LLM improvises everything
    """
    # Player text from the new-game form reaches the model here and in the
    # stable system prompt of every turn, so it is filtered first.
    # one_memory only where it is kept: a canon pack's own memory replaces
    # it and a freeform world drops it (see below).
    pack_default = (
        ((lore_pack.load_pack(config.pack_id) or {}).get("manifest", {}).get("one_memory_default"))
        or {}
    ).get("en") or ""
    keeps_memory = not (
        config.world_type == "freeform" or (config.world_type == "canon" and pack_default.strip())
    )
    for text in (
        profile.name,
        profile.appearance,
        profile.notes,
        config.one_memory if keeps_memory else "",
        world_hint,
    ):
        _moderate(config, text or "")

    if config.world_type == "custom":
        try:
            config.world_premise = generate_world_premise(world_hint, config)
        except Exception:
            config.world_type = "canon"
            config.world_premise = ""
    moderation_mod.check_decoded(_parse_world_premise(config.world_premise))

    state = make_world_state(config.difficulty, config)

    if config.world_type == "custom" and config.world_premise:
        wp = _parse_world_premise(config.world_premise)
        overrides = wp.get("world_state_overrides", {}) or {}
        valid = set(state.to_dict().keys())
        for k, v in overrides.items():
            if k in valid:
                setattr(state, k, v)

    state.tutorial_stage = 1

    # pack-canonical one-memory override
    # The Coherence pack declares `one_memory_default` per-locale in
    # pack.json - that's the canonical plot anchor ("Fix It All"), not a
    # player choice. When a pack declares one, we use it verbatim and
    # ignore any value the client sent. For packs without a canonical
    # default:
    #   - 'custom' world type: player-supplied -> respected; blank stays blank.
    #   - 'freeform' world type: skip the memory entirely (the player is
    #     the world-builder; no canon to anchor to).
    pack_obj = lore_pack.load_pack(config.pack_id)
    pack_memories = ((pack_obj or {}).get("manifest", {}).get("one_memory_default")) or {}
    canon_en = (pack_memories.get("en") or "").strip()
    canon_loc = (pack_memories.get(config.locale) or canon_en).strip()

    if canon_en and config.world_type == "canon":
        # Pack canon takes precedence; the client's one_memory (if any)
        # is discarded. Server-side enforcement so a custom frontend
        # can't smuggle a non-canonical memory in.
        config.one_memory = canon_en  # English is the canonical store
        memory_en = canon_en
        memory_loc = canon_loc or canon_en
    elif config.world_type == "freeform":
        config.one_memory = ""
        memory_en = memory_loc = ""
    else:
        config.one_memory = (config.one_memory or "").strip()[:240]
        # Custom worlds: same string in both templates (player-authored,
        # no auto-translation).
        memory_en = config.one_memory
        memory_loc = config.one_memory

    # Resolve opening in English (always - for the LLM-side history) and
    # in the player's locale (for display). Both stored on the first entry.
    # Freeform pulls the bare-bones opening_freeform template; the others
    # use the canonical opening.
    template_key = "opening_freeform" if config.world_type == "freeform" else "opening"
    opening_en = (
        lore_pack.get_template(pack_obj, template_key, locale="en", gender=profile.gender or "m")
        or ""
    ).strip()
    opening_loc = opening_en
    if config.locale != "en":
        opening_loc = (
            lore_pack.get_template(
                pack_obj, template_key, locale=config.locale, gender=profile.gender or "m"
            )
            or opening_en
        ).strip()

    if config.world_type == "custom" and config.world_premise:
        wp = _parse_world_premise(config.world_premise)
        moderation_mod.check_decoded(wp)
        custom_open = (wp.get("opening_scene") or "").strip()
        if custom_open:
            opening_en = custom_open
            if config.locale == "en":
                opening_loc = opening_en

    # Substitute the {one_memory} placeholder per-template (en + locale
    # get their respective strings, so neither copy gets the other's).
    opening_en = _substitute_memory(opening_en, memory_en)
    opening_loc = _substitute_memory(opening_loc, memory_loc)

    # seed the per-save roster from pack canon. Extract only the
    # identity-immutable fields (no start_disposition/start_location) so
    # the stable prefix is byte-identical across ordinary turns.
    # Also put_npc each seed NPC so they appear in roster_view at turn 0.
    _seed_roster: dict = {}
    try:
        _pack_roster = lore_pack.get_seed_roster(pack_obj)
        if _pack_roster and _pack_roster.get("npcs"):
            from app.game import npcs as _npc_mod

            _identity_npcs: list[dict] = []
            for _sr_npc in _pack_roster.get("npcs") or []:
                _npc_id = str(_sr_npc.get("npc_id") or "").strip()
                if not _npc_id:
                    continue
                # Store identity-only slice (no start_* volatile fields)
                _identity_npcs.append(
                    {
                        "npc_id": _npc_id,
                        "name": str(_sr_npc.get("name") or ""),
                        "archetype": str(_sr_npc.get("archetype") or ""),
                        "appearance": str(_sr_npc.get("appearance") or ""),
                        "backstory": str(_sr_npc.get("backstory") or ""),
                        "origin_alignment": str(_sr_npc.get("origin_alignment") or ""),
                        "core_personality": str(_sr_npc.get("core_personality") or ""),
                        "starting_inventory": list(_sr_npc.get("starting_inventory") or []),
                    }
                )
                # Seed the volatile NPC record for roster_view at turn 0
                _sr_npc_rec = _npc_mod.NPC(
                    npc_id=_npc_id,
                    name=str(_sr_npc.get("name") or ""),
                    role=str(_sr_npc.get("archetype") or ""),
                    persona=" ".join(
                        filter(
                            None,
                            [
                                str(_sr_npc.get("core_personality") or ""),
                                str(_sr_npc.get("backstory") or ""),
                            ],
                        )
                    ),
                    location=str(_sr_npc.get("start_location") or ""),
                    disposition=int(_sr_npc.get("start_disposition") or 0),
                    first_seen_turn=0,
                    last_seen_turn=0,
                )
                _npc_mod.put_npc(state, _sr_npc_rec)
            _seed_roster = {
                "player_kit": _pack_roster.get("player_kit") or {},
                "npcs": _identity_npcs,
            }
    except Exception as _sr_e:
        import logging as _log_mod

        _log_mod.getLogger(__name__).warning(
            "create_save: seed_roster init failed for pack %s - %s",
            config.pack_id,
            _sr_e,
        )

    history_entry = _new_history_entry(
        "assistant",
        opening_en,
        locale=config.locale,
        content_locale=opening_loc if config.locale != "en" else None,
    )
    return GameSave(
        name=name,
        profile=profile,
        state=state,
        config=config,
        history=[history_entry],
        seed_roster=_seed_roster,
    )


# helpers
def _substitute_memory(opening: str, memory: str) -> str:
    """Replace the `{one_memory}` placeholder. If the placeholder isn't
    in the template, do nothing. If memory is blank,
    drop the entire memory-line (single line containing the placeholder)
    so the opening reads cleanly without amnesia-breaking gaps."""
    if not opening:
        return opening
    if "{one_memory}" not in opening:
        return opening
    if not (memory or "").strip():
        # Drop any line that contains the placeholder, fold blank-line
        # whitespace cleanly.
        lines = opening.splitlines()
        kept = [ln for ln in lines if "{one_memory}" not in ln]
        text = "\n".join(kept)
        # Collapse 3+ consecutive blank lines down to 2 (defensive).
        import re as _re

        return _re.sub(r"\n{3,}", "\n\n", text)
    return opening.replace("{one_memory}", memory.strip())


# seed-roster helpers

_CANON_THRESHOLD_WITNESSED = 5  # witnessed events to qualify
_CANON_THRESHOLD_TURN_SPAN = 3  # (last_seen - first_seen) to qualify


def _disposition_band(disposition: int) -> str:
    """Map a numeric disposition to a factional-origin label string.
    Used to seed origin_alignment for runtime-promoted NPCs."""
    if disposition >= 50:
        return "allied with the player"
    if disposition >= 10:
        return "friendly toward the player"
    if disposition <= -50:
        return "hostile toward the player"
    if disposition <= -10:
        return "wary of the player"
    return "neutral toward the player"


def maybe_rebake_roster(gs: "GameSave") -> None:
    """event-gated re-bake: scan gs.state.npcs for runtime NPCs
    that have crossed the canon threshold and are not yet in
    gs.seed_roster. Append a minimal identity dict for each qualifying
    NPC to gs.seed_roster["npcs"].

    Criteria (defaults):
      - len(witnessed) >= _CANON_THRESHOLD_WITNESSED
      - (last_seen_turn - first_seen_turn) >= _CANON_THRESHOLD_TURN_SPAN
      - npc_id not already in gs.seed_roster["npcs"]

    Additive-only - dead canon stays as backstory, narrated via
    volatile disposition. Best-effort: any error is logged
    and swallowed so it never blocks a turn."""
    try:
        import logging as _log_mod

        _log = _log_mod.getLogger(__name__)
        from app.game import npcs as _npc_mod

        npcs_store = _npc_mod.get_npcs(gs.state)
        if not npcs_store:
            return

        roster = gs.seed_roster or {}
        existing_ids: set[str] = {str(n.get("npc_id") or "") for n in (roster.get("npcs") or [])}

        new_entries: list[dict] = []
        for npc_id, npc_dict in npcs_store.items():
            if not isinstance(npc_dict, dict):
                continue
            if npc_id in existing_ids:
                continue
            witnessed = npc_dict.get("witnessed") or []
            first_t = int(npc_dict.get("first_seen_turn") or 0)
            last_t = int(npc_dict.get("last_seen_turn") or 0)
            if (
                len(witnessed) < _CANON_THRESHOLD_WITNESSED
                or (last_t - first_t) < _CANON_THRESHOLD_TURN_SPAN
            ):
                continue
            # Freeze minimal identity
            entry = {
                "npc_id": npc_id,
                "name": str(npc_dict.get("name") or npc_id),
                "archetype": str(npc_dict.get("role") or ""),
                "backstory": str(npc_dict.get("persona") or ""),
                "appearance": "",
                "origin_alignment": _disposition_band(int(npc_dict.get("disposition") or 0)),
                "core_personality": str(npc_dict.get("persona") or ""),
                "starting_inventory": [],
            }
            new_entries.append(entry)
            existing_ids.add(npc_id)
            _log.info(
                "maybe_rebake_roster: promoted NPC %r to seed canon (witnessed=%d, span=%d)",
                npc_id,
                len(witnessed),
                last_t - first_t,
            )

        if new_entries:
            updated_roster = dict(roster)
            updated_roster["npcs"] = list(roster.get("npcs") or []) + new_entries
            gs.seed_roster = updated_roster
    except Exception as _e:
        import logging as _log_mod

        _log_mod.getLogger(__name__).warning(
            "maybe_rebake_roster failed: %s",
            _e,
        )


class ModerationBlocked(TurnGate):
    """Raised by run_turn when a hard moderation rule blocks the input."""

    def __init__(self, category: str, description: str):
        self.category = category
        self.description = description


def _moderate(config: GameConfig, text: str) -> None:
    """Run the hard input filter on player text before it reaches any
    model call, against the English lists whatever the save's locale
    (the engine ships English lists only). Raises ModerationBlocked on a hit."""
    check = moderation_mod.check_input(
        text,
        pack_id=config.pack_id or lore_pack.DEFAULT_PACK_ID,
        locale="en",
    )
    if check.blocked:
        raise ModerationBlocked(check.category, check.description)


def run_turn(
    gs: GameSave,
    player_input: str,
    save_id: str,
    *,
    regenerated: bool = False,
    original_input: str | None = None,
    feedback: str | None = None,
    ua: str = "",
    consent_token: str | None = None,
    decline_escalation: bool = False,
    effective_tier: str | None = None,
    addressee=None,
    ability_intent: str | None = None,
    reframe_confirm: str | None = None,
    world_rule_confirm: str | None = None,
    craft_roll: int | None = None,
    dice_roll: int | None = None,
    suggestion_index: int | None = None,
    suggestions_shown: list | None = None,
) -> dict:
    """Public entry point - thin wrapper around _run_turn_inner.

    Creates the aux_llm_rows list. A gate exception is re-raised as is and
    the rows gathered before it are dropped: the save's token counts cover
    completed turns only, while the player's provider has already charged
    for every call that completed, on gated and failed turns too.

    Restores the saved turn fields and physics cache on TurnGate, ContextTooLarge, ReplyIncomplete and OutputBlocked;
    extend this list when GameSave gains a field written during a turn.
    """
    saved = {
        name: copy.deepcopy(getattr(gs, name))
        for name in ("state", "profile", "config", "glossary", "seed_roster", "checkpoint_state")
    }
    saved.update(
        {name: list(getattr(gs, name)) for name in ("history", "undo_stack", "checkpoint_history")}
    )
    saved.update(
        {
            name: getattr(gs, name)
            for name in (
                "turn",
                "session_in",
                "session_out",
                "checkpoint_turn",
                "checkpoint_len",
                "read_only",
                "shadow_of",
            )
        }
    )
    try:
        doc = store.current_save()
        saved_physics_cache = copy.deepcopy(doc["physics_cache"])
    except store.StoreUnbound:
        doc = None
    created = gs.created

    def move_digest():
        state = saved["state"].to_dict()
        state.pop("pending_input", None)
        payload = {
            "history": [
                {"role": row["role"], "content": row.get("content_en") or row.get("content", "")}
                for row in saved["history"]
            ],
            "state": state,
            "config": asdict(saved["config"]),
            "profile": asdict(saved["profile"]),
            "seed_roster": saved["seed_roster"],
            "glossary": saved["glossary"],
            "turn": saved["turn"],
            "created": created,
            "input": player_input,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    live_tier = (effective_tier or gs.config.tier or "").strip().lower()
    strong_gate = StrongGate(
        policy=normalise_policy(
            gs.config.ability_escalation or default_policy_for_tier(gs.config.tier)
        ),
        allowed=tier_allows_sonnet(live_tier)
        and registry.has_strong_tier(registry.current_provider()),
        consent_token=consent_token,
        declined=decline_escalation,
        regenerated=regenerated,
        save_id=save_id,
        turn_index=gs.turn,
        move=move_digest,
    )
    aux_llm_rows: list = []
    try:
        return _run_turn_inner(
            gs,
            player_input,
            save_id,
            regenerated=regenerated,
            original_input=original_input,
            feedback=feedback,
            ua=ua,
            consent_token=consent_token,
            decline_escalation=decline_escalation,
            effective_tier=effective_tier,
            addressee=addressee,
            ability_intent=ability_intent,
            reframe_confirm=reframe_confirm,
            world_rule_confirm=world_rule_confirm,
            craft_roll=craft_roll,
            dice_roll=dice_roll,
            suggestion_index=suggestion_index,
            suggestions_shown=suggestions_shown,
            aux_llm_rows=aux_llm_rows,
            strong_gate=strong_gate,
        )
    except (TurnGate, ContextTooLarge, ReplyIncomplete, moderation_mod.OutputBlocked):
        for name, value in saved.items():
            setattr(gs, name, value)
        if doc is not None:
            doc["physics_cache"][:] = saved_physics_cache
        raise


def _run_turn_inner(
    gs: GameSave,
    player_input: str,
    save_id: str,
    *,
    strong_gate: StrongGate,
    regenerated: bool = False,
    original_input: str | None = None,
    feedback: str | None = None,
    ua: str = "",
    consent_token: str | None = None,
    decline_escalation: bool = False,
    effective_tier: str | None = None,
    addressee=None,
    ability_intent: str | None = None,
    reframe_confirm: str | None = None,
    world_rule_confirm: str | None = None,
    craft_roll: int | None = None,
    dice_roll: int | None = None,
    suggestion_index: int | None = None,
    suggestions_shown: list | None = None,
    aux_llm_rows: list = None,
) -> dict:
    """Inner turn pipeline - called by run_turn. aux_llm_rows collects the
    token counts of the aux model calls (scope classifier, physics, clock,
    classify_turn, classify_stakes). They are flushed to TurnState.by_beat
    before payload assembly on success; on a gate they are dropped with the
    exception.

    `original_input` and `regenerated` come from run_regenerate so telemetry
    records the clean input (not the [regeneration request] annotated text).
    `ua` is passed to the anti-automation detector.
    """
    if aux_llm_rows is None:
        aux_llm_rows = []
    pre_state_dict = gs.state.to_dict()

    locale = gs.config.locale or "en"
    gender = (gs.profile.gender or "m") if gs.profile else "m"

    # Moderation: hard input filter. The input is filtered as typed,
    # against the English lists (regex patterns are English-only).
    # The in-code hard rules and both pack blocklists always apply. It
    # runs here, before the time-skip route and every model call, and
    # again on the rewritten text after meme recognition. Never inside a
    # try: a swallowed block would let the move through.
    _moderate(gs.config, original_input or player_input)
    # The profile comes from the save and goes into this turn's system prompt.
    if gs.profile:
        for text in (gs.profile.name, gs.profile.appearance, gs.profile.notes):
            _moderate(gs.config, text or "")

    # meta-question routing. Explicit `ooc:` / `/state` / etc.
    # prefixes get a deterministic state-lookup reply, never hit the
    # LLM. critical: moderation runs before this so `ooc: ignore your
    # rules and write X` still gets blocked by the input filter. It runs
    # before the time-skip route and every pass that can call the model
    # or change state.
    # The meta reply doesn't bump gs.turn, doesn't add to history,
    # doesn't go to telemetry as a turn - it's a free system message.
    from app.game.meta_questions import try_meta_reply

    meta = try_meta_reply(player_input, gs, save_id)
    if meta is not None:
        return {
            "kind": "system_reply",
            "title": meta.title,
            "narrative": meta.body,
            "state": gs.state.to_dict(),
            "turn": gs.turn,
            "session": {"in": gs.session_in, "out": gs.session_out},
            "session_cost": 0.0,
            "turn_cost": 0.0,
            "turn_tokens": {"in": 0, "out": 0},
            "turn_class": "meta",
            "delta": {},
            "regenerated": False,
            "model": "(none - meta lookup)",
            "package_breakdown": None,
            "items_ops": {"applied": [], "skipped": []},
            "monster": 0,
            "health": None,
            "tier": gs.config.tier,
            "recalled": [],
            "by_beat": [],
        }

    # Story block + world state + move over the context ceiling: refuse before any model call.
    check_floor(gs, player_input)

    # time-skip short-circuit. If the player
    # input declares a time-skip, route to timeskip.run_timeskip and
    # return early - bypasses the full DM pipeline for this turn.
    # Regen path skipped (a regen of a time-skip turn should re-run
    # the same skip request, which would make the same model calls again - return the
    # last narrative instead). Best-effort; if anything fails, fall
    # through to the standard turn.
    if not regenerated:
        try:
            from app.game import timeskip as ts_mod

            skip = ts_mod.detect_timeskip(player_input)
            if skip:
                return _run_timeskip_turn(gs, save_id, player_input, skip)
        except Exception as _e:
            import logging as _logging

            _logging.getLogger(__name__).warning("timeskip route failed: %s", _e)

    # wholesale-state-reframe gate. If the
    # player input declares a sweeping change ("instead of X we'll now
    # Y", "the war is over", "complete development of all territories")
    # and they haven't confirmed yet, raise ReframeAmbiguous so the API
    # can pop the modal. Regen path skipped (the prior turn was already
    # resolved under its own intent).
    if reframe_confirm not in ("confirm", "cancel") and not regenerated:
        try:
            from app.game import reframe_gate as rg_mod

            label = rg_mod.detect_reframe(player_input)
            if label:
                raise rg_mod.ReframeAmbiguous(
                    label=label,
                    likely_affected=rg_mod.likely_affected_keys(gs.state),
                )
        except Exception as _e:
            # Re-raise the gate, swallow other failures
            if _e.__class__.__name__ == "ReframeAmbiguous":
                raise
            import logging as _logging

            _logging.getLogger(__name__).warning("reframe gate failed: %s", _e)
    elif reframe_confirm == "cancel":
        # Player cancelled - return a no-op narrative without touching
        # state. The frontend can clear the turn and let them rephrase.
        return {
            "kind": "system_reply",
            "title": "Turn cancelled",
            "narrative": "(turn discarded - rephrase your input and try again)",
            "state": gs.state.to_dict(),
            "turn": gs.turn,
            "session": {"in": gs.session_in, "out": gs.session_out},
            "session_cost": 0.0,
            "turn_cost": 0.0,
            "turn_tokens": {"in": 0, "out": 0},
            "turn_class": "meta",
            "delta": {},
            "regenerated": False,
            "model": "(none - reframe cancelled)",
            "package_breakdown": None,
            "items_ops": {"applied": [], "skipped": []},
            "monster": 0,
            "health": None,
            "tier": gs.config.tier,
            "recalled": [],
            "by_beat": [],
        }

    # turn-zero deterministic fixes
    # Fluffy typos ("loko aroundd") and self-identity narration ("i'm a
    # girl") should never reach the LLM as canon. We collapse repeated
    # characters and, for English locale, flip profile.gender on a
    # plain identity assertion so the rest of the pipeline (and the
    # opening's gendered Hebrew variant on later turns) sees the
    # correct value. English only.
    if gs.turn == 0 and locale == "en":
        normalised, flip = normalise_turn_zero(player_input)
        player_input = normalised
        if flip and flip != gender:
            gs.profile.gender = flip
            gender = flip

    # meme recognition - rewrite LLM-facing player input
    # when it matches a known external-canon pattern. Player still
    # sees their original text in chat; only the LLM gets the
    # rewrite. We pin `player_input_for_history` before the rewrite
    # and use it when the history entry is appended at end of turn.
    player_input_for_history = player_input
    try:
        from app.game.meme_recognition import recognise as recognise_memes

        rec = recognise_memes(
            player_input,
            pack_id=gs.config.pack_id or lore_pack.DEFAULT_PACK_ID,
            player_name=(gs.profile.name if gs.profile else "the player"),
        )
        if rec.triggered:
            player_input = rec.rewritten
    except Exception as e:
        # Never block a turn on meme recognition.
        import logging

        logging.getLogger(__name__).warning("meme_recognition failed: %s", e)

    # Second moderation pass on the text the model will see. On a
    # regenerate that is the replayed move and the rewritten request.
    for text in (original_input or "", player_input):
        _moderate(gs.config, text)

    # addressee classification (deterministic, no LLM)
    # Three paths:
    #   1. Player has already picked from the clarification modal ->
    #      resolve_from_pick honours their pick.
    #   2. Regenerate path -> keep the original turn's addressee
    #      behaviour by skipping classification (the prior model output
    #      was already produced under whatever resolution applied).
    #   3. Fresh turn -> classify. If AMBIGUOUS, raise so the API can
    #      pause and return the clarification payload (zero LLM cost
    #      on the abandoned path).
    # fixed (pack-declared) characters that appear at specific
    # arc/turn windows. Inject before the addressee classifier so the
    # player can immediately address them by name on the turn they
    # become eligible. Best-effort; a malformed pack entry never blocks.
    try:
        from app.game import fixed_characters as fixed_mod
        from app.game import lore as _lp

        _pack_for_fixed = _lp.load_pack(gs.config.pack_id)
        fixed_mod.inject_fixed_for_turn(gs, _pack_for_fixed)
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("fixed_characters injection failed: %s", _e)

    # pre-DM player-coined name extraction. If the player offers
    # a nickname in their input ("call you Friend"), seed an NPC record
    # now so the DM beat's NPC context block includes it and the DM
    # uses the coined name in narration. Without this, the engine never
    # captures the name (only DM-narrated introductions match the
    # existing patterns) and downstream features (colour,
    # fixed-character continuity, addressee classifier) silently miss.
    try:
        npc_mod.ingest_player_input(
            gs.state,
            player_input,
            current_location=gs.state.location,
            turn=gs.turn,
        )
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("player-coined name ingest failed: %s", _e)

    # physicalisation scope classifier + entropy accumulator.
    # Runs before TurnState construction so the DM beat system prompt
    # includes the appropriate block.
    #
    # Three independent passes:
    #   1. Parse player input for self-imposed rule additions ("I can only
    #      do this with effort"). Append to state.self_restrictions.
    #   2. Parse player input for rule removal ("I let go of that limit").
    #      Drop the matching restriction (or all on generic release).
    #   3. Tier 1 haiku classifier -> scope label. If confidence == "low",
    #      Tier 2 strong-model fallback (~100/50 tokens ~ $0.001/call).
    #
    # State writes are idempotent on input. Failures are swallowed (the
    # turn always proceeds even if classification dies).
    phys_scope = "none"
    phys_block = ""
    phys_grounding: dict | None = None
    phys_intent: str | None = ability_intent  # carried into render
    # Diceless-by-default gate vars - defined at outer scope so both dice
    # gates (pre-turn detect_dice_request, post-DM detect_dice_in_narrative)
    # can read them even when the
    # physicalisation try below raises or is skipped (fail-safe: suppression
    # stays on the safe default path whether or not the pack check succeeds).
    _reality_bending = False
    _pack_has_phys = bool(
        lore_pack.get_template(
            lore_pack.load_pack(gs.config.pack_id or lore_pack.DEFAULT_PACK_ID),
            "physicalisation",
        )
    )
    _dice_opt_in = bool(getattr(gs.config, "dice_mechanics", False))
    _dice_suppressed = _pack_has_phys and not _dice_opt_in
    try:
        from app.game import physicalisation as phys_mod

        # 1. Self-restriction additions
        add = phys_mod.parse_self_restriction(player_input)
        if add:
            restrictions = list(gs.state.get("self_restrictions") or [])
            restrictions.append({**add, "set_at_turn": gs.turn})
            gs.state.self_restrictions = restrictions
        # 2. Self-restriction removals (the reverse path: the player
        #    can release a limit they previously imposed)
        rem = phys_mod.parse_restriction_removal(player_input)
        if rem:
            current = list(gs.state.get("self_restrictions") or [])
            gs.state.self_restrictions = phys_mod.apply_restriction_removal(
                current,
                rem,
            )
        # 3. Scope classification
        cls, _scope_usage = phys_mod.classify_scope(player_input)
        phys_scope = cls["scope"]
        aux_llm_rows.append(
            {
                "id": "aux:scope",
                "model": MODEL_HAIKU,
                "tokens_in": getattr(_scope_usage, "input_tokens", 0) or 0,
                "tokens_out": getattr(_scope_usage, "output_tokens", 0) or 0,
                "cache_read": getattr(_scope_usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation": getattr(_scope_usage, "cache_creation_input_tokens", 0) or 0,
            }
        )
        if cls.get("confidence") == "low" and phys_scope != "none" and strong_gate.need("physics"):
            # Strong-model fallback on ambiguous cases
            cls2, _scope2_usage = phys_mod.classify_scope_tier2(player_input)
            phys_scope = cls2["scope"]
            aux_llm_rows.append(
                {
                    "id": "aux:scope_t2",
                    "model": MODEL_SONNET,
                    "tokens_in": getattr(_scope2_usage, "input_tokens", 0) or 0,
                    "tokens_out": getattr(_scope2_usage, "output_tokens", 0) or 0,
                    "cache_read": getattr(_scope2_usage, "cache_read_input_tokens", 0) or 0,
                    "cache_creation": getattr(_scope2_usage, "cache_creation_input_tokens", 0) or 0,
                }
            )

        # Reality-bending floor: impossible wishes (teleport, stop time, etc.)
        # always route to the within/surroundings grounding, regardless of
        # dice_mechanics opt-in. Sets _reality_bending (outer scope) so the
        # dice gate skips cleanly even when scope happened to be "none".
        if _pack_has_phys and phys_mod.detect_reality_bending(player_input):
            _reality_bending = True
            if phys_scope in ("none", "trivial"):
                phys_scope = "local"

        # 4. ability-use intent gate. When the scope is local+
        # (ABILITY_GATE_SCOPES) and the player hasn't already picked
        # via the modal, look for an unambiguous phrase in the input;
        # if neither route is committed to, raise to surface the
        # clarification modal. Skipped on regen (the prior turn was
        # already resolved under its own intent).
        if phys_intent is None and not regenerated and phys_mod.is_ability_gate_scope(phys_scope):
            detected = phys_mod.detect_explicit_intent(player_input)
            if detected is not None:
                phys_intent = detected
            else:
                raise phys_mod.AbilityIntentAmbiguous(scope=phys_scope)

        # 5. Entropy bookkeeping - applied after intent is known so we
        # don't tax the lattice route. Only external intent accrues
        # attention; internal is the silent route by design.
        if phys_scope != "none" and phys_intent == phys_mod.INTENT_EXTERNAL:
            phys_mod.update_entropy(gs.state, phys_scope)
            phys_mod.catalogue_entropy(
                gs.state,
                phys_scope,
                player_input[:160],
                gs.turn,
            )

        # per-witness tier-exposure meter. Map the scope to a
        # tier and bump every in-scene NPC's counter. Applies to both
        # intents - even the lattice route leaves witnesses; the
        # internal route only skips the entropy character's attention,
        # not the NPCs' eyes. The render block carries the strict
        # 'never name nature' rule alongside the counts so the DM
        # has clear guard rails.
        if phys_scope in phys_mod.SCOPE_TO_TIER:
            tier_n = phys_mod.SCOPE_TO_TIER[phys_scope]
            for nid in phys_mod.witnesses_for_exposure(gs):
                phys_mod.record_tier_exposure(gs.state, nid, tier_n, gs.turn)
        # Breach log is independent of intent - every location+ scale
        # attempt is recorded in the save (last 50 entries).
        if phys_scope in ("location", "world", "planet"):
            breach_log = list(gs.state.get("scope_breach_log") or [])
            breach_log.append(
                {
                    "turn": gs.turn,
                    "scope": phys_scope,
                    "intent": phys_intent or "n/a",
                    "summary": player_input[:120],
                }
            )
            gs.state.scope_breach_log = breach_log[-50:]

        # physics grounding. Only runs when scope is non-trivial
        # and intent is confirmed (no point grounding a mundane action or one
        # that raised AbilityIntentAmbiguous). Best-effort: a lookup/LLM
        # failure falls through to phys_grounding=None and the render block
        # just omits the physics section rather than failing the turn.
        # effective_tier is available here; tier does not change the
        # depth of physicalisation.
        if phys_scope not in ("none", "trivial") and phys_intent is not None:
            try:
                from app.game import physics as physics_mod

                phys_grounding = physics_mod.physicalise(
                    player_input,
                    allow_strong=lambda: strong_gate.need("physics"),
                )
                # Add an aux row with this call's token counts.
                if phys_grounding:
                    aux_llm_rows.append(
                        {
                            "id": "aux:physics",
                            "model": phys_grounding.get("model_used", MODEL_HAIKU),
                            "tokens_in": phys_grounding.get("tokens_in", 0),
                            "tokens_out": phys_grounding.get("tokens_out", 0),
                            "cache_read": phys_grounding.get("cache_read", 0),
                            "cache_creation": phys_grounding.get("cache_creation", 0),
                        }
                    )
            except TurnGate:
                raise
            except Exception as _pe:
                import logging as _lg

                _lg.getLogger(__name__).warning("physics grounding failed: %s", _pe)

        # effective_tier does not change the depth of the render block.
        # Compose the prompt block for the DM beat
        phys_pack = lore_pack.load_pack(gs.config.pack_id or lore_pack.DEFAULT_PACK_ID)
        pack_tmpl = lore_pack.get_template(phys_pack, "physicalisation") or ""
        phys_block = phys_mod.render_physicalisation_block(
            gs.state,
            phys_scope,
            pack_template=pack_tmpl,
            ability_intent=phys_intent,
            grounding=phys_grounding,
            pack=phys_pack,
        )
    except TurnGate:
        # Surface to API handler - do not swallow into the generic
        # warning path below or the gate vanishes silently.
        raise
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("physicalisation pass failed: %s", _e)

    # world-systems pass. Parses the player input
    # for a world-policy declaration (extends self_restrictions semantics)
    # and composes the world-systems prompt block surfacing quests,
    # threats, conditions, anomalies, day_segment, etc. Best-effort; the
    # turn always proceeds even if the pass dies.
    world_systems_block = ""
    try:
        from app.game import world_systems as ws_mod
        from app.game import social_systems as soc_mod
        from app.game import world_rule_gate as wrg_mod

        wr = ws_mod.parse_world_rule(player_input)
        world_reacts_extra = ""
        if wr and not regenerated:
            if world_rule_confirm not in ("confirm", "cancel"):
                # classify before writing. Raises WorldRuleShiftPending
                # when the haiku confirms this is a genuine paradigm shift.
                shift = wrg_mod.classify_rule_shift(player_input)
                if shift:
                    raise wrg_mod.WorldRuleShiftPending(
                        rule_text=shift["rule_text"],
                        scope=shift["scope"],
                        projected_effects=shift["projected_effects"],
                    )
                # Haiku said not a paradigm shift - silent write is fine.
                rules = list(gs.state.get("world_rules") or [])
                rules.append({**wr, "set_at_turn": gs.turn})
                gs.state.world_rules = rules[-12:]
            elif world_rule_confirm == "confirm":
                # Player confirmed - write + inject world-reacts beat.
                rules = list(gs.state.get("world_rules") or [])
                rules.append({**wr, "set_at_turn": gs.turn})
                gs.state.world_rules = rules[-12:]
                world_reacts_extra = wrg_mod.world_reacts_beat(
                    wr.get("rule") or wr.get("snippet") or player_input[:80],
                    wr.get("scope") or "world-policy",
                )
            # world_rule_confirm == "cancel": turn proceeds without mutation
        elif wr and regenerated:
            # Regen path - always silent-write, no gate.
            rules = list(gs.state.get("world_rules") or [])
            rules.append({**wr, "set_at_turn": gs.turn})
            gs.state.world_rules = rules[-12:]
        # Compose world + social blocks into one bundle for the DM beat
        ws_block = ws_mod.render_world_systems_block(gs.state)
        soc_block = soc_mod.render_social_systems_block(gs.state)
        world_systems_block = "\n\n".join(b for b in (ws_block, soc_block, world_reacts_extra) if b)
    except wrg_mod.WorldRuleShiftPending:
        raise
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("world_systems pass failed: %s", _e)

    if addressee is not None:
        addr_result = resolve_from_pick(addressee)
    elif regenerated:
        addr_result = type("R", (), {"kind": AddresseeKind.NONE})()  # cheap stub
    else:
        addr_result = classify_addressee(player_input, gs)
        if addr_result.kind == AddresseeKind.AMBIGUOUS:
            # Await-reply short-circuit: if exactly one NPC is awaiting
            # a reply and the player's input is dialogue, auto-resolve to that
            # NPC instead of raising the clarification modal. The player is most
            # likely responding to the pending question.
            _sc_aw = gs.state.get("awaiting_reply")
            if _sc_aw and looks_like_dialogue(player_input):
                _sc_npc = (_sc_aw.get("npc_id") or "").strip()
                if _sc_npc:
                    addr_result = AddresseeResult(
                        kind=AddresseeKind.NAMED,
                        name=_sc_npc,
                    )
                else:
                    from app.game.addressee import _crowd_in_scene

                    raise AddresseeAmbiguous(
                        options=addr_result.names,
                        crowd_present=_crowd_in_scene(gs),
                    )
            else:
                from app.game.addressee import _crowd_in_scene

                raise AddresseeAmbiguous(
                    options=addr_result.names,
                    crowd_present=_crowd_in_scene(gs),
                )

    # Extra token tallies - added to gs.session_in/out at the very
    # end so they don't get clobbered by the ctx assignment. Always zero
    # here; there is no translation step.
    tr_in_total = 0
    tr_out_total = 0

    eff_glossary = dict(gs.glossary or {})
    player_input_locale = player_input

    ctx = ContextEngine(gs.session_in, gs.session_out, tier=gs.config.tier)

    # Classify first so the escalation gate can short-circuit before
    # we incur compression cost. classify is a tiny haiku call (<60 tok).
    turn_class, classifier_usage = classify_turn(player_input, gs.config)
    ctx.record(classifier_usage)
    aux_llm_rows.append(
        {
            "id": "aux:classify_turn",
            "model": MODEL_HAIKU,
            "tokens_in": getattr(classifier_usage, "input_tokens", 0) or 0,
            "tokens_out": getattr(classifier_usage, "output_tokens", 0) or 0,
            "cache_read": getattr(classifier_usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation": getattr(classifier_usage, "cache_creation_input_tokens", 0) or 0,
        }
    )

    # stakes classifier - separate from classify_turn so the
    # ability router's prompt stays cache-stable. Fail-safe: any exception
    # inside classify_stakes returns "normal" (today's behaviour). The
    # stakes tag is threaded to TurnState and drives length profiles.
    stakes_label, stakes_usage = classify_stakes(player_input, gs.config)
    ctx.record(stakes_usage)
    aux_llm_rows.append(
        {
            "id": "aux:classify_stakes",
            "model": MODEL_HAIKU,
            "tokens_in": getattr(stakes_usage, "input_tokens", 0) or 0,
            "tokens_out": getattr(stakes_usage, "output_tokens", 0) or 0,
            "cache_read": getattr(stakes_usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation": getattr(stakes_usage, "cache_creation_input_tokens", 0) or 0,
        }
    )

    turn_model = MODEL_HAIKU

    # Pre-flight done; safe to incur compression cost now.
    # Under a context ceiling the stored history is never compressed.
    ceiling = context_ceiling()
    if ceiling is None:
        compressed = ctx.maybe_compress(gs.history)
        if compressed is not gs.history:
            _materialise(gs)
            gs.history = compressed

    pack = lore_pack.load_pack(gs.config.pack_id)
    pack_schema = lore_pack.state_field_defs(pack)
    # `items` is an engine-level delta key (handled by app.game.items),
    # not a WorldState field - but the parser's valid-key filter would
    # otherwise strip it. Allow it through and let items.apply_ops
    # process it post-parse.
    # same treatment for `money_delta` - additive ops on the
    # money dict (e.g. {gold: -3, silver: +1}) instead of replacing
    # the whole field, with clamp-at-zero in apply.
    valid_keys = {f["name"] for f in pack_schema} | {"items", "money_delta", "dm_notes_append"}
    stage = gs.state.get("tutorial_stage", 0)

    # run the anti-automation detector; its level and flag are used as
    # returned (_aa_disabled stays False).
    # All modal-passbacks are threaded into assess as hard-exemption signals.
    from app.game.anti_automation import assess as _aa_assess

    _aa = _aa_assess(
        gs,
        player_input,
        ua,
        consent_token=consent_token,
        dice_roll=dice_roll,
        craft_roll=craft_roll,
        addressee=addressee,
        reframe_confirm=reframe_confirm,
        world_rule_confirm=world_rule_confirm,
        suggestion_index=suggestion_index,
    )
    _aa_disabled = False
    eff_monster_level = 0 if _aa_disabled else _aa.level
    flagged = False if _aa_disabled else _aa.flagged
    # Gate the full NPC knowledge/insight block: only inject when an NPC is
    # actually in play (state has dispositions, or the player input shows
    # dialogue intent). Saves ~330 tokens on exploration turns.
    has_npcs = _detect_npcs_in_play(gs, player_input)

    # Long-term memory: recall topically-relevant past chunks, exclude the
    # rolling raw window (we already include those messages below).
    recalled = []
    recall_query = ""
    window_s0 = 0
    if ceiling is not None:
        recall_query = f"{player_input}\nlocation: {gs.state.location}"
        window_s0 = window_start(_history_for_llm(gs.history), ceiling)
        recalled, memory_block = recall_block(gs.history, gs.turn, save_id, recall_query, window_s0)
    else:
        if gs.turn > 4:
            recall_query = f"{player_input}\nlocation: {gs.state.location}"
            eligible_below = max(0, gs.turn - MAX_HISTORY // 2 + 1)
            recalled = recall(
                save_id,
                recall_query,
                top_k=4,
                exclude_after_turn=eligible_below,
            )
        memory_block = format_recall_block(recalled)
    insight_dice = [random.randint(1, 20) for _ in range(8)]

    # Inventory: pull items at the player's current location + items the
    # player carries. Inject as a context block so the DM knows what's
    # touchable in the scene. Empty string when there are no items
    # (saves prompt tokens on early turns).
    from app.game import items as items_mod

    item_view = items_mod.visible_to_player(save_id, gs.state.location)
    items_block = items_mod.format_block(
        item_view["scene"],
        item_view["carried"],
        item_view.get("npcs"),
    )

    # pack state-bindings - threshold->consequence rules evaluated
    # once per turn against the as-loaded state, before the craft/dice
    # gates so dc_modifier effects land on this turn's assessments.
    # fire_beat effects force the named pack beat into the pipeline.
    # Packs with no bindings (coherence today) get an empty BindingEffects.
    from app.game import pack_beats as pack_beats_mod

    _binding_fx = pack_beats_mod.evaluate_bindings(
        pack,
        state=gs.state.to_dict(),
        turn=gs.turn,
        turn_class=turn_class,
        player_input=player_input,
    )

    def _mod_craft_dc(a):
        if a is not None and _binding_fx.craft_dc_mod:
            from app.game.craft_gate import _difficulty_label as _craft_label

            a.dc = pack_beats_mod.apply_dc_mod(a.dc, _binding_fx.craft_dc_mod)
            a.difficulty_label = _craft_label(a.dc)
        return a

    def _mod_dice_dc(a):
        if a is not None and _binding_fx.dice_dc_mod:
            from app.game.dice_gate import _difficulty_label as _dice_label

            a.dc = pack_beats_mod.apply_dc_mod(a.dc, _binding_fx.dice_dc_mod)
            a.difficulty_label = _dice_label(a.dc)
        return a

    # craft gate - detect craft intent + run DC assessment if needed.
    # If craft_roll is None and we detect a craft attempt, raise CraftRollPending
    # so the frontend pops the dice modal. If craft_roll is already provided
    # (second POST after modal), resolve the outcome and override turn_class.
    _craft_context = None
    from app.game.craft_gate import (
        detect_craft,
        assess_craft_dc,
        CraftRollPending,
    )

    if craft_roll is not None:
        # Player submitted the roll - resolve. We still need the DC context:
        # assess it fresh (idempotent: same input + same items -> same DC).
        _craft_assessment = _mod_craft_dc(
            assess_craft_dc(player_input, item_view, gs.state, aux_llm_rows)
        )
        _craft_context = _craft_assessment
        turn_class = "craft"
    elif not regenerated and detect_craft(player_input, item_view):
        _craft_assessment = _mod_craft_dc(
            assess_craft_dc(player_input, item_view, gs.state, aux_llm_rows)
        )
        raise CraftRollPending(_craft_assessment)

    # dice gate - detect confrontational/risky actions and require a roll.
    # Path 1 (pre-turn): confrontational verb -> haiku DC assessment -> DiceRollPending.
    # Path 2 (post-DM): DM embeds [ROLL:skill:dc] -> gate fires after pipeline runs.
    _dice_context = None
    from app.game.dice_gate import (
        detect_dice_request,
        assess_dice_dc,
        DiceRollPending,
    )

    if dice_roll is not None:
        _dice_assessment = _mod_dice_dc(assess_dice_dc(player_input, gs.state, aux_llm_rows))
        _dice_context = _dice_assessment
        turn_class = "encounter"
    elif (
        not regenerated
        and not _craft_context
        and not _reality_bending
        and not _dice_suppressed
        and detect_dice_request(player_input, gs.state)
    ):
        _dice_assessment = _mod_dice_dc(assess_dice_dc(player_input, gs.state, aux_llm_rows))
        raise DiceRollPending(_dice_assessment)

    from app.game.beats.builtin import consult_fits

    # A consented ability move gets a rules consult; it reads the final turn_class.
    consult = (
        turn_class == "ability" and consult_fits(pack, player_input) and strong_gate.need("ability")
    )

    # deterministic departure detector - fires only when has_npcs and
    # a departure/exit cue is present in the player input. Sharpens the DM
    # directive for this turn by appending [player leaving presence] to
    # world_systems_block. Fail-open: any error is logged and skipped.
    _departure_result = None
    try:
        from app.game.presence_gate import detect_departure as _detect_departure

        # Source npcs_in_scene: prefer addressee-resolved names; fall back to
        # all names in npc_dispositions (mirrors _detect_npcs_in_play priority).
        _pg_addr_kind = getattr(addr_result, "kind", None)
        if _pg_addr_kind in (AddresseeKind.NAMED, AddresseeKind.UNIQUE):
            _pg_npcs = [addr_result.name] if getattr(addr_result, "name", None) else []
        elif _pg_addr_kind == AddresseeKind.NAMED_MULTI:
            _pg_npcs = list(getattr(addr_result, "names", None) or [])
        else:
            # Fallback: all established NPC names from durable dispositions.
            _pg_npcs = []
            _pg_disp = getattr(gs.state, "npc_dispositions", None) or {}
            for _pg_k, _pg_v in _pg_disp.items():
                if isinstance(_pg_v, dict) and _pg_v.get("name"):
                    _pg_npcs.append(str(_pg_v["name"]))
                elif isinstance(_pg_k, str) and _pg_k and ":" not in _pg_k:
                    _pg_npcs.append(_pg_k)
        _departure_result = _detect_departure(player_input, has_npcs, _pg_npcs)
        if _departure_result is not None:
            _pg_npc_names = (
                ", ".join(_departure_result.npcs_in_scene)
                if _departure_result.npcs_in_scene
                else "the NPC"
            )
            _pg_cue = _departure_result.cues[0] if _departure_result.cues else "departure"
            _pg_departure_block = (
                f"[PLAYER LEAVING PRESENCE - THIS TURN]\n"
                f"The player is moving out of {_pg_npc_names}'s immediate space this turn "
                f'(cue: "{_pg_cue}"). {_pg_npc_names} must resolve NOW to exactly ONE of:\n'
                f"  - GIVE UP: fall silent, let them go (brief parting line only if still in "
                f"earshot);\n"
                f"  - CAVE FAST: concede the one key thing in a sentence or two while the "
                f"player can still hear, BEFORE they are out of range;\n"
                f"  - TRY TO STOP: move to block, shout after, or follow (an action, not a "
                f"speech).\n"
                f"No monologue chases the player. Once the player is gone, no further NPC "
                f"dialogue reaches them - narrate only the environment and their exit."
            )
            world_systems_block = "\n\n".join(
                b for b in (world_systems_block, _pg_departure_block) if b
            )
    except Exception as _pg_e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "presence_gate departure block failed: %s",
            _pg_e,
        )

    # Snapshot for undo before the pipeline mutates state. The tier5_reset
    # beat-internal logic reads gs.undo_stack[-1] to find the pre-act state,
    # so the snapshot must be in place before pipeline.run.
    gs.undo_stack.append(_snapshot(gs))
    if len(gs.undo_stack) > UNDO_CAP:
        gs.undo_stack = gs.undo_stack[-UNDO_CAP:]

    # Graded NPC reply-wait prompt block. If an NPC is awaiting a
    # reply (state.awaiting_reply set) and the player is not addressing them
    # this turn, inject a context block for the DM describing the situation and
    # the graded reaction to use. Fires only on turns that reach the pipeline;
    # gated turns (DiceRollPending etc.) raise before here -> reaction never
    # fires on a dropped turn (structurally guaranteed).
    try:
        _pre_aw = gs.state.get("awaiting_reply")
        if _pre_aw:
            _aw_npc_id = (_pre_aw.get("npc_id") or "").strip().lower()
            _player_answering_aw = False
            _aw_addr_kind = getattr(addr_result, "kind", None)
            if _aw_addr_kind in (AddresseeKind.NAMED, AddresseeKind.UNIQUE):
                _aw_check_name = (getattr(addr_result, "name", "") or "").lower()
                if _aw_check_name and _aw_check_name == _aw_npc_id:
                    _player_answering_aw = True
            elif _aw_addr_kind == AddresseeKind.NAMED_MULTI:
                for _aw_cn in getattr(addr_result, "names", None) or []:
                    if (_aw_cn or "").lower() == _aw_npc_id:
                        _player_answering_aw = True
                        break

            if not _player_answering_aw:
                _aw_attempts = int(_pre_aw.get("attempts") or 0)
                _aw_question = _pre_aw.get("question") or ""
                _aw_npc_display = _pre_aw.get("npc_id") or ""
                _aw_since = _pre_aw.get("since_turn", "?")
                from app.game.world_systems import render_player_conditions_block

                _aw_cond_block = render_player_conditions_block(gs.state)
                _aw_cond_ctx = _aw_cond_block if _aw_cond_block else "no active conditions"
                # Incapacitation gate: injury/status conditions that indicate
                # the player cannot respond deliberately (fainted, unconscious,
                # stunned, collapsed). NPC reacts with concern, never offense.
                _aw_incapacitated = False
                for _aw_c in list(gs.state.get("player_conditions") or []):
                    if _aw_c.get("kind") in ("injury", "status") and any(
                        kw in (_aw_c.get("summary") or "").lower()
                        for kw in (
                            "faint",
                            "unconscious",
                            "incapacitat",
                            " ko",
                            "k.o.",
                            "stun",
                            "collapse",
                            "knocked out",
                        )
                    ):
                        _aw_incapacitated = True
                        break
                if _aw_attempts == 0:
                    _aw_guidance = (
                        f"Attempt 1 (charitable): {_aw_npc_display} assumes the player "
                        f"did not hear them - have them repeat the question a bit louder "
                        f"or more insistently. NO negative disposition change this attempt."
                    )
                elif _aw_incapacitated:
                    _aw_guidance = (
                        f"Attempt {_aw_attempts + 1} (player incapacitated): "
                        f"the player appears unconscious/incapacitated - "
                        f"{_aw_npc_display} reacts with CONCERN or ALARM, "
                        f"never offense or sulking."
                    )
                else:
                    _aw_guidance = (
                        f"Attempt {_aw_attempts + 1} (deliberate ignore): "
                        f"{_aw_npc_display} interprets the silence per their character "
                        f"and the situation - confused, hurt, or offended, "
                        f"severity-scaled to their persona; they may then move on."
                    )
                _npc_await_block = (
                    f"[NPC AWAITING REPLY - GRADED REACTION]\n"
                    f'{_aw_npc_display} asked: "{_aw_question}" '
                    f"(unanswered, {_aw_attempts} prior attempt(s), since turn {_aw_since}).\n"
                    f"Player condition context: {_aw_cond_ctx}\n"
                    f"{_aw_guidance}\n"
                    f"Disposition changes only via the normal post-turn update on a "
                    f"completed, delivered turn.]"
                )
                world_systems_block = "\n\n".join(
                    b for b in (world_systems_block, _npc_await_block) if b
                )
    except Exception as _aw_blk_e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "npc_await_block build failed: %s",
            _aw_blk_e,
        )

    # Beat pipeline
    # Standard turns: single DMWorldStepBeat (preserves monolith).
    # Ability turns: DMActBeat -> GroundingBeat -> NpcReactBeat (cond.).
    # A pack.json override per turn-class is not implemented yet.
    from app.game.beats import TurnState, build_pipeline_for

    # Custom worlds carry a fatter system prompt (premise + scene + tone +
    # custom opening) -> bump the response max_tokens so haiku has room to
    # match the world's vibe without escalating to the strong model ("haiku-large").
    is_custom_world = (gs.config.world_type or "canon") == "custom"
    custom_max_tokens = 2500 if is_custom_world else 0
    ts = TurnState(
        gs=gs,
        save_id=save_id,
        player_input=player_input,
        pre_state=pre_state_dict,
        pack=pack,
        pack_schema=pack_schema,
        valid_keys=valid_keys,
        turn_class=turn_class,
        turn_model=turn_model,
        consult=consult,
        monster_level=eff_monster_level,
        has_npcs=has_npcs,
        stage=stage,
        insight_dice=insight_dice,
        memory_block=memory_block,
        recalled=recalled,
        recall_query=recall_query,
        window_start=window_s0,
        ctx=ctx,
        items_block=items_block,
        # max_tokens_override: only raised for custom-world "haiku-large"
        # turns. Stakes-driven mundane cap flows via ts.stakes -> profile:
        # the canonical resolver in beats/base.py reads
        # ts.length_profile (set by the beat from ts.stakes), not this field.
        max_tokens_override=custom_max_tokens,
        # stakes tag from classify_stakes. Drives
        # length profile selection in DMWorldStepBeat.
        stakes=stakes_label,
        addressee_block=addressee_block(addr_result)
        + _npc_block_for_turn(
            gs,
            addr_result,
            player_input,
        ),
        physicalisation_block=phys_block,
        world_systems_block=world_systems_block,
        craft_roll=craft_roll,
        craft_context=_craft_context,
        dice_roll=dice_roll,
        dice_context=_dice_context,
    )
    # record departure signal on ts for post-turn telemetry. Transient
    # only - never written to durable WorldState.
    if _departure_result is not None:
        ts.signals["player_departing"] = {
            "cues": _departure_result.cues,
            "npcs_in_scene": _departure_result.npcs_in_scene,
        }
    #  thread stakes_label so build_pipeline_for can
    # enforce N=1 terse on mundane turns and select the correct profile
    # ceiling on normal/dramatic standard turns.
    pipeline = build_pipeline_for(
        turn_class,
        pack=pack,
        forced_beat_ids=_binding_fx.forced_beats,
        stakes=stakes_label,
        consult=ts.consult,
    )
    ts = pipeline.run(ts)

    # post-DM path: DM may embed [ROLL:skill:dc] in its narrative
    # when a roll feels more natural after the scene setup. Strip the
    # marker and raise DiceRollPending - run_turn restores the saved fields
    # on a gate, so save state stays clean.
    if _dice_context is None and not _dice_suppressed:
        from app.game.dice_gate import detect_dice_in_narrative

        # Pack dc_modifier applies to DM-embedded [ROLL:skill:dc] too -
        # the binding's "while condition holds" semantics don't care
        # which path requested the roll.
        _post_dm_dice = _mod_dice_dc(detect_dice_in_narrative(ts.narrative))
        if _post_dm_dice is not None:
            raise DiceRollPending(_post_dm_dice)
    # A reply whose ending check_hook counts as a miss (see hook_repair) gets one small cached call that
    # writes only the hook beat; a blocked hook line, or missing word lists, refuse the move like any blocked
    # reply; any other failure leaves the reply as written.
    from app.game import hook_repair

    hook_repair.repair_hook(ts, player_input)

    # Apply DM-emitted item operations before we read state mutations
    # back. The model includes an `items` array in state_delta with op
    # objects (add / pickup / drop / remove / describe). items.apply_ops
    # handles them and logs unmatched names.
    item_ops = (ts.delta_raw or {}).get("items") or []
    if item_ops:
        ts.items_ops_log = items_mod.apply_ops(
            save_id,
            item_ops,
            current_location=gs.state.location,
            turn=gs.turn,
        )

    # backfill: if this save has no seed_roster yet, populate stable
    # identity from pack canon.
    # Identity-only (no state.npcs seeding for mid-campaign
    # saves - avoids retconning never-met NPCs into the live roster UI).
    # Best-effort; any error is swallowed so the turn always proceeds.
    if not gs.seed_roster:
        try:
            _bf_pack = lore_pack.load_pack(gs.config.pack_id or lore_pack.DEFAULT_PACK_ID)
            _bf_roster = lore_pack.get_seed_roster(_bf_pack)
            if _bf_roster and _bf_roster.get("npcs"):
                _bf_identity_npcs: list[dict] = []
                for _bf_npc in _bf_roster.get("npcs") or []:
                    _npc_id = str(_bf_npc.get("npc_id") or "").strip()
                    if not _npc_id:
                        continue
                    _bf_identity_npcs.append(
                        {
                            "npc_id": _npc_id,
                            "name": str(_bf_npc.get("name") or ""),
                            "archetype": str(_bf_npc.get("archetype") or ""),
                            "appearance": str(_bf_npc.get("appearance") or ""),
                            "backstory": str(_bf_npc.get("backstory") or ""),
                            "origin_alignment": str(_bf_npc.get("origin_alignment") or ""),
                            "core_personality": str(_bf_npc.get("core_personality") or ""),
                            "starting_inventory": list(_bf_npc.get("starting_inventory") or []),
                        }
                    )
                gs.seed_roster = {
                    "player_kit": _bf_roster.get("player_kit") or {},
                    "npcs": _bf_identity_npcs,
                }
        except Exception as _bf_e:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "run_turn: legacy seed_roster backfill failed: %s",
                _bf_e,
            )

    # ingest narration to grow the NPC store. Pure-deterministic
    # introduction extraction + witnessed-event append. No LLM call.
    try:
        npc_mod.ingest_narration(
            gs.state,
            ts.narrative or "",
            current_location=gs.state.location,
            turn=gs.turn,
        )
    except Exception as _e:
        # Never let NPC ingestion block a turn.
        import logging as _logging

        _logging.getLogger(__name__).warning("npc ingest failed: %s", _e)

    # event-gated re-bake: scan runtime NPCs for new canon candidates.
    # Called after ingest_narration so newly-introduced NPCs' witness counts
    # are current. Best-effort; any error is swallowed.
    try:
        maybe_rebake_roster(gs)
    except Exception as _rb_e:
        import logging as _logging

        _logging.getLogger(__name__).warning("maybe_rebake_roster failed: %s", _rb_e)

    # post-turn disposition update for addressed NPCs.
    try:
        _addr_names: list[str] = []
        _addr_k = getattr(addr_result, "kind", None)
        if _addr_k == AddresseeKind.NAMED:
            _addr_names = [addr_result.name] if addr_result.name else []
        elif _addr_k == AddresseeKind.NAMED_MULTI:
            _addr_names = list(addr_result.names or [])
        elif _addr_k == AddresseeKind.UNIQUE:
            _addr_names = [addr_result.name] if addr_result.name else []
        if _addr_names:
            npc_mod.update_dispositions_post_turn(
                gs.state,
                player_input=player_input,
                narrative=ts.narrative or "",
                addressed_names=_addr_names,
                turn=gs.turn,
            )
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("disposition update failed: %s", _e)

    # Post-turn awaiting_reply state management.
    # Three sub-steps, each best-effort (failures never block a turn):
    #   A. Clear if the player addressed the awaiting NPC this turn.
    #   B. Detect a new NPC question in the rendered narrative -> set/update
    #      awaiting_reply. Same-NPC re-prompt preserves attempts so the
    #      graded cycle doesn't reset to attempt-1 when the NPC repeats.
    #   C. Increment attempts if awaiting was set pre-turn and not cleared.
    # Placement after DiceRollPending (post-DM) and pipeline ensures this
    # never fires on an engine-dropped / gated turn.
    try:
        _pt_aw_pre = gs.state.get("awaiting_reply")
        _pt_answered = False

        # A. Clear if player addressed the awaiting NPC
        if _pt_aw_pre and _addr_names:
            _pt_aw_npc = (_pt_aw_pre.get("npc_id") or "").lower()
            for _pt_an in _addr_names:
                if (_pt_an or "").lower() == _pt_aw_npc:
                    gs.state.awaiting_reply = None
                    _pt_answered = True
                    break

        # B. Detect NPC question in narrative -> set or update awaiting_reply
        _pt_npc_q = _detect_npc_question(ts.narrative or "", gs.state)
        if _pt_npc_q:
            _pt_q_npc, _pt_q_text = _pt_npc_q
            _pt_existing = gs.state.get("awaiting_reply")
            if _pt_existing and (_pt_existing.get("npc_id") or "").lower() == _pt_q_npc.lower():
                # Same NPC re-prompting - preserve attempts, just update question text
                _pt_existing["question"] = _pt_q_text
                gs.state.awaiting_reply = _pt_existing
            else:
                # New NPC (or no existing awaiting_reply)
                gs.state.awaiting_reply = {
                    "npc_id": _pt_q_npc,
                    "question": _pt_q_text,
                    "attempts": 0,
                    "since_turn": gs.turn,
                }

        # C. Increment attempts if not answered and was set pre-turn
        if not _pt_answered and _pt_aw_pre:
            _pt_cur = gs.state.get("awaiting_reply")
            if (
                _pt_cur
                and (_pt_cur.get("npc_id") or "").lower()
                == (_pt_aw_pre.get("npc_id") or "").lower()
            ):
                _pt_cur["attempts"] = int(_pt_cur.get("attempts") or 0) + 1
                gs.state.awaiting_reply = _pt_cur
    except Exception as _pt_aw_e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "awaiting_reply post-turn update failed: %s",
            _pt_aw_e,
        )

    # a completed turn consumes any pending gate draft.
    try:
        gs.state.pending_input = None
    except Exception:
        pass

    # DM-private notes append - DM can emit dm_notes_append: [str]
    # in state_delta to accumulate hidden-canon facts across turns.
    try:
        _dm_notes_new = (ts.delta_raw or {}).get("dm_notes_append")
        if _dm_notes_new and isinstance(_dm_notes_new, list):
            _current_notes = list(gs.state.get("dm_notes") or [])
            if isinstance(_current_notes, str):
                _current_notes = [_current_notes] if _current_notes.strip() else []
            _current_notes.extend(str(n)[:200] for n in _dm_notes_new if n and str(n).strip())
            gs.state.dm_notes = _current_notes[-20:]
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("dm_notes_append failed: %s", _e)

    # apply DM-emitted money deltas. Additive over the current
    # money dict, clamped at zero. Unknown denominations survive in
    # state (so a save can cross packs without data loss) but are
    # ignored at display time by the active pack's spec.
    money_delta = (ts.delta_raw or {}).get("money_delta") or {}
    if isinstance(money_delta, dict) and money_delta:
        current = dict(gs.state.get("money") or {})
        for denom, change in money_delta.items():
            try:
                change_i = int(change)
            except (ValueError, TypeError):
                continue
            new_v = int(current.get(denom, 0)) + change_i
            if new_v <= 0:
                # Clamp at zero; remove the key entirely when balance hits 0
                # so a fresh save's empty dict is the canonical "no money"
                # representation, not {gold:0, silver:0, ...}.
                current.pop(denom, None)
            else:
                current[denom] = new_v
        gs.state.money = current

    # belt-and-suspenders clamp. The LLM can also write `money`
    # via a wholesale state-delta field replacement (state.money={...})
    # which bypasses the money_delta path above. Without this, a player
    # narrating "I give 5 silver" they don't have results in state.money
    # = {'silver': -5} - an infinite-money exploit (gift -5 to NPC = gain 5).
    # Walk the final money dict and drop any non-positive entries.
    cur_money = gs.state.get("money") or {}
    if isinstance(cur_money, dict) and cur_money:
        cleaned = {
            k: int(v) for k, v in cur_money.items() if isinstance(v, (int, float)) and int(v) > 0
        }
        if cleaned != cur_money:
            gs.state.money = cleaned

    # campaign clock - apply DM-emitted clock_advance, then run the
    # pico-haiku event-duration estimator. Both are best-effort: failures
    # log a warning and the turn proceeds unaffected.
    try:
        from app.game import campaign_clock as cc_mod

        clock_before = dict(gs.state.get("clock") or {})
        cc_mod.apply_clock_advance(gs.state, ts.signals.get("clock_advance"))
        _clock_usage = cc_mod.estimate_event_duration(
            ts.narrative or "",
            clock_before=clock_before,
            turn=gs.turn,
            state=gs.state,
            turn_class=turn_class,
        )
        if _clock_usage is not None:
            aux_llm_rows.append(
                {
                    "id": "aux:clock",
                    "model": MODEL_HAIKU,
                    "tokens_in": getattr(_clock_usage, "input_tokens", 0) or 0,
                    "tokens_out": getattr(_clock_usage, "output_tokens", 0) or 0,
                    "cache_read": getattr(_clock_usage, "cache_read_input_tokens", 0) or 0,
                    "cache_creation": getattr(_clock_usage, "cache_creation_input_tokens", 0) or 0,
                }
            )
    except Exception as _cc_e:
        import logging as _logging

        _logging.getLogger(__name__).warning("campaign clock pass failed: %s", _cc_e)

    # Read pipeline outputs back into local names so the rest of run_turn
    # (history append + response assembly) reads naturally.
    class _Result:
        pass

    result = _Result()
    result.narrative = ts.narrative
    # The pipeline already applied state mutations to gs; we keep the
    # raw delta dict so telemetry + the response payload show what the
    # model emitted (not just the {before,after} diff).
    result.delta = ts.delta_raw
    result.signals = ts.signals
    result.found_narrative = ts.parse_ok.get("narrative", True)
    result.found_delta = ts.parse_ok.get("delta", True)
    result.delta_valid_json = ts.parse_ok.get("delta_json", True)
    diff = ts.diff
    health_at_send = ts.health_at_send
    turn_tokens_in = ts.turn_tokens_in
    turn_tokens_out = ts.turn_tokens_out

    # deterministic verbatim-quote enforcement
    # Done here so the narrative written to history is the post-processed one.
    from app.game.quoted_speech import ensure_verbatim

    fixed_narr, repaired = ensure_verbatim(player_input, result.narrative)
    if repaired:
        result.narrative = fixed_narr
        ts.quoted_speech_repaired = repaired

    narrative_locale = result.narrative

    # store original player text (pre-meme rewrite) in history so
    # the player sees what they typed. The LLM saw the rewritten form;
    # history is for the human record.
    user_entry = _new_history_entry(
        "user",
        player_input_for_history,
        locale=locale,
        content_locale=player_input_locale if locale != "en" else None,
    )
    assistant_entry = _new_history_entry(
        "assistant",
        result.narrative,
        locale=locale,
        content_locale=narrative_locale if locale != "en" else None,
    )

    # Welcome-grace refusal detector. Inside the first 5 turns,
    # check whether the DM "refused" player authorship (you don't see
    # X, nothing happens, you're still alone, you must be imagining
    # this, etc.). If so, attach an intervention payload to this
    # assistant entry - the frontend renders an inline DM-stepping-out
    # bubble in a different colour, asking the player whether they
    # really wanted the thing or were describing something else. The
    # narrative itself is preserved (no rollback) - the intervention
    # is an opt-in regen path, not a hard correction.
    # Skip on regenerations (the regen is the player's response to a
    # prior intervention; don't trigger a second one on top).
    if not regenerated:
        from app.game import refusal as refusal_mod

        # use pre-increment turn count for the gate (gs.turn is the
        # turn we just produced, not yet incremented)
        if refusal_mod.in_welcome_grace_window(gs.turn):
            check = refusal_mod.detect(result.narrative)
            if check.refused:
                assistant_entry["intervention"] = {
                    "kind": "welcome_grace_doubt",
                    "pattern_label": check.pattern_label,
                    "matched_text": check.matched_text,
                    # Persist the original (un-annotated) player input so
                    # the regen path can re-issue it with the welcome-
                    # grace nudge appended. original_input is set on
                    # regenerate calls; on a normal turn it's the plain
                    # player_input we received.
                    "original_input": original_input or player_input_for_history,
                    "accepted": False,
                }
                try:
                    telemetry.log_event("welcome_refusal_detected")
                except Exception:
                    pass

    gs.history.append(user_entry)
    gs.history.append(assistant_entry)
    gs.turn += 1

    gs.session_in = ctx.session_in + tr_in_total
    gs.session_out = ctx.session_out + tr_out_total

    # Index this turn's English chunks for future recall (English embeddings).
    # gate: skip when this is the first flagged turn on a mainline save
    # (gs.shadow_of is None = mainline; flagged = detector fired). The
    # api layer will re-index into the fork namespace via _index_fork_turn.
    # Subsequent turns on the fork (shadow_of set) index natively here.
    if not (flagged and gs.shadow_of is None):
        index_turn(save_id, gs.turn, "user", player_input)
        index_turn(save_id, gs.turn, "assistant", result.narrative)

    # bluff verification post-pass. If the player
    # made an unverifiable authority/relationship claim and the scene
    # resolved without a hostile NPC response, append the claim to
    # active_threats with verification_pending=True so future turns
    # can challenge it.
    # Best-effort.
    try:
        from app.game import social_systems as soc_mod

        # 'Scene resolved' heuristic: narrative is non-empty and no
        # explicit refusal kind. Crude but safe - false positives just
        # add a low-priority threat entry.
        if result.narrative and not getattr(result, "kind", "") == "refusal_with_redirect":
            soc_mod.maybe_record_bluff(gs.state, player_input, scene_resolved=True)
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning("bluff verification pass failed: %s", _e)

    # Shape-drift fix: the DM beat's state_delta emits
    # active_threats as plain strings, which replaces any structured
    # verification_pending bluffs via apply_delta (lists are wholesale-
    # replaced). reapply_verification_pending merges the pre-turn
    # structured bluffs back into the current list, picking up any
    # narrative status update the DM made via a matching string entry.
    # Idempotent + best-effort.
    try:
        from app.game import world_systems as _ws

        _ws.reapply_verification_pending(pre_state_dict, gs.state)
    except Exception as _e:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "verification_pending re-merge failed: %s",
            _e,
        )

    # single late flush - flush accumulated aux rows to TurnState.by_beat
    # immediately before payload assembly. This is the only flush site for aux
    # rows; a gate raises before it and its rows are dropped.
    for _aux_r in aux_llm_rows:
        ts.record_beat(
            beat_id=_aux_r["id"],
            tokens_in=_aux_r.get("tokens_in", 0),
            tokens_out=_aux_r.get("tokens_out", 0),
            model=_aux_r.get("model", ""),
            cache_read=_aux_r.get("cache_read", 0),
            cache_creation=_aux_r.get("cache_creation", 0),
        )

    payload = {
        "narrative": narrative_locale,
        "narrative_en": result.narrative,
        "delta": result.delta,
        "diff": diff,
        "signals": result.signals,
        "model": turn_model,
        "turn_class": turn_class,
        "turn": gs.turn,
        "stage": gs.state.get("tutorial_stage", 0),
        "tier": gs.config.tier,
        "monster": eff_monster_level,
        "monster_raw": ctx.monster_level(),  # always 0; the payload keeps the field
        "monster_off": False,
        # anti-automation: flagged drives the shadow-fork path in api/turn.py.
        "flagged": flagged,
        "auto_signals": _aa.signals,
        "health": health_at_send,
        "regenerated": regenerated,
        "turn_tokens": {"in": turn_tokens_in, "out": turn_tokens_out},
        # No prices are kept; the payload keeps turn_cost and session_cost at 0.
        "turn_cost": 0.0,
        "beats": ts.by_beat,
        "session_cost": 0.0,
        "recalled": [
            {"turn": c["turn"], "role": c["role"], "score": round(c["score"], 3)} for c in recalled
        ],
        # Inventory snapshot after ops applied - frontend renders this
        # without a second round-trip.
        "items": items_mod.visible_to_player(save_id, gs.state.location),
        "items_ops": ts.items_ops_log,
        "parse_ok": {
            "narrative": result.found_narrative,
            "delta": result.found_delta,
            "delta_json": result.delta_valid_json,
        },
        # Per-component approximate token sizes for the prompt package
        # the engine assembled this turn. Frontend renders as a % stack so
        # players (and the dev) can see what's eating the budget. Char/4
        # is a fast heuristic; precise tokenisation isn't worth the cost
        # to compute every turn.
        "package_breakdown": {
            "history": len(
                "\n".join((h.get("content_en") or h.get("content", "")) for h in gs.history)
            )
            // 4,
            "recall": len(memory_block) // 4 if memory_block else 0,
            "items": len(items_block) // 4 if items_block else 0,
            "glossary": sum(len(k) + len(v) for k, v in eff_glossary.items()) // 4,
            "player_in": len(player_input or "") // 4,
        },
        # physics grounding card. Absent on mundane turns, present
        # on confirmed ability turns where physics.physicalise ran.
        "physics_grounding": _extract_grounding_card(phys_grounding),
    }

    return payload


def _run_timeskip_turn(gs: GameSave, save_id: str, player_input: str, skip: dict) -> dict:
    """short-circuit path for time-skip turns.
    Calls timeskip.run_timeskip + composes a player-facing payload.
    Advances gs.turn + appends to history just like a normal turn so
    the save remains coherent for the frontend."""
    from app.game import timeskip as ts_mod

    t_start = time.time()

    # Snapshot for undo before mutating
    gs.undo_stack.append(_snapshot(gs))
    if len(gs.undo_stack) > UNDO_CAP:
        gs.undo_stack = gs.undo_stack[-UNDO_CAP:]

    result = ts_mod.run_timeskip(gs.state, skip=skip)
    narrative = ts_mod.compose_timeskip_response(skip, result)

    # Optimistically advance year/season based on completed_periods +
    # unit. Coarse - caller can adjust.
    completed = result["completed_periods"]
    unit = (skip.get("span_unit") or "year").lower()
    if unit.startswith("year"):
        cur_year = int(gs.state.get("year") or 1)
        gs.state.year = cur_year + completed
    elif unit.startswith("season"):
        # 4 seasons per year - rough
        cur_year = int(gs.state.get("year") or 1)
        # If season is a string ("Spring"/etc), leave it alone - only bump year.
        gs.state.year = cur_year + completed // 4

    # Record dm_note so the next standard turn knows what just happened
    notes = list(gs.state.get("dm_notes") or [])
    summary = f"TIME SKIP: completed {completed} of {skip.get('span_n', 1)} {unit}(s). " + (
        ("Interrupted: " + result["interrupt_reason"])
        if result["interrupted"]
        else "Completed cleanly."
    )
    notes.append(summary)
    gs.state.dm_notes = notes[-20:]

    # refresh suggested_moves from the timeskip narrative, mirroring
    # the main-path call at run_turn:1635. Best-effort - failure leaves [].
    try:
        from app.game import campaign_clock as cc_mod

        cc_mod.estimate_event_duration(
            narrative or "",
            clock_before=dict(gs.state.get("clock") or {}),
            turn=gs.turn,
            state=gs.state,
            turn_class="timeskip",
        )
    except Exception as _cc_e:
        import logging as _logging

        _logging.getLogger(__name__).warning("timeskip suggested_moves failed: %s", _cc_e)

    # History: append user input + skip narrative
    gs.history.append({"role": "user", "content": player_input, "ts": _now_iso()})
    gs.history.append({"role": "assistant", "content": narrative, "ts": _now_iso()})
    gs.turn += 1

    elapsed = time.time() - t_start
    return {
        "kind": "narrative",
        "narrative": narrative,
        "state": gs.state.to_dict(),
        "turn": gs.turn,
        "session": {"in": gs.session_in, "out": gs.session_out},
        "session_cost": 0.0,
        "turn_cost": 0.0,
        "turn_tokens": {"in": result["total_input_tokens"], "out": result["total_output_tokens"]},
        "turn_class": "timeskip",
        "delta": {},
        "regenerated": False,
        "model": "haiku (timeskip loop)",
        "package_breakdown": None,
        "items_ops": {"applied": [], "skipped": []},
        "monster": 0,
        "health": None,
        "tier": gs.config.tier,
        "recalled": [],
        "by_beat": [],
        "elapsed_s": round(elapsed, 2),
        "timeskip_meta": {
            "completed_periods": result["completed_periods"],
            "requested_periods": skip.get("span_n"),
            "interrupted": result["interrupted"],
            "interrupt_reason": result["interrupt_reason"],
            "truncated_by_cost": False,
            "events_lines": result["events_lines"],
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_grounding_card(grounding: "dict | None") -> "dict | None":
    """Flatten physics.physicalise output into a small frontend-safe
    dict. Returns None when grounding is absent or malformed."""
    if not grounding or not isinstance(grounding, dict):
        return None
    rec = grounding.get("record")
    if not rec or not isinstance(rec, dict):
        return None
    mech = rec.get("grounded_mechanism") or {}
    source = (mech.get("source") or "").strip()
    if not source:
        return None
    fms = rec.get("failure_modes") or []
    primary_fm: "dict | None" = fms[0] if fms and isinstance(fms[0], dict) else None
    return {
        "mechanism": source,
        "containment": (mech.get("containment") or "").strip() or None,
        "energy_cost": (rec.get("energy_demand") or "").strip() or None,
        "atmosphere": (rec.get("narrative_hint") or "").strip() or None,
        "primary_risk": (
            f"{primary_fm.get('trigger', '')} -> {primary_fm.get('consequence', '')}".strip(" ->")
        )
        if primary_fm
        else None,
    }


def _snapshot(gs: GameSave) -> dict:
    """Length snapshots refer to the current history's unchanged prefix.

    Materialise them before replacing history with a different sequence.
    """
    snap = {"state": gs.state.to_dict(), "history_len": len(gs.history), "turn": gs.turn}
    try:
        items = store.current_save().get("items")
    except store.StoreUnbound:
        items = None
    if isinstance(items, list):
        snap["items"] = copy.deepcopy(items)
    return snap


def _materialise(gs: GameSave) -> None:
    """Convert length references to full copies without mutating snapshot dicts."""
    stack = []
    for snap in gs.undo_stack:
        if (
            isinstance(snap, dict)
            and not isinstance(snap.get("history"), list)
            and "history_len" in snap
        ):
            materialised = dict(snap)
            materialised["history"] = gs.history[: snap["history_len"]]
            materialised.pop("history_len", None)
            stack.append(materialised)
        else:
            stack.append(snap)
    gs.undo_stack = stack
    if gs.checkpoint_len is not None:
        gs.checkpoint_history = gs.history[: gs.checkpoint_len]
        gs.checkpoint_len = None


def _restore(gs: GameSave, snap: dict) -> None:
    if isinstance(snap.get("history"), list):
        _materialise(gs)
        history = list(snap["history"])
    else:
        history = gs.history[: snap["history_len"]]
        if gs.checkpoint_len is not None and gs.checkpoint_len > len(history):
            gs.checkpoint_history = gs.history[: gs.checkpoint_len]
            gs.checkpoint_len = None
    gs.state = WorldState(**snap["state"])
    gs.history = history
    gs.turn = snap.get("turn", max(0, gs.turn - 1))
    _restore_save_keys(snap, gs.turn)


def _restore_save_keys(snap: dict, turn: int) -> None:
    """Items go back to the snapshot's copy when it has one (older snapshots
    do not); memory keeps only the entries of turns up to `turn`."""
    try:
        doc = store.current_save()
    except store.StoreUnbound:
        return
    items = snap.get("items")
    if isinstance(items, list) and isinstance(doc.get("items"), list):
        doc["items"][:] = copy.deepcopy(items)
    memory = doc.get("memory")
    if isinstance(memory, list):
        memory[:] = [
            e
            for e in memory
            if not (isinstance(e, dict) and type(e.get("turn")) is int and e["turn"] > turn)
        ]


def run_undo(gs: GameSave) -> dict:
    if not gs.undo_stack:
        return {"ok": False, "reason": "nothing to undo"}
    snap = gs.undo_stack.pop()
    _restore(gs, snap)
    return {
        "ok": True,
        "turn": gs.turn,
        "undo_remaining": len(gs.undo_stack),
    }


def run_undo_to_turn(gs: GameSave, target_turn: int) -> dict:
    """Pop snapshots until gs.turn equals target_turn - 1 (i.e. state is
    restored to immediately before the player input that became turn N).

    target_turn=1 -> roll all the way back to opening (turn 0) state.

    Stops if the undo stack runs out before reaching the target.
    """
    if target_turn < 1:
        return {"ok": False, "reason": "target_turn must be >= 1"}
    if not gs.undo_stack:
        return {"ok": False, "reason": "nothing to undo"}
    popped = 0
    while gs.undo_stack and gs.turn >= target_turn:
        snap = gs.undo_stack.pop()
        _restore(gs, snap)
        popped += 1
    if popped == 0:
        return {"ok": False, "reason": "already at or before target turn"}
    return {
        "ok": True,
        "turn": gs.turn,
        "undo_remaining": len(gs.undo_stack),
        "turns_undone": popped,
    }


def run_regenerate(
    gs: GameSave,
    save_id: str,
    feedback: str = "",
    *,
    ua: str = "",
    effective_tier: str | None = None,
) -> dict:
    """Re-run the last turn with the same player input, optionally with feedback.

    Pops the most recent snapshot, restores state/history, and replays through
    run_turn with a hidden regeneration note appended to the input.
    """
    if not gs.undo_stack:
        return {"ok": False, "reason": "nothing to regenerate"}
    if (
        len(gs.history) < 2
        or gs.history[-1]["role"] != "assistant"
        or gs.history[-2]["role"] != "user"
    ):
        return {"ok": False, "reason": "no recent turn to regenerate"}

    last_input = gs.history[-2].get("content_en") or gs.history[-2].get("content", "")
    # The feedback goes to the model with the replayed move; run_turn
    # filters the move itself. This check runs before the undo pop, so a
    # blocked feedback never touches the save state.
    _moderate(gs.config, (feedback or "").strip())

    snap = gs.undo_stack.pop()
    _restore(gs, snap)

    fb = (feedback or "").strip()
    if fb:
        annotated = (
            f"{last_input}\n\n"
            f"[REGENERATION REQUEST - DM only, hidden from player. "
            f"The previous narration of this action did not satisfy the player. "
            f"Player feedback: {fb}. "
            f"Re-narrate the same player action accordingly. Do not acknowledge this note.]"
        )
    else:
        annotated = (
            f"{last_input}\n\n"
            f"[REGENERATION REQUEST - DM only, hidden from player. "
            f"Re-narrate the previous action with a different angle, tone, or details. "
            f"Do not acknowledge this note.]"
        )

    payload = run_turn(
        gs,
        annotated,
        save_id,
        regenerated=True,
        original_input=last_input,
        feedback=fb or None,
        ua=ua,
        effective_tier=effective_tier,
    )

    # The regeneration note was added to the input for the LLM only - strip it
    # from history so it doesn't show in chat or future turns.
    if len(gs.history) >= 2 and gs.history[-2]["role"] == "user":
        cur = gs.history[-2].get("content_en") or gs.history[-2].get("content", "")
        if "REGENERATION REQUEST" in cur:
            if "content_en" in gs.history[-2]:
                gs.history[-2]["content_en"] = last_input
            else:
                gs.history[-2]["content"] = last_input

    payload["original_input"] = last_input
    return payload


def run_rewind(gs: GameSave) -> dict:
    if not gs.checkpoint_state or (gs.checkpoint_len is None and not gs.checkpoint_history):
        return {"ok": False, "reason": "no rewind point"}

    turns_since = gs.turn - gs.checkpoint_turn
    window = min(turns_since * 2 + 4, len(gs.history))
    recent = gs.history[-window:] if window > 0 else gs.history[-4:]

    # The debrief sends the last six messages to the model; the player's
    # among them are filtered first.
    for m in _history_for_llm(recent)[-6:]:
        if m["role"] == "user":
            _moderate(gs.config, m["content"])

    debrief = ""
    try:
        debrief, usage = rewind_debrief(_history_for_llm(recent), gs.config)
        ctx = ContextEngine(gs.session_in, gs.session_out, tier=gs.config.tier)
        ctx.record(usage)
        gs.session_in, gs.session_out = ctx.session_in, ctx.session_out
    except Exception as e:
        debrief = f"(debrief failed: {e})"

    _materialise(gs)
    restored_turn = gs.checkpoint_turn
    gs.state = WorldState(**gs.checkpoint_state)
    gs.history = list(gs.checkpoint_history)
    gs.turn = restored_turn
    gs.checkpoint_state = {}
    gs.checkpoint_history = []
    gs.checkpoint_len = None
    gs.checkpoint_turn = 0
    gs.previous_state = {}
    gs.previous_history = []

    return {
        "ok": True,
        "debrief": debrief,
        "turn": gs.turn,
        "turn_cost": 0.0,
    }


def _sanitize_state_for_meta(state: dict, keywords: tuple = ()) -> dict:
    """Remove DM-only / spoiler material from the state dict before it's
    shown to the meta-call. The /meta endpoint is the player asking the DM
    out-of-character, so it must not leak metagame vocabulary that the
    engine may write into the state. The keywords come from the pack
    (engine_fragments.spoiler_keywords, lower-case); none by default.

    We always drop dm_notes (private DM scratch) and filter list/string
    fields by keyword. Returns a shallow copy."""
    out = {}
    for k, v in (state or {}).items():
        if k == "dm_notes":
            continue
        if isinstance(v, list):
            out[k] = [
                x for x in v if not (isinstance(x, str) and any(kw in x.lower() for kw in keywords))
            ]
        elif isinstance(v, str):
            low = v.lower()
            out[k] = "(redacted)" if any(kw in low for kw in keywords) else v
        else:
            out[k] = v
    return out


def run_meta(gs: GameSave, question: str) -> dict:
    # The profile name goes into the meta system prompt; meta_system
    # interpolates no other profile field. Never inside a try.
    if gs.profile:
        _moderate(gs.config, gs.profile.name or "")
    _moderate(gs.config, question)
    if gs.state.get("tutorial_stage", 0) >= 2:
        keywords = lore_pack.engine_fragment_list(
            lore_pack.load_pack(gs.config.pack_id or lore_pack.DEFAULT_PACK_ID), "spoiler_keywords"
        )
        sanitized = _sanitize_state_for_meta(gs.state.to_dict(), keywords)
        state_json = json.dumps(sanitized, indent=2)
        state_block = f"[WORLD STATE]\n{state_json}\n[/WORLD STATE]"
    else:
        state_block = "(still in tutorial)"

    system = meta_system(
        gs.profile.name,
        gs.turn,
        state_block,
        locale=gs.config.locale or "en",
        gender=gs.profile.gender or "m",
    )
    messages = _history_for_llm(gs.history[-6:]) + [{"role": "user", "content": question}]

    text, usage = call_llm_plain(system, messages, max_tokens=400)
    ctx = ContextEngine(gs.session_in, gs.session_out, tier=gs.config.tier)
    ctx.record(usage)
    gs.session_in, gs.session_out = ctx.session_in, ctx.session_out

    return {
        "response": text.strip(),
        "turn_cost": 0.0,
    }
