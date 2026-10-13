"""Automated-client detector.

A conservative multi-signal detector. Humans are essentially never
flagged; bots degrade aggressively and early.

hard exemption (level=0, flagged=False, no scan):
  - any modal-passback present (consent_token / dice_roll / craft_roll /
    addressee / reframe_confirm / world_rule_confirm / suggestion_index)

signals (soft corroborators - K=2 sustained fires the flag):
  1. repetition - player_input identical to a recent history entry
  2. ua - empty or automation-pattern user-agent string

degradation (aggressive-early):
  level = min(active_signal_count, MAX_LEVEL)
  flagged = (active_signal_count >= K) and no hard-exemption

Per-turn reversible: level recomputed every turn, never latched.
Does not mutate gs.state / gs.history / gs.session_in/out.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# constants

K = 2  # signal count to flag
MAX_LEVEL = 3  # maximum degradation level (maps to pack directive count)
REPEAT_WINDOW = 3  # check last N user-role history entries for repetition

# Patterns that identify likely automated or non-human user-agents.
# Weak corroborator - must combine with other signals to reach K.
_AUTOMATION_UA_RE = re.compile(
    r"python|curl|httpx|requests\b|axios|playwright|selenium|headless|"
    r"puppeteer|scrapy|wget|libwww|java/|go-http|ruby|mechanize",
    re.IGNORECASE,
)

# Assessment


@dataclass
class Assessment:
    """Result of a single-turn automated-client assessment."""

    level: int  # 0..MAX_LEVEL, drives monster directive injection
    flagged: bool  # True when >=K signals and no hard-exemption
    signals: list = field(default_factory=list)  # active signal name list


# signal checks


def _check_repetition(player_input: str, history: list) -> bool:
    """True if player_input (case-insensitive, stripped) matches any of the
    last REPEAT_WINDOW user-role history entries."""
    if not player_input:
        return False
    needle = player_input.strip().lower()
    count = 0
    for entry in reversed(history):
        if entry.get("role") != "user":
            continue
        prev = (entry.get("content_en") or entry.get("content") or "").strip().lower()
        if prev == needle:
            return True
        count += 1
        if count >= REPEAT_WINDOW:
            break
    return False


def _check_ua(ua: str) -> bool:
    """True if ua is empty or matches an automation-pattern user-agent."""
    if not ua or not ua.strip():
        return True
    return bool(_AUTOMATION_UA_RE.search(ua))


# public API


def assess(
    gs,
    player_input: str,
    ua: str,
    *,
    consent_token: Optional[str] = None,
    dice_roll: Optional[int] = None,
    craft_roll: Optional[int] = None,
    addressee=None,
    reframe_confirm: Optional[str] = None,
    world_rule_confirm: Optional[str] = None,
    suggestion_index: Optional[int] = None,
) -> Assessment:
    """Compute the automated-client assessment for the current turn.

    Arguments mirror the run_turn signature so the engine can call this
    at the point in _run_turn_inner where gs/pack/ctx/ua
    are all available.

    Never raises. Fail-open on all signal checks.
    """
    # hard exemption - skip all signal checks
    is_modal_passback = any(
        [
            consent_token is not None,
            dice_roll is not None,
            craft_roll is not None,
            addressee is not None,
            reframe_confirm is not None,
            world_rule_confirm is not None,
            suggestion_index is not None,
        ]
    )
    if is_modal_passback:
        return Assessment(level=0, flagged=False, signals=[])

    # compute signals
    active: list[str] = []
    try:
        history = getattr(gs, "history", []) or []
        if _check_repetition(player_input, history):
            active.append("repetition")
    except Exception as _e:
        log.debug("anti_automation: repetition signal error: %s", _e)

    try:
        if _check_ua(ua):
            active.append("ua")
    except Exception as _e:
        log.debug("anti_automation: ua signal error: %s", _e)

    # derive level and flag
    # aggressive-early: level jumps to MAX_LEVEL as soon as K signals
    # are sustained - no graduated pre-flag escalation.  Below K the level
    # stays 0 so human sessions with a single soft signal never
    # receive any prompt-level degradation.
    n = len(active)
    flagged = n >= K
    level = MAX_LEVEL if flagged else 0

    return Assessment(level=level, flagged=flagged, signals=active)
