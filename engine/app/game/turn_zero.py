"""Turn-zero player input normalisation.

A new player who types `i loko aroundd` or `i'm a giurl` on their very
first turn deserves a graceful first response - not the DM faithfully
reproducing the typo as proper-noun lore, and not the DM pushing back on
identity. Two deterministic transforms run before the LLM ever sees the
text:

  - _collapse_runs:         heyyyyy -> hey, dragoooon -> dragoon
                             (caps runs of the same character to 2; common
                              English-language excitement / mashed-key fix.)
  - _detect_identity_flip:  matches a plain "i'm a girl|boy|woman|man|..."
                             pattern outside quoted speech and returns the
                             new gender so the engine can flip
                             gs.profile.gender immediately.

Pure functions; no LLM, no IO. Designed to be safe-by-default - every
transform short-circuits on input that doesn't match. Run only when
gs.turn == 0 and locale == "en" (English only).
"""

from __future__ import annotations

import re
from typing import Optional


# repeated-character collapser
# Cap any run of >=3 identical characters to 2. We allow 2 because real
# English words have doubles ("see", "look", "happen"); only >=3 is
# excitement/mashed-key fluff. We leave digits and punctuation alone:
# "1000" must not become "100", "..." stays as ellipsis.
_RUN_RE = re.compile("([a-zA-Z\u05d0-\u05ea])\\1{2,}")


def _collapse_runs(text: str) -> str:
    return _RUN_RE.sub(lambda m: m.group(1) * 2, text)


# identity-flip detector
# Matches: "i'm a girl", "i am a boy", "I'm female", "i am male" - but
# not inside double-quoted speech ("she said 'i'm a girl'"), since
# quoted speech is the player narrating an NPC, not themselves.
#
# The regex covers girl/boy/woman/man/female/male; we keep that
# set deliberately small to avoid false positives on roleplay flavour
# ("i'm a knight", "i'm a wizard" are character-class statements, not
# gender flips).
_IDENTITY_RE = re.compile(
    r"\b(?:i'?m|i\s+am)\s+(?:a\s+)?(girl|boy|woman|man|female|male)\b",
    re.IGNORECASE,
)

_FEM = {"girl", "woman", "female"}
_MAS = {"boy", "man", "male"}


def _strip_quoted(text: str) -> str:
    """Blank out everything inside single/double quotes so quoted speech
    can't trip the identity regex. Keeps positions stable by replacing
    with spaces of equal length."""

    def _blank(m: re.Match) -> str:
        return " " * len(m.group(0))

    # Order matters: greedy outer pairs first.
    text = re.sub(r'"[^"]*"', _blank, text)
    text = re.sub(r"'[^']*'", _blank, text)
    return text


def _detect_identity_flip(text: str) -> Optional[str]:
    """Returns "f" / "m" if the player asserts a gender outside quotes,
    else None. Last match wins on multi-clause inputs."""
    if not text:
        return None
    cleaned = _strip_quoted(text)
    last: Optional[str] = None
    for m in _IDENTITY_RE.finditer(cleaned):
        word = m.group(1).lower()
        if word in _FEM:
            last = "f"
        elif word in _MAS:
            last = "m"
    return last


# public entry


def normalise_turn_zero(text: str) -> tuple[str, Optional[str]]:
    """Apply turn-zero deterministic fixes.

    Returns (normalised_text, gender_override).

    `normalised_text` may be identical to the input (collapse_runs is
    a no-op on already-clean text). `gender_override` is "f" / "m" /
    None - the engine should set gs.profile.gender to this value when
    not None, and persist it.
    """
    if not text:
        return text, None
    out = _collapse_runs(text)
    flip = _detect_identity_flip(out)
    return out, flip
