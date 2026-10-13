"""NPC store with relevance-filtered context injection.

Key idea: each named NPC has a backend record carrying their
persona, what they've personally witnessed, their relationships,
their disposition toward the player, and their visual colour.
Records are read-only context injected into the main DM beat - NPCs do not make their own LLM calls (yet).

Anti-fabrication is enforced *structurally*: the model speaks for
NPC X only with what's in X's `witnessed` list as the knowledge
envelope. If the bridge isn't in Marta's witnessed list, she can't
fabricate "you don't remember the bridge, do you?" because we
literally don't tell the model she knows about a bridge.

Brain-like recall: store everything (cap 20 witnessed events FIFO),
inject only what's relevant to *this turn* (cap 5 events injected).
Always include the last 2 events involving the player + topic-match
on player input + location-match on current scene. The "other
memories not shown" hint lets the model do callbacks without
fabrication - if the player references something Marta did witness
that the filter excluded, the model knows she could remember it.

Pack-scoped colour assignment for frontend rendering of NPC names.
HSL palette is dark-theme-safe (no yellow). Hash(npc_id) % palette.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# colour palette
# 10 curated HSL hues that read well on the dark theme. Avoids yellow
# (poor contrast) and grey/black (would blend with body text).
COLOUR_PALETTE_HUES = [
    10,  # warm red
    35,  # orange
    55,  # gold-ish
    130,  # green
    170,  # teal
    200,  # sky blue
    220,  # cool blue
    280,  # purple
    310,  # magenta
    340,  # rose
]


def assign_colour_hue(npc_id: str) -> int:
    """Deterministic colour assignment from npc_id. Same NPC always
    gets the same hue across sessions; collisions across different
    NPCs sharing a hue are acceptable at this palette size."""
    h = hashlib.sha256(npc_id.encode()).digest()
    return COLOUR_PALETTE_HUES[h[0] % len(COLOUR_PALETTE_HUES)]


# data shape


WITNESSED_CAP = 20  # FIFO eviction beyond this per-NPC


@dataclass
class NPC:
    npc_id: str
    name: str
    role: str = ""
    persona: str = ""
    location: str = ""
    witnessed: list = field(default_factory=list)
    disposition: int = 0  # -100..+100 toward player
    first_seen_turn: int = 0
    last_seen_turn: int = 0
    color_hue: int = 0
    # Reveal-state triplet. Lets the engine track separately what an NPC has seen, what
    # they believe about the player, and what they will do next.
    # Free-text per slot, capped at 4 entries each. Default empty.
    reveal_state: dict = field(default_factory=dict)
    # Observation-only flag describing physical constraints on the NPC
    # (e.g. "wrists bound", "blindfolded"). never used to refuse
    # player action - the lore physicalisation rule forbids stasis
    # outcomes. Default empty string.
    bound_status: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NPC":
        rev = d.get("reveal_state") or {}
        # Tolerate legacy saves where the field doesn't exist; cap each
        # slot at 4 entries so the prompt block stays bounded.
        rev_clean = {
            "saw_what": [str(x)[:160] for x in (rev.get("saw_what") or [])][:4],
            "believes_what": [str(x)[:160] for x in (rev.get("believes_what") or [])][:4],
            "will_do_what": [str(x)[:160] for x in (rev.get("will_do_what") or [])][:4],
        }
        return cls(
            npc_id=str(d.get("npc_id") or d.get("name") or "").strip(),
            name=str(d.get("name") or "").strip(),
            role=str(d.get("role") or "").strip(),
            persona=str(d.get("persona") or "").strip(),
            location=str(d.get("location") or "").strip(),
            witnessed=list(d.get("witnessed") or [])[:WITNESSED_CAP],
            disposition=max(-100, min(100, int(d.get("disposition") or 0))),
            first_seen_turn=int(d.get("first_seen_turn") or 0),
            last_seen_turn=int(d.get("last_seen_turn") or 0),
            color_hue=int(d.get("color_hue") or 0),
            reveal_state=rev_clean,
            bound_status=str(d.get("bound_status") or "")[:80].strip(),
        )


# slug helpers


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug_for(name: str, role: str = "") -> str:
    """Stable identifier from name (+ optional role disambiguator).
    Used as the key in gs.state.npcs."""
    parts = [(name or "").lower(), (role or "").lower()]
    s = "-".join(_SLUG_RE.sub("-", p).strip("-") for p in parts if p)
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "npc"


# store accessors (operate on gs.state.npcs dict)


def get_npcs(state) -> dict:
    """Return the NPC dict from state, defaulting to {} for legacy
    saves missing the field.

    Defensive: state.npcs is a dict-of-dicts in this module's view, but
    the LLM delta path may write freeform strings/values into the same
    field. Filter to dict values only so downstream readers can assume
    NPC.from_dict-compatible shape."""
    try:
        raw = getattr(state, "npcs", None) or {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        return {}


def put_npc(state, npc: NPC) -> None:
    """Upsert an NPC record. Assigns colour_hue if unset."""
    npcs = get_npcs(state)
    if not npc.color_hue:
        npc.color_hue = assign_colour_hue(npc.npc_id)
    npcs[npc.npc_id] = npc.to_dict()
    state.npcs = npcs


def find_by_name(state, name: str) -> Optional[NPC]:
    """Resolve by exact name (case-insensitive). Returns None if no
    NPC matches. When two NPCs share a name (rare; role-disambiguated
    slugs collide), returns the most-recently-seen one."""
    if not name:
        return None
    target = name.strip().lower()
    npcs = get_npcs(state)
    candidates = [d for d in npcs.values() if str(d.get("name", "")).lower() == target]
    if not candidates:
        return None
    candidates.sort(key=lambda d: int(d.get("last_seen_turn") or 0), reverse=True)
    return NPC.from_dict(candidates[0])


# introduction extractor
# Deterministic regex pass over recent DM narration. Catches "Marta,
# the X" and "his name was Voss" shapes. Imperfect by design -
# false positives are filtered by the role-shape requirement.
# Pure-Python; no LLM call.


# Common stop-words we shouldn't treat as NPC names even if they look
# capitalised at sentence start. Includes direction adverbs and
# prepositions that frequently lead a sentence + ", the X" shape (e.g.
# "Ahead, the cottage stands..." - without "Ahead" here the extractor
# would file "Ahead" as an NPC named after a building).
_STOPWORDS = {
    "The",
    "A",
    "An",
    "You",
    "Your",
    "Yours",
    "I",
    "Me",
    "My",
    "He",
    "She",
    "They",
    "We",
    "It",
    "His",
    "Her",
    "Their",
    "Its",
    "There",
    "Then",
    "But",
    "And",
    "Or",
    "So",
    "If",
    "When",
    "Where",
    "While",
    "Yet",
    "Also",
    "Just",
    "Even",
    "Now",
    "Today",
    "Year",
    "Spring",
    "Summer",
    "Autumn",
    "Winter",
    "Day",
    "Night",
    # Direction / position adverbs
    "Ahead",
    "Behind",
    "Above",
    "Below",
    "Beyond",
    "Beside",
    "Inside",
    "Outside",
    "Around",
    "Across",
    "Along",
    "Beneath",
    "Underneath",
    "Overhead",
    "Nearby",
    "Far",
    "Close",
    "Left",
    "Right",
    "North",
    "South",
    "East",
    "West",
    "Northwest",
    "Northeast",
    "Southwest",
    "Southeast",
    "Up",
    "Down",
    "Forward",
    "Backward",
    "Inward",
    "Outward",
    # Time / sequence
    "Soon",
    "Later",
    "Before",
    "After",
    "Once",
    "Twice",
    "Eventually",
    "Suddenly",
    "Finally",
    "Meanwhile",
    "Earlier",
    "Tomorrow",
    "Yesterday",
    "Tonight",
    # Common sentence-starters that fit "X, the Y" innocently
    "Here",
    "Somewhere",
    "Anywhere",
    "Everywhere",
    "Nowhere",
    "Something",
    "Someone",
    "Nothing",
    "Everything",
    "Everyone",
}


# "Marta, the village healer" / "Aldwin, a tax collector".
# Note: the name group is case-sensitive on purpose - a global
# re.IGNORECASE would defeat the [A-Z] anchor and turn every "longhouse,
# the largest building" into an NPC named "longhouse". The article and
# role descriptor use inline (?i:...) so "The/A/An" at sentence starts
# still match.
_PATTERN_NAMED_ROLE = re.compile(
    r"\b([A-Z][a-z]{1,14})\b\s*,\s*"
    r"(?i:the|a|an)\s+([a-z][a-z\- ]{2,40}?)\b"
    r"(?=[\s,.;:!?]|$)",
)

# "his name was Voss" / "Her name is Marta"
_PATTERN_NAME_WAS = re.compile(
    r"\b(?i:his|her|their|the)\s+name\s+(?i:was|is)\s+"
    r"([A-Z][a-z]{1,14})\b",
)

# player-coined name patterns. Catches the player offering a
# nickname to an NPC in their own input, e.g. "I'll call you Friend",
# "do you mind if I call you Mar", "let's call them Voss". Without
# this, the engine never extracts a name because the DM rolls with
# the player's coined name in narration but the existing patterns
# only fire on DM-introduced names ("Marta, the healer" shapes).
#
# Case-insensitive on the captured name so player's lowercase input
# ("call you friend") still produces a record. Title-cased on
# extraction so the NPC store gets a proper-noun-style name.
_PATTERN_PLAYER_COINED = re.compile(
    r"\b(?:call|name|dub)\s+(?:you|him|her|them|it)\s+"
    r"([A-Za-z][a-zA-Z]{1,14})\b",
    re.IGNORECASE,
)


def extract_introductions(narration: str) -> list[dict]:
    """Find probable NPC introductions in DM narration. Returns
    list of {name, role, persona_seed} dicts. Deduplicated by
    case-insensitive name."""
    if not narration:
        return []
    seen: dict[str, dict] = {}

    for m in _PATTERN_NAMED_ROLE.finditer(narration):
        name = (m.group(1) or "").strip()
        role = (m.group(2) or "").strip()
        if not name or name in _STOPWORDS:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen[key] = {
            "name": name,
            "role": role[:60],
            "persona_seed": f"{name}, {role}".strip(),
        }

    for m in _PATTERN_NAME_WAS.finditer(narration):
        name = (m.group(1) or "").strip()
        if not name or name in _STOPWORDS:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen[key] = {
            "name": name,
            "role": "",
            "persona_seed": name,
        }

    return list(seen.values())


def extract_player_coined_names(player_input: str) -> list[dict]:
    """find names the player coins for an NPC in their own input
    (e.g. "I'll call you Friend"). Title-cases lowercase input so the
    NPC store gets a proper-noun-style name. Returns list of {name,
    role, persona_seed} dicts.

    Distinct from extract_introductions which only reads DM narration.
    This runs on player input before the DM beat so the DM's context
    block lists the coined NPC by name and uses it in narration.
    """
    if not player_input:
        return []
    seen: dict[str, dict] = {}
    for m in _PATTERN_PLAYER_COINED.finditer(player_input):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        # Title-case so "friend" -> "Friend"; uppercase already-cased
        # names stay as-is (a typed "Friend" matches lowercase
        # via re.IGNORECASE then title-cases idempotently).
        name = raw[0].upper() + raw[1:].lower()
        if name in _STOPWORDS:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen[key] = {
            "name": name,
            "role": "",
            "persona_seed": name,
        }
    return list(seen.values())


def ingest_player_input(state, player_input: str, current_location: str, turn: int) -> dict:
    """pre-DM hook: extract player-coined names from input, upsert
    them into the NPC store. Mirrors ingest_narration's API for
    consistency. Returns {added: [...], updated: [...]}."""
    added: list[str] = []
    updated: list[str] = []
    if not player_input:
        return {"added": added, "updated": updated}

    for coined in extract_player_coined_names(player_input):
        npc_id = slug_for(coined["name"], "")
        npcs = get_npcs(state)
        existing = npcs.get(npc_id) or {}
        was_new = not existing
        npc = NPC.from_dict(
            {
                **existing,
                "npc_id": npc_id,
                "name": coined["name"],
                "role": existing.get("role", ""),
                "persona": existing.get("persona") or coined["persona_seed"],
                "location": current_location,
                "first_seen_turn": existing.get("first_seen_turn") or turn,
                "last_seen_turn": turn,
                "witnessed": existing.get("witnessed", []),
                "disposition": existing.get("disposition", 0),
            }
        )
        put_npc(state, npc)
        if was_new:
            added.append(npc_id)
        else:
            updated.append(npc_id)

    return {"added": added, "updated": updated}


# relevance filter


_WORD_RE = re.compile(r"\b([a-zA-Z]{3,})\b")


def _topic_words(player_input: str) -> set[str]:
    """Pull whole-word tokens >=3 chars from player input. Used as the
    topic-match key against witnessed event text."""
    if not player_input:
        return set()
    return {m.group(1).lower() for m in _WORD_RE.finditer(player_input)}


def relevant_memories(
    npc: NPC, player_input: str, current_location: str, cap: int = 5
) -> list[str]:
    """Pick at most `cap` items from npc.witnessed, prioritising:
      1. Last 2 events (recency baseline - non-negotiable)
      2. Events whose text shares a whole-word with player input
      3. Events tagged with current_location
    Preserves chronological order in the output."""
    if not npc.witnessed:
        return []

    # Last 2 are always in.
    n = len(npc.witnessed)
    baseline_idx = set(range(max(0, n - 2), n))

    # Topic + location matches expand the index set.
    topics = _topic_words(player_input)
    loc = (current_location or "").strip().lower()
    extra: set[int] = set()
    for i, ev in enumerate(npc.witnessed):
        text = str(ev).lower()
        if topics and any(t in text for t in topics):
            extra.add(i)
        if loc and loc in text:
            extra.add(i)

    chosen = sorted(baseline_idx | extra)
    if len(chosen) > cap:
        # Trim oldest non-baseline first (keep recency baseline).
        non_base = [i for i in chosen if i not in baseline_idx]
        keep_extra = cap - len(baseline_idx)
        non_base_kept = sorted(non_base, reverse=True)[:keep_extra]
        chosen = sorted(baseline_idx | set(non_base_kept))

    return [str(npc.witnessed[i]) for i in chosen]


# post-beat hook: introductions + witnessed appends


def record_witnessed(state, npc_id: str, event: str, turn: int) -> None:
    """Append a witnessed event to an NPC's record. FIFO at WITNESSED_CAP."""
    npcs = get_npcs(state)
    d = npcs.get(npc_id)
    if not d:
        return
    npc = NPC.from_dict(d)
    npc.witnessed.append(event)
    if len(npc.witnessed) > WITNESSED_CAP:
        npc.witnessed = npc.witnessed[-WITNESSED_CAP:]
    npc.last_seen_turn = max(npc.last_seen_turn, int(turn))
    put_npc(state, npc)


