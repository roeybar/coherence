"""check_hook reads how the final paragraph of a reply, as the player will read it, ends. A reply
passes when it is empty, when its final paragraph holds no double quote marks and does not end on a
stock prompt, or when its last sentence ends on a run of end marks (a ' ) or right single quote
after the run is ignored) that holds "?" and is not a stock prompt, the player's own line (its
words in order within a quote the player typed, ignoring case, punctuation, question marks and
chat spellings), or inside a paragraph whose double quote marks do not pair up. Every
double-quoted span counts as speech; single quotes and guillemets are not read as quotes. On a
miss, repair_hook asks one small cached model call for a hook beat and adds it unless it is a
stock prompt, a beat with any paragraph whose double quote marks do not pair up, the player's own
line, a repeat of the reply or the move (an exact repeat of a short quoted line counts), or a beat
that verbatim enforcement would change; a statement beat is allowed. A blocked hook line, or
missing word lists, refuse the move like any blocked reply; any other failure leaves the reply as
written."""

import logging
import re
import unicodedata
from dataclasses import dataclass

from app.game import moderation
from app.game.beats.builtin import (
    BLOCKED_STOPS,
    TRUNCATION_STOPS,
    _TRAILING_PROMPT_PATTERNS,
    _replace_em_dashes,
    _strip_metagame_brackets,
    _strip_trailing_prompt,
)
from app.game.llm import cache_split_system, call_llm_reply
from app.game.parser import parse_response
from app.game.prompts import build_system_blocks
from app.game.quoted_speech import ensure_verbatim, extract_player_quotes, line_within_quote
from app.llm_client import ProviderError

log = logging.getLogger("coherence.llm.hook")  # child of the call log: same stream, INFO
REPAIR_MAX_TOKENS = 150
BEAT_MAX_CHARS = 400
MOVE_MAX_CHARS = 600
HOOK_SHAPES = ("question", "narrator_question")
PASS_SHAPES = HOOK_SHAPES + ("no_speech", "empty")
MISS_SHAPES = ("statement", "after_quote", "player_line", "stock", "unbalanced")
_ELLIPSIS = chr(0x2026)
_G = chr(0x05F4)
_DOUBLE = {ord(c): '"' for c in (chr(0x201C), chr(0x201D), _G)}
_APOS = {ord(c): "'" for c in (chr(0x2018), chr(0x2019), chr(0x02BC), chr(0x05F3))}
# A gershayim between two word characters abbreviates (as in quoted_speech); it is no quote.
_ABBR = re.compile(r"(?<=\w)" + _G + r"(?=\w)")
_PARA = re.compile(r"\n[^\S\n]*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?" + _ELLIPSIS + r"])\s+")
_END_RUN = re.compile(r"[.!?" + _ELLIPSIS + r"]+$")
_LEADING = re.compile(r"^(?:and|but|so|then|now)\b[\s,]*")
_STOCK_EXTRA = ("what's your next move", "what is your next move", "what's your next move, then")
_REPAIR_FOCUS = (
    "BEAT FOCUS - hook: the reply above is already shown to the player and does not end on "
    "a hook. This call writes ONLY the fourth STORY PART, the HOOK, which comes straight "
    "after the reply: one or two short new sentences in which a person already named or present "
    "in the reply above puts a new question to the player, in straight double quotes, with "
    "at most one short gesture before the opening quote and nothing after the closing quote. "
    "Never bring in a new or unnamed voice. Never repeat or "
    "rephrase a line that is already in the reply or in the player's move. If the reply ends "
    "on the player's own words, the other person answers them and ends on a question to the "
    "player. If no one in the scene can speak, end on one plain sentence that asks the player "
    "a question. Do not change or continue the reply, do not retell the act, and add no "
    "development. "
    'Never a list of options or a stock "What do you do?". The state delta is empty.'
)


@dataclass
class HookReport:
    before: str
    repair: str = "none"
    after: str = ""
    beat: str = "-"


