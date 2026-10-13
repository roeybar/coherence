import json
import re
from dataclasses import dataclass, field

from app.game import moderation
from app.llm_client import ProviderError


@dataclass
class ParseResult:
    narrative: str
    delta: dict
    raw: str
    signals: dict
    found_narrative: bool = True
    found_delta: bool = True
    delta_valid_json: bool = True
    delta_ignored_keys: list = field(default_factory=list)


SIGNAL_KEYS = {"checkpoint_worthy", "tier5_reset", "clock_advance"}


class ReplyIncomplete(ProviderError):
    """A tagged reply was cut (or carried no stop reason), or its narration was
    empty, after this move's one retry. The move is refused as provider_unavailable."""

    def __init__(self):
        super().__init__(502, {"kind": "provider_unavailable", "provider": None, "status": None})


def _extract_narrative(raw: str) -> tuple[str, bool]:
    """Robust narrative extraction.

    Strict format: [NARRATIVE]...[/NARRATIVE]. The strong model sometimes drops the
    opening tag entirely and just emits prose followed by [/NARRATIVE] then
    [STATE_DELTA]...[/STATE_DELTA]. Fall back to grabbing everything before
    whichever closing-or-state tag appears first, and strip a stray opening
    tag if it leads.
    """
    m = re.search(r"\[NARRATIVE\](.*?)\[/NARRATIVE\]", raw, re.DOTALL)
    if m:
        return m.group(1).strip(), True

    cut = len(raw)
    for tag in ("[/NARRATIVE]", "[STATE_DELTA]"):
        idx = raw.find(tag)
        if 0 <= idx < cut:
            cut = idx

    text = raw[:cut].strip()
    if text.startswith("[NARRATIVE]"):
        text = text[len("[NARRATIVE]") :].strip()
    return text, cut < len(raw)


def parse_response(raw: str, valid_keys: set) -> ParseResult:
    delta = {}
    signals = {}
    found_delta = False
    delta_valid_json = True

    narrative, found_narrative = _extract_narrative(raw)

    m = re.search(r"\[STATE_DELTA\](.*?)\[/STATE_DELTA\]", raw, re.DOTALL)
    if m:
        found_delta = True
        try:
            raw_delta = json.loads(m.group(1).strip())
            moderation.check_decoded(raw_delta)
            for sig in SIGNAL_KEYS:
                if sig in raw_delta:
                    signals[sig] = raw_delta.pop(sig)
            delta = raw_delta
        except json.JSONDecodeError:
            delta_valid_json = False

    ignored = [k for k in delta if k not in valid_keys]
    return ParseResult(
        narrative=narrative,
        delta={k: v for k, v in delta.items() if k in valid_keys},
        raw=raw,
        signals=signals,
        found_narrative=found_narrative,
        found_delta=found_delta,
        delta_valid_json=delta_valid_json,
        delta_ignored_keys=ignored,
    )


# Rewrites the move did not make. A str value that only
# restates the current value in another case or spacing is not a change. The
# counters cycle, chapter and day may stay or step up by one; a decrease or a
# larger jump is ignored.
ENGINE_COUNTERS = ("cycle", "chapter", "day")


def _norm_text(v) -> str:
    return " ".join(str(v).split()).casefold()


def guard_delta(delta: dict, current: dict) -> tuple[dict, list]:
    """Drop delta fields that rewrite what the move did not change.
    Returns (kept delta, dropped field names)."""
    kept, dropped = {}, []
    for key, value in (delta or {}).items():
        old = (current or {}).get(key)
        if (
            isinstance(old, str)
            and isinstance(value, str)
            and value != old
            and _norm_text(value) == _norm_text(old)
        ):
            dropped.append(key)
            continue
        if key in ENGINE_COUNTERS and isinstance(old, int) and not isinstance(old, bool):
            try:
                new = int(value)
            except (TypeError, ValueError):
                new = None
            if new is not None and not (old <= new <= old + 1):
                dropped.append(key)
                continue
        kept[key] = value
    return kept, dropped
