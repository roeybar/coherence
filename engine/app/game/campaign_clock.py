"""Campaign clock.

Tracks in-fiction time via state.clock and state.event_log.

state.clock: { year: int, cycle: int, day: int, hour: int, minute: int }
  All integers. hour / minute are optional - packs that want sundial-coarse
  accuracy can omit them; missing keys are treated as 0 when arithmetic runs.
  state.clock is written on the first clock_advance the engine receives.

state.event_log: rolling list of event dicts, capped at EVENT_LOG_CAP entries.
  { event: str, started_at: dict, duration_minutes: int, certainty: float, turn: int }
  Populated by estimate_event_duration after each DM beat (pico-haiku call).

Public API:
  apply_clock_advance(state, clock_advance: dict | None) -> None
  estimate_event_duration(narrative: str, clock_before: dict, turn: int,
                          state) -> None
  render_clock_block(state) -> str
"""

from __future__ import annotations

import json
import logging

from app.game import moderation

log = logging.getLogger(__name__)

EVENT_LOG_CAP = 50

# Day-segment -> canonical start hour (24 h clock).
_SEGMENT_HOURS: dict[str, tuple[int, int]] = {
    "dawn": (5, 0),
    "morning": (8, 0),
    "midday": (12, 0),
    "afternoon": (14, 0),
    "evening": (18, 0),
    "night": (21, 0),
    "next_morning": (8, 0),  # also advances day by 1
}


# helpers


def _get_clock(state) -> dict:
    """Return the current clock dict (always a copy, never None)."""
    raw = state.get("clock") or {}
    return dict(raw) if raw else {}


def _set_clock(state, clk: dict) -> None:
    state.clock = clk


def _fmt_clock(clk: dict) -> str:
    parts = []
    if clk.get("cycle") is not None:
        parts.append(f"cycle {clk['cycle']}")
    elif clk.get("year") is not None:
        parts.append(f"year {clk['year']}")
    if clk.get("day") is not None:
        parts.append(f"day {clk['day']}")
    if clk.get("hour") is not None:
        hh = str(clk["hour"]).zfill(2)
        mm = str(clk.get("minute") or 0).zfill(2)
        parts.append(f"{hh}:{mm}")
    return " | ".join(parts) if parts else "time unknown"


# public API


def apply_clock_advance(state, clock_advance: dict | None) -> None:
    """Apply a clock_advance signal from the DM beat to state.clock.

    Accepts:
      {"advance_minutes": N} - add N minutes; carry into hour/day
      {"advance_to": "segment"} - jump to named segment's start time
    No-ops when clock_advance is None or empty.
    """
    if not clock_advance or not isinstance(clock_advance, dict):
        return

    clk = _get_clock(state)

    advance_minutes = clock_advance.get("advance_minutes")
    advance_to = clock_advance.get("advance_to")

    if advance_to and isinstance(advance_to, str):
        seg = advance_to.lower().strip().replace(" ", "_").replace("-", "_")
        if seg not in _SEGMENT_HOURS:
            # Snap LLM drift ("late morning" -> "morning") to a known segment
            # so the clock advances instead of silently stalling.
            seg = next((k for k in _SEGMENT_HOURS if k in seg), seg)
        if seg in _SEGMENT_HOURS:
            h, m = _SEGMENT_HOURS[seg]
            if seg == "next_morning":
                clk["day"] = int(clk.get("day") or 1) + 1
            clk["hour"] = h
            clk["minute"] = m
        else:
            log.debug("campaign_clock: unknown advance_to segment %r", seg)
        _set_clock(state, clk)
        return

    if advance_minutes is not None:
        try:
            mins = int(advance_minutes)
        except (TypeError, ValueError):
            log.debug("campaign_clock: bad advance_minutes %r", advance_minutes)
            return
        if mins <= 0:
            return

        # If the clock has hour/minute, do full minute arithmetic.
        if "hour" in clk or "minute" in clk:
            total_min = int(clk.get("minute") or 0) + mins
            extra_hours, clk["minute"] = divmod(total_min, 60)
            total_hours = int(clk.get("hour") or 0) + extra_hours
            extra_days, clk["hour"] = divmod(total_hours, 24)
            clk["day"] = int(clk.get("day") or 1) + extra_days
        else:
            # Sundial-coarse pack: accumulate minutes as fractional days
            # only when we cross the full-day threshold. Packs that don't
            # want day tracking at all won't have "day" in clock either.
            if "day" in clk:
                total_min = mins
                extra_days = total_min // (24 * 60)
                clk["day"] = int(clk.get("day") or 1) + extra_days

        _set_clock(state, clk)