def ingest_narration(state, narration: str, current_location: str, turn: int) -> dict:
    """Post-beat hook: extract introductions from `narration`, upsert
    them into the NPC store, and append a witnessed event for any
    NPC mentioned by name in this narration. Returns a summary
    {added: [], updated: []} for telemetry."""
    added: list[str] = []
    updated: list[str] = []
    if not narration:
        return {"added": added, "updated": updated}

    # 1) Introductions -> new records (or fill in role for an existing
    #    record where we previously only had a bare name).
    for intro in extract_introductions(narration):
        npc_id = slug_for(intro["name"], intro["role"])
        npcs = get_npcs(state)
        existing = npcs.get(npc_id) or {}
        was_new = not existing
        npc = NPC.from_dict(
            {
                **existing,
                "npc_id": npc_id,
                "name": intro["name"],
                "role": intro["role"] or existing.get("role", ""),
                "persona": existing.get("persona") or intro["persona_seed"],
                "location": current_location,
                "first_seen_turn": existing.get("first_seen_turn") or turn,
                "last_seen_turn": turn,
                "witnessed": existing.get("witnessed", []),
                "disposition": existing.get("disposition", 0),
            }
        )
        put_npc(state, npc)
        if was_new:
            added.append(npc_id)
        else:
            updated.append(npc_id)

    # 2) Witnessed-event append: for any NPC whose name appears in
    #    this narration, record a short event line. Bounded summary -
    #    not the whole narration.
    npcs = get_npcs(state)
    short_event = f"turn {turn} @ {current_location}: " + (narration[:140].strip())
    for d in npcs.values():
        nm = d.get("name", "")
        if not nm:
            continue
        if re.search(rf"\b{re.escape(nm)}\b", narration):
            record_witnessed(state, d["npc_id"], short_event, turn)
            if d["npc_id"] not in updated and d["npc_id"] not in added:
                updated.append(d["npc_id"])

    return {"added": added, "updated": updated}


