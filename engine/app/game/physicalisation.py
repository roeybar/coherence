"""Physicalisation scope classifier + entropy accumulator.

In a pack that uses this template, the player character may be capable
of creating matter/energy from nothing without knowing it. The engine
never refuses physical alteration attempts. Constraints emerge from:

  1. Player's own self-imposed rules (`state.self_restrictions`)
  2. Entropy character intervention at scope >= location
     (accumulator at `state.entropy_attention`)
  3. Player's self-image of being limited (DM narrative tone)

This module:
  - classify_scope(player_input) - Tier 1 haiku call (~$0.0003/turn)
      returns {"scope": str, "confidence": str}
  - classify_scope_tier2(player_input, ctx) - Tier 2 strong-model fallback
      called only when Tier 1 confidence == "low". 100 in / 50 out
      strong model ~ $0.001/call.
  - parse_self_restriction(player_input) - detects player imposing a
      new limit on themselves ("I can only do this with effort", "this
      drains me"). Engine adds to state.self_restrictions so the DM
      beat honours it on future turns.
  - parse_restriction_removal(player_input) - detects player removing
      a self-imposed limit ("I let go of that rule", "I realize I can
      do more"). Engine drops the matching restriction (or all if
      no clear match).
  - update_entropy(state, scope) - accumulator math; scope->delta
      table determines how much attention each attempt draws.
  - check_entropy_thresholds(state) - returns hint level (none / stir
      / notice / converge) for the DM beat to narrate as appropriate.
  - render_physicalisation_block(state, scope) - the prompt block
      injected into the DM beat system prompt; loads pack template +
      current self_restrictions + entropy hint level.

Pure deterministic except the Tier 1/2 LLM calls. No state mutation
in the classifiers - engine.py reads results + writes to state.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app.game.llm import MODEL_HAIKU, MODEL_SONNET, call_llm_plain
from app.game import moderation
from app.game.gate_base import TurnGate
from app.game import lore as lore_pack


log = logging.getLogger("physicalisation")


# scope taxonomy


SCOPES = ("none", "trivial", "local", "scene", "location", "world", "planet")


# Attention deltas per scope. Engine adds to state.entropy_attention
# after each turn. Calibrated so:
#   ~1 location-scope attempt = 5    (stir hint)
#   ~6 location-scope = 30           (first notice)
#   ~3 world-scope = 150             (entropy character converges)
#   ~1 planet+ scope = 200           (intervention triggered)
SCOPE_ATTENTION_DELTA = {
    "none": 0,
    "trivial": 0,
    "local": 0,
    "scene": 1,
    "location": 5,
    "world": 50,
    "planet": 200,
}


# Thresholds for entropy-character narrative escalation.
ENTROPY_THRESHOLDS = {
    "stir": 10,  # "something stirs at the edge of awareness"
    "notice": 30,  # "you feel attention bend toward you"
    "converge": 100,  # the character begins to manifest
    "intervene": 200,  # the character directly intervenes
}


# intent labels for the ability-use clarification modal.
# Policy: when a player attempts a materialisation /
# ability of local+ scope and intent is unclear, the engine pauses
# and asks them to pick one. internal = "look within yourself" (lattice
# lattice route, no entropy attention). external = "at the expense of
# what's nearby" (entropy beat catalogues silently, then standard
# attention math; at small scale this means a quiet log entry, no
# narrative intervention).
INTENT_INTERNAL = "internal"
INTENT_EXTERNAL = "external"
INTENT_VALUES = (INTENT_INTERNAL, INTENT_EXTERNAL)


# Scopes that trigger the ability-use intent gate. "trivial" and "none"
# are deliberately excluded - lighting a candle by touch doesn't need
# a modal pop-up.
ABILITY_GATE_SCOPES = frozenset({"local", "scene", "location", "world", "planet"})


# Cap on the catalogue list so it never grows unbounded across long saves.
ENTROPY_LOG_LIMIT = 50


# Tier 1 - fast haiku classifier


_TIER1_SYSTEM = """You classify the SCOPE of a player's attempted physical alteration in an RPG turn.

Return ONLY a JSON object: {"scope": "<value>", "confidence": "<value>"}

