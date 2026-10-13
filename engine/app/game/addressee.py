"""Addressee classification for player dialogue.

Deterministic. No LLM call. Run before any LLM beat fires.

Why this exists: a human can't reliably parse "respect your elders boy"
without context the resolver doesn't have either. So instead of
guessing, detect ambiguity and ask the player - same UX pattern as
the escalation consent flow. Zero-latency knowledge of who they
meant, zero wasted LLM calls.

The classifier reads:
- Player input  (regex shape detection)
- gs.state.npc_dispositions (existing NPC field)
- gs.state.npcs (the engine NPC store)

Result kinds:
- NAMED(name) - player said one NPC's name explicitly
- NAMED_MULTI([n1, n2]) - multiple names explicit ("Marta and Aldwin")
- broadcast - "everyone", "all of you", "anyone", etc.
- crowd - scene has a crowd, zero named NPCs in scope
- UNIQUE(name) - exactly one eligible named NPC, safe default
- AMBIGUOUS(options) - >=2 eligible names, no name, no broadcast -> modal
- none - non-dialogue input (action turn), no addressee

When kind == AMBIGUOUS, the engine raises AddresseeAmbiguous and the
API returns a 200 with kind="addressee_clarification_needed" so the
frontend modal can collect the player's pick. Player retries the same
turn with `addressee` set; classifier is bypassed on the retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from app.game.gate_base import TurnGate


class AddresseeKind(str, Enum):
    NAMED = "named"
    NAMED_MULTI = "named_multi"
    BROADCAST = "broadcast"
    CROWD = "crowd"
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    NONE = "none"


@dataclass
class AddresseeResult:
    kind: AddresseeKind
    name: str = ""
    names: list[str] = field(default_factory=list)
    out_of_scene: bool = False  # NAMED but named NPC not currently present


# shape detection
_QUOTED_RE = re.compile(r'"[^"]+"|\u201c[^\u201d]+\u201d')
# tell / ask / say count as speech only with an addressee-shaped object
# as follows: a pronoun, a determiner and a word, or a name (Mara, MARA,
# O'Brien). ask also takes a question word, if, whether or about; say takes
# "to <addressee>" within four words. tell after can't / cannot / couldn't /
# can not is not speech ("I can't tell how far", "can't tell them apart").
_PRONOUN_OBJ = r"(?i:him|her|them|you|me|us|everyone|everybody|anyone|someone|y'?all)\b"
_DET_OBJ = r"(?i:a|an|the|my|his|her|their|this|that)\s+[A-Za-z]"
_NAME_OBJ = r"[A-Z][A-Za-z'\-]+\b"
_ADDRESSEE_OBJ = r"(?:" + _PRONOUN_OBJ + r"|" + _DET_OBJ + r"|" + _NAME_OBJ + r")"
_NOT_CANT = (
    r"(?i:(?<!can't\s)(?<!cannot\s)(?<!couldn't\s)(?<!can not\s)"
    r"(?<!can\u2019t\s)(?<!couldn\u2019t\s))"
)
_SPEAK_RE = re.compile(
    r"\b(?:(?i:shout|yell|whisper|scream|cry|call out|reply|answer|order|command|beg|address)\b"
    r"|"
    + _NOT_CANT
    + r"(?i:tell)\s+"
    + _ADDRESSEE_OBJ
    + r"|(?i:ask)\s+(?:"
    + _ADDRESSEE_OBJ
    + r"|(?i:where|what|who|whom|why|how|when|which|if|whether|about)\b)"
    r"|(?i:say)\s+(?:(?i:to)\s+"
    + _ADDRESSEE_OBJ
    + r"|(?:\S+\s+){0,3}?\S+\s+(?i:to)\s+"
    + _ADDRESSEE_OBJ
    + r"))"
)


def looks_like_dialogue(text: str) -> bool:
    """True iff the input has dialogue shape. Action-only turns
    ("I walk north") return False and skip the addressee path entirely."""
    if not text:
        return False
    if _QUOTED_RE.search(text):
        return True
    if _SPEAK_RE.search(text):
        return True
    return False


# broadcast + crowd markers
_BROADCAST_RE = re.compile(
    r"\b(everyone|everybody|y'?all|listen up|anyone|hey all|"
    r"all of (you|them)|people|folks)\b",
    re.IGNORECASE,
)

# Directed-address verb-object extractor: "reply to Soledad", "tell soledad",
# "answer SOLEDAD" etc. Group 1 captures the name token (case-insensitive
# match vs `known` returns canonical stored name, not the player's casing).
_DIRECTED_ADDRESS_RE = re.compile(
    r"\b(?:reply|answer|respond|tell|ask|say(?:\s+to)?|shout(?:\s+to)?|"
    r"whisper(?:\s+to)?|address|call\s+out\s+to)\s+(?:to\s+)?([A-Za-z][A-Za-z'\-]{1,20})\b",
    re.IGNORECASE,
)

_CROWD_RE = re.compile(
    r"\b(crowd|villagers|soldiers|gathering|congregation|"
    r"people in the (square|tavern|street|hall)|"
    r"the (dozen|score|mob|throng|assembly|onlookers))\b",
    re.IGNORECASE,
)

# Capitalised tokens that aren't names - skip during the proper-noun
# scan of recent narration.
_NAMELIKE_BLOCKLIST = {
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
    "Tomorrow",
    "Yesterday",
    "Year",
    "Spring",
    "Summer",
    "Autumn",
    "Winter",
}


def _known_npc_names(gs) -> list[str]:
    """Names of NPCs the save knows: gs.state.npc_dispositions, then gs.state.npcs. Narration words are never names. Deduped, original case preserved."""
    names: list[str] = []
    seen: set[str] = set()

    # 1) Dispositions - keys are sometimes slug-form ("npc:aelric") and
    # the canonical name lives in v["name"] when the value is a dict.
    # Pick names from both shapes.
    try:
        disp = getattr(gs.state, "npc_dispositions", None) or {}
        for k, v in disp.items():
            candidate = None
            if isinstance(v, dict) and v.get("name"):
                candidate = str(v["name"])
            elif isinstance(k, str) and k and ":" not in k:
                candidate = k
            if candidate and candidate not in seen:
                names.append(candidate)
                seen.add(candidate)
    except Exception:
        pass

    # 2) The engine NPC store.
    try:
        from app.game import npcs as npc_mod

        for v in npc_mod.get_npcs(gs.state).values():
            candidate = str(v.get("name") or "").strip()
            if candidate and candidate not in seen:
                names.append(candidate)
                seen.add(candidate)
    except Exception:
        pass

    return names


def _eligible(gs, known: list[str]) -> list[str]:
    """Eligibility = name appears in the last ~6 assistant
    narration entries. Narrower than the known-names window so an NPC
    mentioned 20 turns ago and not since is `known` but not `eligible` - i.e. they're out-of-scene right now. Cap at 8.

    Once the follow-up lands, this becomes proper state-driven filtering by
    npc.location == gs.state.location and npc.last_seen_turn >=
    gs.turn - 6, replacing the window heuristic."""
    try:
        recent_text = " ".join(
            (h.get("content_en") or h.get("content") or "")
            for h in (gs.history or [])[-6:]
            if h.get("role") == "assistant"
        )
    except Exception:
        recent_text = ""

    out: list[str] = []
    for n in known:
        if re.search(rf"\b{re.escape(n)}\b", recent_text, re.IGNORECASE):
            out.append(n)
        if len(out) >= 8:
            break
    return out


def _crowd_in_scene(gs) -> bool:
    try:
        recent = " ".join(
            (h.get("content_en") or h.get("content") or "")
            for h in (gs.history or [])[-6:]
            if h.get("role") == "assistant"
        )
        return bool(_CROWD_RE.search(recent))
    except Exception:
        return False


# classifier


def classify_addressee(text: str, gs) -> AddresseeResult:
    if not text or gs is None:
        return AddresseeResult(kind=AddresseeKind.NONE)

    if not looks_like_dialogue(text):
        return AddresseeResult(kind=AddresseeKind.NONE)

    known = _known_npc_names(gs)
    eligible = _eligible(gs, known)
    crowd = _crowd_in_scene(gs)

    # 0) Directed-address verb-object extractor - "reply to Soledad",
    # "tell soledad", "answer SOLEDAD" etc. Matches before the general
    # name scan so unambiguous verb-object inputs never reach AMBIGUOUS,
    # even when >=2 eligible NPCs are present. Returns canonical stored
    # name (case-insensitive match vs `known`); player casing is ignored.
    _da_m = _DIRECTED_ADDRESS_RE.search(text)
    if _da_m:
        token = _da_m.group(1)
        for n in known:
            if n.lower() == token.lower():
                return AddresseeResult(
                    kind=AddresseeKind.NAMED,
                    name=n,  # canonical stored name
                    out_of_scene=(n not in eligible),
                )
        # Not found in known; if token looks like a proper noun (starts
        # with uppercase) treat as a first-mention introduction.
        if token[0:1].isupper():
            return AddresseeResult(
                kind=AddresseeKind.NAMED,
                name=token,
                out_of_scene=False,  # introduced this turn
            )
        # Lowercase, not in known -> fall through to regular scan

    # 1) Explicit name match in player input (IGNORECASE so "soledad" hits
    # "Soledad" in `known`; the loop var `n` preserves canonical case).
    named: list[str] = []
    for n in known:
        if re.search(rf"\b{re.escape(n)}\b", text, re.IGNORECASE):
            named.append(n)

    # 1b) First-mention: player introduces a new capitalised name not yet
    # in `known`. e.g. "I find a baker named Aelric and ask...". Without
    # this branch the classifier falls through to AMBIGUOUS on every
    # turn that introduces a name. We pick out tokens that look like
    # proper names (Aa pattern, 2-15 chars), skip the blocklist, and
    # treat exactly-one-new-name as NAMED.
    if not named:
        new_names: list[str] = []
        seen_new: set[str] = set()
        for m in re.finditer(r"\b([A-Z][a-z]{1,14}(?:'[a-z]{1,5})?)\b", text):
            n = m.group(1)
            if n in _NAMELIKE_BLOCKLIST:
                continue
            if n in known or n in seen_new:
                continue
            new_names.append(n)
            seen_new.add(n)
        if len(new_names) == 1:
            return AddresseeResult(
                kind=AddresseeKind.NAMED,
                name=new_names[0],
                out_of_scene=False,  # introduced this turn -> in scene
            )
        # >=2 new capitalised tokens: ambiguous between them; fall through

    if len(named) >= 2:
        return AddresseeResult(kind=AddresseeKind.NAMED_MULTI, names=named)
    if len(named) == 1:
        return AddresseeResult(
            kind=AddresseeKind.NAMED,
            name=named[0],
            out_of_scene=(named[0] not in eligible),
        )

    # 2) Broadcast markers
    if _BROADCAST_RE.search(text):
        return AddresseeResult(kind=AddresseeKind.BROADCAST)

    # 3) Eligibility-driven defaults
    if not eligible:
        if crowd:
            return AddresseeResult(kind=AddresseeKind.CROWD)
        return AddresseeResult(kind=AddresseeKind.NONE)

    if len(eligible) == 1:
        return AddresseeResult(kind=AddresseeKind.UNIQUE, name=eligible[0])

    # 4) >=2 eligible names, no name, no broadcast -> AMBIGUOUS
    return AddresseeResult(kind=AddresseeKind.AMBIGUOUS, names=eligible)


# pause + retry plumbing


class AddresseeAmbiguous(TurnGate):
    """Raised by run_turn when classifier returns AMBIGUOUS and the
    player hasn't yet picked. API catches this and returns 200 with
    the modal payload. The save is not mutated on the gate path."""

    def __init__(self, options: list[str], crowd_present: bool):
        super().__init__("addressee_clarification_needed")
        self.options = options
        self.crowd_present = crowd_present

    def to_payload(self) -> dict:
        return {
            "kind": "addressee_clarification_needed",
            "options": list(self.options),
            "allow_crowd": bool(self.crowd_present),
            "message": "Who are you talking to?",
        }


# Sentinels for the explicit-pick path through TurnIn.addressee
PICK_BROADCAST = "__broadcast__"
PICK_CROWD = "__crowd__"


def resolve_from_pick(addressee) -> AddresseeResult:
    """Player has already picked from the modal. Convert their pick into
    an AddresseeResult so the engine can render the system-prompt block.
    Accepts a string, a list of strings, or one of the sentinels above."""
    if isinstance(addressee, list):
        clean = [a for a in addressee if isinstance(a, str) and a.strip()]
        if not clean:
            return AddresseeResult(kind=AddresseeKind.NONE)
        if len(clean) == 1:
            return resolve_from_pick(clean[0])
        return AddresseeResult(kind=AddresseeKind.NAMED_MULTI, names=clean)

    if not isinstance(addressee, str):
        return AddresseeResult(kind=AddresseeKind.NONE)

    s = addressee.strip()
    if not s:
        return AddresseeResult(kind=AddresseeKind.NONE)
    if s == PICK_BROADCAST:
        return AddresseeResult(kind=AddresseeKind.BROADCAST)
    if s == PICK_CROWD:
        return AddresseeResult(kind=AddresseeKind.CROWD)
    return AddresseeResult(kind=AddresseeKind.NAMED, name=s)


# prompt-block rendering


def system_prompt_block(result: AddresseeResult) -> str:
    """One-block addition for the DM system prompt. Empty string when
    nothing to disambiguate (none / AMBIGUOUS - the latter never reaches
    the LLM)."""
    k = result.kind
    if k == AddresseeKind.NAMED:
        line = f"[Player dialogue is addressed to: {result.name}."
        if result.out_of_scene:
            line += (
                f" Note: {result.name} is not currently present in the scene"
                f" - the player may be calling for them, recalling them, or"
                f" asking after them."
            )
        return line + "]"
    if k == AddresseeKind.NAMED_MULTI:
        joined = " and ".join(result.names)
        return (
            f"[Player dialogue is addressed to {joined}. They may respond"
            f" in turn; reflect each perspective without one overshadowing"
            f" the other.]"
        )
    if k == AddresseeKind.BROADCAST:
        return "[Player is broadcasting to everyone present. NPCs in scope may react briefly.]"
    if k == AddresseeKind.CROWD:
        return (
            "[Player is addressing the crowd. Render a collective response"
            " - murmur, scattered shouts - and optionally one anonymous"
            " voice cutting through.]"
        )
    if k == AddresseeKind.UNIQUE:
        return f"[Player dialogue is addressed to {result.name} (only NPC in scope).]"
    return ""
