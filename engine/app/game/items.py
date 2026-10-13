"""Per-save item store. Items live at locations in the world, in the
player's hands, or in NPCs' hands. The DM sees them in the system
context, can mutate them via state_delta operations, and the engine
applies the ops.

Schema (one record per item, kept in the "items" list of the bound save doc):
    _id           str (hex id assigned on insert)
    save_id       str
    name          str - short noun phrase ("leather pouch", "rusted dagger")
    holder        str - "player" | "scene" | "npc:<npc-name>"
    location      str - scene-location string when holder == "scene".
                          Empty / unused when holder is player or npc.
                          (A "carried" location with no holder reads
                          as holder="player".)
    emoji         str - ASCII kind word (weapon, shield, armor, bow,
                          container, coin, scroll, key, jewel, food, herb,
                          stone, rope, light, potion, staff, construct, or
                          item when none fits). The UI picks the icon by it.
    description   str
    props         dict - free-form (weight, value, magical, etc.)
    created_turn  int
    updated_turn  int
    created_at    datetime

DM emits an `items` array in state_delta with op dicts:

    {"op":"add",     "name":"...", "where":"scene"|"carried"|"npc:<name>",
                     "description":"...", "emoji":"<kind word>", "props":{}}
    {"op":"pickup",  "name":"..."}                # scene -> player
    {"op":"drop",    "name":"..."}                # player -> scene
    {"op":"give",    "name":"...", "to":"npc:<name>"}    # player -> npc
    {"op":"take",    "name":"...", "from":"npc:<name>"}  # npc -> player
    {"op":"move",    "name":"...", "to":"player"|"scene"|"npc:<name>"}
    {"op":"remove",  "name":"..."}                # destroy / consume
    {"op":"describe","name":"...", "description":"..."}

apply_ops resolves names against current items and performs the
corresponding updates to the bound save doc. Unknown ops or unmatched
names are logged but do not raise - the engine never trusts the model to be
schema-correct."""

import logging
import re as _re_emoji
from datetime import datetime, timezone
from typing import Optional

from app import store


log = logging.getLogger(__name__)

# Holder sentinels.
PLAYER = "player"
SCENE = "scene"
NPC_PREFIX = "npc:"  # holders for NPCs are "npc:<name-normalised>"

# An item doc with no holder and location="carried" is held by the
# player; _holder_of reads it that way.
LEGACY_CARRIED = "carried"


def _coll():
    return store.MiniColl(store.current_save(), "items")


def _normalise_npc_name(name: str) -> str:
    """NPC names are matched case-insensitive, whitespace-trimmed.
    Stored as-typed but compared via _norm."""
    return (name or "").strip().lower()


def _holder_of(doc: dict) -> str:
    """Compute holder from the doc; a doc with no holder falls back to
    its `location`."""
    h = doc.get("holder")
    if h:
        return h
    loc = doc.get("location") or ""
    if loc == LEGACY_CARRIED:
        return PLAYER
    return SCENE


def _public(doc: dict) -> dict:
    """JSON-safe projection - id as str, drop save_id (caller owns)."""
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name", ""),
        "holder": _holder_of(doc),
        "location": doc.get("location", ""),
        "emoji": _kind_word(doc.get("emoji"), doc),
        "description": doc.get("description", ""),
        "props": dict(doc.get("props") or {}),
        "created_turn": doc.get("created_turn"),
        "updated_turn": doc.get("updated_turn"),
    }


# Lightweight kind default by name keyword - fallback when the DM
# didn't tag the item with a kind word. Not authoritative; just nicer UI.
_EMOJI_HINTS = (
    ("sword|blade|dagger|knife|axe|spear|lance|halberd", "weapon"),
    ("shield|buckler", "shield"),
    ("armor|armour|mail|helm|cuirass", "armor"),
    ("bow|crossbow|arrow|quiver", "bow"),
    ("pouch|sack|bag|satchel|pack|purse", "container"),
    ("coin|gold|silver|shilling|crown", "coin"),
    ("scroll|parchment|letter|tome|book", "scroll"),
    ("key", "key"),
    ("ring|amulet|pendant|necklace|jewel", "jewel"),
    ("bread|loaf|cheese|apple|food|stew|fruit", "food"),
    ("herb|flower|root|mushroom", "herb"),
    ("stone|rock|pebble", "stone"),
    ("rope|twine", "rope"),
    ("torch|lantern|candle", "light"),
    ("potion|vial|flask|elixir", "potion"),
    ("staff|cane|wand|rod", "staff"),
    ("robot|automaton|construct|golem", "construct"),
)
_ITEM_KIND = "item"
_KIND_WORDS = frozenset(kind for _, kind in _EMOJI_HINTS) | {_ITEM_KIND}