scope values (pick exactly one):
- none: no physical alteration attempt (talking, walking, looking, asking)
- trivial: hand-scale, requires no effort to imagine (light a candle by touch)
- local: room-scale (materialize an item, move furniture, summon a small creature)
- scene: building-scale (collapse a wall, brew a storm in the square, summon a crowd)
- location: settlement-scale (raze a village, fog a valley, raise a tower)
- world: regional (calm an ocean, end a season, terraform a continent)
- planet: planetary or beyond (redirect orbit, ignite a sun, unmake a moon)

confidence values: "high" if obvious, "low" if ambiguous or borderline.

Return ONLY the JSON. No explanation."""


def classify_scope(player_input: str) -> tuple:
    """Tier 1 - haiku classifier. Returns ({"scope": ..., "confidence": ...}, usage).
    Falls back to ({"scope": "none", "confidence": "high"}, None) on any error
    so we never block a turn on classifier failure."""
    if not player_input or not player_input.strip():
        return {"scope": "none", "confidence": "high"}, None

    try:
        text, usage = call_llm_plain(
            _TIER1_SYSTEM,
            messages=[{"role": "user", "content": player_input.strip()[:600]}],
            model=MODEL_HAIKU,
            max_tokens=40,
        )
        return _parse_classifier_json(text), usage
    except Exception as e:
        log.warning("tier1 physicalisation classifier failed: %s", e)
        return {"scope": "none", "confidence": "high"}, None


# Tier 2 - strong-model fallback on low confidence


_TIER2_SYSTEM = """You are the second-opinion scope classifier for an RPG physical-alteration attempt.

The first-pass haiku classifier was uncertain. Read the player input carefully and pick the SCOPE:

- none / trivial / local / scene / location / world / planet

Consider: what's the LARGEST physical thing the player is attempting to alter? Use the upper bound, not the average.

