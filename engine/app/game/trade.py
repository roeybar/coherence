"""NPC give/take trade logic.

Single seam for player<->NPC item transfers.  The trade route in
api/turn.py is the only entry point for player-initiated item ops;
all server-side validation is re-executed here on every call so the
client disabled-state is never trusted.

Architecture seams:
  1. can_trade - single predicate: same-scene or last_seen_turn==turn.
     No inline location checks anywhere else.
  2. perform_trade - ownership + save-scope + holder + can_trade
     re-validated every call; calls items.move to persist.
  3. take requires disposition_band in {cordial, warm}.
     give has no disposition check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TradeVerdict:
    allowed: bool
    reason: str  # 'ok' | 'not_same_scene'


# can_trade predicate


def can_trade(state, npc, *, turn: Optional[int] = None) -> TradeVerdict:
    """Determine whether trading with `npc` is permitted.

    Default rule: allowed when
      npc.location.lower == state.location.lower
      or npc.last_seen_turn == turn
    (second clause mitigates stale npc.location from record_witnessed
    not refreshing on every interaction turn.)
    """
    # Default predicate.
    npc_loc = (getattr(npc, "location", None) or "").strip().lower()
    state_loc = (getattr(state, "location", None) or "").strip().lower()
    same_scene = npc_loc == state_loc and bool(npc_loc)

    last_seen = getattr(npc, "last_seen_turn", None)
    seen_this_turn = turn is not None and last_seen == turn

    if same_scene or seen_this_turn:
        return TradeVerdict(allowed=True, reason="ok")
    return TradeVerdict(allowed=False, reason="not_same_scene")


def can_trade_dict(state, npc, *, turn: Optional[int] = None) -> dict:
    """JSON-serialisable projection of can_trade for the roster payload."""
    v = can_trade(state, npc, turn=turn)
    return {"allowed": v.allowed, "reason": v.reason}


# perform_trade


def perform_trade(gs, save_id: str, npc_id: str, action: str, item_id: str) -> dict:
    """Execute a player-initiated give or take.

    Validates (in order):
      1. NPC exists in gs.state (npc_not_found)
      2. Item exists scoped to save (item_not_found - closes cross-save gap)
      3. can_trade passes (not_same_scene)
      4a. give: item holder == 'player' (not_player_held), then move->npc
      4b. take: item holder == npc_holder (not_npc_held) and
                disposition_band in {cordial, warm} (npc_unwilling),
                then move->'player'
    Returns {ok, reason, item} - item is the updated public doc or None.
    """
    from app.game import npcs as _npcs
    from app.game import items as _items

    # 1. Resolve NPC.
    state = gs.state
    raw_npcs = _npcs.get_npcs(state)
    npc_raw = raw_npcs.get(npc_id)
    if not npc_raw:
        return {"ok": False, "reason": "npc_not_found", "item": None}
    try:
        npc = _npcs.NPC.from_dict(npc_raw)
    except Exception:
        return {"ok": False, "reason": "npc_not_found", "item": None}

    # 2. Resolve item scoped to this save.
    item_doc = _items.get_item(save_id, item_id)
    if item_doc is None:
        return {"ok": False, "reason": "item_not_found", "item": None}

    # 3. Trade reachability.
    turn = getattr(gs, "turn", None)
    verdict = can_trade(state, npc, turn=turn)
    if not verdict.allowed:
        return {"ok": False, "reason": verdict.reason, "item": None}

    npc_holder = _items.NPC_PREFIX + _items._normalise_npc_name(npc.name)

    if action == "give":
        # 4a. Give: player must hold the item.
        if item_doc.get("holder") != _items.PLAYER:
            return {"ok": False, "reason": "not_player_held", "item": None}
        updated = _items.move(item_id, npc_holder, turn=turn or 0)
        return {"ok": True, "reason": "ok", "item": updated}

    elif action == "take":
        # 4b. Take: NPC must hold the item.
        if item_doc.get("holder") != npc_holder:
            return {"ok": False, "reason": "not_npc_held", "item": None}
        # disposition_band must be cordial or warm.
        band = _npcs.disposition_band(npc.disposition)
        if band not in ("cordial", "warm"):
            return {"ok": False, "reason": "npc_unwilling", "item": None}
        updated = _items.move(item_id, _items.PLAYER, turn=turn or 0)
        return {"ok": True, "reason": "ok", "item": updated}

    else:
        return {"ok": False, "reason": "bad_action", "item": None}
