"""Physicalisation cache - convert player wishes/impossibilities into
hard-SF mechanism records, then re-use them within the save.

Storage: the bound save doc's "physics_cache" list, at most
PHYSICS_CACHE_MAX entries, oldest dropped first. Each entry is the
grounding record plus "_id" and "v" (base64 float16 embedding of the
normalised wish). A new wish is embed-searched against that list; on hit
(cosine >= MATCH_THRESHOLD) we reuse the existing record. On miss we run
the model, append a new record, and use the result. The cache is per
save: nothing is shared across players.

The engine calls it during a turn when the scope classifier flags a
non-trivial wish.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

from app.game.llm import MODEL_HAIKU, MODEL_SONNET
from app.game import moderation
from app.llm_client import call_messages
from app.game.memory import _decode, _embed, _encode
from app.store import current_save


log = logging.getLogger(__name__)


# constants
VECTOR_SIZE = 384  # bge-small-en-v1.5
MATCH_THRESHOLD = 0.85  # cosine - above this = same wish
PHYSICS_CACHE_MAX = 64  # entries per save
# The strong-model call is the real expense - cap output so a runaway grounding
# can't blow up cost. ~600 tokens is enough for the JSON schema below
# with reasonable detail in each field.
PHYSICALISER_MAX_TOKENS_SMALL = 1500  # haiku-small default
PHYSICALISER_MAX_TOKENS_LARGE = 4000  # haiku-large fallback (more headroom)
PHYSICALISER_MAX_TOKENS_SONNET = 3000  # sonnet last-resort


def _cache() -> list:
    return current_save()["physics_cache"]


def _bump(rec: dict, wish: str) -> None:
    rec["use_count"] = int(rec.get("use_count") or 0) + 1
    rec["updated_at"] = _now()
    variant = (wish or "")[:200]
    variants = rec.setdefault("seen_variants", [])
    if variant not in variants:
        variants.append(variant)


# normalisation
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_wish(text: str) -> str:
    """Reduce a wish to a canonical phrase for keying. Casefold, strip
    punctuation, collapse whitespace. Embedding does the heavy lifting
    for fuzzy matching - this just stops trivial casing differences from
    creating duplicate cache rows."""
    s = (text or "").lower().strip()
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s[:200]


# lookup
def lookup(wish: str, threshold: float = MATCH_THRESHOLD) -> Optional[dict]:
    """Embedding-similarity lookup. Returns the cached record dict if a
    semantic match exists above threshold; None otherwise.

    Side-effect on hit: increments use_count + appends the raw variant to
    seen_variants (so the corpus accumulates real player phrasings)."""
    if not (wish or "").strip():
        return None
    norm = normalize_wish(wish)
    if not norm:
        return None

    try:
        vec = _embed(norm)
    except Exception as e:
        log.warning("physics lookup embed failed: %s", e)
        return None

    try:
        entries = [e for e in _cache() if e.get("v")]
        if not entries:
            return None
        q = np.asarray(vec, dtype=np.float32)
        mat = np.stack([_decode(e["v"]) for e in entries])
        denom = np.linalg.norm(mat, axis=1) * (np.linalg.norm(q) or 1.0)
        scores = (mat @ q) / np.where(denom == 0, 1.0, denom)
        best = int(np.argmax(scores))
        score = float(scores[best])
    except Exception as e:
        log.warning("physics cache search failed: %s", e)
        return None
    if score < threshold:
        return None

    rec = entries[best]
    # Bump use stats - errors don't affect result
    try:
        _bump(rec, wish)
    except Exception as e:
        log.debug("physics use-count bump failed: %s", e)

    return _public(rec, score=score)


# strong-model grounding
_PHYSICALISER_SYSTEM = """You are a hard science-fiction physicist embedded in a fantasy/SF game engine.

A player has asserted a magical, impossible, or wished-into-existence thing. Your job is to GROUND that wish in plausible physics: what infrastructure must exist for it to actually work, what energy budget it requires, and what failure modes it has.

