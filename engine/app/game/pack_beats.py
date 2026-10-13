"""Pack-declared beats, pipeline overrides and state bindings.

The doctrine: the engine is a neutral platform; packs author mechanics.
This module is the substrate - three optional pack.json sections let a
lore pack hook the turn pipeline without engine releases:

  beats[]              type A - conditional prompt injections. When the
                       trigger holds, a PackTriggerBeat appends a
                       directive block the DM beat must honour.
  pipeline_overrides[] type B - gate the optional engine beats
                       (grounding / npc_react / encounter) off entirely
                       or per-condition.
  state_bindings[]     the binding layer - threshold->consequence rules
                       evaluated once per turn before the craft/dice
                       gates. Effects: force a pack beat to fire, or
                       modify assessed DCs while the condition holds.
                       This is how a pack authors its own fail-state
                       feel (death-at-0, sanity spirals, collapse).

Validation runs at pack load (lore.load_pack) and fails loudly via
PackBeatsError - a malformed pack must never get as far as a player
turn. Evaluation at turn time never raises: a condition that can't be
resolved is False (log.warning), because a pack bug must not eat a
player's turn.

Pack-declared LLM call specs are not supported.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


class PackBeatsError(Exception):
    """Raised at validation time only. lore.load_pack wraps into PackError."""


# schema constants

_OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists", "regex"}
_NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
# Engine beats a pack may override. The narrative spine (dm_world_step /
# dm_act) and the roll-resolution beat (craft) are deliberately not
# overridable: a pack that silenced the DM would produce empty turns,
# and a craft roll the player paid for must always resolve.
_OVERRIDABLE = {"grounding", "npc_react", "encounter"}
_MODES = {"off", "only_when", "skip_when"}
_FIELD_ROOTS = ("turn", "turn_class", "input")  # besides "state.*"

# Valid profile names for length_profiles override map (mirrors PROFILE_NAMES
# in beats/base.py - these must stay in sync if profiles are added).
_VALID_PROFILE_NAMES: frozenset[str] = frozenset({"short", "medium", "long", "expo-dump"})

MAX_BEATS, MAX_OVERRIDES, MAX_BINDINGS = 16, 8, 16
MAX_WHEN_DEPTH = 5
MAX_TEMPLATE_BYTES = 8_192
DC_MOD_MIN, DC_MOD_MAX = -10, 10
MAX_TIERS = 8
DELTA_MIN, DELTA_MAX = -100, 100
_DICE_RE = re.compile(r"^(\d{1,2})d(\d{1,3})([+-]\d{1,2})?$")


# validation (load time, fail fast)


def validate_pack(manifest: dict, pack_dir: Path) -> dict:
    """Validate + normalize the three pack-beat sections. Returns
    {"beats": [...], "overrides": [...], "bindings": [...]} with beat
    template text resolved inline (so turn-time never touches disk).
    All three sections optional; empty pack -> empty dict values."""
    beats = manifest.get("beats") or []
    overrides = manifest.get("pipeline_overrides") or []
    bindings = manifest.get("state_bindings") or []
    for name, section, cap in (
        ("beats", beats, MAX_BEATS),
        ("pipeline_overrides", overrides, MAX_OVERRIDES),
        ("state_bindings", bindings, MAX_BINDINGS),
    ):
        if not isinstance(section, list):
            raise PackBeatsError(f"{name} must be a list")
        if len(section) > cap:
            raise PackBeatsError(f"{name}: max {cap} entries, got {len(section)}")

    norm_beats: list[dict] = []
    beat_ids: set[str] = set()
    for i, b in enumerate(beats):
        if not isinstance(b, dict):
            raise PackBeatsError(f"beats[{i}] must be an object")
        bid = b.get("id")
        if not bid or not isinstance(bid, str) or not re.fullmatch(r"[a-z0-9_]{1,48}", bid):
            raise PackBeatsError(f"beats[{i}].id must be snake_case <=48 chars, got {bid!r}")
        if bid in beat_ids:
            raise PackBeatsError(f"duplicate beat id {bid!r}")
        beat_ids.add(bid)
        when = b.get("when")
        if when is not None:
            _validate_when(when, f"beats[{i}].when")
        inject = b.get("inject")
        if not isinstance(inject, dict):
            raise PackBeatsError(f"beats[{i}].inject must be an object")
        has_tpl, has_txt = "template" in inject, "text" in inject
        if has_tpl == has_txt:
            raise PackBeatsError(f"beats[{i}].inject needs exactly one of template|text")
        if has_txt:
            text = inject["text"]
            if not isinstance(text, str) or not text.strip():
                raise PackBeatsError(f"beats[{i}].inject.text must be a non-empty string")
        else:
            rel = inject["template"]
            if not isinstance(rel, str) or not rel:
                raise PackBeatsError(f"beats[{i}].inject.template must be a path string")
            full = (Path(pack_dir) / rel).resolve()
            if not str(full).startswith(str(Path(pack_dir).resolve())):
                raise PackBeatsError(f"beats[{i}].inject.template escapes the pack dir: {rel}")
            if not full.is_file():
                raise PackBeatsError(f"beats[{i}].inject.template not found: {rel}")
            raw = full.read_bytes()
            if len(raw) > MAX_TEMPLATE_BYTES:
                raise PackBeatsError(
                    f"beats[{i}].inject.template {rel}: {len(raw)}B exceeds {MAX_TEMPLATE_BYTES}B"
                )
            text = raw.decode("utf-8")
        norm_beats.append(
            {
                "id": bid,
                "label": b.get("label") or bid,
                "when": when,
                "text": text.strip(),
            }
        )

    norm_overrides: list[dict] = []
    seen_targets: set[str] = set()
    for i, o in enumerate(overrides):
        if not isinstance(o, dict):
            raise PackBeatsError(f"pipeline_overrides[{i}] must be an object")
        target = o.get("beat")
        if target not in _OVERRIDABLE:
            raise PackBeatsError(
                f"pipeline_overrides[{i}].beat must be one of {sorted(_OVERRIDABLE)}, "
                f"got {target!r} (dm beats + craft are not overridable by design)"
            )
        if target in seen_targets:
            raise PackBeatsError(f"pipeline_overrides: duplicate target {target!r}")
        seen_targets.add(target)
        mode = o.get("mode")
        if mode not in _MODES:
            raise PackBeatsError(
                f"pipeline_overrides[{i}].mode must be one of {sorted(_MODES)}, got {mode!r}"
            )
        when = o.get("when")
        if mode == "off" and when is not None:
            raise PackBeatsError(f"pipeline_overrides[{i}]: mode=off takes no `when`")
        if mode in ("only_when", "skip_when"):
            if when is None:
                raise PackBeatsError(f"pipeline_overrides[{i}]: mode={mode} requires `when`")
            _validate_when(when, f"pipeline_overrides[{i}].when")
        norm_overrides.append({"beat": target, "mode": mode, "when": when})

    norm_bindings: list[dict] = []
    binding_ids: set[str] = set()
    for i, nb in enumerate(bindings):
        if not isinstance(nb, dict):
            raise PackBeatsError(f"state_bindings[{i}] must be an object")
        bid = nb.get("id")
        if not bid or not isinstance(bid, str) or not re.fullmatch(r"[a-z0-9_]{1,48}", bid):
            raise PackBeatsError(f"state_bindings[{i}].id must be snake_case <=48 chars")
        if bid in binding_ids:
            raise PackBeatsError(f"duplicate binding id {bid!r}")
        binding_ids.add(bid)
        when = nb.get("when")
        if when is None:
            raise PackBeatsError(f"state_bindings[{i}] requires `when`")
        _validate_when(when, f"state_bindings[{i}].when")
        then = nb.get("then")
        if not isinstance(then, dict):
            raise PackBeatsError(f"state_bindings[{i}].then must be an object")
        has_fire, has_dc = "fire_beat" in then, "dc_modifier" in then
        if has_fire == has_dc:
            raise PackBeatsError(
                f"state_bindings[{i}].then needs exactly one of fire_beat|dc_modifier"
            )
        if has_fire:
            if then["fire_beat"] not in beat_ids:
                raise PackBeatsError(
                    f"state_bindings[{i}].then.fire_beat references unknown beat "
                    f"{then['fire_beat']!r}"
                )
        else:
            dm = then["dc_modifier"]
            if not isinstance(dm, dict):
                raise PackBeatsError(f"state_bindings[{i}].then.dc_modifier must be an object")
            if dm.get("applies_to") not in ("dice", "craft", "both"):
                raise PackBeatsError(
                    f"state_bindings[{i}].dc_modifier.applies_to must be dice|craft|both"
                )
            mod = dm.get("modifier")
            if not isinstance(mod, int) or mod == 0 or not (DC_MOD_MIN <= mod <= DC_MOD_MAX):
                raise PackBeatsError(
                    f"state_bindings[{i}].dc_modifier.modifier must be a non-zero int "
                    f"in [{DC_MOD_MIN}, {DC_MOD_MAX}]"
                )
        norm_bindings.append({"id": bid, "when": when, "then": then})

    enc = validate_encounter_mechanics(manifest, pack_dir)

    # optional length_profiles override map.
    # Keys = beat-id or turn-type; values = profile name in VALID_PROFILE_NAMES.
    # Fail-LOUD-at-load (PackBeatsError) on any bad value, per module doctrine.
    # The ceiling resolver does not read this map.
    raw_lp = manifest.get("length_profiles")
    norm_lp: dict[str, str] = {}
    if raw_lp is not None:
        if not isinstance(raw_lp, dict):
            raise PackBeatsError("length_profiles must be an object")
        for k, v in raw_lp.items():
            if not isinstance(k, str) or not k:
                raise PackBeatsError(f"length_profiles key must be a non-empty string, got {k!r}")
            if v not in _VALID_PROFILE_NAMES:
                raise PackBeatsError(
                    f"length_profiles[{k!r}]: unknown profile {v!r}; "
                    f"must be one of {sorted(_VALID_PROFILE_NAMES)}"
                )
            norm_lp[k] = v

    return {
        "beats": norm_beats,
        "overrides": norm_overrides,
        "bindings": norm_bindings,
        "encounter_mechanics": enc,
        "length_profiles": norm_lp,
    }


def validate_encounter_mechanics(manifest: dict, pack_dir: Path) -> Optional[dict]:
    """Validate the optional encounter_mechanics section of a pack manifest.

    Returns None when absent. Raises PackBeatsError on any violation
    (fail-LOUD-at-load style per module docstring lines 21-25).
    """
    raw = manifest.get("encounter_mechanics")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PackBeatsError("encounter_mechanics must be an object")

    # dice_notation - default "1d20"
    dice_notation = raw.get("dice_notation", "1d20")
    if not isinstance(dice_notation, str) or not _DICE_RE.fullmatch(dice_notation):
        raise PackBeatsError(
            f"encounter_mechanics.dice_notation {dice_notation!r} is not a valid "
            "dice expression (e.g. '1d20')"
        )

    # engine_rolls - default False
    engine_rolls = raw.get("engine_rolls", False)
    if not isinstance(engine_rolls, bool):
        raise PackBeatsError("encounter_mechanics.engine_rolls must be a bool")

    # outcome_tiers - list, max MAX_TIERS, each {tier, min_margin}
    tiers_raw = raw.get("outcome_tiers", [])
    if not isinstance(tiers_raw, list):
        raise PackBeatsError("encounter_mechanics.outcome_tiers must be a list")
    if len(tiers_raw) > MAX_TIERS:
        raise PackBeatsError(
            f"encounter_mechanics.outcome_tiers: max {MAX_TIERS} entries, got {len(tiers_raw)}"
        )

    tier_names: list[str] = []
    null_seen = False
    for i, t in enumerate(tiers_raw):
        if not isinstance(t, dict):
            raise PackBeatsError(f"encounter_mechanics.outcome_tiers[{i}] must be an object")
        tname = t.get("tier")
        if not tname or not isinstance(tname, str) or not re.fullmatch(r"[a-z0-9_]{1,48}", tname):
            raise PackBeatsError(
                f"encounter_mechanics.outcome_tiers[{i}].tier must be "
                f"snake_case <=48 chars, got {tname!r}"
            )
        if tname in tier_names:
            raise PackBeatsError(f"encounter_mechanics.outcome_tiers: duplicate tier {tname!r}")
        tier_names.append(tname)
        mm = t.get("min_margin")
        if mm is None:
            if null_seen:
                raise PackBeatsError(
                    "encounter_mechanics.outcome_tiers: exactly one null "
                    f"min_margin allowed, found second at index {i}"
                )
            null_seen = True
        else:
            if null_seen:
                raise PackBeatsError(
                    f"encounter_mechanics.outcome_tiers[{i}]: null min_margin must be last"
                )
            if not isinstance(mm, int) or not (-25 <= mm <= 25):
                raise PackBeatsError(
                    f"encounter_mechanics.outcome_tiers[{i}].min_margin must be "
                    f"int in [-25,25] or null, got {mm!r}"
                )

    if tiers_raw and not null_seen:
        raise PackBeatsError(
            "encounter_mechanics.outcome_tiers: the last entry must have null min_margin (floor)"
        )

    # effects - {tier_name: {state_delta: {str: int}, directive?: str}}
    effects_raw = raw.get("effects", {})
    if not isinstance(effects_raw, dict):
        raise PackBeatsError("encounter_mechanics.effects must be an object")
    for tname, ev in effects_raw.items():
        if tier_names and tname not in tier_names:
            raise PackBeatsError(
                f"encounter_mechanics.effects key {tname!r} is not a declared tier"
            )
        if not isinstance(ev, dict):
            raise PackBeatsError(f"encounter_mechanics.effects.{tname} must be an object")
        sd = ev.get("state_delta", {})
        if not isinstance(sd, dict):
            raise PackBeatsError(
                f"encounter_mechanics.effects.{tname}.state_delta must be an object"
            )
        for fname, delta in sd.items():
            if not isinstance(delta, int) or not (DELTA_MIN <= delta <= DELTA_MAX):
                raise PackBeatsError(
                    f"encounter_mechanics.effects.{tname}.state_delta.{fname} "
                    f"must be int in [{DELTA_MIN},{DELTA_MAX}], got {delta!r}"
                )
        directive = ev.get("directive")
        if directive is not None:
            if not isinstance(directive, str):
                raise PackBeatsError(
                    f"encounter_mechanics.effects.{tname}.directive must be a string"
                )
            if len(directive.encode("utf-8")) > MAX_TEMPLATE_BYTES:
                raise PackBeatsError(
                    f"encounter_mechanics.effects.{tname}.directive exceeds "
                    f"{MAX_TEMPLATE_BYTES} bytes"
                )

    # clamps - {field: [lo, hi]}
    clamps_raw = raw.get("clamps", {})
    if not isinstance(clamps_raw, dict):
        raise PackBeatsError("encounter_mechanics.clamps must be an object")
    for fname, bounds in clamps_raw.items():
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise PackBeatsError(f"encounter_mechanics.clamps.{fname} must be [lo, hi]")
        lo, hi = bounds
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            raise PackBeatsError(f"encounter_mechanics.clamps.{fname}: lo and hi must be numbers")
        if lo > hi:
            raise PackBeatsError(f"encounter_mechanics.clamps.{fname}: lo ({lo}) > hi ({hi})")

    return {
        "dice_notation": dice_notation,
        "engine_rolls": engine_rolls,
        "outcome_tiers": tiers_raw,
        "effects": effects_raw,
        "clamps": clamps_raw,
    }


def _validate_when(node: Any, path: str, depth: int = 0) -> None:
    if depth > MAX_WHEN_DEPTH:
        raise PackBeatsError(f"{path}: nesting deeper than {MAX_WHEN_DEPTH}")
    if not isinstance(node, dict):
        raise PackBeatsError(f"{path}: condition must be an object")
    combos = [k for k in ("all", "any", "not") if k in node]
    if combos:
        if len(node) != 1:
            raise PackBeatsError(f"{path}: combinator {combos[0]} must be the only key")
        if combos[0] == "not":
            _validate_when(node["not"], f"{path}.not", depth + 1)
            return
        kids = node[combos[0]]
        if not isinstance(kids, list) or not kids:
            raise PackBeatsError(f"{path}.{combos[0]} must be a non-empty list")
        for j, kid in enumerate(kids):
            _validate_when(kid, f"{path}.{combos[0]}[{j}]", depth + 1)
        return
    if "input_matches" in node:
        if len(node) != 1 or not isinstance(node["input_matches"], str):
            raise PackBeatsError(f"{path}: input_matches must be the only key, a regex string")
        _compile_or_raise(node["input_matches"], path)
        return
    fieldp, op = node.get("field"), node.get("op")
    if not isinstance(fieldp, str) or not (fieldp in _FIELD_ROOTS or fieldp.startswith("state.")):
        raise PackBeatsError(
            f"{path}.field must be 'state.<path>' or one of {_FIELD_ROOTS}, got {fieldp!r}"
        )
    if op not in _OPS:
        raise PackBeatsError(f"{path}.op must be one of {sorted(_OPS)}, got {op!r}")
    if op == "exists":
        if not isinstance(node.get("value"), bool):
            raise PackBeatsError(f"{path}: op=exists takes a boolean value")
    elif op == "regex":
        if not isinstance(node.get("value"), str):
            raise PackBeatsError(f"{path}: op=regex takes a string value")
        _compile_or_raise(node["value"], path)
    elif "value" not in node:
        raise PackBeatsError(f"{path}: missing value")


def _compile_or_raise(pattern: str, path: str) -> None:
    try:
        re.compile(pattern)
    except re.error as e:
        raise PackBeatsError(f"{path}: bad regex {pattern!r}- {e}")


# evaluation (turn time, never raises)


def _resolve_field(fieldp: str, state: dict, turn: int, turn_class: str, player_input: str) -> Any:
    if fieldp == "turn":
        return turn
    if fieldp == "turn_class":
        return turn_class
    if fieldp == "input":
        return player_input
    cur: Any = state
    for part in fieldp[len("state.") :].split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _eval_leaf(node: dict, state: dict, turn: int, turn_class: str, player_input: str) -> bool:
    if "input_matches" in node:
        return re.search(node["input_matches"], player_input or "", re.IGNORECASE) is not None
    val = _resolve_field(node["field"], state, turn, turn_class, player_input)
    op, ref = node["op"], node.get("value")
    if op == "exists":
        return (val is not None) == bool(ref)
    if val is None:
        return False
    if op in _NUMERIC_OPS:
        try:
            v, r = float(val), float(ref)
        except (TypeError, ValueError):
            return False
        return {"gt": v > r, "gte": v >= r, "lt": v < r, "lte": v <= r}[op]
    if op == "eq":
        return val == ref
    if op == "ne":
        return val != ref
    if op == "contains":
        try:
            return ref in val
        except TypeError:
            return False
    if op == "regex":
        return re.search(ref, str(val), re.IGNORECASE) is not None
    return False


def eval_when(
    node: Optional[dict], *, state: dict, turn: int, turn_class: str, player_input: str
) -> bool:
    """Evaluate a (validated) condition tree. None = unconditional True.
    Any runtime surprise resolves to False - a pack bug must never
    break a player's turn."""
    if node is None:
        return True
    try:
        if "all" in node:
            return all(
                eval_when(
                    k, state=state, turn=turn, turn_class=turn_class, player_input=player_input
                )
                for k in node["all"]
            )
        if "any" in node:
            return any(
                eval_when(
                    k, state=state, turn=turn, turn_class=turn_class, player_input=player_input
                )
                for k in node["any"]
            )
        if "not" in node:
            return not eval_when(
                node["not"],
                state=state,
                turn=turn,
                turn_class=turn_class,
                player_input=player_input,
            )
        return _eval_leaf(node, state, turn, turn_class, player_input)
    except Exception as e:
        log.warning("pack_beats.eval_when: %s on %r - treating as False", e, node)
        return False


