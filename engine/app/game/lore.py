"""Lore pack loader.

Reads a pack directory (pack.json + ancillary files), validates the manifest,
caches the result in process. Packs live under /app/engine/lore/ in the
container (copied in from the repo's engine/lore/).

A pack is identified by its directory name, which must match `pack.json.id`.
The engine references packs by id; the loader resolves that to a directory.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)

LORE_ROOT = Path(os.environ.get("LORE_ROOT", "/app/engine/lore"))
SCHEMA_VERSION_SUPPORTED = {1}

DEFAULT_PACK_ID = os.environ.get("DEFAULT_PACK_ID") or "neon_rain"


_cache: dict[str, dict] = {}


class PackError(Exception):
    pass


def list_packs() -> list[dict]:
    """Return summary of every loadable pack."""
    if not LORE_ROOT.exists():
        return []
    out = []
    for entry in sorted(LORE_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "pack.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text())
            out.append(
                {
                    "id": data.get("id", entry.name),
                    "name": data.get("name", entry.name),
                    "version": data.get("version", "?"),
                    "description": data.get("description", ""),
                }
            )
        except Exception as e:
            log.warning("lore.list_packs: skipping %s - %s", entry.name, e)
    return out


def load_pack(pack_id: str) -> dict:
    """Load and cache a pack. Returns the pack dict with manifest + state_schema
    + starting_state + resolved file paths. Raises PackError on validation fail."""
    if not pack_id:
        pack_id = DEFAULT_PACK_ID
    if pack_id in _cache:
        return _cache[pack_id]

    pack_dir = LORE_ROOT / pack_id
    if not pack_dir.is_dir():
        raise PackError(f"pack directory not found: {pack_dir}")

    manifest_path = pack_dir / "pack.json"
    if not manifest_path.exists():
        raise PackError(f"pack.json missing in {pack_dir}")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") not in SCHEMA_VERSION_SUPPORTED:
        raise PackError(
            f"unsupported schema_version {manifest.get('schema_version')}"
            f"- supported: {SCHEMA_VERSION_SUPPORTED}"
        )
    if manifest.get("id") != pack_id:
        raise PackError(
            f"pack.json id={manifest.get('id')!r} doesn't match directory name {pack_id!r}"
        )

    schema_path = pack_dir / "state_schema.json"
    if not schema_path.exists():
        raise PackError(f"state_schema.json missing in {pack_dir}")
    state_schema = json.loads(schema_path.read_text())
    if "fields" not in state_schema:
        raise PackError("state_schema.json missing 'fields' array")

    starting_path = pack_dir / "starting_state.json"
    starting = {}
    if starting_path.exists():
        starting = json.loads(starting_path.read_text())

    # validate + normalize the optional beats / pipeline_overrides /
    # state_bindings sections. Templates resolve to inline text here so
    # turn-time never touches disk. A malformed pack fails at load - it
    # must never get as far as a player's turn.
    from app.game import pack_beats as pack_beats_mod

    try:
        pack_beats_norm = pack_beats_mod.validate_pack(manifest, pack_dir)
    except pack_beats_mod.PackBeatsError as e:
        raise PackError(f"pack beats invalid in {pack_id}: {e}")

    # optional seed-roster - identity-only NPC canon for stable prefix
    # padding. Load and validate at pack-load time; fail-open (missing or
    # malformed roster leaves seed_roster as {}, pack still loads normally).
    seed_roster: dict = {}
    seed_roster_rel = manifest.get("seed_roster_file")
    if seed_roster_rel:
        seed_roster_path = pack_dir / seed_roster_rel
        if seed_roster_path.exists():
            try:
                raw_roster = json.loads(seed_roster_path.read_text())
                if isinstance(raw_roster, dict):
                    # Validate basic shape: player_kit dict + npcs list with
                    # required identity fields. Warn and skip malformed entries;
                    # never raise (a bad roster must not prevent the pack loading).
                    required_npc_fields = {
                        "npc_id",
                        "name",
                        "appearance",
                        "archetype",
                        "backstory",
                        "origin_alignment",
                        "core_personality",
                    }
                    valid_npcs = []
                    for entry in raw_roster.get("npcs") or []:
                        missing = required_npc_fields - set(entry.keys())
                        if missing:
                            log.warning(
                                "lore.load_pack[%s]: seed NPC %r missing fields %s - skipped",
                                pack_id,
                                entry.get("npc_id", "?"),
                                missing,
                            )
                            continue
                        valid_npcs.append(entry)
                    seed_roster = {
                        "player_kit": raw_roster.get("player_kit") or {},
                        "npcs": valid_npcs,
                    }
            except Exception as _e:
                log.warning(
                    "lore.load_pack[%s]: seed_roster load failed (%s) - continuing without",
                    pack_id,
                    _e,
                )

    pack = {
        "id": pack_id,
        "manifest": manifest,
        "state_schema": state_schema,
        "starting_state": starting,
        "dir": str(pack_dir),
        "pack_beats": pack_beats_norm,
        "seed_roster": seed_roster,
    }
    _cache[pack_id] = pack
    return pack


def read_template(pack: dict, rel_path: str) -> Optional[str]:
    """Read a template file relative to the pack directory. Returns None if missing."""
    if not rel_path:
        return None
    full = Path(pack["dir"]) / rel_path
    if not full.exists():
        return None
    return full.read_text()


def get_template(pack: dict, slot: str, locale: str = "en", gender: str = "m") -> Optional[str]:
    """Look up a template by manifest slot name with locale + gender resolution.

    For slot "opening" with locale="he" and gender="m", we try in order:
        opening.he.m.md -> opening.he.f.md (fallback) -> opening.md (English)
    The base path comes from the manifest; the suffix variants are derived
    by inserting `.{locale}` (and optionally `.{gender}`) before the
    extension.
    """
    rel_base = (pack["manifest"].get("templates") or {}).get(slot)
    if not rel_base:
        return None

    # If locale is en (default), use the base file directly.
    if locale == "en":
        return read_template(pack, rel_base)

    # Otherwise try locale+gender, then locale-only, then fall back to base.
    base_path = Path(rel_base)
    stem = base_path.stem  # e.g. "opening"
    suffix = base_path.suffix or ".md"  # e.g. ".md"
    parent = base_path.parent

    candidates = [
        parent / f"{stem}.{locale}.{gender}{suffix}",
        parent / f"{stem}.{locale}.m{suffix}",  # masc as Hebrew default
        parent / f"{stem}.{locale}{suffix}",
        rel_base,  # English fallback
    ]
    for c in candidates:
        text = read_template(pack, str(c))
        if text is not None:
            return text
    return None


def get_monster_directive(pack: dict, level: int) -> Optional[str]:
    """Return the directive text for a given monster level, or None if disabled
    or out of range."""
    monster = pack["manifest"].get("monster") or {}
    if not monster.get("enabled"):
        return None
    # A pack author may typo `directives` as a non-dict - never
    # let that 500 a live turn; degrade to "no directive" instead.
    directives = monster.get("directives")
    if not isinstance(directives, dict):
        return None
    rel = directives.get(str(level))
    if not rel:
        return None
    return read_template(pack, rel)


def get_classifier_keywords(pack: dict) -> Optional[set[str]]:
    """Read keyword pre-gate list, one per line. None if not provided."""
    cls = pack["manifest"].get("classifier") or {}
    rel = cls.get("keywords_file")
    if not rel:
        return None
    full = Path(pack["dir"]) / rel
    if not full.exists():
        return None
    keywords = set()
    for line in full.read_text().splitlines():
        word = line.strip().lower()
        if word and not word.startswith("#"):
            keywords.add(word)
    return keywords


def _engine_fragment_value(pack: Optional[dict], name: str):
    frags = ((pack or {}).get("manifest") or {}).get("engine_fragments")
    return frags.get(name) if isinstance(frags, dict) else None


def engine_fragment(pack: Optional[dict], name: str) -> str:
    """Optional pack text the engine splices into one of its own prompt
    lines (manifest `engine_fragments`). Empty when the pack has none."""
    value = _engine_fragment_value(pack, name)
    return value if isinstance(value, str) else ""


def engine_fragment_list(pack: Optional[dict], name: str) -> tuple[str, ...]:
    """List-valued engine fragment, lower-cased. Empty when absent."""
    value = _engine_fragment_value(pack, name)
    if not isinstance(value, list):
        return ()
    return tuple(v.lower() for v in value if isinstance(v, str) and v)


def state_field_defs(pack: dict) -> list[dict]:
    """List of {name, type, default, engine, monotonic_increase} dicts."""
    return pack["state_schema"]["fields"]


def money_denominations(pack: dict) -> dict[str, dict]:
    """pack-driven currency denominations.

    Returns a dict keyed by canonical denomination id, each value:
      {"label": str, "display_order": int}

    Coherence pack ships gold/silver/copper; a sci-fi pack overrides
    with credits, etc. Packs without a `money_denominations` block
    return an empty dict - money tracking is implicitly off for them.

    Money values live in gs.state.money as a flat dict (e.g.
    {"gold": 47, "silver": 12}). The denomination spec is for display
    only - values not in the spec still survive in state across pack
    changes; they just don't render. This means if a sci-fi pack save
    gets loaded by a coherence pack (or vice versa), no data is lost,
    only the status-bar render adapts to the active pack's spec."""
    return dict(pack.get("state_schema", {}).get("money_denominations") or {})


def starting_state_for(pack: dict, difficulty: str = "normal") -> dict:
    """Compose the initial state dict by overlaying difficulty patch on default,
    falling back to schema defaults for any missing fields."""
    defaults = {f["name"]: f.get("default") for f in state_field_defs(pack)}
    defaults.update((pack["starting_state"] or {}).get("default", {}) or {})
    defaults.update((pack["starting_state"] or {}).get(difficulty, {}) or {})
    return defaults


def get_seed_roster(pack: dict) -> dict:
    """Return the seed-roster dict for a pack, or {} if not defined.

    The roster is pre-loaded from seed_roster_file at load_pack time and
    cached under pack["seed_roster"]. Returns the already-loaded value;
    falls back to a live read (tests that call lore.clear_cache and
    reload a pack get a fresh copy naturally)."""
    return dict(pack.get("seed_roster") or {})


def clear_cache() -> None:
    """Drop the in-process pack cache. Used in tests / on hot-reload."""
    _cache.clear()