Treat every wish like real engineering. An "infinite gold pouch" is implicitly a micro black hole + magnetic containment + isotope extractor + pocket-dimension storage. A flying horse has a propulsion system, a fuel source, and structural fatigue limits. Time-travel has a paradox-vulnerability and a causality cost. You are NOT softening, refusing, or moralising - you are revealing what the wish actually IS, mechanically, when it works.

Be terse, specific and hard-science in tone, with dry humour. "Magnetic confinement," not "magic field." Name materials, energies, failure modes that actually make sense.

Output JSON exactly matching this schema, no prose outside the JSON:

{
  "normalized_wish":   "infinite gold pouch",
  "narrative_hint":    "A worn leather pouch that hums faintly and feels warmer than the air around it.",
  "grounded_mechanism": {
    "source":      "Micro black hole, ~2.4 kg, evaporating slowly via Hawking radiation",
    "containment": "Toroidal magnetic confinement field, requires 12 kJ/s sustain",
    "extractor":   "Mass-to-element converter biased to gold-187 isotope",
    "storage":     "50 m^3 pocket-dimension fold, edge instability +/-2 sigma",
    "interface":   "Leather pouch lining, treated to dampen output to manageable rate"
  },
  "energy_demand":     "12 kJ/s while active. Replenished from local sunlight or operator's lifeforce drain.",
  "failure_modes": [
    {"trigger": "Submersion in lava (>1500K)", "consequence": "Containment fails, black hole eats local geometry within ~200 km", "scale": "regional"},
    {"trigger": "Cold-iron strike at containment ring", "consequence": "Magnetic confinement breaks, antimatter back-flush", "scale": "local"},
    {"trigger": "Pocket-dimension storage overload (>10 tonnes input)", "consequence": "Edge instability cascades, kinetic release ~1 Mt", "scale": "regional"}
  ],
  "vulnerabilities":   ["Heat-death of containment if energy starved", "Operator inexperience can release wrong isotope", "Pouch leather degrades - physical container is the weak link"],
  "scale_of_consequence": "regional"
}