# system-prompt block rendering


def render_npc_block(npc: NPC, *, relevant: list[str], total_witnessed: int) -> str:
    """One injected block for the DM beat. Uses the relevance-filtered
    witnessed list; includes the "other memories not shown" hint so
    callbacks work without licensing fabrication."""
    disp_phrase = _disposition_phrase(npc.disposition)
    parts = [
        f"[NPC IN SCENE: {npc.name}]",
        f"role: {npc.role}" if npc.role else "",
        f"persona: {npc.persona}" if npc.persona else "",
        "",
        f"what {npc.name} remembers right now (relevance-filtered):",
    ]
    if relevant:
        parts.extend(f"  - {ev}" for ev in relevant)
    else:
        parts.append("  - (no prior interactions with this player)")
    if total_witnessed > len(relevant):
        parts.append(
            f"\n{npc.name} has other memories not shown here "
            f"({total_witnessed - len(relevant)} more). If the player "
            f"references an event that would plausibly be in {npc.name}'s "
            f"past, they may recall it; if the event is NOT in their "
            f"witnessed list at all, they do not know."
        )
    if disp_phrase:
        parts.append(f"\n{npc.name}'s disposition toward the player: {disp_phrase}.")
    parts.append(
        f"\nConstraint: speak FOR {npc.name} only within what they "
        f"remember and what they know about the player. Do not narrate "
        f"events they could not have witnessed as if they had."
    )
    return "\n".join(p for p in parts if p is not None).strip()