def _default_emoji(doc: dict) -> str:
    name = (doc.get("name") or "").lower()
    for pattern, kind in _EMOJI_HINTS:
        if _re_emoji.search(pattern, name):
            return kind
    return _ITEM_KIND


def _kind_word(value, doc: dict) -> str:
    """A stored or model-supplied kind word, or the name-derived one when
    the value is not a kind word (a glyph from an old save, say)."""
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in _KIND_WORDS else _default_emoji(doc)


def _now():
    return datetime.now(timezone.utc)


# reads
def _all_for_save(save_id: str) -> list[dict]:
    try:
        return [_public(r) for r in _coll().find({"save_id": save_id})]
    except Exception as e:
        log.warning("_all_for_save failed: %s", e)
        return []


def visible_to_player(save_id: str, location: str) -> dict:
    """Returns {carried, scene, npcs} for the inventory panel:
       carried - held by player
       scene - on the ground at the player's current location
       npcs - dict {npc_name: [items]} for NPCs in scene who carry stuff

    NPC location isn't tracked structurally (yet); we return all
    NPC-held items regardless of where the NPC supposedly is. The DM
    narrative carries the spatial context. Inventory panel sees them
    grouped by NPC name."""
    rows = _all_for_save(save_id)
    carried, scene = [], []
    npcs: dict[str, list[dict]] = {}
    for r in rows:
        h = r["holder"]
        if h == PLAYER:
            carried.append(r)
        elif h == SCENE and (r.get("location") or "") == location:
            scene.append(r)
        elif h.startswith(NPC_PREFIX):
            name = h[len(NPC_PREFIX) :]
            npcs.setdefault(name, []).append(r)
    return {"carried": carried, "scene": scene, "npcs": npcs}


# writes
def _resolve_holder(where: str, current_location: str) -> tuple[str, str]:
    """Translate a DM-emitted `where` value into the (holder, location)
    pair we store. Cases:
      "scene"          -> ("scene",      current_location)
      "carried"        -> ("player",     "")
      "player"         -> ("player",     "")
      "npc:<name>"     -> ("npc:<norm>", "")
      <other string>   -> ("scene",      <string>) - treats as a scene location
    """
    w = (where or "").strip()
    if not w or w == SCENE:
        return SCENE, current_location
    if w in (PLAYER, LEGACY_CARRIED):
        return PLAYER, ""
    if w.startswith(NPC_PREFIX):
        return NPC_PREFIX + _normalise_npc_name(w[len(NPC_PREFIX) :]), ""
    # Bare string - treat as a scene location override
    return SCENE, w


def add(
    save_id: str,
    name: str,
    where: str,
    *,
    description: str = "",
    props: Optional[dict] = None,
    emoji: str = "",
    turn: int = 0,
    current_location: str = "",
) -> Optional[dict]:
    """Create a new item. `where` accepts player|scene|npc:<name> or a
    bare scene-location string."""
    name = (name or "").strip()
    if not name:
        return None
    holder, loc = _resolve_holder(where, current_location)
    doc = {
        "save_id": save_id,
        "name": name[:120],
        "holder": holder,
        "location": loc[:200],
        "emoji": _kind_word(emoji, {"name": name}),
        "description": (description or "")[:500],
        "props": dict(props or {}),
        "created_turn": int(turn),
        "updated_turn": int(turn),
        "created_at": _now(),
    }
    res = _coll().insert_one(doc)
    doc["_id"] = res.inserted_id
    return _public(doc)