Rules:
- Specific over poetic. Numbers when you can.
- Failure modes are physically meaningful - what actually breaks, what it takes with it.
- "scale" must be one of: local | regional | planetary | stellar | cosmic.
- If the wish is essentially mundane (e.g. "I open my pouch", "I notice a coin on the ground"), output a minimal record: source/containment/extractor empty strings, interface is the mundane object, energy_demand "negligible", failure_modes empty list, scale "local".
- Output ONLY the JSON. No preamble, no commentary, no markdown.
- Inside string values: use ONLY plain ASCII quotes - no smart quotes, no em-dashes (use ' -- ' instead), no unicode arrows. If you need an apostrophe inside a string, write it plainly (the outer quotes are double quotes, so a single ' is safe). Never put an unescaped double-quote inside a string value - rephrase to avoid it."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_parse_json(raw: str) -> Optional[dict]:
    """Strip code-fence + prose envelopes, then try a sequence of
    increasingly-tolerant parsers. Strong-model output is dense and occasionally
    has unescaped commas / smart-quotes / trailing commas - this
    progresses through (a) raw json, (b) trailing-comma cleanup,
    (c) smart-quote normalisation, (d) ast.literal_eval as a last resort."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*", "", s).strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    i = s.find("{")
    j = s.rfind("}")
    if i == -1 or j == -1 or j <= i:
        return None
    body = s[i : j + 1]

    # Pass 1 - raw
    try:
        return json.loads(body)
    except Exception:
        pass

    # Pass 2 - strip trailing commas before } or ]
    cleaned = re.sub(r",(\s*[}\]])", r"\1", body)
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Pass 3 - normalise smart quotes / non-ASCII quotation marks
    repl = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"}
    smart = cleaned.translate(str.maketrans(repl))
    try:
        return json.loads(smart)
    except Exception:
        pass

    # Pass 4 - last resort: ast.literal_eval (handles single quotes, etc.)
    try:
        import ast

        v = ast.literal_eval(smart)
        if isinstance(v, dict):
            return v
    except Exception as e:
        log.warning("physics JSON parse failed (all fallbacks): %s", e)
    return None


def _ground_via_llm(
    wish: str, *, model: str = MODEL_HAIKU, max_tokens: int = PHYSICALISER_MAX_TOKENS_SMALL
) -> tuple:
    """One LLM call at the given (model, max_tokens). Returns
    (record_dict, in_tokens, out_tokens, cache_read, cache_creation, model_used).
    record_dict is None on parse failure. Caller orchestrates the
    haiku-small -> haiku-large -> strong-model escalation ladder."""
    resp = call_messages(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,  # determinism > creativity for cache keys
        system=_PHYSICALISER_SYSTEM,
        messages=[{"role": "user", "content": (wish or "").strip()[:2000]}],
    )
    raw = "".join(b.text for b in resp.content if hasattr(b, "text"))
    rec = _safe_parse_json(raw)
    moderation.check_decoded(rec)
    in_t = resp.usage.input_tokens if resp.usage else 0
    out_t = resp.usage.output_tokens if resp.usage else 0
    cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0 if resp.usage else 0
    cache_cr = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0 if resp.usage else 0
    return rec, in_t, out_t, cache_read, cache_cr, model


# write path
def _store_new(
    record: dict, *, wish_text: str, in_tokens: int, out_tokens: int, model: str = MODEL_HAIKU
) -> dict:
    """Append to the save's physics cache. Idempotent on normalized_wish:
    a unique constraint isn't enforced (different wordings can ground the
    same concept) but if the exact normalized_wish already exists we
    upsert seen_variants instead of duplicating."""
    norm = record.get("normalized_wish") or normalize_wish(wish_text)
    record["normalized_wish"] = norm

    doc = {
        "normalized_wish": norm,
        "seen_variants": [wish_text[:200]],
        "narrative_hint": record.get("narrative_hint", ""),
        "grounded_mechanism": dict(record.get("grounded_mechanism") or {}),
        "energy_demand": record.get("energy_demand", ""),
        "failure_modes": list(record.get("failure_modes") or []),
        "vulnerabilities": list(record.get("vulnerabilities") or []),
        "scale_of_consequence": record.get("scale_of_consequence", "local"),
        "model": model,
        "tokens_in": int(in_tokens),
        "tokens_out": int(out_tokens),
        "cost_usd": 0.0,
        "version": 1,
        "admin_reviewed": False,
        "use_count": 1,
        "created_at": _now(),
        "updated_at": _now(),
    }

    # Upsert by normalized_wish: same canonical form = update variants list
    cache = _cache()
    existing = next((e for e in cache if e.get("normalized_wish") == norm), None)
    if existing:
        _bump(existing, wish_text)
        return _public(existing, score=None)

    doc["_id"] = uuid.uuid4().hex
    # Embedding - best-effort
    try:
        doc["v"] = _encode(_embed(norm))
    except Exception as e:
        log.warning("physics embed failed: %s", e)
    if len(cache) >= PHYSICS_CACHE_MAX:
        del cache[0]
    cache.append(doc)

    return _public(doc, score=None)


def _public(doc: dict, score: Optional[float]) -> dict:
    """JSON-safe projection - stringify the id, attach optional cosine score."""
    if not doc:
        return {}
    return {
        "id": str(doc["_id"]),
        "normalized_wish": doc.get("normalized_wish", ""),
        "seen_variants": list(doc.get("seen_variants") or []),
        "narrative_hint": doc.get("narrative_hint", ""),
        "grounded_mechanism": dict(doc.get("grounded_mechanism") or {}),
        "energy_demand": doc.get("energy_demand", ""),
        "failure_modes": list(doc.get("failure_modes") or []),
        "vulnerabilities": list(doc.get("vulnerabilities") or []),
        "scale_of_consequence": doc.get("scale_of_consequence", "local"),
        "model": doc.get("model", ""),
        "version": int(doc.get("version", 1)),
        "admin_reviewed": bool(doc.get("admin_reviewed")),
        "use_count": int(doc.get("use_count", 0)),
        "tokens_in": int(doc.get("tokens_in", 0)),
        "tokens_out": int(doc.get("tokens_out", 0)),
        "cost_usd": float(doc.get("cost_usd", 0.0)),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "match_score": score,
    }


# main entry
def physicalise(
    wish: str,
    *,
    threshold: float = MATCH_THRESHOLD,
    model: str = MODEL_HAIKU,
    escalate_on_failure: bool = True,
    allow_strong: Callable[[], bool] = lambda: False,
) -> dict:
    """Resolve a wish to its grounded mechanism. Returns:

      {
        "record":         <public-shape dict>,
        "cache_hit":      bool,
        "match_score":    float | None,
        "model_used":     "tier:fast" | "tier:strong",
      }

    Token counts are returned so the caller can append an aux_llm_rows
    entry. A completed provider call is charged to the player's key even
    if the turn later fails.

    Default model is the fast tier (MODEL_HAIKU). On
    parse failure, the call uses the strong tier only if escalate_on_failure
    and allow_strong permit it."""
    cached = lookup(wish, threshold=threshold)
    if cached:
        # Token accounting flows through the engine's aux_llm_rows on completed
        # turns; on a gate the rows are dropped.
        return {
            "record": cached,
            "cache_hit": True,
            "match_score": cached.get("match_score"),
            "model_used": "cache",
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read": 0,
            "cache_creation": 0,
        }

    # Cache miss - three-tier escalation ladder:
    #   1. haiku-small  (default)
    #   2. haiku-large  (more max_tokens; covers truncation-induced parse fails)
    #   3. strong model (last resort)
    rec, in_t, out_t, cr, cc, model_used = _ground_via_llm(
        wish,
        model=model,
        max_tokens=PHYSICALISER_MAX_TOKENS_SMALL,
    )
    cr_total = cr
    cc_total = cc

    if not rec and escalate_on_failure and model == MODEL_HAIKU:
        log.debug("physics: haiku-small unparseable; escalating to haiku-large for %r", wish)
        rec2, in_t2, out_t2, cr2, cc2, _ = _ground_via_llm(
            wish,
            model=MODEL_HAIKU,
            max_tokens=PHYSICALISER_MAX_TOKENS_LARGE,
        )
        cr_total += cr2
        cc_total += cc2
        if rec2:
            rec, in_t, out_t = rec2, in_t2, out_t2

    strong_attempted = False
    if not rec and escalate_on_failure and model_used != MODEL_SONNET and allow_strong():
        strong_attempted = True
        log.debug("physics: haiku-large unparseable; escalating to sonnet for %r", wish)
        rec3, in_t3, out_t3, cr3, cc3, model3 = _ground_via_llm(
            wish,
            model=MODEL_SONNET,
            max_tokens=PHYSICALISER_MAX_TOKENS_SONNET,
        )
        cr_total += cr3
        cc_total += cc3
        if rec3:
            rec, in_t, out_t, model_used = rec3, in_t3, out_t3, model3

    if not rec:
        return {
            "record": None,
            "cache_hit": False,
            "match_score": None,
            "model_used": model_used,
            "tokens_in": int(in_t),
            "tokens_out": int(out_t),
            "cache_read": int(cr_total),
            "cache_creation": int(cc_total),
            "error": (
                "llm output failed to parse (after haiku + sonnet escalation)"
                if strong_attempted
                else "llm output failed to parse (fast tier only)"
            ),
        }

    public = _store_new(rec, wish_text=wish, in_tokens=in_t, out_tokens=out_t, model=model_used)
    # Token counts and cache terms are returned so the caller can compose the
    # aux row.
    return {
        "record": public,
        "cache_hit": False,
        "match_score": None,
        "model_used": model_used,
        "tokens_in": int(in_t),
        "tokens_out": int(out_t),
        "cache_read": int(cr_total),
        "cache_creation": int(cc_total),
    }


def find_one(normalized_wish: str) -> Optional[dict]:
    norm = normalize_wish(normalized_wish)
    rec = next((e for e in _cache() if e.get("normalized_wish") == norm), None)
    return _public(rec, score=None) if rec else None
