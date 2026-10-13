"""Stateless game endpoints. The body carries the save doc; the response
carries the updated doc. Each handler binds the doc (and, for message
routes, the provider key) inside one try/finally."""

import copy
import functools
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app import ratelimit, store
from app.game import lore as lore_pack
from app.game import moderation
from app.game import telemetry
from app.game.addressee import AddresseeAmbiguous
from app.game.context import ContextTooLarge
from app.game.craft_gate import CraftRollPending
from app.game.dice_gate import DiceRollPending
from app.game.engine import (
    ModerationBlocked,
    _history_for_display,
    _parse_world_premise,
    create_save,
    run_meta,
    run_regenerate,
    run_rewind,
    run_turn,
    run_undo,
    run_undo_to_turn,
)
from app.game.escalation import EscalationPending
from app.game.physicalisation import INTENT_VALUES, AbilityIntentAmbiguous
from app.game.reframe_gate import ReframeAmbiguous
from app.game.state import UNDO_CAP, CharacterProfile, GameConfig, doc_to_save, save_to_doc
from app.game.world_rule_gate import WorldRuleShiftPending
from app.llm_client import ProviderError
from app.providers import registry

log = logging.getLogger(__name__)
_block_log = logging.getLogger("coherence.llm.block")

router = APIRouter()

ENGINE_ESCALATION_TIER = "pro"
STATELESS_SAVE_ID = "local"
PHYSICS_CACHE_KEEP = 64
HOSTED_SAVE_TEXT_CAP = 500_000  # characters of save text a hosted server checks


def _history_cap(raw: Optional[str]) -> Optional[int]:
    """SAVE_HISTORY_CAP: unset or blank means no cap; otherwise a whole number of 1 or more."""
    if raw is None or not raw.strip():
        return None
    cap = int(raw)
    if cap < 1:
        raise ValueError("SAVE_HISTORY_CAP must be 1 or more")
    return cap


SAVE_HISTORY_CAP = _history_cap(os.environ.get("SAVE_HISTORY_CAP"))
TURN_GROWTH = 2  # a played move adds the player's entry and the reply

_RO_EN = (
    "This is a read-only copy of a flagged session - it can't be continued. "
    "Your original save is safe: load it to keep playing."
)


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")


class _ReadOnlySave(Exception):
    def __init__(self, gs):
        self.gs = gs


def _state_payload(gs) -> dict:
    return {
        "state": gs.state.to_dict(),
        "turn": gs.turn,
        "history": _history_for_display(gs.history, gs.config.locale or "en"),
        "session": {"in": gs.session_in, "out": gs.session_out},
        "checkpoint_available": bool(gs.checkpoint_state),
        "undo_available": bool(gs.undo_stack),
        "undo_depth": len(gs.undo_stack),
        "locale": gs.config.locale or "en",
    }


def _ro_response(ros: _ReadOnlySave) -> dict:
    return {
        "narrative": _RO_EN,
        "narrative_en": _RO_EN,
        "delta": None,
        "diff": {},
        "kind": "read_only_gate",
        **_state_payload(ros.gs),
    }


def _resolve_key(request: Request):
    provider = request.headers.get("x-provider") or None
    if request.app.state.mode == "hosted":
        raw = request.headers.get("x-provider-key") or ""
        from pydantic import SecretStr

        key = SecretStr(raw) if raw else None
        why, stored = "ok" if key else "none", ()
    else:
        from app import creds

        why, key, stored = creds.key_state(request.cookies.get("coh_session"), provider or "")
    return provider, key, why, stored


def _require_key(request: Request):
    """Key check (428, no slot used)."""
    provider, key, why, stored = _resolve_key(request)
    if key is None or provider not in registry.REAL_PROVIDERS:
        if why == "locked":
            raise ratelimit.api_error(428, "key_store_locked", provider)
        if why == "other":
            exc = ratelimit.api_error(428, "key_other_provider", provider)
            exc.detail["stored"] = list(stored)
            raise exc
        raise ratelimit.api_error(428, "byok_required", provider)
    return provider, key


def _llm_preflight(request: Request):
    """Key check (428, no slot used), then the message limit (400/429)."""
    provider, key = _require_key(request)
    ratelimit.check_message(
        request.headers.get("x-coherence-session"), ratelimit.client_ip(request)
    )
    return provider, key