def _last_sentence(text: str) -> str:
    return _SENTENCE_END.split(text.strip())[-1]


def _ends_on_question(text: str) -> bool:
    m = _END_RUN.search(text.rstrip().rstrip("')" + chr(0x2019)).rstrip())
    return bool(m) and "?" in m.group()


def _stock(sentence: str) -> bool:
    s = _LEADING.sub("", sentence.translate(_APOS).strip().lower()).rstrip("?.!").strip()
    return (
        s in _TRAILING_PROMPT_PATTERNS
        or s.rstrip("?.!") in _TRAILING_PROMPT_PATTERNS
        or s in _STOCK_EXTRA
    )


def _final_paragraph(text: str) -> str:
    """The last non-empty paragraph, with format characters, abbreviation marks and quote styles folded."""
    t = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    paras = [p.strip() for p in _PARA.split(_ABBR.sub("", t).translate(_DOUBLE)) if p.strip()]
    return paras[-1] if paras else ""


def check_hook(text: str, player_input: str = "") -> str:
    """Shape of how the final paragraph ends; a shape in PASS_SHAPES is not repaired."""
    t = _final_paragraph(text)
    if not t:
        return "empty"
    if t.count('"') % 2:
        return "unbalanced"
    if t.endswith('"'):
        inner = t[t.rfind('"', 0, len(t) - 1) + 1 : -1].strip()
        last = _last_sentence(inner)
        quotes = extract_player_quotes(player_input or "")
        if inner and any(line_within_quote(inner, q) for q in quotes):
            return "player_line"
        if _stock(last) or _stock(_last_sentence(t.replace('"', ""))):
            return "stock"
        if _ends_on_question(inner):
            return "question"
        return "statement"
    tail = t[t.rfind('"') + 1 :]
    # No letter or digit after the last quote: judge the sentence with its quote marks removed.
    last = _last_sentence(tail if any(ch.isalnum() for ch in tail) else t.replace('"', ""))
    if _stock(last) or _stock(_last_sentence(t.replace('"', ""))):
        return "stock"
    if _ends_on_question(last):
        return "narrator_question"
    return "after_quote" if '"' in t else "no_speech"


def _reason(e: BaseException) -> str:
    if isinstance(e, ProviderError):
        return "provider"
    if isinstance(e, TimeoutError):
        return "timeout"
    return type(e).__name__


def _fold_space(s: str) -> str:
    """Lower case, one space per whitespace run, quote styles folded."""
    return re.sub(r"\s+", " ", s.translate(_DOUBLE).translate(_APOS)).strip().lower()


def _contains(text: str, span: str) -> bool:
    """span occurs in text as whole words, ignoring case, spacing and quote style."""
    n = _fold_space(span)
    found = re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", _fold_space(text))
    return bool(n) and found is not None


def _quoted(s: str) -> list:
    """The non-empty double-quoted spans of s, stripped."""
    return [x.strip() for x in s.translate(_DOUBLE).split('"')[1::2] if x.strip()]


def _repeats(beat: str, shown: str, move: str) -> bool:
    """True when the whole beat is already in the reply as shown or in the player's move (as whole
    words); when a quoted line of the beat equals a whole quoted line there; or when a quoted line
    of the beat (two words or more) or its last sentence (three words or more) is already there."""
    if _contains(shown, beat) or _contains(move, beat):
        return True
    whole = {_fold_space(x) for x in _quoted(shown) + _quoted(move)}
    if any(_fold_space(s) in whole for s in _quoted(beat)):
        return True
    folded = beat.translate(_DOUBLE)
    spans = [s.strip() for s in folded.split('"')[1::2] if len(s.split()) >= 2]
    last = _last_sentence(folded.replace('"', " ")).strip()
    if len(last.split()) >= 3:
        spans.append(last)
    return any(_contains(shown, s) or _contains(move, s) for s in spans)