def _norm_name(s: str) -> str:
    """Normalise for matching: lowercase, collapse whitespace, treat
    underscores and hyphens as spaces. The DM is inconsistent about
    whether it writes "brass lantern" or "brass_lantern", and we
    don't want a coin-flip on whether the op applies."""
    s = (s or "").lower().strip()
    s = s.replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def _find_by_name(save_id: str, name: str, holder_filter: Optional[str] = None) -> Optional[dict]:
    """Best-effort name match within optional holder constraint.
    Order: exact normalised -> prefix -> substring. Normalisation makes
    underscores/hyphens equivalent to spaces."""
    needle = _norm_name(name)
    if not needle:
        return None
    candidates = list(_coll().find({"save_id": save_id}))
    if holder_filter:
        candidates = [c for c in candidates if _holder_of(c) == holder_filter]
    for c in candidates:
        if _norm_name(c.get("name") or "") == needle:
            return c
    for c in candidates:
        if _norm_name(c.get("name") or "").startswith(needle):
            return c
    for c in candidates:
        if needle in _norm_name(c.get("name") or ""):
            return c
    return None


def move(
    item_id_or_doc, where: str, *, current_location: str = "", turn: int = 0
) -> Optional[dict]:
    """Move an item. `where` accepts player|scene|npc:<name> or a bare
    scene-location string."""
    if isinstance(item_id_or_doc, dict):
        oid = item_id_or_doc["_id"]
    else:
        try:
            oid = str(item_id_or_doc)
        except Exception:
            return None
    holder, loc = _resolve_holder(where, current_location)
    res = _coll().find_one_and_update(
        {"_id": oid},
        {
            "$set": {
                "holder": holder,
                "location": loc[:200],
                "updated_turn": int(turn),
                "updated_at": _now(),
            }
        },
        return_document=True,
    )
    return _public(res) if res else None


def describe(item_id_or_doc, description: str, *, turn: int = 0) -> Optional[dict]:
    if isinstance(item_id_or_doc, dict):
        oid = item_id_or_doc["_id"]
    else:
        try:
            oid = str(item_id_or_doc)
        except Exception:
            return None
    res = _coll().find_one_and_update(
        {"_id": oid},
        {
            "$set": {
                "description": (description or "")[:500],
                "updated_turn": int(turn),
                "updated_at": _now(),
            }
        },
        return_document=True,
    )
    return _public(res) if res else None


def remove(item_id_or_doc) -> bool:
    if isinstance(item_id_or_doc, dict):
        oid = item_id_or_doc["_id"]
    else:
        try:
            oid = str(item_id_or_doc)
        except Exception:
            return False
    return _coll().delete_one({"_id": oid}).deleted_count > 0


# save-scoped item resolution + NPC inventory


def get_item(save_id: str, item_id: str) -> Optional[dict]:
    """Resolve a single item by id scoped to the save.

    Returns the public projection dict, or None when the item doesn't
    exist, has an invalid id, or belongs to a different save.  This
    closes the un-scoped move gap: callers can never reach across saves
    by supplying a valid item_id from another save.
    """
    try:
        oid = str(item_id)
    except Exception:
        return None
    try:
        doc = _coll().find_one({"_id": oid, "save_id": save_id})
        return _public(doc) if doc else None
    except Exception as e:
        log.warning("get_item failed save=%s item=%s: %s", save_id, item_id, e)
        return None


def items_held_by_npc(save_id: str, npc_name: str) -> list:
    """Return the public-projection list of items held by `npc_name` in
    `save_id`.  Normalises the name the same way NPC_PREFIX storage does.
    Returns [] on any error.
    """
    try:
        holder = NPC_PREFIX + _normalise_npc_name(npc_name)
        docs = list(_coll().find({"save_id": save_id, "holder": holder}))
        return [_public(d) for d in docs]
    except Exception as e:
        log.warning("items_held_by_npc failed save=%s npc=%s: %s", save_id, npc_name, e)
        return []


