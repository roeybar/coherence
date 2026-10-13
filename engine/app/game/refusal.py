"""Refusal-of-player-authorship detector.

Inside the first 5 turns of any save, players are establishing the
world alongside the DM. The DM's job is to translate fanciful or
out-of-world additions into world idiom - never to flat-refuse them
('you don't see a demon, you're still alone') or imply the player is
hallucinating. This module is a tiny regex-only detector that scans
the DM's narrative for those refusal patterns. On a match the engine
attaches a `welcome_intervention` payload to the assistant history
entry, and the frontend renders a colored DM-bubble asking the
player whether they really wanted the thing or were describing
something else.

Deliberately deterministic + regex-only - no LLM call, no DB hit.
Pattern set is tight; false-positive rate is monitored via the
welcome_refusal_detected telemetry event. Tune the patterns here as
the corpus tells us what's missing or over-firing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Window: refusal-detection only runs while the player is still
# co-authoring the world. After turn 4 the player has accepted the
# world's shape; the DM "refusing" their addition is no longer a
# violation of welcome-grace (it could be a legitimate consequence).
WELCOME_GRACE_TURN_CAP = 5


# Each pattern is a (label, regex) tuple. Patterns are tight on purpose
# - we'd rather miss a few than over-fire on atmospheric narration
# (e.g. "the wind dies; nothing answers but the cold" is fine; "you
# don't see a demon" is not).
_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "you_dont_see",
        re.compile(
            r"\byou\s+(?:don'?t|do\s+not)\s+(?:see|notice|find|encounter|spot)\b", re.IGNORECASE
        ),
    ),
    (
        "nothing_x",
        re.compile(
            # "nothing happens / appears / is here / is there / answers your"
            r"\bnothing\s+(?:happens|appears|is\s+here|is\s+there|answers\s+(?:your|to))\b",
            re.IGNORECASE,
        ),
    ),
    ("still_alone", re.compile(r"\byou(?:'?re|\s+are)\s+(?:still\s+)?alone\b", re.IGNORECASE)),
    ("no_x_is_here", re.compile(r"\bno\s+\w+\s+is\s+(?:here|there|present)\b", re.IGNORECASE)),
    (
        "must_be_imagining",
        re.compile(
            r"\byou\s+must\s+be\s+(?:imagining|hallucinating|dreaming|mistaken)\b", re.IGNORECASE
        ),
    ),
    (
        "there_is_no",
        re.compile(
            # "there is no demon, there is no robot, ..." - direct denial
            r"\bthere\s+(?:is|are)\s+no\s+\w+\b(?:\s+here|\s+there)?",
            re.IGNORECASE,
        ),
    ),
]


@dataclass
class RefusalCheck:
    """Result of running the detector on DM narrative text."""

    refused: bool
    pattern_label: Optional[str] = None
    matched_text: Optional[str] = None  # the substring that matched


def detect(narrative: str) -> RefusalCheck:
    """First-match-wins scan. Returns RefusalCheck with refused=False
    if no pattern hit, else carries the label + the matched substring
    (truncated to 80 chars) for telemetry + UI display."""
    if not narrative:
        return RefusalCheck(refused=False)
    for label, pat in _PATTERNS:
        m = pat.search(narrative)
        if m:
            sample = m.group(0)[:80]
            return RefusalCheck(refused=True, pattern_label=label, matched_text=sample)
    return RefusalCheck(refused=False)


def in_welcome_grace_window(turn_index: int) -> bool:
    """True when refusal-detection should be active. Currently gates
    on turn index alone - works identically for the Coherence pack,
    custom worlds, and freeform mode (tutorial_stage is unreliable
    outside the built-in pack)."""
    try:
        return int(turn_index) < WELCOME_GRACE_TURN_CAP
    except (TypeError, ValueError):
        return False
