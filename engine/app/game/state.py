"""Game save model - pack-driven state schema.

WorldState wraps a dict whose shape is defined by the loaded lore pack's
state_schema.json. Engine-marked fields (location, active_threats,
active_quests, npc_dispositions, dm_notes) are present in every pack.

Ergonomic dotted access (state.location, state.population) goes through
__getattr__/__setattr__; missing fields raise AttributeError. Use .get
when a field may not exist for the current pack.
"""

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from app.game.lore import DEFAULT_PACK_ID


_STATE_FIELDS_ATTR = "_fields"


class WorldState:
    """Dict-backed mutable state container.

    Construct with kwargs: WorldState(location="forest", population=340).
    Or from a dict:        WorldState(**existing_dict).
    """

    __slots__ = ("_fields",)

    def __init__(self, **kwargs: Any):
        object.__setattr__(self, _STATE_FIELDS_ATTR, dict(kwargs))

    # ergonomic dotted access
    def __getattr__(self, name: str) -> Any:
        # Called only when normal lookup fails. Block dunders and underscored
        # names so deepcopy / pickle / repr behave correctly.
        if name.startswith("_"):
            raise AttributeError(name)
        fields = object.__getattribute__(self, _STATE_FIELDS_ATTR)
        if name in fields:
            return fields[name]
        raise AttributeError(f"WorldState has no field {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            object.__getattribute__(self, _STATE_FIELDS_ATTR)[name] = value

    def __repr__(self) -> str:
        return f"WorldState({object.__getattribute__(self, _STATE_FIELDS_ATTR)!r})"

    # dict-like
    def get(self, name: str, default: Any = None) -> Any:
        return object.__getattribute__(self, _STATE_FIELDS_ATTR).get(name, default)

    def to_dict(self) -> dict:
        return dict(object.__getattribute__(self, _STATE_FIELDS_ATTR))

    def keys(self):
        return object.__getattribute__(self, _STATE_FIELDS_ATTR).keys()

    # delta application - typed against pack schema
    def apply_delta(self, delta: dict, schema: Optional[list[dict]] = None) -> dict:
        """Apply a STATE_DELTA dict to this state. Returns a {field: {before, after}} diff.

        `schema` is the pack's field definition list (from state_schema.json).
        When given, deltas are validated against declared types and any
        `monotonic_increase` constraint. When None, all keys present in the
        current state are accepted with type-inferred coercion.
        """
        diff: dict = {}
        fields = object.__getattribute__(self, _STATE_FIELDS_ATTR)

        if schema:
            specs = {f["name"]: f for f in schema}
        else:
            specs = {k: {"name": k, "type": _infer_type(v)} for k, v in fields.items()}

        for key, value in delta.items():
            spec = specs.get(key)
            if not spec:
                continue
            # `npcs` is the engine-managed structured NPC store
            # (dict-of-NPC.to_dict). LLM-side deltas would otherwise
            # overwrite it with freeform descriptions; reject any write
            # to this field from the delta path. The engine writes to
            # state.npcs directly via app.game.npcs.put_npc.
            # engine-owned via engine.py / api.turn gate handlers; never LLM/delta-writable
            # (awaiting_reply: post-turn NPC question tracking; pending_input: gate-side draft store)
            if key in ("npcs", "awaiting_reply", "pending_input"):
                continue
            ftype = spec.get("type", "str")
            try:
                if ftype == "int":
                    value = int(value)
                elif ftype == "str":
                    value = str(value)
                elif ftype == "list" and not isinstance(value, list):
                    continue
                elif ftype == "dict" and not isinstance(value, dict):
                    continue
            except (ValueError, TypeError):
                continue

            # day_segment must stay on-enum: snap LLM drift ("late morning"
            # -> "morning") and drop anything unmappable rather than storing
            # garbage the world-systems layer can't index.
            if key == "day_segment":
                norm = _normalize_day_segment(value)
                if norm is None:
                    continue
                value = norm

            old = fields.get(key)
            if (
                spec.get("monotonic_increase")
                and isinstance(old, int)
                and isinstance(value, int)
                and value < old
            ):
                continue
            if old == value:
                continue
            fields[key] = value
            diff[key] = {"before": old, "after": value}
        return diff


# Mirror of world_systems.DAY_SEGMENTS (kept inline to avoid a state->
# world_systems import cycle; both must stay in sync - 8 stable values).
_DAY_SEGMENTS = ("dawn", "morning", "midday", "afternoon", "evening", "dusk", "night", "deep_night")


def _normalize_day_segment(value: Any) -> Optional[str]:
    """Snap an LLM-emitted day_segment to a valid enum member. Returns the
    snapped value, or None if unmappable (caller skips the write so the
    state never holds an off-enum value like 'late morning')."""
    raw = str(value).strip().lower()
    s = raw.replace(" ", "_").replace("-", "_")
    if s in _DAY_SEGMENTS:
        return s
    for seg in _DAY_SEGMENTS:  # 'late_morning' -> 'morning'
        if seg in s:
            return seg
    return None


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "int"
    if isinstance(value, int):
        return "int"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "str"


@dataclass
class CharacterProfile:
    name: str = "nameless"
    appearance: str = "unremarkable"
    notes: str = ""
    gender: str = "m"  # m | f - required for gendered languages (he, etc.)
    # English default = m for irrelevance; Hebrew uses
    # this to pick the right gendered variant.


@dataclass
class GameConfig:
    difficulty: str = "normal"  # easy | normal | hard
    world_type: str = "canon"  # canon | custom | freeform
    world_premise: str = ""  # JSON string for custom worlds
    tier: str = "basic"  # plain config value; escalation.py reads "pro" and "turbo"
    pack_id: str = DEFAULT_PACK_ID  # lore pack id - selects the content layer
    locale: str = "en"  # display language - en | he
    # the player wakes with exactly one retained memory. Either the
    # player provided it at save creation (free-text field), or
    # engine.create_save generated one with a tiny haiku call. Threaded
    # into the opening narrative + held sacred across the campaign by a
    # writing rule. Empty string means "DM hasn't picked one yet" -
    # treated as a no-op anchor (legacy saves stay valid).
    one_memory: str = ""
    # structured custom-world intake form. Frontend collapses
    # these into the labelled `world_hint` string we send to haiku, but
    # we persist the original dict here so a replay can see what
    # the player asked for. Empty dict means "legacy free-text path"
    # or "default world" - both still valid.
    world_hint_form: dict = field(default_factory=dict)
    # first-mention NPC appearance restraint. "restrained" injects
    # the appearance_restraint writing rule (no physical-attractiveness
    # language on first NPC mention, any gender; lore-relevant details
    # still allowed). "free" skips the restraint entirely.
    # Default restrained for the safer floor.
    appearance_descriptors_first_mention: str = "restrained"
    # per-save policy for fast->strong model escalation on ability turns.
    #   "ask" - engine pauses, frontend shows a consent modal (default
    #             for every tier value except pro and turbo).
    #   "auto" - escalate without asking (default for pro/turbo).
    #   "never" - always run on haiku, even ability-classed turns.
    # Empty/invalid values resolve via normalise_policy at call time.
    ability_escalation: str = ""
    # Diceless-by-default: in a pack that ships a physicalisation template
    # the dice gate is suppressed unless the player opts in here. Dice-declaring
    # packs ignore this (they always dice). Default False -> diceless in a
    # diceless-default pack. Legacy saves lack the key -> default (no migration).
    dice_mechanics: bool = False


UNDO_CAP = int(os.environ.get("UNDO_CAP", "25"))


@dataclass
class GameSave:
    name: str = "untitled"
    profile: CharacterProfile = field(default_factory=CharacterProfile)
    state: WorldState = field(default_factory=WorldState)
    config: GameConfig = field(default_factory=GameConfig)
    history: list = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    turn: int = 0
    undo_stack: list = field(default_factory=list)
    checkpoint_state: dict = field(default_factory=dict)
    checkpoint_history: list = field(default_factory=list)
    checkpoint_turn: int = 0
    checkpoint_len: Optional[int] = None
    session_in: int = 0
    session_out: int = 0
    # Per-save entity glossary, kept to hold NPC names / locations
    # consistent. Empty by default.
    glossary: dict = field(default_factory=dict)
    # anti-automation: shadow-fork fields. read_only=True means this
    # save is a recovered fork handed back for inspection only - turns are
    # hard-gated. shadow_of is the mainline save_id this fork branched from
    # (None on all normal saves).
    read_only: bool = False
    shadow_of: Optional[str] = None
    # per-save baked seed-roster - immutable NPC identity layer injected
    # into the stable prompt prefix to pad it past the prompt cache
    # floor. Shape: {"player_kit": {...}, "npcs": [identity_dict, ...]}.
    # Initialised from pack canon at create_save; may grow via maybe_rebake_roster
    # when established runtime NPCs cross the canon threshold.
    # Empty dict on legacy saves until the first-turn backfill in run_turn.
    seed_roster: dict = field(default_factory=dict)


def make_world_state(difficulty: str, config: Optional[GameConfig] = None) -> WorldState:
    """Build the initial state for a new save by reading the pack's
    starting_state.json (with difficulty patches)."""
    # Lazy import to avoid circular dependency at module load.
    from app.game import lore as lore_pack

    pack_id = (config and config.pack_id) or lore_pack.DEFAULT_PACK_ID
    pack = lore_pack.load_pack(pack_id)
    initial = lore_pack.starting_state_for(pack, difficulty)
    return WorldState(**initial)


def save_to_doc(gs: GameSave) -> dict:
    doc = {
        "name": gs.name,
        "profile": asdict(gs.profile),
        "state": gs.state.to_dict(),
        "config": asdict(gs.config),
        "history": gs.history,
        "created": gs.created,
        "updated": gs.updated,
        "turn": gs.turn,
        "undo_stack": gs.undo_stack,
        "checkpoint_state": gs.checkpoint_state,
        "checkpoint_history": gs.checkpoint_history,
        "checkpoint_turn": gs.checkpoint_turn,
        "session_in": gs.session_in,
        "session_out": gs.session_out,
        "glossary": gs.glossary,
        "read_only": gs.read_only,
        "shadow_of": gs.shadow_of,
        "seed_roster": gs.seed_roster,
    }
    if gs.checkpoint_len is not None:
        doc["checkpoint_len"] = gs.checkpoint_len
    return doc


def doc_to_save(doc: dict) -> GameSave:
    gs = GameSave()
    gs.name = doc.get("name", "untitled")
    gs.profile = CharacterProfile(**doc.get("profile", {}))
    gs.state = WorldState(**doc.get("state", {}))
    gs.config = GameConfig(**doc.get("config", {}))
    gs.history = doc.get("history", [])
    gs.created = doc.get("created", "")
    gs.updated = doc.get("updated", "")
    gs.turn = doc.get("turn", 0)
    gs.undo_stack = doc.get("undo_stack") or []
    if not gs.undo_stack and doc.get("previous_state"):
        # migrate single-step previous into stack
        gs.undo_stack = [
            {
                "state": doc.get("previous_state") or {},
                "history": doc.get("previous_history") or [],
                "turn": max(0, gs.turn - 1),
            }
        ]
    gs.checkpoint_state = doc.get("checkpoint_state", {})
    gs.checkpoint_history = doc.get("checkpoint_history", [])
    gs.checkpoint_turn = doc.get("checkpoint_turn", 0)
    gs.checkpoint_len = doc.get("checkpoint_len")
    for snap in gs.undo_stack:
        if (
            isinstance(snap, dict)
            and not isinstance(snap.get("history"), list)
            and "history_len" in snap
        ):
            if not isinstance(gs.history, list):
                raise ValueError("invalid history")
            n = snap["history_len"]
            if type(n) is not int or not 0 <= n <= len(gs.history):
                raise ValueError("invalid history_len")
    if gs.checkpoint_len is not None:
        if not isinstance(gs.history, list):
            raise ValueError("invalid history")
        n = gs.checkpoint_len
        if type(n) is not int or not 0 <= n <= len(gs.history):
            raise ValueError("invalid checkpoint_len")
    gs.session_in = doc.get("session_in", 0)
    gs.session_out = doc.get("session_out", 0)
    gs.glossary = doc.get("glossary") or {}
    gs.read_only = bool(doc.get("read_only", False))
    gs.shadow_of = doc.get("shadow_of", None)
    # seed_roster - empty dict on legacy saves; backfill in run_turn.
    gs.seed_roster = doc.get("seed_roster") or {}
    return gs
