"""Pack-declared NPCs that always appear at specific arc/turn windows.

The emergent NPC store lets the DM roll up brand-new characters as
the narrative demands. This module handles the other axis: characters
who exist by design - the named pack-author-decided figures who should
always show up at predictable beats no matter how the player's first
two arcs went.

Design:

Each pack.json may declare a `fixed_characters` array. Each entry:

    {
      "npc_id":   "mira-the-archivist",      # stable key -> state.npcs[npc_id]
      "name":     "Mira",
      "role":     "the archivist",
      "persona":  "Old. Knows where every book is. Suspicious of strangers.",
      "location": "the library",              # optional; DM can re-locate later
      "disposition": 0,                       # optional default toward player
      "triggers": {
        "arc":                1,              # required - exact arc match
        "tutorial_stage_min": 2,              # optional lower bound (inclusive)
        "tutorial_stage_max": 99,             # optional upper bound (inclusive)
        "turn_min":           0,              # optional global-turn lower bound
        "turn_max":           99999           # optional global-turn upper bound
      }
    }

On every turn, before the addressee classifier runs, we check each
declared fixed_character. If its triggers match the current gs.state.arc
+ tutorial_stage + gs.turn and it isn't already in state.npcs, we
upsert it. Subsequent beats see it in the relevance/render path
identical to emergent NPCs from.

Idempotent: if the NPC is already present (because we injected it on a
prior turn, or because the DM independently rolled an NPC with the same
slug), we skip - never reset first_seen_turn or overwrite witnessed events.

Best-effort: a malformed entry logs a warning but never blocks a turn.
The engine.run_turn hook wraps this in try/except for the same reason.
"""

from __future__ import annotations

import logging

from app.game import npcs as npc_mod


log = logging.getLogger("game.fixed_characters")


def _triggers_match(triggers: dict, *, arc: int, tutorial_stage: int, turn: int) -> bool:
    """All declared conditions must hold. Missing keys mean 'no constraint'.
    `arc` is the only required key - a fixed_character with no arc constraint
    would always inject, which would defeat the "by design at arc N" intent."""
    required_arc = triggers.get("arc")
    if required_arc is None or int(required_arc) != int(arc):
        return False

    ts_min = triggers.get("tutorial_stage_min")
    ts_max = triggers.get("tutorial_stage_max")
    if ts_min is not None and int(tutorial_stage) < int(ts_min):
        return False
    if ts_max is not None and int(tutorial_stage) > int(ts_max):
        return False

    t_min = triggers.get("turn_min")
    t_max = triggers.get("turn_max")
    if t_min is not None and int(turn) < int(t_min):
        return False
    if t_max is not None and int(turn) > int(t_max):
        return False

    return True


def _record_to_npc(entry: dict) -> npc_mod.NPC | None:
    """Validate + coerce a pack entry into an NPC dataclass.
    Returns None for unusable entries (logged)."""
    try:
        name = (entry.get("name") or "").strip()
        if not name:
            log.warning("fixed_characters: entry missing 'name': %r", entry)
            return None
        # npc_id may be explicit or derived from name+role (matches slug rule).
        npc_id = (entry.get("npc_id") or "").strip()
        if not npc_id:
            npc_id = npc_mod.slug_for(name, entry.get("role", ""))
        return npc_mod.NPC(
            npc_id=npc_id,
            name=name,
            role=(entry.get("role") or "").strip(),
            persona=(entry.get("persona") or "").strip(),
            location=(entry.get("location") or "").strip(),
            disposition=max(-100, min(100, int(entry.get("disposition") or 0))),
        )
    except Exception as e:
        log.warning("fixed_characters: bad entry %r -> %s", entry, e)
        return None


def inject_fixed_for_turn(gs, pack: dict) -> dict:
    """Walk pack.fixed_characters; inject any whose triggers match current
    arc/tutorial_stage/turn and who aren't already in state.npcs.

    Returns a summary {checked, injected, skipped_existing, skipped_trigger,
    skipped_invalid} for caller logging."""
    summary = {
        "checked": 0,
        "injected": 0,
        "skipped_existing": 0,
        "skipped_trigger": 0,
        "skipped_invalid": 0,
    }

    entries = pack.get("fixed_characters") or []
    if not isinstance(entries, list):
        log.warning("fixed_characters: pack section is not a list - skipping")
        return summary

    state = gs.state
    arc = int(getattr(state, "arc", 1) or 1)
    tutorial_stage = int(getattr(state, "tutorial_stage", 0) or 0)
    turn = int(getattr(gs, "turn", 0) or 0)

    existing = npc_mod.get_npcs(state)

    for entry in entries:
        summary["checked"] += 1
        if not isinstance(entry, dict):
            summary["skipped_invalid"] += 1
            continue

        npc = _record_to_npc(entry)
        if npc is None:
            summary["skipped_invalid"] += 1
            continue

        triggers = entry.get("triggers") or {}
        if not _triggers_match(triggers, arc=arc, tutorial_stage=tutorial_stage, turn=turn):
            summary["skipped_trigger"] += 1
            continue

        if npc.npc_id in existing:
            summary["skipped_existing"] += 1
            continue

        # First-seen-turn captures when the player effectively encounters
        # them. last_seen_turn stays 0 until ingest_narration spots them
        # in a DM response.
        npc.first_seen_turn = turn
        npc_mod.put_npc(state, npc)
        summary["injected"] += 1
        log.info(
            "fixed_characters: injected npc_id=%s name=%s arc=%s turn=%s",
            npc.npc_id,
            npc.name,
            arc,
            turn,
        )

    return summary
