"""Deterministic enforcement of the player's quoted words.

Players write things like:
  > I tell the cook "your soup is divine and I'd marry you for it"

... and want those exact words to come out of their character's mouth
in the DM's narrative, not a paraphrase. Prompt rules alone do not always
get this right.

This module repairs the rest server-side. After the DM beat returns
the narrative, we:

  1. extract every quoted substring from the player_input
  2. check each is present in the narrative (see _quote_present)
  3. for each missing quote, splice it back in by either
       (a) finding a speech-marker (you say / tell / reply / ask / ...)
           and rewriting what follows it as `, "<exact quote>"`
       (b) prepending `You say, "<exact quote>".` to the narrative if
           no marker is found (the full stop is added only when the quote
           does not end a sentence itself: . ! ? or an ellipsis)

Counter is exposed in TurnState (quoted_speech_repaired) for the current
turn only; no per-turn record is kept.

Quote-flavor coverage in the player's input (extract_player_quotes):
  - ASCII straight double quote (U+0022)
  - curly double quotes (U+201C ... U+201D)
  - Hebrew gershayim (U+05F4 ... U+05F4)
  - curly single quotes (U+2018 ... U+2019; the first U+2019 not followed
                  by a word character closes it, so don't does not, and a
                  plural possessive (the guards' orders) does)
  - ASCII single quote (U+0027)  (not extracted - too much overlap with
                  contractions like won't / I'd / it's)

In the narrative, the same five styles count as quotations that a speech
marker must not be used inside or across (see _quotation_spans).

Never raises for str arguments.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple


# Quote-extraction patterns, in priority order. Group 1 of each pattern
# captures the inner content (excluding the quote characters themselves).
# Pre-compiled to keep run_turn hot-path cheap. The curly single pattern is
# lazy: the first U+2019 not followed by a word character closes, while a
# U+2019 before a letter (don't) is an apostrophe, not the closer. Accepted
# trade-off: a plural possessive inside a curly single quote (the guards'
# orders) ends the quote there, because the alternative runs into the action
# text after it.
_QUOTE_PATTERNS = [
    re.compile(r'"([^"\n]{1,500})"'),  # ASCII straight  "..."
    re.compile("\u201c([^\u201c\u201d\\n]{1,500})\u201d"),  # curly double U+201C..U+201D
    re.compile("\u05f4([^\u05f4\\n]{1,500})\u05f4"),  # Hebrew gershayim U+05F4..U+05F4
    re.compile("\u2018([^\u2018\\n]{1,500}?)\u2019(?!\\w)"),  # curly single U+2018..U+2019
]

# Opening quote characters (U+0022, U+201C, U+05F4, U+2018). A paraphrase that
# holds one is part of a quote the narrative already carries (the paraphrase
# stops at the first . ! or ?, which may sit inside that quote).
_OPEN_QUOTES = '"\u201c\u05f4\u2018'

# Quotations in the narrative (unlike _QUOTE_PATTERNS, which are built for the
# player's input, there is no length cap), found one paragraph at a time:
#   1. A paragraph ends at a blank line.
#   2. The double styles pair inside a paragraph. Curly U+201C/U+201D is
#      directional and pairs left to right; a paragraph whose curly marks do
#      not pair up is protected whole. For ASCII " and gershayim U+05F4 each
#      mark is read from its context (a gershayim between two letters is an
#      abbreviation mark and is skipped). OPENER: at the paragraph start, or
#      after whitespace, one of ( [ { or an em dash, and followed by a
#      non-space. CLOSER: after a non-space, and at the paragraph end or
#      followed by whitespace, one of . , ; : ! ? ) ] } or an em dash. A mark
#      that fits both or neither is ambiguous. If the marks read strictly
#      OPENER, CLOSER, OPENER, CLOSER, ... (even count, no ambiguous mark),
#      each OPENER-CLOSER pair is one quotation; otherwise the whole
#      paragraph is protected.
#   3. The single styles run to the last plausible closer in the paragraph,
#      or to the paragraph end if there is none. ASCII ': the opener is not
#      after a word character and is before a non-space; the closer is after
#      a non-space and not before a word character. Curly: U+2018 opens, and
#      the last U+2019 not before a word character in the paragraph closes.
#   4. A single-quote quotation that is missing its opener cannot be told
#      apart from a plural possessive (the guards' orders), so it is not
#      detected.
_CO, _CC = chr(0x201C), chr(0x201D)
_G = chr(0x05F4)
_SO, _SC = chr(0x2018), chr(0x2019)
_PARAGRAPH = re.compile(r"(?:(?!\n[^\S\n]*\n).)+", re.DOTALL)
_GQ = "(?:(?<!\\w)" + _G + "|" + _G + "(?!\\w))"
_CURLY_PAIR = re.compile(_CO + "[^" + _CO + _CC + "]*" + _CC)
_CURLY_MARK = re.compile("[" + _CO + _CC + "]")
_FLAT_MARKS = [re.compile('"'), re.compile(_GQ)]  # opener and closer look alike
_EM_DASH = chr(0x2014)
_BEFORE_OPENER = "([{" + _EM_DASH  # besides whitespace and the paragraph start
_AFTER_CLOSER = ".,;:!?)]}" + _EM_DASH  # besides whitespace and the paragraph end
_SINGLE_QUOTES = [
    re.compile(r"(?<!\w)'(?=\S)(?:.*(?<=\S)'(?!\w)|.*)", re.DOTALL),
    re.compile(_SO + "(?:.*" + _SC + "(?!\\w)|.*)", re.DOTALL),
]


def _alternating_pairs(text: str, mark: re.Pattern[str]) -> Optional[List[Tuple[int, int]]]:
    """Quotations of a style whose opener and closer look alike, read from
    the context of each mark (rule 2); None when the marks do not strictly
    alternate OPENER, CLOSER, so the caller protects the whole paragraph."""
    pairs: List[Tuple[int, int]] = []
    start = -1  # offset of the open OPENER, -1 when none is open
    for k in mark.finditer(text):
        before = text[k.start() - 1] if k.start() else ""
        after = text[k.end()] if k.end() < len(text) else ""
        opener = (not before or before.isspace() or before in _BEFORE_OPENER) and (
            bool(after) and not after.isspace()
        )
        closer = (
            bool(before)
            and not before.isspace()
            and (not after or after.isspace() or after in _AFTER_CLOSER)
        )
        if opener == closer:  # fits both or neither
            return None
        if opener and start < 0:
            start = k.start()
        elif closer and start >= 0:
            pairs.append((start, k.end()))
            start = -1
        else:  # two openers or two closers in a row
            return None
    return None if start >= 0 else pairs


def _quotation_spans(narrative: str) -> List[Tuple[int, int]]:
    """(start, end) offsets of every quotation the narrative carries, by the
    four rules above."""
    spans: List[Tuple[int, int]] = []
    for para in _PARAGRAPH.finditer(narrative):
        text, base = para.group(), para.start()
        found = [(m.start(), m.end()) for m in _CURLY_PAIR.finditer(text)]
        if any(not any(s <= k.start() < e for s, e in found) for k in _CURLY_MARK.finditer(text)):
            spans.append((base, para.end()))  # marks that do not pair up: whole paragraph
        else:
            spans.extend((base + s, base + e) for s, e in found)
        for flat in _FLAT_MARKS:
            pairs = _alternating_pairs(text, flat)
            if pairs is None:
                spans.append((base, para.end()))
            else:
                spans.extend((base + s, base + e) for s, e in pairs)
        for single in _SINGLE_QUOTES:
            spans.extend((base + m.start(), base + m.end()) for m in single.finditer(text))
    return spans


# Speech-marker pattern in the DM narrative. When a marker is present,
# the prose immediately after it (up to the next sentence terminator)
# is what we'll replace with the player's verbatim quote.
_SPEECH_MARKER = re.compile(
    r"\b("
    r"you\s+(?:say|says|said|tell|told|reply|replies|replied|"
    r"ask|asks|asked|answer|answered|whisper|whispered|call|called|"
    r"shout|shouted|murmur|murmured|growl|growled|snap|snapped|"
    r"declare|declared|insist|insisted|state|stated|mutter|muttered|"
    r"add|added|continue|continued)"
    r")"
    r"(?:\s*[,\u2014:]\s*|\s+)"
    r"(?P<paraphrase>[^.!?\n]{1,400}[.!?])",
    re.IGNORECASE,
)


def extract_player_quotes(text: str) -> List[str]:
    """Return all quoted substrings the player wrote, in document order.
    Whitespace is preserved verbatim. Empty results on bad input."""
    if not text:
        return []
    kept: List[Tuple[int, str]] = []  # (start offset, quote)
    seen_spans: List[Tuple[int, int]] = []
    for pat in _QUOTE_PATTERNS:
        for m in pat.finditer(text):
            # Skip overlaps: a span we've already extracted (e.g. nested
            # smart-quote inside ASCII quotes).
            if any(s <= m.start() < e or s < m.end() <= e for s, e in seen_spans):
                continue
            inner = m.group(1).strip()
            if inner:
                kept.append((m.start(), inner))
                seen_spans.append((m.start(), m.end()))
    # Pattern priority only decides which match wins a span; the result
    # is in the order the quotes appear in the input.
    kept.sort(key=lambda t: t[0])
    return [q for _, q in kept]


# Quote styles that compare alike when a player quote is looked for in the
# narrative: the apostrophes and single quotes (U+2018, U+2019, U+02BC,
# U+0027, and the Hebrew geresh U+05F3) are one class, the double quotes
# (U+201C, U+201D, U+0022, and the Hebrew gershayim U+05F4) another.
_APOSTROPHES = chr(0x2018) + chr(0x2019) + chr(0x02BC) + chr(0x05F3) + "'"
_DOUBLE_QUOTES = _CO + _CC + _G + '"'
_FOLD = {ord(c): "'" for c in _APOSTROPHES}
_FOLD.update({ord(c): '"' for c in _DOUBLE_QUOTES})
_ELLIPSIS = chr(0x2026)


def _loose_word(word: str) -> str:
    """Regex source for a word that matches the same word in any of the
    apostrophe and double-quote styles of _FOLD."""
    out = []
    for ch in word:
        if ch in _APOSTROPHES:
            out.append("[" + _APOSTROPHES + "]")
        elif ch in _DOUBLE_QUOTES:
            out.append("[" + _DOUBLE_QUOTES + "]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _ends_sentence(quote: str) -> bool:
    """True when the quote already ends in . ! ? or an ellipsis."""
    return quote.rstrip()[-1:] in tuple(".!?" + _ELLIPSIS)


# Chat spellings a model may write out in full, each read with its usual meaning.
_SPELLING = {
    "ur": "your",
    "u": "you",
    "r": "are",
    "ya": "you",
    "yr": "your",
    "pls": "please",
    "plz": "please",
    "thx": "thanks",
    "cuz": "because",
    "coz": "because",
    "b4": "before",
}


def _words(s: str) -> List[str]:
    """Lower-case words and question marks (any run of ? and ! holding a ? reads as one ?), with
    apostrophes and quote styles dropped and chat spellings expanded."""
    s = s.translate(_FOLD).lower().replace("'", "")
    out: List[str] = []
    for w in re.findall(r"\w+|[?!]*\?[?!]*", s):
        out.append("?" if "?" in w else _SPELLING.get(w, w))
    return out


def _flagged(s: str) -> List[Tuple[str, bool]]:
    """(word, followed by a question mark) pairs; a mark before the first word is ignored."""
    out: List[Tuple[str, bool]] = []
    for tok in _words(s):
        if tok == "?":
            if out:
                out[-1] = (out[-1][0], True)
        else:
            out.append((tok, False))
    return out


def _quote_present(quote: str, narrative: str) -> bool:
    """A quote is 'present' if its content appears as a contiguous
    substring of the narrative that neither starts nor ends inside a word,
    ignoring case and collapsing whitespace runs to a single space.
    Tolerant of quote-character flavor differences
    in the narrative (the DM may use smart quotes around our content, or a
    curly apostrophe for a straight one; see _FOLD); failing that, the quote
    counts as present when its words appear in order in the narrative after
    case, punctuation, apostrophes and quote styles are dropped (a question
    mark in the player's quote must be matched in place; one the narrative
    adds is ignored, so a question never matches a statement) and a short list
    of chat spellings is expanded (ur -> your, u -> you, ...). A changed,
    added or dropped word is still missing."""
    if not quote or not narrative:
        return False

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.translate(_FOLD)).strip().lower()

    nq = _norm(quote)
    if nq and re.search(r"(?<!\w)" + re.escape(nq) + r"(?!\w)", _norm(narrative)):
        return True
    q, n = _flagged(quote), _flagged(narrative)
    if not q:
        return False
    k = len(q)
    return any(
        all(n[i + j][0] == q[j][0] and (n[i + j][1] or not q[j][1]) for j in range(k))
        for i in range(len(n) - k + 1)
    )


def line_within_quote(line: str, quote: str) -> bool:
    """True when line has a word and its words (see _words), in order and with no gap, are among
    the words of quote; question marks are ignored on both sides."""
    a = [w for w in _words(line) if w != "?"]
    b = [w for w in _words(quote) if w != "?"]
    k = len(a)
    return k > 0 and any(b[i : i + k] == a for i in range(len(b) - k + 1))


def find_missing_quotes(player_quotes: List[str], narrative: str) -> List[str]:
    """Returns the subset of player_quotes not present in the narrative
    (see _quote_present), in document order."""
    return [q for q in player_quotes if not _quote_present(q, narrative)]


def _splice_prepend(narrative: str, quote: str) -> str:
    """Path B fallback: a single sentence at the very top of the
    narrative. Honest about being a server-side splice - never tries to
    invent a paraphrase site that wasn't there."""
    stop = "" if _ends_sentence(quote) else "."
    prefix = f'You say, "{quote}"{stop}\n\n'
    return prefix + narrative


