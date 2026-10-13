"""Time-skip with pause-on-attention contract.

Player declares 'skip 5 years; advance everything; pause if my
attention is needed'. Engine simulates background world progression
via N internal mini-beats and returns one combined narrative.

Architecture:
  - Detector in player input (regex - pattern is fairly stable)
  - Internal loop of K simulated periods (years / seasons / turns)
  - Per-period interrupt classifier (fast tier)
  - Returned as one narrative + state diff summary

A skip runs at most 30 periods (detect_timeskip caps the span); the player
can request another skip.

This module owns the detection, interrupt classifier, and prompt
composition. The engine.run_turn integration calls into here and
applies the resulting state deltas.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from app.game.llm import MODEL_HAIKU, call_llm_plain
from app.game import moderation
from app.llm_client import ProviderError


log = logging.getLogger("timeskip")

# Shown to the player when a period call fails; the exception goes to the log only.
_SKIP_ERROR_LINE = (
    "The time skip stopped early because the game master could not continue it; "
    "the story is kept up to here."
)


# detection


# Recognised time-skip declarations. Tolerant of phrasings.
_PATTERN_TIMESKIP = re.compile(
    r"\b(?:"
    r"skip\s+(\d+)\s+(years?|seasons?|months?|weeks?|turns?)|"
    r"advance\s+(\d+)\s+(years?|seasons?|months?|weeks?|turns?)|"
    r"fast[- ]forward\s+(\d+)\s+(years?|seasons?|months?|weeks?|turns?)|"
    r"jump\s+(?:ahead\s+)?(\d+)\s+(years?|seasons?|months?|weeks?|turns?)"
    r")\b",
    re.IGNORECASE,
)


# Interrupt-condition phrases - player declares what should stop the skip.
_DEFAULT_INTERRUPT_CONDITIONS = (
    "existential threat to player or civilisation",
    "policy decision requiring player input",
    "first-contact event",
    "war declared on player",
    "an NPC the player cares about is in mortal danger",
)


def detect_timeskip(player_input: str) -> Optional[dict]:
    """If the input requests a time skip, return
    {span_n, span_unit, interrupt_conditions, raw_match}. None otherwise."""
    if not player_input:
        return None
    m = _PATTERN_TIMESKIP.search(player_input)
    if not m:
        return None
    # Pick the first non-None group pair
    n, unit = None, None
    for i in range(1, len(m.groups()), 2):
        if m.group(i):
            n, unit = int(m.group(i)), m.group(i + 1).lower().rstrip("s")
            break
    if n is None or n < 1:
        return None
    # Cap aggressively - the player can always request another skip
    n = min(n, 30)
    # Crude interrupt-condition extraction: player added "pause if X"?
    interrupts = list(_DEFAULT_INTERRUPT_CONDITIONS)
    pause_m = re.search(
        r"pause\s+(?:if|when|on)\s+([^\.]{4,200})",
        player_input,
        re.IGNORECASE,
    )
    if pause_m:
        interrupts.append(pause_m.group(1).strip())
    return {
        "span_n": n,
        "span_unit": unit,
        "interrupt_conditions": interrupts,
        "raw_match": m.group(0),
    }


# interrupt classifier


_INTERRUPT_SYSTEM = """You are the timeskip interrupt classifier for an RPG engine.

The player is fast-forwarding world progression. Each simulated period, you read a one-paragraph summary of what happened in that period and decide whether the player's attention is needed.

Return ONLY a JSON object: {"interrupt": true|false, "reason": "<one short sentence>"}

Interrupt when ANY of the player-declared conditions fire:
{conditions}

Do NOT interrupt for ordinary progression: routine policy issuance, new NPCs being introduced, slow tech advancement, minor disasters with no player exposure. The skip exists to let the player elide these.

Bias toward NOT interrupting. The player asked for the skip; only stop when something genuinely requires their input."""


def _err_label(e: Exception) -> str:
    """The exception's type, plus the provider kind when it has one; never its text."""
    kind = ""
    if isinstance(e, ProviderError) and isinstance(getattr(e, "detail", None), dict):
        kind = f" kind={e.detail.get('kind')}"
    return type(e).__name__ + kind