# state bindings


@dataclass
class BindingEffects:
    forced_beats: set[str] = field(default_factory=set)
    dice_dc_mod: int = 0
    craft_dc_mod: int = 0
    fired: list[str] = field(default_factory=list)  # binding ids, telemetry


def evaluate_bindings(
    pack: Optional[dict], *, state: dict, turn: int, turn_class: str, player_input: str
) -> BindingEffects:
    """Run the pack's state_bindings against this turn's pre-pipeline
    view. Called once per turn by engine.run_turn, before the craft and
    dice gates so dc_modifier effects land on this turn's assessments."""
    fx = BindingEffects()
    spec = (pack or {}).get("pack_beats") or {}
    for b in spec.get("bindings", []):
        if not eval_when(
            b["when"], state=state, turn=turn, turn_class=turn_class, player_input=player_input
        ):
            continue
        fx.fired.append(b["id"])
        then = b["then"]
        if "fire_beat" in then:
            fx.forced_beats.add(then["fire_beat"])
        else:
            dm = then["dc_modifier"]
            if dm["applies_to"] in ("dice", "both"):
                fx.dice_dc_mod += dm["modifier"]
            if dm["applies_to"] in ("craft", "both"):
                fx.craft_dc_mod += dm["modifier"]
    fx.dice_dc_mod = max(DC_MOD_MIN, min(DC_MOD_MAX, fx.dice_dc_mod))
    fx.craft_dc_mod = max(DC_MOD_MIN, min(DC_MOD_MAX, fx.craft_dc_mod))
    if fx.fired:
        log.info(
            "pack_beats: bindings fired %s (dice_mod=%+d craft_mod=%+d forced=%s)",
            fx.fired,
            fx.dice_dc_mod,
            fx.craft_dc_mod,
            sorted(fx.forced_beats),
        )
    return fx