# DM-driven op application
def apply_ops(save_id: str, ops: list, *, current_location: str, turn: int = 0) -> dict:
    """Apply a list of DM-emitted op dicts. Returns a {applied, skipped,
    diff} summary the engine attaches to the turn payload for telemetry +
    debug."""
    applied, skipped = [], []
    if not ops:
        return {"applied": [], "skipped": [], "diff": {}}

    for op in ops:
        if not isinstance(op, dict):
            skipped.append({"reason": "not_a_dict", "op": str(op)[:120]})
            continue
        # str coerce: a non-string op/name (model emits int/dict) would
        # crash .lower/.strip - caught upstream but silently drops the op.
        kind = str(op.get("op") or "").lower().strip()
        name = str(op.get("name") or "").strip()

        if kind == "add":
            where = op.get("where") or "scene"
            doc = add(
                save_id,
                name,
                where,
                description=op.get("description", ""),
                props=op.get("props") or {},
                emoji=op.get("emoji", ""),
                turn=turn,
                current_location=current_location,
            )
            if doc:
                applied.append({"op": "add", **doc})
            else:
                skipped.append({"reason": "add_failed", "op": op})

        elif kind == "pickup":
            # Look in scene first; if not found, fall back to any holder
            # (handles "I take the dagger from the table" where the DM
            # didn't explicitly track that the dagger was in the scene).
            doc = _find_by_name(save_id, name, holder_filter=SCENE) or _find_by_name(save_id, name)
            if not doc:
                skipped.append({"reason": "pickup_not_found", "name": name})
                continue
            updated = move(doc, PLAYER, current_location=current_location, turn=turn)
            if updated:
                applied.append({"op": "pickup", **updated})

        elif kind == "drop":
            # Source-agnostic: player-held or npc-held -> scene. The DM
            # uses "drop" for both "I drop my torch" and "the bandit drops
            # the loaf"; constraining to player-only forced a `take` then
            # `drop` workflow that the DM rarely emits cleanly.
            doc = _find_by_name(save_id, name)
            if not doc:
                skipped.append({"reason": "drop_not_found", "name": name})
                continue
            updated = move(doc, SCENE, current_location=current_location, turn=turn)
            if updated:
                applied.append({"op": "drop", **updated})

        elif kind == "give":
            # Source-agnostic transfer to a target holder (typically NPC,
            # but allow scene/player too - the DM sometimes uses "give"
            # for "place X in Y's possession" regardless of who held it).
            # If `from` is supplied, restrict the source; otherwise find
            # the item under any holder.
            to = (op.get("to") or "").strip()
            frm = (op.get("from") or "").strip()
            holder_filter = None
            if frm:
                if frm.startswith(NPC_PREFIX):
                    holder_filter = NPC_PREFIX + _normalise_npc_name(frm[len(NPC_PREFIX) :])
                elif frm in (PLAYER, LEGACY_CARRIED):
                    holder_filter = PLAYER
                elif frm == SCENE:
                    holder_filter = SCENE
            doc = _find_by_name(save_id, name, holder_filter=holder_filter)
            if not doc:
                skipped.append({"reason": "give_not_found", "name": name, "from": frm or "any"})
                continue
            updated = move(doc, to or "scene", current_location=current_location, turn=turn)
            if updated:
                applied.append({"op": "give", **updated})

        elif kind == "take":
            # NPC -> player. Item must currently be held by the named NPC.
            frm = (op.get("from") or "").strip()
            if not frm.startswith(NPC_PREFIX):
                skipped.append({"reason": "take_source_not_npc", "from": frm})
                continue
            holder = NPC_PREFIX + _normalise_npc_name(frm[len(NPC_PREFIX) :])
            doc = _find_by_name(save_id, name, holder_filter=holder)
            if not doc:
                skipped.append({"reason": "take_not_found_with_npc", "name": name, "from": frm})
                continue
            updated = move(doc, PLAYER, current_location=current_location, turn=turn)
            if updated:
                applied.append({"op": "take", **updated})

        elif kind == "move":
            # General move: name + to. Resolves source automatically.
            to = op.get("to") or ""
            doc = _find_by_name(save_id, name)
            if not doc:
                skipped.append({"reason": "move_not_found", "name": name})
                continue
            updated = move(doc, to, current_location=current_location, turn=turn)
            if updated:
                applied.append({"op": "move", **updated})

        elif kind == "remove":
            doc = _find_by_name(save_id, name)
            if not doc:
                skipped.append({"reason": "remove_not_found", "name": name})
                continue
            ok = remove(doc)
            if ok:
                applied.append({"op": "remove", "id": str(doc["_id"]), "name": doc.get("name")})

        elif kind == "describe":
            doc = _find_by_name(save_id, name)
            if not doc:
                skipped.append({"reason": "describe_not_found", "name": name})
                continue
            updated = describe(doc, op.get("description", ""), turn=turn)
            if updated:
                applied.append({"op": "describe", **updated})

        else:
            skipped.append({"reason": "unknown_op", "kind": kind, "op": op})

    return {"applied": applied, "skipped": skipped}