def classify_interrupt(period_summary: str, interrupt_conditions: list[str]) -> tuple:
    """Tier 1 haiku classifier. Returns
    ({"interrupt": bool, "reason": str}, usage). On any error, returns
    (interrupt=False, None) so the skip continues - caller decides terminal
    behaviour. Returns (verdict, usage)."""
    if not period_summary:
        return {"interrupt": False, "reason": "(no summary)"}, None
    try:
        sys_prompt = _INTERRUPT_SYSTEM.replace(
            "{conditions}",
            "\n".join(f"  - {c}" for c in interrupt_conditions),
        )
        text, usage = call_llm_plain(
            sys_prompt,
            messages=[{"role": "user", "content": period_summary[:1200]}],
            model=MODEL_HAIKU,
            max_tokens=60,
        )
        return _parse_interrupt_json(text), usage
    except Exception as e:
        label = _err_label(e)
        log.warning("timeskip interrupt classifier failed: %s", label)
        return {"interrupt": False, "reason": f"(classifier error: {label})"}, None


def _parse_interrupt_json(text: str) -> dict:
    if not text:
        return {"interrupt": False, "reason": "(empty classifier output)"}
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{[^{}]*\}", s, re.DOTALL)
    if not m:
        return {"interrupt": False, "reason": "(no JSON found)"}
    try:
        d = json.loads(m.group(0))
        moderation.check_decoded(d)
    except Exception:
        return {"interrupt": False, "reason": "(JSON parse error)"}
    return {
        "interrupt": bool(d.get("interrupt") or False),
        "reason": str(d.get("reason") or "")[:200],
    }


# period-advance prompt


_PERIOD_ADVANCE_SYSTEM = """You are the world-progression engine for a long-running RPG.

The player has fast-forwarded time. Your job: narrate ONE simulated period of background world progression. Keep it tight - 4-6 sentences max. Cover material progress, NPC shifts, political/social drift, threat evolution. Write it in story parts, in order: ACKNOWLEDGE the time passing as the player chose; REACT - how the world and its people change over the period; DEVELOP - the one change that matters most for what comes next.
The request says "(period N of M)".
- If N is less than M: end on that most important change. No hook, and do not address the player.
- If N equals M (the final period): add a HOOK as the last sentence - a human hook: a person, a situation or a question waiting for the player now that the skip is over, specific to this world. Never a list of options or a stock prompt such as "What do you do?".

End your response with a single line beginning EVENTS: followed by a brief comma-separated list of the period's notable events (<=8 items). The interrupt classifier reads this line to decide whether to stop the skip. Do not put the hook in the EVENTS: line; the last sentence before it is the hook on the final period, or the most important change on earlier periods.

Tone matches the established world. No metagame language."""


def build_period_prompt(state, period_unit: str, period_index: int, total_periods: int) -> str:
    """Build the user-message content for one period-advance call."""
    npcs = state.get("npcs") or {}
    npc_names = [n.get("name") for n in npcs.values() if isinstance(n, dict) and n.get("name")][:6]
    threats = state.get("active_threats") or []
    quests = state.get("active_quests") or []
    year = state.get("year")
    season = state.get("season")
    arc = state.get("arc")

    parts = [
        f"Advance the world by 1 {period_unit} (period {period_index + 1} of {total_periods}).",
        "",
        f"Current temporal state: year={year}, season={season}, arc={arc}.",
    ]
    if npc_names:
        parts.append(f"Persistent NPCs: {', '.join(npc_names)}.")
    if threats:
        parts.append("Open threats (carry forward / resolve as appropriate):")
        for t in threats[:6]:
            if isinstance(t, dict):
                parts.append(f"  - {t.get('actor_or_thing', '')}")
            else:
                parts.append(f"  - {t}")
    if quests:
        parts.append("Open quests (may progress passively):")
        for q in quests[:6]:
            if isinstance(q, dict):
                parts.append(f"  - {q.get('title', '')}")
            else:
                parts.append(f"  - {q}")
    parts.append("")
    parts.append(f"Narrate the {period_unit}. End with the EVENTS: line.")
    return "\n".join(parts)


