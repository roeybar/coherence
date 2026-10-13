"""Meme recognition for player input.

Purpose: player input can quote a well-known line from outside the
fiction (a film or book catchphrase). Read literally, the DM would take
it as a character claim and narrate around it.

This module rewrites the LLM-facing player input when known
external-canon references match a pack-shipped registry. The
player always sees their original text in chat - only the LLM sees
the rewrite. Original text is preserved in gs.history with no
indication a rewrite happened.

Registry lookups are pure regex; no LLM. Pack-scoped via
lore/<pack>/meme_registry.yaml. Empty / missing registry -> no-op.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml

from app.game.lore import DEFAULT_PACK_ID, LORE_ROOT


log = logging.getLogger(__name__)


@dataclass
class Recognition:
    rewritten: str = ""  # LLM-facing rewrite
    triggered: bool = False
    matches: list[dict] = field(default_factory=list)
    # Each match: {original_span, pattern, reason}


# Cache the compiled registry per pack so we don't re-parse YAML on
# every turn. Invalidate by restarting the api container.
_REGISTRY_CACHE: dict[str, list[dict]] = {}


def _load_registry(pack_id: str) -> list[dict]:
    """Load + compile the pack's meme registry. Returns a list of
    dicts: {compiled_pattern, translation, reason}. Missing file =
    empty list (feature is silently off for that pack)."""
    if pack_id in _REGISTRY_CACHE:
        return _REGISTRY_CACHE[pack_id]
    path = LORE_ROOT / pack_id / "meme_registry.yaml"
    if not path.is_file():
        _REGISTRY_CACHE[pack_id] = []
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("meme_recognition: failed to load %s: %s", path, e)
        _REGISTRY_CACHE[pack_id] = []
        return []
    entries = data.get("entries") or []
    compiled: list[dict] = []
    for e in entries:
        pat = (e or {}).get("pattern")
        tx = (e or {}).get("translation")
        if not pat or not tx:
            continue
        try:
            rx = re.compile(pat, re.IGNORECASE)
        except re.error as exc:
            log.warning("meme_recognition: bad pattern %r in %s: %s", pat, pack_id, exc)
            continue
        compiled.append(
            {
                "rx": rx,
                "translation": tx,
                "reason": (e.get("reason") or "")[:80],
                "pattern": pat,
            }
        )
    _REGISTRY_CACHE[pack_id] = compiled
    return compiled


def recognise(
    text: str, *, pack_id: str = DEFAULT_PACK_ID, player_name: str = "the player"
) -> Recognition:
    """Apply the pack's meme registry. Returns a Recognition.

    On a no-match or empty registry, `rewritten == text` and
    `triggered == False`. On any match, `rewritten` is the text with
    each matched span replaced by the registry translation.

    Substitutions are applied LEFT-TO-RIGHT in source order, then the
    match list reports the original spans (positions in the input).
    """
    if not text:
        return Recognition(rewritten="")
    registry = _load_registry(pack_id)
    if not registry:
        return Recognition(rewritten=text)

    matches: list[dict] = []
    out = text
    # Apply each entry once. Multiple entries can hit different
    # spans of the same input. Apply in registry order; if a later
    # entry's pattern doesn't match the already-rewritten text, it
    # silently drops out.
    for entry in registry:
        m = entry["rx"].search(out)
        if not m:
            continue
        substitution = entry["translation"].format(player_name=player_name)
        new_text = out[: m.start()] + substitution + out[m.end() :]
        matches.append(
            {
                "pattern": entry["pattern"],
                "original": m.group(0),
                "reason": entry["reason"],
            }
        )
        out = new_text

    return Recognition(
        rewritten=out,
        triggered=bool(matches),
        matches=matches,
    )