def _protected_spans(narrative: str, quotes: List[str]) -> List[Tuple[int, int]]:
    """(start, end) offsets a speech marker must not touch: every quotation
    the narrative carries (see _quotation_spans), and every place a present
    player quote (see _quote_present) appears exactly, as whole words, case and whitespace
    runs ignored; a quote present only by its words (a chat spelling written
    out) has no such place and protects nothing."""
    spans = _quotation_spans(narrative)
    for q in quotes:
        words = q.split()
        if words and _quote_present(q, narrative):
            where = re.compile(
                r"(?<!\w)" + r"\s+".join(_loose_word(w) for w in words) + r"(?!\w)", re.IGNORECASE
            )
            spans.extend((m.start(), m.end()) for m in where.finditer(narrative))
    return spans


def ensure_verbatim(player_input: str, narrative: str) -> Tuple[str, int]:
    """Public entry point. Runs in engine.run_turn after the DM beat
    returns. Returns (possibly-modified narrative, number-of-splices).

    Approach: find all speech-marker matches in the original narrative
    up-front and keep those that do not overlap a quotation the
    narrative carries (see _quotation_spans) or a place where a player
    quote already appears, whose paraphrase holds none of the player's
    quotes and does not open a quote of one of the four listed styles,
    pair them 1:1 with missing quotes in order, then apply every splice
    in a single right-to-left pass over the original narrative so
    earlier splices don't shift later splice indices and the markers
    found pre-edit stay valid. Quotes that have no marker to pair with
    fall through to Path B (prepend at top, in the player's order)."""
    quotes = extract_player_quotes(player_input)
    if not quotes:
        return narrative, 0
    missing = find_missing_quotes(quotes, narrative)
    if not missing:
        return narrative, 0

    # Pair as many missing quotes as possible with distinct markers.
    # A marker is not used when it overlaps a quotation the narrative
    # carries (see _quotation_spans) or a place where a player quote
    # already appears, when its paraphrase already holds a player quote,
    # or when its paraphrase opens a quote of one of the four listed styles.
    spans = _protected_spans(narrative, quotes)
    markers = [
        m
        for m in _SPEECH_MARKER.finditer(narrative)
        if not any(_quote_present(q, m.group("paraphrase")) for q in quotes)
        and not any(c in m.group("paraphrase") for c in _OPEN_QUOTES)
        and not any(s < m.end("paraphrase") and m.start() < e for s, e in spans)
    ]
    # (verb_end, start, end, quote_to_splice): the head is cut at the end of
    # the verb so the narrative's own separator is replaced, not kept.
    paired: list[tuple[int, int, int, str]] = []
    fall_thru: list[str] = []  # quotes with no marker -> prepend
    for i, q in enumerate(missing):
        if i < len(markers):
            m = markers[i]
            paired.append((m.end(1), m.start("paraphrase"), m.end("paraphrase"), q))
        else:
            fall_thru.append(q)

    # Apply paired splices right-to-left so earlier indices stay valid.
    fixed = narrative
    for verb_end, start, end, q in sorted(paired, key=lambda t: t[0], reverse=True):
        head = fixed[:verb_end]
        tail = fixed[end:]
        orig_para = fixed[start:end]
        terminator = orig_para[-1] if orig_para and orig_para[-1] in ".!?" else "."
        if _ends_sentence(q):
            terminator = ""
        fixed = f'{head.rstrip()}, "{q}"{terminator}{tail}'

    # Then prepend any leftover unpaired quotes. Each prepend lands above
    # the last, so go last-to-first for the player's order at the top.
    for q in reversed(fall_thru):
        fixed = _splice_prepend(fixed, q)

    return fixed, len(paired) + len(fall_thru)