def _handle_upstream(exc: ProviderError, provider: Optional[str]):
    kind = (exc.detail or {}).get("kind")
    if kind in ("byok_rejected", "byok_required"):
        return ratelimit.api_error(428, kind, provider)
    if kind == "provider_rate_limited":
        return ratelimit.api_error(429, kind, provider)
    return ratelimit.api_error(503, "provider_unavailable", provider)


def _store_pending_input(gs, text: str, gate_kind: str) -> None:
    pending = {
        "text": (text or "")[:4000],
        "gate_kind": gate_kind,
        "at_turn": gs.turn,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        gs.state.pending_input = pending
    except Exception:
        pass


def _index_fork_turn(turn: int, player_input: str, narrative_en: str) -> None:
    try:
        from app.game.memory import index_turn as _mem_index

        _mem_index(STATELESS_SAVE_ID, turn, "user", player_input)
        _mem_index(STATELESS_SAVE_ID, turn, "assistant", narrative_en)
    except Exception as e:
        log.warning("_index_fork_turn failed: %s", type(e).__name__)


def _fork_if_flagged(gs, result: dict, player_input: str) -> None:
    """A first flagged turn makes the returned doc the shadow fork."""
    if result.get("flagged", False) and gs.shadow_of is None:
        gs.shadow_of = STATELESS_SAVE_ID
        gs.read_only = False
        _index_fork_turn(gs.turn, player_input, result.get("narrative_en", ""))
        telemetry.log_event("automation_flag")


def _pack_of(doc) -> Optional[str]:
    config = doc.get("config") if isinstance(doc, dict) else None
    return config.get("pack_id") if isinstance(config, dict) else None


def _text_cap(request: Request) -> Optional[int]:
    """Hosted servers cap the save text at HOSTED_SAVE_TEXT_CAP; self-host has no cap (None)."""
    return HOSTED_SAVE_TEXT_CAP if request.app.state.mode == "hosted" else None


def _history_ok(history) -> bool:
    """History entries are dicts whose content fields hold text or None."""
    if not isinstance(history, list):
        return True
    for entry in history:
        if not isinstance(entry, dict):
            return False
        for k, v in entry.items():
            if (
                isinstance(k, str)
                and (k == "content" or k.startswith("content_"))
                and v is not None
                and not isinstance(v, str)
            ):
                return False
    return True


def _display_ok(save: dict) -> bool:
    if "name" in save and not isinstance(save["name"], str):
        return False
    stacks = [u.get("history") for u in save.get("undo_stack", []) if isinstance(u, dict)]
    return all(
        _history_ok(h) for h in [save.get("history"), save.get("checkpoint_history"), *stacks]
    )


def _entries_error():
    exc = ratelimit.api_error(413, "text_too_large")
    exc.detail["scope"] = "history"
    return exc


def _check_entries(save: dict, grows: int) -> None:
    """Over SAVE_HISTORY_CAP: the history, the checkpoint copy or an undo copy is longer
    than the cap, or this route would take the history past it. Refused as text_too_large
    with scope history, whose copy names no number."""
    cap = SAVE_HISTORY_CAP
    if cap is None:
        return
    history = save.get("history")
    now = len(history) if isinstance(history, list) else 0
    stacks = [u.get("history") for u in save.get("undo_stack", []) if isinstance(u, dict)]
    lists = [history, save.get("checkpoint_history"), save.get("previous_history"), *stacks]
    if now + grows > cap or any(isinstance(h, list) and len(h) > cap for h in lists):
        raise _entries_error()


def _regen_growth(save) -> int:
    """How a regenerate moves the history length: it restores the top undo snapshot
    (or the legacy previous_history), then plays a move."""
    if not isinstance(save, dict):
        return 0
    history = save.get("history")
    now = len(history) if isinstance(history, list) else 0
    stack = save.get("undo_stack")
    if isinstance(stack, list) and stack:
        top = stack[-1]
    elif save.get("previous_state"):
        top = {"history": save.get("previous_history") or []}
    else:
        return 0
    if not isinstance(top, dict):
        return 0
    if isinstance(top.get("history"), list):
        restored = len(top["history"])
    elif type(top.get("history_len")) is int:
        restored = top["history_len"]
    else:
        return 0
    return restored + TURN_GROWTH - now


def _admit(
    request: Request,
    save: dict,
    message: bool,
    *,
    extra=None,
    budget_extra=None,
    pack_id=None,
    grows=0,
):
    """Runs first on every game route: the key and message limit (message
    routes) or the action limit, the save's shape, the history cap, the text
    cap over the save and the request, then every string of the request and
    of the save (including the parsed world premise). Returns (provider, key,
    checked strings, pack)."""
    if message:
        provider, key = _llm_preflight(request)
    else:
        provider, key = None, None
        ratelimit.check_action(
            request.headers.get("x-coherence-session"), ratelimit.client_ip(request)
        )
    if not isinstance(save, dict) or any(
        not isinstance(save.get(k, []), list) for k in store.SAVE_KEYS
    ):
        raise ratelimit.api_error(400, "bad_save")
    if not _display_ok(save):
        raise ratelimit.api_error(400, "bad_save")
    _check_entries(save, grows)
    pack = moderation.resolve_pack(pack_id if pack_id is not None else _pack_of(save))
    cap = _text_cap(request)
    # the walk counts as it goes and stops at the cap, before the rest is read
    walk = moderation.TextBudget(cap) if cap is not None else None
    try:
        saved = moderation.collect_strings(save, exempt=moderation.SAVE_EXEMPT, budget=walk)
        config = save.get("config")
        premise = config.get("world_premise") if isinstance(config, dict) else None
        derived = (
            moderation.collect_strings(_parse_world_premise(premise), budget=walk)
            if isinstance(premise, str) and premise.strip()
            else set()
        )
        asked = moderation.collect_strings(extra, budget=walk) if extra is not None else set()
        budget = (
            moderation.collect_strings(budget_extra, budget=walk)
            if budget_extra is not None and walk is not None
            else set()
        )
    except moderation.TextTooLarge:
        raise ratelimit.api_error(413, "text_too_large") from None
    seen = saved | derived | asked | budget
    if cap is not None:
        if sum(len(s) for s in seen) > cap:
            raise ratelimit.api_error(413, "text_too_large")
        # the decoded and joined views are checked too, so their text counts
        total = sum(len(s) for s in seen)
        for s in seen:
            total += moderation.view_chars(s)
            if total > cap:
                raise ratelimit.api_error(413, "text_too_large")
    check_text = (
        moderation.check_input
        if request.app.state.mode == "hosted"
        else moderation.check_input_cached
    )
    for s in sorted(asked):
        check = check_text(s, pack_id=pack)
        if check.blocked:
            raise _blocked(check, "content_blocked")
    for s in sorted((saved | derived) - asked):
        check = check_text(s, pack_id=pack)
        if check.blocked:
            raise _blocked(check, "save_blocked")
    # egress skips only what was matched here, not budget-only request text
    return provider, key, saved | derived | asked, pack


def _block_site(exc: BaseException) -> str:
    """module:function of the innermost engine frames that raised a block (no text)."""
    frames = [
        f
        for f in traceback.extract_tb(exc.__traceback__)
        if "/app/" in f.filename.replace("\\", "/")
    ]
    names = [f"{Path(f.filename).stem}:{f.name}" for f in frames[-3:]]
    return ">".join(names) or "-"


def _guarded(handler):
    """A blocked model reply is 451 output_blocked; missing word lists are 500."""

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except moderation.OutputBlocked as ob:
            _block_log.warning(f"output_blocked category={ob.category} site={_block_site(ob)}")
            raise _blocked(ob, "output_blocked") from None
        except moderation.ListsUnavailable as lu:
            _block_log.warning(f"output_blocked category=lists_unavailable site={_block_site(lu)}")
            raise ratelimit.api_error(500, "internal") from None

    return wrapper


def _run(
    request: Request,
    save: dict,
    fn,
    message: bool,
    *,
    extra=None,
    budget_extra=None,
    pack_id=None,
    grows=0,
):
    """Admit, bind, load, call fn(gs) -> dict, check the result and attach
    the updated doc."""
    provider, key, seen, pack = _admit(
        request, save, message, extra=extra, budget_extra=budget_extra, pack_id=pack_id, grows=grows
    )
    doc = save
    tok = store.bind_save(doc)
    mtok = moderation.bind_pack(pack)
    ptok = None
    try:
        if message:
            bound = "fake" if registry.fake_enabled() else provider
            ptok = registry.bind_request(bound, key)
        doc["physics_cache"] = doc["physics_cache"][-PHYSICS_CACHE_KEEP:]
        try:
            gs = doc_to_save(doc)
        except (TypeError, ValueError, KeyError):
            raise ratelimit.api_error(400, "bad_save") from None
        gs.undo_stack = gs.undo_stack[-UNDO_CAP:]
        try:
            if gs.read_only:
                raise _ReadOnlySave(gs)
            out = fn(gs)
        except _ReadOnlySave as ros:
            out = _ro_response(ros)
        except ProviderError as e:
            raise _handle_upstream(e, provider) from None
        if (
            SAVE_HISTORY_CAP is not None
            and isinstance(gs.history, list)
            and len(gs.history) > SAVE_HISTORY_CAP
        ):
            raise _entries_error()
        result = {
            **out,
            "save": {**{k: v for k, v in doc.items() if k != "checkpoint_len"}, **save_to_doc(gs)},
        }
        body = {k: v for k, v in result.items() if k != "save"}
        check, _ = moderation.check_tree(body, pack_id=pack, skip=seen)
        if not check.blocked:
            check, _ = moderation.check_tree(
                result["save"], pack_id=pack, skip=seen, exempt=moderation.SAVE_EXEMPT
            )
        if check.blocked:
            _block_log.warning(f"output_blocked category={check.category} site=game:egress")
            raise _blocked(check, "output_blocked")
        return result
    finally:
        if ptok is not None:
            registry.unbind_request(ptok)
        moderation.unbind_pack(mtok)
        store.unbind_save(tok)


def _blocked(mb, kind: str = "content_blocked") -> Exception:
    exc = ratelimit.api_error(451, kind)
    exc.detail["category"] = mb.category
    return exc


class TurnIn(BaseModel):
    save: dict
    player_input: str = Field(min_length=1, max_length=2000)
    consent_token: Optional[str] = None
    decline_escalation: bool = False
    addressee: Optional[object] = None
    ability_intent: Optional[str] = None
    reframe_confirm: Optional[str] = None
    world_rule_confirm: Optional[str] = None
    suggestion_index: Optional[int] = None
    craft_roll: Optional[int] = None
    dice_roll: Optional[int] = None


class SaveIn(BaseModel):
    save: dict


class MetaIn(SaveIn):
    question: str = Field(min_length=1, max_length=2000)


class RegenIn(SaveIn):
    feedback: str = Field(default="", max_length=1000)


class UndoToIn(SaveIn):
    turn_index: int


class TradeIn(SaveIn):
    npc_id: str = Field(min_length=1, max_length=120)
    action: str = Field(pattern=r"^(give|take)$")
    item_id: str = Field(min_length=1, max_length=48)


_GATES = (
    (AddresseeAmbiguous, "addressee_ambiguous"),
    (AbilityIntentAmbiguous, "ability_intent"),
    (ReframeAmbiguous, "reframe"),
    (WorldRuleShiftPending, "world_rule"),
    (CraftRollPending, "craft_roll"),
    (DiceRollPending, "dice_roll"),
    (EscalationPending, "escalation"),
)


@router.post("/api/game/turn")
@_guarded
def take_turn(req: TurnIn, request: Request):
    def fn(gs):
        intent = req.ability_intent if req.ability_intent in INTENT_VALUES else None
        reframe = req.reframe_confirm if req.reframe_confirm in ("confirm", "cancel") else None
        wr = req.world_rule_confirm if req.world_rule_confirm in ("confirm", "cancel") else None
        shown = list(gs.state.get("suggested_moves") or [])
        try:
            result = run_turn(
                gs,
                req.player_input,
                STATELESS_SAVE_ID,
                ua=_ua(request),
                consent_token=req.consent_token,
                decline_escalation=req.decline_escalation,
                effective_tier=ENGINE_ESCALATION_TIER,
                addressee=req.addressee,
                ability_intent=intent,
                reframe_confirm=reframe,
                world_rule_confirm=wr,
                craft_roll=req.craft_roll,
                dice_roll=req.dice_roll,
                suggestion_index=req.suggestion_index,
                suggestions_shown=shown,
            )
        except ModerationBlocked as mb:
            _store_pending_input(gs, req.player_input, "moderation")
            raise _blocked(mb) from None
        except ContextTooLarge:
            raise ratelimit.api_error(413, "context_too_large") from None
        except tuple(g for g, _ in _GATES) as gate:
            kind = next(k for g, k in _GATES if isinstance(gate, g))
            _store_pending_input(gs, req.player_input, kind)
            return {**gate.to_payload(), **_state_payload(gs)}
        _fork_if_flagged(gs, result, req.player_input)
        return {**result, **_state_payload(gs)}

    extra = {
        "addressee": req.addressee,
        "consent_token": req.consent_token,
        "ability_intent": req.ability_intent,
        "reframe_confirm": req.reframe_confirm,
        "world_rule_confirm": req.world_rule_confirm,
        # the rolls reach the model's prompt as these digits
        "craft_roll": None if req.craft_roll is None else str(req.craft_roll),
        "dice_roll": None if req.dice_roll is None else str(req.dice_roll),
    }
    return _run(request, req.save, fn, message=True, extra=extra, grows=TURN_GROWTH)


@router.post("/api/game/undo")
@_guarded
def undo(req: SaveIn, request: Request):
    return _run(request, req.save, lambda gs: {**run_undo(gs), **_state_payload(gs)}, False)


@router.post("/api/game/undo-to-turn")
@_guarded
def undo_to_turn(req: UndoToIn, request: Request):
    return _run(
        request,
        req.save,
        lambda gs: {**run_undo_to_turn(gs, req.turn_index), **_state_payload(gs)},
        False,
    )


@router.post("/api/game/rewind")
@_guarded
def rewind(req: SaveIn, request: Request):
    def fn(gs):
        try:
            return {**run_rewind(gs), **_state_payload(gs)}
        except ModerationBlocked as mb:
            raise _blocked(mb) from None

    return _run(request, req.save, fn, True)


@router.post("/api/game/meta")
@_guarded
def meta(req: MetaIn, request: Request):
    def fn(gs):
        try:
            return {**run_meta(gs, req.question), **_state_payload(gs)}
        except ModerationBlocked as mb:
            raise _blocked(mb) from None

    return _run(request, req.save, fn, True)


@router.post("/api/game/check")
@_guarded
def check_save(req: SaveIn, request: Request):
    _admit(request, req.save, False)
    return {"ok": True}


def _regen(request: Request, gs, feedback: str) -> dict:
    try:
        result = run_regenerate(
            gs,
            STATELESS_SAVE_ID,
            feedback,
            ua=_ua(request),
            effective_tier=ENGINE_ESCALATION_TIER,
        )
    except ModerationBlocked as mb:
        raise _blocked(mb) from None
    except ContextTooLarge:
        raise ratelimit.api_error(413, "context_too_large") from None
    if result.get("ok") is not False:
        _fork_if_flagged(gs, result, "")
    return {**result, **_state_payload(gs)}


@router.post("/api/game/regenerate")
@_guarded
def regenerate(req: RegenIn, request: Request):
    return _run(
        request,
        req.save,
        lambda gs: _regen(request, gs, req.feedback or ""),
        True,
        grows=_regen_growth(req.save),
    )


@router.post("/api/game/welcome-intervention/regen")
@_guarded
def welcome_intervention_regen(req: SaveIn, request: Request):
    def fn(gs):
        interv = None
        if gs.history and gs.history[-1].get("role") == "assistant":
            interv = gs.history[-1].get("intervention")
        if not interv or interv.get("kind") != "welcome_grace_doubt":
            raise ratelimit.api_error(409, "intervention_missing")
        matched = interv.get("matched_text") or ""
        orig = (interv.get("original_input") or "").strip()
        nudge = (
            "the previous turn was detected as refusing player authorship "
            f'(pattern: "{matched}"). The player\'s input was: "{orig}". '
            "ground that addition in world idiom on this regenerate - "
            "translate the player's fanciful or out-of-world contribution "
            "into the closest world-consistent equivalent and run with it. "
            "do NOT flat-refuse, do NOT imply the player is hallucinating, "
            "do NOT say nothing happens"
        )
        return _regen(request, gs, nudge)

    return _run(request, req.save, fn, True, grows=_regen_growth(req.save))


@router.post("/api/game/welcome-intervention/dismiss")
@_guarded
def welcome_intervention_dismiss(req: SaveIn, request: Request):
    def fn(gs):
        if gs.history and gs.history[-1].get("role") == "assistant":
            interv = gs.history[-1].get("intervention")
            if interv and interv.get("kind") == "welcome_grace_doubt":
                interv["accepted"] = True
        return {"ok": True}

    return _run(request, req.save, fn, False)


@router.post("/api/game/npc-trade")
@_guarded
def npc_trade(req: TradeIn, request: Request):
    extra = {"npc_id": req.npc_id, "item_id": req.item_id}
    if isinstance(req.save, dict) and req.save.get("read_only"):
        _admit(request, req.save, False, extra=extra)
        return {"ok": False, "reason": "read_only"}
    from app.game import trade as _trade

    return _run(
        request,
        req.save,
        lambda gs: _trade.perform_trade(
            gs, STATELESS_SAVE_ID, npc_id=req.npc_id, action=req.action, item_id=req.item_id
        ),
        False,
        extra=extra,
    )


class ProfileIn(BaseModel):
    name: str = "nameless"
    appearance: str = "unremarkable"
    notes: str = ""


class ConfigIn(BaseModel):
    difficulty: str = "normal"
    world_type: str = Field(default="canon", pattern="^(canon|custom|freeform)$")
    tier: str = "basic"
    pack_id: str = Field(
        default_factory=lambda: lore_pack.DEFAULT_PACK_ID, pattern="^[a-z0-9_]{1,48}$"
    )
    one_memory: str = ""
    appearance_descriptors_first_mention: str = Field(
        default="restrained", pattern="^(restrained|free)$"
    )
    world_hint_form: dict = Field(default_factory=dict)
    dice_mechanics: bool = False


class SaveCreate(BaseModel):
    name: str = Field(default="untitled", max_length=80)
    profile: ProfileIn = ProfileIn()
    config: ConfigIn = ConfigIn()
    world_hint: str = ""


def _save_payload(gs) -> dict:
    items_view = {"scene": [], "carried": []}
    try:
        from app.game import items as items_mod

        items_view = items_mod.visible_to_player(STATELESS_SAVE_ID, gs.state.location)
    except Exception:
        pass
    money: dict = {}
    try:
        money = lore_pack.money_denominations(lore_pack.load_pack(gs.config.pack_id))
    except Exception:
        pass
    colors, roster = {}, []
    try:
        from app.game import npcs as _npcs

        colors = _npcs.colour_map(gs.state)
        roster = _npcs.roster_view(gs.state, save_id=STATELESS_SAVE_ID, turn=gs.turn)
    except Exception:
        pass
    return {
        "id": STATELESS_SAVE_ID,
        "name": gs.name,
        **_state_payload(gs),
        "items": items_view,
        "money_denominations": money,
        "npc_colors": colors,
        "npc_roster": roster,
    }


@router.post("/api/game/new")
@_guarded
def create(req: SaveCreate, request: Request):
    profile = CharacterProfile(**req.profile.model_dump())
    config = GameConfig(**req.config.model_dump())
    try:
        lore_pack.load_pack(config.pack_id)
    except Exception:
        raise ratelimit.api_error(400, "bad_pack") from None
    # Only a custom world calls the model at creation, so only it takes the
    # message slot. A canon world still needs a key, then takes the action limit.
    message = config.world_type == "custom"
    if config.world_type == "canon":
        _require_key(request)
    doc: dict = {}

    def fn(_unused):
        try:
            gs = create_save(req.name, profile, config, req.world_hint)
        except ModerationBlocked as mb:
            raise _blocked(mb) from None
        if gs.history:
            from app.game.memory import index_turn

            opening = gs.history[0].get("content_en") or gs.history[0].get("content", "")
            index_turn(STATELESS_SAVE_ID, 0, "assistant", opening)
        holder["gs"] = gs
        return _save_payload(gs)

    holder: dict = {}
    asked = req.config.model_dump()
    asked.pop("one_memory", None)
    out = _run(
        request,
        doc,
        fn,
        message,
        extra={"name": req.name, "config": asked},
        budget_extra=req.model_dump(),
        pack_id=config.pack_id,
    )
    gs = holder["gs"]
    out["save"] = {**{k: v for k, v in doc.items() if k in store.SAVE_KEYS}, **save_to_doc(gs)}
    out["save"] = copy.deepcopy(out["save"])
    check, _ = moderation.check_tree(
        out["save"], pack_id=moderation.resolve_pack(config.pack_id), exempt=moderation.SAVE_EXEMPT
    )
    if check.blocked:
        raise _blocked(check, "output_blocked")
    return out


@router.get("/api/packs")
def list_packs_for_player():
    return {
        "packs": [
            {"id": p["id"], "name": p["name"], "description": p["description"]}
            for p in lore_pack.list_packs()
        ]
    }