def _repair(ts, move: str, shown: str) -> tuple:
    """One cached call for the hook beat. Returns (repair, after_shape or "", beat_shape or "-")."""
    gs = ts.gs
    stable = build_system_blocks(
        gs.profile,
        ts.stage,
        gs.config,
        monster_level=ts.monster_level,
        has_npcs=ts.has_npcs,
        turn_class=ts.turn_class,
        seed_roster=gs.seed_roster,
    )[0]
    user = (
        "[PLAYER MOVE]\n"
        + move[:MOVE_MAX_CHARS]
        + "\n[/PLAYER MOVE]\n\n[REPLY AS WRITTEN]\n"
        + shown
        + "\n[/REPLY AS WRITTEN]\n\n"
        + _REPAIR_FOCUS
    )
    raw, usage, stop, truncated = call_llm_reply(
        cache_split_system(stable, ""),
        [{"role": "user", "content": user}],
        model=ts.turn_model,
        max_tokens=REPAIR_MAX_TOKENS,
    )
    if usage:
        ts.ctx.record(usage)
        ts.record_beat(
            beat_id="aux:hook_repair",
            model=ts.turn_model,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
    if stop in BLOCKED_STOPS:
        raise moderation.OutputBlocked("provider_filter")
    if truncated or stop in TRUNCATION_STOPS:
        return "failed:cut", "", "-"
    beat = parse_response(raw, set()).narrative or ""
    beat = _strip_metagame_brackets(_strip_trailing_prompt(_replace_em_dashes(beat))).strip()
    moderation.check_decoded(beat)
    if not beat:
        return "failed:empty", "", "-"
    if len(beat) > BEAT_MAX_CHARS:
        return "failed:long", "", "-"
    if any(p.count('"') % 2 for p in _PARA.split(_ABBR.sub("", beat).translate(_DOUBLE))):
        return "failed:recheck", "", "unbalanced"
    would_be = "\n\n".join(c for c in ts.narrative_chunks + [beat] if c).strip()  # state.py:139
    shown_after = ensure_verbatim(move, would_be)[0]
    if shown_after != ensure_verbatim(move, would_be[: -len(beat)])[0] + beat:
        return "failed:verbatim", "", "-"
    after = check_hook(shown_after, move)
    if _repeats(beat, shown, move):
        return "failed:repeat", "", after
    if after in ("stock", "player_line", "unbalanced"):
        return "failed:recheck", "", after
    ts.narrative_chunks.append(beat)  # last statement: nothing after it can fail
    return "added", after, after


def _log_report(ts, r: HookReport) -> None:
    try:
        log.info(
            f"hook_check path={ts.turn_class or '-'} profile={ts.length_profile or '-'} "
            f"before={'pass' if r.before in PASS_SHAPES else 'miss'} shape={r.before} "
            f"repair={r.repair} after={'pass' if r.after in PASS_SHAPES else 'miss'} "
            f"after_shape={r.after} beat_shape={r.beat}"
        )
    except (Exception, moderation.OutputBlocked, moderation.ListsUnavailable):
        pass


def repair_hook(ts, player_input: str) -> HookReport:
    """Judge the reply as the player will read it (after verbatim enforcement); on a miss,
    add one hook beat. A blocked hook line, or missing word lists, refuse the move like any
    blocked reply; any other failure leaves the reply as written."""
    r = HookReport(before="error", after="error")
    try:
        move = player_input or ""
        shown = ensure_verbatim(move, ts.narrative)[0]
        r.before = r.after = check_hook(shown, move)
        if r.before not in PASS_SHAPES:
            r.repair, after, beat = _repair(ts, move, shown)
            r.after = after or r.before
            r.beat = beat
    except moderation.OutputBlocked:
        r.repair = "refused:blocked"
        _log_report(ts, r)
        raise
    except moderation.ListsUnavailable:
        r.repair = "refused:lists_unavailable"
        _log_report(ts, r)
        raise
    except Exception as e:
        r.repair = "failed:" + _reason(e)
        r.after = r.before
    _log_report(ts, r)
    return r