# system-prompt formatting
def format_block(scene: list[dict], carried: list[dict], npcs: Optional[dict] = None) -> str:
    """Render the items the DM should know about + the schema for
    mutating them. Always emits the schema line so the DM can seed
    new items even on an empty scene (chicken-and-egg avoidance)."""
    npcs = npcs or {}

    def fmt_item(it: dict) -> str:
        em = _kind_word(it.get("emoji"), it)
        d = it.get("description") or ""
        return f"  {em} {it['name']}" + (f" - {d}" if d else "")

    # Rule block first (before the state listing) so the model reads
    # "when an item moves, emit an op" before it reads the current
    # inventory and starts narrating against it.
    lines = [
        "[ITEMS - INVENTORY TRACKING]",
        "MANDATORY: any time the narrative shows an item changing hands, "
        "being created, dropped, picked up, given, taken, or destroyed, "
        "you MUST include an `items` array in state_delta with the "
        "matching op(s). Narrative without the matching op = bug.",
        "",
        "ITEMS are GRASPABLE OBJECTS only - swords, lanterns, pouches, "
        "scrolls, coins, herbs. NPCs (people, robots, animals, conjured "
        "constructs, animal companions) are NOT items - track NPCs in "
        "npc_dispositions; they auto-exist as item holders the moment a "
        "give/take/move op references them.",
        "",
        "Op vocabulary:",
        '  {"op":"add",      "name":"...", "where":"scene"|"carried"|"npc:<name>",',
        '                     "emoji":"weapon|shield|armor|bow|container|coin|scroll|key|jewel|food|herb|stone|rope|light|potion|staff|construct|item",',
        '                     "description":"...", "props":{}}',
        '  {"op":"pickup",   "name":"..."}                    # scene -> player',
        '  {"op":"drop",     "name":"..."}                    # player -> scene',
        '  {"op":"give",     "name":"...", "to":"npc:<name>", "from":"player|npc:<name>"}  # from optional',
        '  {"op":"take",     "name":"...", "from":"npc:<name>"}  # NPC -> player',
        '  {"op":"move",     "name":"...", "to":"player"|"scene"|"npc:<name>"}',
        '  {"op":"remove",   "name":"..."}                    # destroys / consumes',
        '  {"op":"describe", "name":"...", "description":"..."}',
        "",
        "Use spaces in item names ('brass lantern', not 'brass_lantern'). "
        "Set emoji to one of the kind words above so the inventory panel "
        "renders an icon. NPC names in holders are lowercase_with_underscores "
        "('npc:robot_one', 'npc:grey_bearded_sergeant').",
        "",
        "Current inventory state:",
    ]
    any_listed = False
    if carried:
        lines.append("Carried by player:")
        for it in carried:
            lines.append(fmt_item(it))
        any_listed = True
    if scene:
        lines.append("In this scene (on ground / nearby):")
        for it in scene:
            lines.append(fmt_item(it))
        any_listed = True
    if npcs:
        lines.append("Held by NPCs:")
        for npc_name, items in npcs.items():
            lines.append(f"  npc:{npc_name}")
            for it in items:
                lines.append("  " + fmt_item(it))
        any_listed = True
    if not any_listed:
        lines.append("  (nothing tracked yet - emit add ops as items appear)")
    lines.append("[/ITEMS]")
    return "\n".join(lines)