def call_period_advance(state, period_unit: str, period_index: int, total_periods: int) -> tuple:
    """Call haiku for one period advance.
    Returns (narrative, usage_dict) with input, output and cache token counts."""
    user = build_period_prompt(state, period_unit, period_index, total_periods)
    text, usage = call_llm_plain(
        _PERIOD_ADVANCE_SYSTEM,
        messages=[{"role": "user", "content": user}],
        model=MODEL_HAIKU,
        max_tokens=350,
    )
    usage_d = {
        "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0 if usage else 0,
        "cache_creation": getattr(usage, "cache_creation_input_tokens", 0) or 0 if usage else 0,
    }
    return text or "", usage_d


def split_narrative_and_events(period_text: str) -> tuple[str, str]:
    """Split a period response into (narrative_paragraph, events_line)."""
    if not period_text:
        return "", ""
    lines = period_text.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("EVENTS:"):
            narr = "\n".join(lines[:i]).strip()
            evts = line.split(":", 1)[1].strip()
            return narr, evts
    return period_text.strip(), ""


# orchestration


def run_timeskip(state, *, skip: dict, on_period=None) -> dict:
    """Top-level orchestration. Caller (engine.run_turn) provides the
    state to mutate + the detected skip dict. Returns:

      {
        "completed_periods":  int,
        "interrupted":        bool,
        "interrupt_reason":   str,
        "narratives":         [str, ...],
        "events_lines":       [str, ...],
        "total_input_tokens": int,
        "total_output_tokens": int,
      }

    State is not directly mutated here - caller decides whether to
    advance state.year/season/turn based on completed_periods. This
    keeps the function pure-ish (LLM calls aside) and testable."""
    span_n = int(skip.get("span_n") or 1)
    span_unit = (skip.get("span_unit") or "year").lower()
    conditions = list(skip.get("interrupt_conditions") or _DEFAULT_INTERRUPT_CONDITIONS)

    narratives: list[str] = []
    events_lines: list[str] = []
    in_total = out_total = 0
    interrupted = False
    interrupt_reason = ""
    completed = 0

    for i in range(span_n):
        try:
            text, usage = call_period_advance(state, span_unit, i, span_n)
        except Exception as e:
            log.warning("timeskip period %d/%d failed: %s", i + 1, span_n, _err_label(e))
            interrupted = True
            interrupt_reason = _SKIP_ERROR_LINE
            break

        narr, events = split_narrative_and_events(text)
        if narr:
            narratives.append(narr)
        if events:
            events_lines.append(events)
        in_total += usage.get("input_tokens", 0)
        out_total += usage.get("output_tokens", 0)

        if on_period is not None:
            try:
                on_period(i, narr, events)
            except Exception:
                pass

        # Interrupt classifier - uses the events line if available, else
        # the trailing narrative paragraph.
        summary = events if events else narr
        verdict, _ = classify_interrupt(summary, conditions)
        if verdict.get("interrupt"):
            interrupted = True
            interrupt_reason = verdict.get("reason", "(no reason given)")
            completed = i + 1
            break

        completed = i + 1

    return {
        "completed_periods": completed,
        "interrupted": interrupted,
        "interrupt_reason": interrupt_reason,
        "narratives": narratives,
        "events_lines": events_lines,
        "total_input_tokens": in_total,
        "total_output_tokens": out_total,
    }


def compose_timeskip_response(skip: dict, result: dict) -> str:
    """Compose the player-facing narrative from the period results."""
    span_n = skip.get("span_n", 1)
    span_unit = skip.get("span_unit", "year")
    parts = [
        f"[TIME SKIP - {result['completed_periods']} of {span_n} {span_unit}"
        + ("s" if span_n != 1 else "")
        + "]",
        "",
    ]
    for n in result["narratives"]:
        parts.append(n)
        parts.append("")
    if result["interrupted"]:
        parts.append("--- attention needed ---")
        parts.append(result["interrupt_reason"] or "Something requires the player's input.")
    return "\n".join(parts).strip()