Return ONLY a JSON object: {"scope": "<value>", "confidence": "high"}"""


def classify_scope_tier2(player_input: str) -> tuple:
    """Tier 2 - strong-model fallback. ~100 in / 50 out tokens ~ $0.001/call.
    Called only when Tier 1 confidence == 'low'.
    Returns ({"scope": ..., "confidence": ...}, usage)."""
    if not player_input or not player_input.strip():
        return {"scope": "none", "confidence": "high"}, None
    try:
        text, usage = call_llm_plain(
            _TIER2_SYSTEM,
            messages=[{"role": "user", "content": player_input.strip()[:800]}],
            model=MODEL_SONNET,
            max_tokens=1000,
        )
        return _parse_classifier_json(text), usage
    except Exception as e:
        log.warning("tier2 physicalisation classifier failed: %s", e)
        return {"scope": "none", "confidence": "high"}, None


def _parse_classifier_json(text: str) -> dict:
    """Tolerant JSON parse. Strips markdown fences, picks first
    {...} block, validates scope is one of scopes (else 'none')."""
    if not text:
        return {"scope": "none", "confidence": "high"}
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[^{}]*\}", s, re.DOTALL)
    if not m:
        return {"scope": "none", "confidence": "high"}
    try:
        d = json.loads(m.group(0))
        moderation.check_decoded(d)
    except Exception:
        return {"scope": "none", "confidence": "high"}
    scope = str(d.get("scope", "none")).strip().lower()
    if scope not in SCOPES:
        scope = "none"
    conf = str(d.get("confidence", "high")).strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "high"
    return {"scope": scope, "confidence": conf}


# self-restriction parsing (add path)


# Phrases that suggest the player is imposing a new limit on themselves.
# Heuristic + intentionally generous; engine + DM beat both honour the
# resulting list, so a false positive just adds a "rule" the DM acknowledges.
_PATTERN_RESTRICTION_ADD = re.compile(
    r"\b(?:"
    r"i\s+can\s+only|"
    r"i\s+only\s+(?:do|use|cast|perform)|"
    r"this\s+(?:drains?|tires?|exhausts?|costs?|takes?\s+from)\s+me|"
    r"once\s+(?:per|a)\s+(?:day|week|month|year|hour)|"
    r"i\s+(?:need|have)\s+to\s+(?:rest|recover|recharge|wait)|"
    r"i\s+(?:promise|vow|swear|bind\s+myself)\s+(?:not\s+to|to\s+only)|"
    r"i\s+(?:limit|restrict|cap)\s+(?:myself|my\s+\w+)"
    r")\b",
    re.IGNORECASE,
)


def parse_self_restriction(player_input: str) -> Optional[dict]:
    """If player input contains a self-imposed-limit pattern, return
    {rule, snippet} suitable for appending to state.self_restrictions.
    Returns None if no pattern matches."""
    if not player_input:
        return None
    m = _PATTERN_RESTRICTION_ADD.search(player_input)
    if not m:
        return None
    # Extract the sentence containing the match for context.
    start = max(0, player_input.rfind(".", 0, m.start()) + 1)
    end = player_input.find(".", m.end())
    if end < 0:
        end = len(player_input)
    snippet = player_input[start:end].strip()[:220]
    return {
        "rule": m.group(0).strip()[:80],
        "snippet": snippet,
    }


# self-restriction removal path


# Policy: a player can also release a self-imposed
# limit when they discover (in-character) that they don't need it.
# Three flavours of removal phrasing:
#   1. Discovery - "I realize I don't need to limit this"
#   2. Release - "I let go of that rule / I cast off the chains"
#   3. Reframing - "the rule wasn't real" / "I no longer need to hold back"
_PATTERN_RESTRICTION_REMOVE = re.compile(
    r"\b(?:"
    # Discovery: I realize/discover/understand ... (don't need|can|never needed|am not bound)
    r"i\s+(?:realiz|realis|understand|discover|see)e?d?\s+(?:that\s+)?"
    r"(?:i\s+)?(?:can|don'?t\s+need|never\s+needed|am\s+not\s+bound|"
    r"could\s+always|wasn'?t\s+(?:really\s+)?(?:limited|bound))|"
    # Release: I let go / release / drop / abandon / cast off / forget
    r"i\s+(?:let\s+go|release|drop|abandon|cast\s+off|forget|shed)\s+"
    r"(?:of\s+)?(?:this|that|the|my|all|every)?\s*"
    r"(?:rule|limit|restriction|bound|chain|shackle|fetter|self-imposed)s?|"
    # Reframing: the rule was/wasn't/isn't real|true|binding|necessary
    r"(?:the|my|that|this)\s+(?:rule|limit|restriction|bound|chain)s?\s+"
    r"(?:was|wasn'?t|isn'?t|doesn'?t)\s+(?:real|true|binding|necessary|hold)|"
    # Outright reject: I no longer / don't have to / wasn't meant to (limit|hold back|restrain)
    r"i\s+(?:no\s+longer|don'?t\s+have\s+to|wasn'?t\s+meant\s+to)\s+"
    r"(?:limit|hold\s+back|restrain|restrict|cap)"
    r")\b",
    re.IGNORECASE,
)


def parse_restriction_removal(player_input: str) -> Optional[dict]:
    """If player input contains a release-from-limit pattern, return
    {removal_phrase, snippet} so the engine can drop matching entries
    from state.self_restrictions. Returns None if no pattern matches."""
    if not player_input:
        return None
    m = _PATTERN_RESTRICTION_REMOVE.search(player_input)
    if not m:
        return None
    start = max(0, player_input.rfind(".", 0, m.start()) + 1)
    end = player_input.find(".", m.end())
    if end < 0:
        end = len(player_input)
    snippet = player_input[start:end].strip()[:220]
    return {
        "removal_phrase": m.group(0).strip()[:80],
        "snippet": snippet,
    }


def apply_restriction_removal(
    restrictions: list[dict],
    removal: dict,
) -> list[dict]:
    """Drop the restriction that best matches the removal snippet via
    Jaccard word overlap. If no restriction has Jaccard >= 0.20, drop
    all of them (player having an ego-death / general-release moment).

    Returns the new restriction list. Empty input restrictions returns
    empty list (no-op)."""
    if not restrictions:
        return []

    removal_words = _word_set(removal.get("snippet", ""))
    if not removal_words:
        return []

    best: tuple[float, int] = (0.0, -1)
    for i, r in enumerate(restrictions):
        rule_words = _word_set(r.get("snippet", "") + " " + r.get("rule", ""))
        if not rule_words:
            continue
        score = _jaccard(removal_words, rule_words)
        if score > best[0]:
            best = (score, i)

    if best[0] >= 0.20 and best[1] >= 0:
        # Targeted removal: drop the matching restriction
        return [r for i, r in enumerate(restrictions) if i != best[1]]

    # No strong match - assume general release, drop everything
    return []


_WORD_RX = re.compile(r"\b([a-zA-Z]{3,})\b")


def _word_set(s: str) -> set[str]:
    return {m.group(1).lower() for m in _WORD_RX.finditer(s or "")}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ability-use intent gate


# Phrases that unambiguously commit to the INTERNAL/lattice route -
# matter from nothing, no nearby source involved. When matched, we
# skip the clarification modal and pre-fill intent=internal.
_PATTERN_INTENT_INTERNAL = re.compile(
    r"(?:"
    r"from\s+nothing|out\s+of\s+nothing|from\s+nowhere|"
    r"from\s+thin\s+air|from\s+the\s+void|ex\s+nihilo|"
    r"out\s+of\s+thin\s+air|into\s+being\s+from\s+(?:nothing|nowhere|void)"
    r")",
    re.IGNORECASE,
)


# Phrases that unambiguously commit to the external route - pulling
# something nearby that already exists into the player's possession.
_PATTERN_INTENT_EXTERNAL = re.compile(
    r"(?:"
    r"summon\s+(?:a\s+)?(?:nearby|real|the)\s+|"
    r"call\s+(?:a\s+)?(?:nearby|real|the)\s+|"
    r"draw\s+(?:a\s+)?(?:nearby|real|the).{0,20}\s+(?:to|toward)\s+(?:me|my)|"
    r"redirect\s+(?:a\s+|the\s+)?(?:nearby|real)|"
    r"from\s+(?:the\s+)?(?:trees|eaves|ground|alley|street|nearby)|"
    r"borrow\s+from"
    r")",
    re.IGNORECASE,
)


def detect_explicit_intent(player_input: str) -> Optional[str]:
    """Return INTENT_INTERNAL or INTENT_EXTERNAL if the player's input
    contains an unambiguous commitment to one route. None when neither
    pattern matches (-> engine will gate via the clarification modal)."""
    if not player_input:
        return None
    if _PATTERN_INTENT_INTERNAL.search(player_input):
        return INTENT_INTERNAL
    if _PATTERN_INTENT_EXTERNAL.search(player_input):
        return INTENT_EXTERNAL
    return None


# reality-bending / impossible-wish detector
# Deterministic floor: an impossible act reaches the within/surroundings
# grounding and never the dice gate. The seed lexicon below is hand-curated.
_PATTERN_REALITY_BENDING = re.compile(
    r"(?:"
    r"\bteleport\w*\b|"
    r"\bblink\s+(?:to|across|through)\b|"
    r"\bphase\s+through\b|"
    r"\bwalk\s+through\s+(?:the\s+)?(?:wall|walls|solid)\b|"
    r"\b(?:fly|leap|jump|rise|float|levitate|ascend)\s+"
    r"(?:up\s+)?(?:to|into|through)\s+"
    r"(?:the\s+)?(?:moon|sun|sky|stars?|space|orbit|clouds?|"
    r"heavens?|another\s+planet|mars|the\s+void)\b|"
    r"\blevitate\b|"
    r"\b(?:stop|rewind|reverse|freeze)\s+time\b|"
    r"\btime[-\s]?travel\b|travel\s+(?:back|forward)\s+in\s+time|"
    r"\b(?:turn|become|go)\s+invisible\b|"
    r"\bbreathe\s+in\s+(?:space|the\s+void|vacuum)\b|"
    r"\b(?:resurrect|raise)\s+(?:the\s+)?dead\b"
    r")",
    re.IGNORECASE,
)


def detect_reality_bending(player_input: str) -> bool:
    """True when the player asserts a physically-impossible act that must
    be grounded (energy within/surroundings) rather than rolled. Pure
    lexical, zero I/O - deterministic routing floor."""
    if not player_input:
        return False
    return bool(_PATTERN_REALITY_BENDING.search(player_input))


def is_ability_gate_scope(scope: str) -> bool:
    """True iff scope is one where the player should be asked to
    confirm intent. Used by the engine to decide whether to raise
    AbilityIntentAmbiguous."""
    return scope in ABILITY_GATE_SCOPES


class AbilityIntentAmbiguous(TurnGate):
    """Raised by run_turn when the physicalisation classifier flagged
    an ability-use of local+ scope and the player hasn't yet picked an
    intent (and the input doesn't unambiguously pick one). API catches
    this and returns a 200 with the modal payload. The save is not
    mutated on the gate path - re-posting with `ability_intent` set
    re-runs the turn cleanly."""

    def __init__(self, scope: str):
        super().__init__("ability_intent_clarification_needed")
        self.scope = scope

    def to_payload(self) -> dict:
        return {
            "kind": "ability_intent_clarification_needed",
            "scope": self.scope,
            "message": "How does this happen?",
            "options": [
                {
                    "value": INTENT_INTERNAL,
                    "label": "Within you (your own energy)",
                    "hint": "The energy comes from within you - your own body "
                    "and will. Nothing nearby is spent.",
                },
                {
                    "value": INTENT_EXTERNAL,
                    "label": "From your surroundings",
                    "hint": "The energy is drawn from your surroundings - the "
                    "environment and materials around you pay the cost. "
                    "Something is watching.",
                },
            ],
        }


def catalogue_entropy(state, scope: str, summary: str, turn: int) -> None:
    """Append a catalogue entry to state.entropy_log. Called when the
    player picks INTENT_EXTERNAL - the entropy character notices but
    does not intervene at small scales. The log is bounded at
    ENTROPY_LOG_LIMIT (FIFO drop)."""
    log_list = list(state.get("entropy_log") or [])
    log_list.append(
        {
            "turn": int(turn),
            "scope": str(scope),
            "summary": (summary or "")[:160],
        }
    )
    state.entropy_log = log_list[-ENTROPY_LOG_LIMIT:]


# entropy accumulator


def update_entropy(state, scope: str) -> int:
    """Add the scope's attention delta to state.entropy_attention.
    Returns the new total. No-op if scope unknown or delta is 0.

    state is a WorldState - read via .get / write via attribute set."""
    delta = SCOPE_ATTENTION_DELTA.get(scope, 0)
    if delta <= 0:
        return int(state.get("entropy_attention", 0))
    cur = int(state.get("entropy_attention", 0))
    new = cur + delta
    state.entropy_attention = new
    return new


def entropy_threshold_hint(attention: int) -> str:
    """Return the highest-tier threshold the attention level has crossed.
    Returns "" if below all thresholds."""
    if attention >= ENTROPY_THRESHOLDS["intervene"]:
        return "intervene"
    if attention >= ENTROPY_THRESHOLDS["converge"]:
        return "converge"
    if attention >= ENTROPY_THRESHOLDS["notice"]:
        return "notice"
    if attention >= ENTROPY_THRESHOLDS["stir"]:
        return "stir"
    return ""


# prompt block render


def _fragment(pack: Optional[dict], name: str, sep: str) -> str:
    """Optional pack-supplied prompt text with its separator; empty when
    the pack supplies none."""
    text = lore_pack.engine_fragment(pack, name)
    return sep + text if text else ""


def render_physicalisation_block(
    state,
    scope: str,
    pack_template: Optional[str] = None,
    ability_intent: Optional[str] = None,
    grounding: Optional[dict] = None,
    pack: Optional[dict] = None,
) -> str:
    """Compose the block injected into the DM beat system prompt when
    the classifier flagged a physical-alteration attempt.

    pack_template: optional raw text from lore/<pack>/prompts/
    physicalisation.md. Pack tonality applied first; engine appends
    state-specific lines (current self_restrictions, entropy hint).

    grounding: optional dict from physics.physicalise. When present,
    a physics section is prepended with the canonical mechanism so the
    DM doesn't have to improvise physics inline.

    Empty string return => caller should skip injection entirely."""
    if scope == "none":
        return ""

    parts: list[str] = []

    # Physics grounding block - prepended before pack tonality so the
    # DM gets hard constraints first, then the pack's narrative flavour.
    if grounding and grounding.get("record"):
        rec = grounding["record"]
        mech = rec.get("grounded_mechanism") or {}
        lines = ["PHYSICS (canonical grounding - DO NOT contradict these constraints):"]
        # Mechanism summary - source is the most important field
        src = mech.get("source", "").strip()
        cont = mech.get("containment", "").strip()
        if src:
            lines.append(f"  Mechanism: {src}" + (f". {cont}" if cont else ""))
        energy = rec.get("energy_demand", "").strip()
        if energy:
            lines.append(f"  Energy cost: {energy}")
        hint = rec.get("narrative_hint", "").strip()
        if hint:
            lines.append(f"  Atmosphere: {hint}")
        # Top failure mode - enough for the DM to hint at risk
        fms = rec.get("failure_modes") or []
        if fms:
            fm = fms[0]
            trigger = (fm.get("trigger") or "").strip()
            consequence = (fm.get("consequence") or "").strip()
            scale = (fm.get("scale") or "").strip()
            if trigger and consequence:
                lines.append(
                    f"  Primary risk: {trigger} -> {consequence}" + (f" ({scale})" if scale else "")
                )
        if len(lines) > 1:  # at least one content line beyond the header
            parts.append("\n".join(lines))

    if pack_template:
        parts.append(pack_template.strip())

    # Self-restrictions the player has declared
    restrictions = state.get("self_restrictions") or []
    if restrictions:
        parts.append(
            "PLAYER-IMPOSED RULES (honour these unless the player explicitly releases them):"
        )
        for r in restrictions[-5:]:  # show most recent 5
            quote = r.get("snippet", "") or r.get("rule", "")
            if quote:
                parts.append(f'  - "{quote}"')

    # Entropy hint - only emit when threshold crossed
    attention = int(state.get("entropy_attention", 0))
    hint = entropy_threshold_hint(attention)
    if hint:
        hint_text = _ENTROPY_HINTS.get(hint, "")
        if hint_text:
            parts.append(f"ENTROPY ({hint}, attention={attention}): {hint_text}")

    # Scope-specific guidance
    if scope in ("local", "trivial"):
        parts.append(
            "This attempt is small in scope. Narrate it happening cleanly. Do not impose costs."
        )
    elif scope == "scene":
        parts.append(
            "This attempt affects the scene. Narrate it happening; let consequences ripple in the scene only."
        )
    elif scope == "location":
        parts.append(
            "This attempt affects the settlement scale. Narrate it. Something at the edge of awareness MAY notice."
        )
    elif scope in ("world", "planet"):
        parts.append(
            "This attempt is at world+ scale. Narrate it. Something IS noticing now. The entropy character may intervene soon or now."
        )

    # confirmed intent line. Player has explicitly picked how the
    # ability resolves; the DM must commit to that framing instead of
    # hedging (e.g. "a sparrow happens to drop from the eave" when the
    # player wanted matter-from-nothing). This is a hard assertion the
    # model can't dodge without contradicting the player's stated choice.
    if ability_intent == INTENT_INTERNAL:
        parts.append(
            "PLAYER CONFIRMED INTENT - INTERNAL: matter / change comes from "
            "the player themselves, not from anywhere nearby. Narrate the "
            "result MANIFESTING directly. Do NOT stage a convenient "
            "real-world source (no sparrows dropping from eaves, no coin "
            "glinting in the dust). Hint at the player's hidden nature "
            "obliquely - DO NOT name it"
            + _fragment(pack, "physics_examples_a", ", DO NOT use the words ")
            + "."
        )
    elif ability_intent == INTENT_EXTERNAL:
        parts.append(
            "PLAYER CONFIRMED INTENT - EXTERNAL: the player draws on what "
            "is already nearby. Narrate a believable real-world source "
            "(a bird answers the call, a coin lifts from the dust, a "
            "candle's flame jumps across the room). Something IS noticing "
            "this - the entropy character catalogues silently. At this "
            "scale do NOT have them intervene; only the player feels a "
            "faint pressure of being watched, briefly."
        )

    # per-witness tier exposure surface
    tier_block = render_tier_exposure_block(state, pack=pack)
    if tier_block:
        parts.append(tier_block)

    return "\n".join(parts).strip()


# per-witness tier-exposure meter


# Map physicalisation scope -> ability tier (5 tiers).
SCOPE_TO_TIER = {
    "trivial": 1,
    "local": 1,  # local geometry edits = Tier 1-2 depending; classifier-only
    "scene": 2,
    "location": 3,
    "world": 4,
    "planet": 5,
}


def record_tier_exposure(state, npc_id: str, tier: int, turn: int) -> dict:
    """Bump an NPC's per-tier witness counter. Returns the updated
    per-NPC entry. Capped per-NPC at 5 entries (one per tier); the
    overall ledger is unbounded but each entry stays tiny.

    NPCs may show dread/recoil/awe
    in proportion to exposure, but must never name the player's nature
    until a story milestone. render_tier_exposure_block emits this
    rule alongside the counts."""
    if not npc_id or tier < 1 or tier > 5:
        return {}
    ledger = dict(state.get("tier_exposure") or {})
    entry = ledger.get(npc_id) or {
        "tier_1_count": 0,
        "tier_2_count": 0,
        "tier_3_count": 0,
        "tier_4_count": 0,
        "tier_5_count": 0,
        "last_witnessed_turn": 0,
        "first_witnessed_turn": int(turn),
    }
    key = f"tier_{tier}_count"
    entry[key] = int(entry.get(key, 0)) + 1
    entry["last_witnessed_turn"] = int(turn)
    if not entry.get("first_witnessed_turn"):
        entry["first_witnessed_turn"] = int(turn)
    ledger[npc_id] = entry
    state.tier_exposure = ledger
    return entry


def witnesses_for_exposure(gs) -> list[str]:
    """Return the list of npc_ids who are 'in scene' for the current
    turn - eligible to witness a player ability. Uses the existing
    NPC store + scene-NPCs heuristic. Crude: any NPC whose location
    matches the player's current location."""
    state = gs.state
    npcs = state.get("npcs") or {}
    loc = (state.get("location") or "").lower().strip()
    if not loc:
        return list(npcs.keys())[:6]  # safety fallback
    in_scene = []
    for nid, npc in npcs.items():
        if not isinstance(npc, dict):
            continue
        npc_loc = (npc.get("location") or "").lower().strip()
        if npc_loc and (npc_loc == loc or npc_loc in loc or loc in npc_loc):
            in_scene.append(nid)
    return in_scene[:8]


def render_tier_exposure_block(state, pack: Optional[dict] = None) -> str:
    """Surface per-NPC exposure counts to the DM beat, alongside the
    strict rule that NPCs never name the player's nature."""
    ledger = state.get("tier_exposure") or {}
    if not ledger:
        return ""
    # Find NPCs with non-trivial exposure (any tier-3+ or >=3 tier-1/2)
    notable = []
    for nid, entry in ledger.items():
        t1 = int(entry.get("tier_1_count", 0))
        t2 = int(entry.get("tier_2_count", 0))
        t3 = int(entry.get("tier_3_count", 0))
        t4 = int(entry.get("tier_4_count", 0))
        t5 = int(entry.get("tier_5_count", 0))
        if t3 + t4 + t5 > 0 or (t1 + t2) >= 3:
            notable.append((nid, entry, t1 + t2 + t3 + t4 + t5))
    if not notable:
        return ""
    notable.sort(key=lambda x: -x[2])
    parts = [
        "TIER-EXPOSURE PER WITNESS (these NPCs have personally seen "
        "the player do uncanny things; their reactions should escalate "
        "as exposure accumulates - dread -> recoil -> awe -> reverence. "
        "STRICT: NPCs may NEVER name the player's nature"
        + _fragment(pack, "physics_examples_b", " - no ")
        + ". They register what they saw, not what it means):",
    ]
    for nid, entry, total in notable[:6]:
        parts.append(
            f"  - {nid}: T1x{entry.get('tier_1_count', 0)} "
            f"T2x{entry.get('tier_2_count', 0)} "
            f"T3x{entry.get('tier_3_count', 0)} "
            f"T4x{entry.get('tier_4_count', 0)} "
            f"T5x{entry.get('tier_5_count', 0)}  (last seen T{entry.get('last_witnessed_turn', '?')})"
        )
    return "\n".join(parts)


_ENTROPY_HINTS = {
    "stir": (
        "Something at the edge of awareness stirs. The player feels watched, "
        "briefly, but cannot place by what. Narrate this as a half-felt sensation, no source."
    ),
    "notice": (
        "Attention bends toward the player. Not threat, not yet - but presence. "
        "A pressure between the words. Animals quiet for a beat."
    ),
    "converge": (
        "Something has noticed the player and is converging. Subtle inversions in "
        "the world's grain - shadows fall a fraction wrong, sound carries oddly. "
        "Hint at presence; do not name."
    ),
    "intervene": (
        "The entropy character INTERVENES on this turn. They do not yet show form "
        "but they ACT - the player's attempt is bent, dampened, mirrored. Treat as a "
        "narrative beat that hands the player a new mystery rather than a refusal."
    ),
}