def _disposition_phrase(d: int) -> str:
    if d <= -60:
        return "hostile"
    if d <= -25:
        return "wary"
    if d < 25:
        return "neutral"
    if d < 60:
        return "cordial"
    return "warm / trusting"


# disposition band (same thresholds as _disposition_phrase)
# Deliberately duplicates the thresholds - _disposition_phrase feeds the
# DM prompt (invariant, must not change) while disposition_band feeds the
# player-facing roster projection and trade authority check.


def disposition_band(d: int) -> str:
    """Categorical band from a raw disposition int (-100..+100).
    Thresholds mirror _disposition_phrase; return values are single
    words so they can be used as i18n keys and trade-authority checks.
    """
    if d <= -60:
        return "hostile"
    if d <= -25:
        return "wary"
    if d < 25:
        return "neutral"
    if d < 60:
        return "cordial"
    return "warm"


# reveal-gated roster projection
# whitelist: npc_id, name, role, persona, disposition, disposition_band,
# first_seen_turn, last_seen_turn, color_hue.
# never include: witnessed, reveal_state, bound_status, location.
# Signature pre-widened: save_id + turn support body extension
# (items + can_trade appended when save_id given); body returns
# the whitelist-only projection.


def roster_view(state, save_id: Optional[str] = None, turn: Optional[int] = None) -> list:
    """Return a reveal-gated list of NPC dicts for the frontend roster.

    Whitelist fields only - witnessed/reveal_state/bound_status/location
    are never included. Returns [] on any error (never raises).

    extension: when save_id is given, each entry is augmented
    with items=<npc inventory> and can_trade=<trade verdict dict>.
    """
    try:
        rows = []
        for d in get_npcs(state).values():
            try:
                npc = NPC.from_dict(d)
            except Exception:
                continue
            band = disposition_band(npc.disposition)
            entry: dict = {
                "npc_id": npc.npc_id,
                "name": npc.name,
                "role": npc.role,
                "persona": npc.persona,
                "disposition": npc.disposition,
                "disposition_band": band,
                "first_seen_turn": npc.first_seen_turn,
                "last_seen_turn": npc.last_seen_turn,
                "color_hue": npc.color_hue,
            }
            # Append per-NPC inventory + trade verdict when
            # save_id is provided.  Imported here to avoid circular
            # imports at module load time.
            if save_id is not None:
                try:
                    from app.game import items as _items_mod
                    from app.game import trade as _trade_mod

                    entry["items"] = _items_mod.items_held_by_npc(save_id, npc.name)
                    entry["can_trade"] = _trade_mod.can_trade_dict(state, npc, turn=turn)
                except Exception:
                    entry["items"] = []
                    entry["can_trade"] = {"allowed": False, "reason": "error"}
            rows.append(entry)
        rows.sort(key=lambda e: (-int(e.get("last_seen_turn") or 0), str(e.get("name") or "")))
        return rows
    except Exception:
        return []