def _build_scene_context(state) -> str:
    """Compact scene context string for the pico-haiku suggested-moves prompt."""
    parts = []
    location = str(state.get("location") or "").strip()
    if location:
        parts.append(f"Location: {location}")
    threats = state.get("active_threats") or []
    if isinstance(threats, list) and threats:
        parts.append(f"Threats: {', '.join(str(t) for t in threats[:3])}")
    conditions = state.get("player_conditions") or []
    if isinstance(conditions, list) and conditions:
        parts.append(f"Conditions: {', '.join(str(c) for c in conditions[:3])}")
    quests = state.get("quests") or []
    if isinstance(quests, list) and quests:
        q_names = []
        for q in quests[:3]:
            if isinstance(q, dict):
                q_names.append(str(q.get("name") or q.get("title") or ""))
            elif isinstance(q, str):
                q_names.append(q)
        q_names = [n for n in q_names if n]
        if q_names:
            parts.append(f"Quests: {', '.join(q_names)}")
    return "\n".join(parts)


def _truncate_move(text: str, max_words: int = 10) -> str:
    words = text.strip().split()
    return " ".join(words[:max_words])


def estimate_event_duration(
    narrative: str,
    clock_before: dict,
    turn: int,
    state,
    turn_class: str = "",
):
    """Run a pico-haiku call to estimate event duration and suggest next moves.

    Appends a record to state.event_log (capped at EVENT_LOG_CAP) when the
    pack has clock tracking enabled.  Always writes state.suggested_moves
    (cleared to [] when narrative empty or generation yields none). Handles
    ability, craft, encounter, timeskip, and standard turns identically.

    Returns the resp.usage object from the haiku call so the engine can
    append an aux row with its token counts (aux:clock). Returns None when no LLM call
    was made or on any error.

    Best-effort - logs and returns if anything fails; never blocks the turn.
    """
    if not narrative or not narrative.strip():
        state.suggested_moves = []
        return None

    # Clear first so any subsequent parse/LLM failure leaves [] rather than
    # stale options from a prior turn.
    state.suggested_moves = []

    has_clock = bool(state.get("clock") or state.get("event_log") is not None)

    try:
        from app.llm_client import call_messages
        from app.game.llm import MODEL_HAIKU

        if has_clock:
            system = (
                "You are a timeline analyst for a text RPG. "
                "Given the narrative and scene context, return ONLY valid JSON: "
                '{"event": "<one sentence describing the main action>", '
                '"duration_minutes": <integer, realistic in-universe duration>, '
                '"certainty": <float 0.0-1.0>, '
                '"suggested_moves": ["<action, max 8 words>", "<action, max 8 words>", '
                '"<action, max 8 words>"]}. '
                "Duration 0 for brief exchanges. "
                "Suggested moves: 3 specific player actions fitting the current scene. "
                "No explanation, just JSON."
            )
        else:
            system = (
                "You are a scene assistant for a text RPG. "
                "Given the narrative and scene context, return ONLY valid JSON: "
                '{"suggested_moves": ["<action, max 8 words>", "<action, max 8 words>", '
                '"<action, max 8 words>"]}. '
                "Suggested moves: 3 specific player actions fitting the current scene. "
                "No explanation, just JSON."
            )

        scene_ctx = _build_scene_context(state)
        user_content = narrative[:1200]
        if scene_ctx:
            user_content += f"\n\n---SCENE CONTEXT---\n{scene_ctx}"

        messages = [{"role": "user", "content": user_content}]
        resp = call_messages(
            model=MODEL_HAIKU,
            max_tokens=150,
            system=[{"type": "text", "text": system}],
            messages=messages,
        )
        raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()

        # Strip markdown code fences if the model wraps in ```json
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = json.loads(raw)
        moderation.check_decoded(parsed)

        if has_clock:
            entry = {
                "event": str(parsed.get("event", ""))[:120],
                "started_at": dict(clock_before),
                "duration_minutes": max(0, int(parsed.get("duration_minutes") or 0)),
                "certainty": float(parsed.get("certainty") or 0.5),
                "turn": turn,
            }
            moderation.check_decoded(entry["event"])
            log_list = list(state.get("event_log") or [])
            log_list.append(entry)
            state.event_log = log_list[-EVENT_LOG_CAP:]

        raw_moves = parsed.get("suggested_moves") or []
        moves = []
        for m in raw_moves[:3]:
            if isinstance(m, str) and m.strip():
                moves.append(_truncate_move(m))
        moderation.check_decoded(moves)
        state.suggested_moves = moves

        return resp.usage

    except Exception as exc:
        log.debug("campaign_clock estimate_event_duration failed: %s", exc)
        return None


def render_clock_block(state) -> str:
    """Return a short campaign clock line for the DM system prompt.

    Only emits when state.clock has content.
    """
    clk = _get_clock(state)
    if not clk:
        return ""
    return f"CAMPAIGN CLOCK: {_fmt_clock(clk)}"