def apply_dc_mod(dc: int, mod: int) -> int:
    """Pack DC modifier, clamped to the engine's 1-25 DC scale."""
    if not mod:
        return dc
    return max(1, min(25, int(dc) + int(mod)))


# the trigger beat


class PackTriggerBeat:
    """Type A - pack-declared prompt injection. Zero LLM calls: when the
    trigger holds (or a binding forced it), append a directive block to
    ts.pack_blocks; the DM beats fold those into the system prompt."""

    def __init__(self, spec: dict, forced: bool = False):
        self.spec = spec
        self.forced = forced
        self.id = f"pack:{spec['id']}"

    def applies(self, ts) -> bool:
        if self.forced:
            return True
        return eval_when(
            self.spec.get("when"),
            state=ts.gs.state.to_dict(),
            turn=ts.gs.turn,
            turn_class=ts.turn_class,
            player_input=ts.player_input,
        )

    def run(self, ts):
        block = f"[PACK BEAT - {self.spec['label']}]\n{self.spec['text']}\n[/PACK BEAT]"
        ts.pack_blocks.append(block)
        ts.record_beat(beat_id=self.id)  # zero-token row keeps by_beat complete
        return ts


# pipeline overrides


class _OverriddenBeat:
    """Wraps an engine beat with a pack condition. only_when: run only
    while the condition holds; skip_when: run unless it holds. The
    wrapped beat's own applies is still honoured first."""

    def __init__(self, beat, mode: str, when: dict):
        self.beat, self.mode, self.when = beat, mode, when
        self.id = getattr(beat, "id", getattr(beat, "beat_id", "?"))

    def applies(self, ts) -> bool:
        if not self.beat.applies(ts):
            return False
        hit = eval_when(
            self.when,
            state=ts.gs.state.to_dict(),
            turn=ts.gs.turn,
            turn_class=ts.turn_class,
            player_input=ts.player_input,
        )
        return hit if self.mode == "only_when" else not hit

    def run(self, ts):
        return self.beat.run(ts)


def _public_name(beat) -> Optional[str]:
    """Stable override-target name for an engine beat instance."""
    return {
        "GroundingBeat": "grounding",
        "NpcReactBeat": "npc_react",
        "EncounterBeat": "encounter",
    }.get(type(beat).__name__)


def compose_pipeline(engine_beats: list, pack: Optional[dict], forced_beat_ids: set[str]) -> list:
    """Compose the pipeline: pack trigger beats run first (they inject
    context the DM beats consume), then the engine beats with the
    pack's pipeline_overrides applied."""
    spec = (pack or {}).get("pack_beats") or {}
    out: list = [
        PackTriggerBeat(b, forced=b["id"] in forced_beat_ids) for b in spec.get("beats", [])
    ]
    overrides = {o["beat"]: o for o in spec.get("overrides", [])}
    for beat in engine_beats:
        o = overrides.get(_public_name(beat) or "")
        if o is None:
            out.append(beat)
        elif o["mode"] == "off":
            continue
        else:
            out.append(_OverriddenBeat(beat, o["mode"], o["when"]))
    return out