def render_in_scope(state, names: list[str], player_input: str, current_location: str) -> str:
    """Compose injection blocks for the named NPCs. Used by the
    engine when the addressee classifier identifies in-scope NPCs."""
    if not names:
        return ""
    blocks = []
    for name in names:
        npc = find_by_name(state, name)
        if not npc:
            continue
        relevant = relevant_memories(npc, player_input, current_location)
        blocks.append(
            render_npc_block(
                npc,
                relevant=relevant,
                total_witnessed=len(npc.witnessed),
            )
        )
    return "\n\n".join(blocks)


# post-turn disposition update

_DISPOSITION_POS = re.compile(
    r"\b(gift|give|gave|help|helped|heal|healed|thank|thanks|honor|honour|"
    r"protect|protected|save|saved|loyal|praise|praised|offer|offered|"
    r"defend|defended|assist|assisted|kind|generous|trust|trusted|friend)\b",
    re.IGNORECASE,
)
_DISPOSITION_NEG = re.compile(
    r"\b(insult|insulted|betray|betray|betrayed|threaten|threatened|"
    r"attack|attacked|steal|stole|stolen|lie|lied|deceive|deceived|"
    r"accuse|accused|humiliate|humiliated|mock|mocked|abandon|abandoned|"
    r"harm|harmed|hurt|reject|rejected|dismiss|dismissed)\b",
    re.IGNORECASE,
)


def update_dispositions_post_turn(
    state,
    *,
    player_input: str,
    narrative: str,
    addressed_names: list[str],
    turn: int,
) -> None:
    """Deterministic post-turn disposition update for NPCs the player
    addressed this turn. Keyword-scans player_input + narrative for
    positive/negative signals; applies a small delta per net signal,
    capped at +/-15 per turn. The LLM still owns nuance - this just
    drifts the number so long-term patterns accumulate."""
    if not addressed_names:
        return
    combined = f"{player_input}\n{narrative}"
    pos_hits = len(_DISPOSITION_POS.findall(combined))
    neg_hits = len(_DISPOSITION_NEG.findall(combined))
    if pos_hits == neg_hits:
        return
    delta = (pos_hits - neg_hits) * 5
    delta = max(-15, min(15, delta))
    for name in addressed_names:
        npc = find_by_name(state, name)
        if npc is None:
            continue
        new_disp = max(-100, min(100, npc.disposition + delta))
        if new_disp == npc.disposition:
            continue
        npc_upd = NPC(
            npc_id=npc.npc_id,
            name=npc.name,
            role=npc.role,
            persona=npc.persona,
            relationships=npc.relationships,
            witnessed=npc.witnessed,
            disposition=new_disp,
            color_hue=npc.color_hue,
            last_seen_turn=npc.last_seen_turn,
            last_seen_location=npc.last_seen_location,
        )
        put_npc(state, npc_upd)


# DM-private notes block


def render_dm_notes_block(state) -> str:
    """Return a formatted DM-private notes block for the system prompt.
    Returns empty string when dm_notes is empty."""
    raw = state.get("dm_notes")
    if not raw:
        return ""
    if isinstance(raw, list):
        notes = [str(n).strip() for n in raw if n]
    else:
        notes = [str(raw).strip()] if str(raw).strip() else []
    if not notes:
        return ""
    lines = "\n".join(f"  - {n}" for n in notes)
    return f"[DM PRIVATE NOTES - player never sees this]\n{lines}"


# frontend colour map (read at save-load time)


def colour_map(state) -> dict[str, int]:
    """name -> HSL hue map for the frontend's name-colouring regex.
    The frontend reads this from the save payload and uses it to wrap
    NPC names in coloured spans. Cap is enforced display-side."""
    out: dict[str, int] = {}
    for d in get_npcs(state).values():
        name = d.get("name")
        hue = int(d.get("color_hue") or 0)
        if name and hue:
            out[name] = hue
    return out
