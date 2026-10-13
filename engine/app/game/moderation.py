"""Content moderation: a hard filter on player text, model replies,
records parsed from model replies, and saves. check_input() runs the
in-code hard rules (CSAM, weapons, doxx) and the pack word lists after
normalising the text (see _fold and _views). Player text is checked
before any model call (app.game.engine), every model reply before any
caller sees it (app.llm_client), and every save the engine receives and
sends (app.api.game). A hit refuses the request with 451. OutputBlocked
and ListsUnavailable derive from BaseException so no fallback swallows
them; never catch BaseException around a model call. No moderation
record is kept; logs carry only the category. False positives are
accepted: the filter is strict by design.
"""

import contextvars
import hashlib
import json
import logging
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.game.lore import DEFAULT_PACK_ID


log = logging.getLogger(__name__)

MODERATION_RULES_VERSION = 1
CODE_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_NS_SEED = "a7813666"
_RULES_LOCK = threading.Lock()
_EPOCH = 0
_RULES_CACHE: dict = {}
_VERDICTS = OrderedDict()
_VERDICT_MAX = 262_144
_HOOKS = {"after_read": None, "before_return": None}


# hard categories
# Regex hits trigger an immediate refusal. Comment each pattern with the
# specific harm we're blocking. Keep the set small to limit false
# positives.


@dataclass
class HardRule:
    category: str
    description: str
    pattern: re.Pattern


_HARD_RULES: list[HardRule] = [
    HardRule(
        "csam",
        "Explicit sexual content involving minors. Word-boundary-anchored "
        "to reduce false positives on benign words like 'children'.",
        re.compile(
            r"\b(child|children|kid|kids|minor|minors|underage|preteen|"
            r"toddler|baby|babies|infant|infants|boy|boys|girl|girls)\b"
            r"[\s\S]{0,80}?\b("
            r"sex(?:ual)?|sexy|nude|naked|fuck\w*|cum|penis|vagina|breast|breasts|"
            r"nipple|nipples|orgasm|masturbat\w*|aroused|seductive|erotic|"
            r"fondle\w*|molest\w*|grope\w*"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    HardRule(
        "csam",
        "Reverse direction - sexual term followed by minor descriptor.",
        re.compile(
            r"\b("
            r"sex(?:ual)?|sexy|nude|naked|fuck\w*|cum|orgasm|"
            r"masturbat\w*|aroused|seductive|erotic|fondle\w*|molest\w*|grope\w*"
            r")\b[\s\S]{0,80}?\b("
            r"child|children|kid|kids|minor|minors|underage|preteen|"
            r"toddler|baby|infant|boy|boys|girl|girls"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    HardRule(
        "harm_instruction",
        "Specific weapon/explosive/poison synthesis requests.",
        re.compile(
            r"\b(how (to|do (i|you))|step[- ]by[- ]step|recipe|synthesi[sz]e|"
            r"build|construct|make|brew|cook)\b"
            r"[\s\S]{0,40}?\b("
            r"bomb|explosive|nerve agent|sarin|tabun|vx |vx,|vx\.|anthrax|"
            r"ricin|cyanide gas|chlorine gas|mustard gas|"
            r"meth(amphetamine)?|fentanyl|heroin|cocaine|"
            r"ar[- ]?15|ak[- ]?47|silencer|suppressor"
            r")\b",
            re.IGNORECASE,
        ),
    ),
    HardRule(
        "doxx",
        "SSN-like pattern (US 9-digit social security number with "
        "common formatting). Other PII shapes (full name + address) "
        "need a smarter check than regex.",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
]


@dataclass
class CheckResult:
    blocked: bool
    category: Optional[str] = None
    description: Optional[str] = None


# PACK-SCOPED blocklists
# Pack files (newline-delimited words/phrases, # for comments) are
# loaded lazily on first check_input call and cached per-pack. Two
# lists, both always active:
#   - `always_blocklist.<locale>.txt` - the floor, checked first
#   - `blocklist.<locale>.txt` - the main list, checked second
# The v1 source is LDNOOBW; the floor entries are copied from it.


def reset_pack_cache() -> None:
    """For testing - drop the per-pack list cache."""
    global _EPOCH
    with _RULES_LOCK:
        _EPOCH += 1
        _RULES_CACHE.clear()
        _PACKS_WITH_LISTS.clear()
        _VERDICTS.clear()


def check_input(text: str, *, pack_id: str = DEFAULT_PACK_ID, locale: str = "en") -> CheckResult:
    """Run hard rules + pack blocklists against player input.
    First-match-wins, order:
      1. Hardcoded _HARD_RULES (CSAM, weapons, doxx)
      2. Pack always_blocklist (the floor)
      3. Pack blocklist
    Each step also runs on the normalised text, then the symbol entry;
    then the same order runs on every decoded view of the text (_views).
    An unknown pack falls back to DEFAULT_PACK_ID; a pack without a list
    file raises ListsUnavailable.
    """
    if not text:
        return CheckResult(blocked=False)
    pack_id = resolve_pack(pack_id, locale)
    for _ in range(3):
        r = _load_rules(pack_id, locale)
        res = _check_rules(text, r.keys, r.lists, pack_id, locale)
        if _EPOCH == r.epoch:
            return res
    return res


def _check_rules(text, keys, pack_lists, pack_id, locale):
    for rule in _HARD_RULES:
        if rule.pattern.search(text):
            return CheckResult(blocked=True, category=rule.category, description=rule.description)
    first = _Prepared(text)
    hard = _hard_hit(first, skip_raw=True)
    if hard is not None:
        return _rule_result(hard)
    always = pack_lists.get("always")
    if always is not None and always.search(text):
        return CheckResult(
            blocked=True,
            category="pack_always",
            description=f"pack always-blocklist hit ({pack_id}/{locale})",
        )
    if _tier_hit(first, keys.get("always")):
        return _list_result("pack_always", pack_id, locale)
    default = pack_lists.get("default")
    if default is not None and default.search(text):
        return CheckResult(
            blocked=True, category="pack", description=f"pack blocklist hit ({pack_id}/{locale})"
        )
    if _tier_hit(first, keys.get("default")) or _symbol_hit(first):
        return _list_result("pack", pack_id, locale)
    seen = {text}
    for view in _views(text):
        if view in seen:
            continue
        seen.add(view)
        result = _check_view(_Prepared(view), keys, pack_id, locale)
        if result is not None:
            return result
    return CheckResult(blocked=False)


def _namespace(pack_id, locale, r) -> str:
    from app.game.lore import LORE_ROOT

    return hashlib.sha256(
        "\0".join(
            [
                str(MODERATION_RULES_VERSION),
                CODE_DIGEST,
                _NS_SEED,
                str(LORE_ROOT),
                pack_id,
                locale,
                r.digest,
            ]
        ).encode("utf-8", "surrogatepass")
    ).hexdigest()


def check_input_cached(text, *, pack_id=DEFAULT_PACK_ID, locale="en"):
    if not text:
        return CheckResult(blocked=False)
    pack_id = resolve_pack(pack_id, locale)
    for _ in range(3):
        r = _load_rules(pack_id, locale)
        key = hashlib.sha256(
            (_namespace(pack_id, locale, r) + "\0" + text).encode("utf-8", "surrogatepass")
        ).digest()
        with _RULES_LOCK:
            hit = key in _VERDICTS and _EPOCH == r.epoch
            if hit:
                _VERDICTS.move_to_end(key)
        result = (
            CheckResult(blocked=False) if hit else check_input(text, pack_id=pack_id, locale=locale)
        )
        hook = _HOOKS["before_return"]
        if hook is not None:
            hook()
        with _RULES_LOCK:
            if _EPOCH == r.epoch:
                if not hit and not result.blocked:
                    _VERDICTS[key] = None
                    while len(_VERDICTS) > _VERDICT_MAX:
                        _VERDICTS.popitem(last=False)
                return result
    return check_input(text, pack_id=pack_id, locale=locale)


class OutputBlocked(BaseException):
    """A model reply, or a record parsed from one, hit the filter."""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


class ListsUnavailable(BaseException):
    """The resolved pack has no list file, so nothing can be checked."""


# pack resolution

_PACK_VAR: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "moderation_pack", default=None
)
_PACK_ID = re.compile(r"[a-z0-9_]{1,48}")
_PACKS_WITH_LISTS: dict[tuple, frozenset] = {}  # (lore root, locale) -> pack ids


def bind_pack(pack_id: str) -> contextvars.Token:
    return _PACK_VAR.set(pack_id)


def unbind_pack(token: contextvars.Token) -> None:
    _PACK_VAR.reset(token)


def current_pack() -> str:
    pack = _PACK_VAR.get()
    return pack if pack is not None else DEFAULT_PACK_ID


def _packs_with_lists(locale: str) -> frozenset:
    from app.game.lore import LORE_ROOT  # local import - avoids circular

    key = (str(LORE_ROOT), locale)
    packs = _PACKS_WITH_LISTS.get(key)
    if packs is None:
        found = set()
        if LORE_ROOT.is_dir():
            for d in LORE_ROOT.iterdir():
                lists = d / "moderation"
                if (lists / f"blocklist.{locale}.txt").is_file() or (
                    lists / f"always_blocklist.{locale}.txt"
                ).is_file():
                    found.add(d.name)
        packs = _PACKS_WITH_LISTS[key] = frozenset(found)
    return packs


def resolve_pack(pack_id, locale: str = "en") -> str:
    """pack_id when it names a pack with a list file, otherwise
    DEFAULT_PACK_ID as it stands at call time."""
    if (
        isinstance(pack_id, str)
        and _PACK_ID.fullmatch(pack_id)
        and pack_id in _packs_with_lists(locale)
    ):
        return pack_id
    return DEFAULT_PACK_ID


# normalisation

_CONFUSABLE_NAMES = {
    "CYRILLIC SMALL LETTER A": "a",
    "CYRILLIC SMALL LETTER IE": "e",
    "CYRILLIC SMALL LETTER O": "o",
    "CYRILLIC SMALL LETTER ES": "c",
    "CYRILLIC SMALL LETTER ER": "p",
    "CYRILLIC SMALL LETTER HA": "x",
    "CYRILLIC SMALL LETTER U": "y",
    "CYRILLIC SMALL LETTER BYELORUSSIAN-UKRAINIAN I": "i",
    "CYRILLIC SMALL LETTER JE": "j",
    "CYRILLIC SMALL LETTER DZE": "s",
    "CYRILLIC SMALL LETTER KA": "k",
    "CYRILLIC SMALL LETTER SHHA": "h",
    "GREEK SMALL LETTER ALPHA": "a",
    "GREEK SMALL LETTER BETA": "b",
    "GREEK SMALL LETTER EPSILON": "e",
    "GREEK SMALL LETTER ETA": "n",
    "GREEK SMALL LETTER IOTA": "i",
    "GREEK SMALL LETTER KAPPA": "k",
    "GREEK SMALL LETTER NU": "v",
    "GREEK SMALL LETTER OMICRON": "o",
    "GREEK SMALL LETTER RHO": "p",
    "GREEK SMALL LETTER TAU": "t",
    "GREEK SMALL LETTER UPSILON": "u",
    "GREEK SMALL LETTER CHI": "x",
}
_MINUS = chr(0x2212)
_LEET = str.maketrans("013456789@$!|+", "oieasgtbgasiit")
_LEET_DIGITS = {d: chr(_LEET[ord(d)]) for d in "013456789"}

_SQUEEZE = re.compile(r"(.)\1+", re.S)
_RUN = re.compile(r"(.)\1*", re.S)
_LONG_RUN = re.compile(r"([^\W\d_])\1{2,}")
_WORD = re.compile(r"[^\W_]+")
_NONWORD = re.compile(r"[\W_]+")
_SPACE = re.compile(r"\s")
_LEAD = "\"'(["
_TRAIL = ".!?;:,\"')]"
_STOPS = ".!?;:,"


def _fold(s: str) -> str:
    """Compatibility forms and case folded; outside ASCII, marks and
    invisible characters dropped, dashes made '-', look-alikes made Latin."""
    t = unicodedata.normalize("NFKC", s).casefold()
    if t.isascii():
        return t
    out = []
    for c in unicodedata.normalize("NFKD", t):
        if c.isascii():
            out.append(c)
            continue
        cat = unicodedata.category(c)
        if cat in ("Mn", "Me", "Cf"):
            continue
        if cat == "Pd" or c == _MINUS:
            out.append("-")
            continue
        out.append(_CONFUSABLE_NAMES.get(unicodedata.name(c, ""), c))
    return unicodedata.normalize("NFC", "".join(out))


def _squeeze(s: str) -> str:
    return _SQUEEZE.sub(r"\1", s)


def _runs(s: str) -> tuple:
    return tuple(len(m.group(0)) for m in _RUN.finditer(s))


def _collapse(s: str) -> str:
    # every same-letter run cut to at most 2 letters
    return _LONG_RUN.sub(r"\1\1", s)


def _t0(s: str) -> list:
    """Letter-digit tokens in segments; a separator holding both space and
    another character ends a segment."""
    segs: list = [[]]
    prev = 0
    for m in _WORD.finditer(s):
        sep = s[prev : m.start()]
        if segs[-1] and _SPACE.search(sep) and not sep.isspace():
            segs.append([])
        segs[-1].append(m.group(0))
        prev = m.end()
    return [seg for seg in segs if seg]


def _t1(f: str, merge_il: bool) -> list:
    """Whitespace chunks with stand-ins mapped and separators removed, in
    segments that end at sentence punctuation."""
    segs: list = [[]]
    for chunk in f.split():
        chunk = chunk.lstrip(_LEAD)
        body = chunk.rstrip(_TRAIL)
        brk = any(c in _STOPS for c in chunk[len(body) :])
        tok = body.translate(_LEET)
        if merge_il:
            tok = tok.replace("l", "i")
        tok = _NONWORD.sub("", tok)
        if not tok:
            continue
        segs[-1].append(tok)
        if brk:
            segs.append([])
    return [seg for seg in segs if seg]


_SPLIT = re.compile(r"[^\w@$!|+]+")
_TRAIL_KEEP_BANG = ".?;:,\"')]"


def _t1p(f: str) -> list:
    """_t1 with each chunk also split at punctuation other than the stand-in
    symbols, so a word glued on by punctuation stays apart; a trailing '!'
    stays as a stand-in letter. Sentence breaks as in _t1."""
    segs: list = [[]]
    for chunk in f.split():
        chunk = chunk.lstrip(_LEAD)
        body = chunk.rstrip(_TRAIL)
        brk = any(c in _STOPS for c in chunk[len(body) :])
        for part in _SPLIT.split(chunk.rstrip(_TRAIL_KEEP_BANG)):
            tok = _NONWORD.sub("", part.translate(_LEET).replace("l", "i"))
            if tok:
                segs[-1].append(tok)
        if brk:
            segs.append([])
    return [seg for seg in segs if seg]


def _one_letter(tok: str) -> bool:
    return tok[:1].isalpha() and tok == tok[0] * len(tok)


def _one_letter_runs(toks: list) -> list:
    """Runs of two or more one-letter tokens, as units [letter, count, ntok]
    with neighbouring same-letter tokens merged."""
    out, run, ntok = [], [], 0
    for tok in toks + [""]:
        if _one_letter(tok):
            if run and run[-1][0] == tok[0]:
                run[-1][1] += len(tok)
                run[-1][2] += 1
            else:
                run.append([tok[0], len(tok), 1])
            ntok += 1
            continue
        if ntok >= 2:
            out.append(run)
        run, ntok = [], 0
    return out


def _join_runs(toks: list) -> tuple:
    """The tokens joined by spaces, each run of one-letter tokens written
    as one word, runs collapsed; returns (text, run spans)."""
    pieces, spans = [], []
    pos, i, n = 0, 0, len(toks)
    while i < n:
        j = i
        while j < n and _one_letter(toks[j]):
            j += 1
        if j - i >= 2:
            piece = _collapse("".join(toks[i:j]))
            spans.append((pos, pos + len(piece)))
        else:
            j = i + 1
            piece = _collapse(toks[i])
        pieces.append(piece)
        pos += len(piece) + 1
        i = j
    return " ".join(pieces), spans


# decoded views

_ESC = re.compile(
    r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2})"
    r"|N\{([A-Za-z0-9 \-]+)\}|([0-7]{1,3})|(\r\n|[\s\S]))"
)
_SINGLE = {
    "n": chr(10),
    "t": chr(9),
    "r": chr(13),
    "b": chr(8),
    "f": chr(12),
    "v": chr(11),
    "a": chr(7),
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
}
_BREAKS = ("\r\n", "\n", "\r")
_COMMENT = re.compile(r"#[^\r\n]*")
_Q = re.escape("\"'" + "".join(map(chr, (0x2018, 0x2019, 0x201C, 0x201D))))
_ADJ = re.compile("[" + _Q + r"]\s*+[rRuUbBfF]{0,2}[" + _Q + "]")
_PUNCT = re.compile(r"""["'{}\[\]:,]""")


def _dec(m: re.Match) -> str:
    u4, u8, x2, name, octal, single = m.groups()
    if u4 or x2:
        return chr(int(u4 or x2, 16))
    if u8:
        code = int(u8, 16)
        return chr(code) if code <= 0x10FFFF else m.group(0)
    if name:
        try:
            return unicodedata.lookup(name)
        except KeyError:
            return m.group(0)
    if octal:
        return chr(int(octal, 8))
    if single in _BREAKS:
        return ""
    return _SINGLE.get(single, m.group(0))


def _fix(s: str) -> str:
    return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _views(s: str) -> list:
    """Each decoding stage (the single pass first, up to 3 passes), the last
    one with adjacent literals and comments joined, and each decoded stage
    with quotes, brackets, colons and commas made spaces."""
    stages = []
    t = s
    for _ in range(3):
        n = _ESC.sub(_dec, t) if "\\" in t else t
        if stages and n == t:
            break
        stages.append(_fix(n) if n != s else n)
        t = n
    joined = _ADJ.sub("", _COMMENT.sub("", stages[-1]))
    decoded = [v for v in stages if v != s]
    return stages + [joined] + [_PUNCT.sub(" ", v) for v in decoded]


# list index


@dataclass
class _Keys:
    k0: set
    k0_max_n: int
    k1: dict
    k1_firsts: set
    k1_max_n: int
    mkeys: dict
    prefixes: set
    lmax: int
    literal: Optional[re.Pattern]


_SYMBOL_JOINED = re.compile(r"[^\w\s-]")
_K1_FORM = re.compile(r"[a-z -]+")


def _build_keys(entries: list) -> _Keys:
    k0, k1, mkeys, literal = set(), {}, {}, []
    for entry in entries:
        if _SYMBOL_JOINED.search(entry):
            literal.append(r"\s+".join(re.escape(w) for w in entry.split()))
        else:
            words = _WORD.findall(entry)
            if words:
                k0.add(" ".join(words))
        leet = entry.translate(_LEET)
        if not _K1_FORM.fullmatch(leet):
            continue
        forms = [leet.replace("-", "")]
        if "-" in leet:
            forms.append(leet.replace("-", " "))
        for form in forms:
            words = form.replace("l", "i").split()
            if not words:
                continue
            runs: tuple = ()
            for w in words:
                runs += _runs(w)
            k1.setdefault(" ".join(_squeeze(w) for w in words), []).append(runs)
            joined = "".join(words)
            mkeys.setdefault(_squeeze(joined), []).append(_runs(joined))
    pattern = None
    if literal:
        literal.sort(key=len, reverse=True)
        pattern = re.compile(r"\b(" + "|".join(literal) + r")\b", re.IGNORECASE)
    return _Keys(
        k0=k0,
        k0_max_n=max((k.count(" ") + 1 for k in k0), default=0),
        k1=k1,
        k1_firsts={k.split(" ")[0] for k in k1},
        k1_max_n=max((k.count(" ") + 1 for k in k1), default=0),
        mkeys=mkeys,
        prefixes={k[:i] for k in mkeys for i in range(1, len(k) + 1)},
        lmax=max((len(k) for k in mkeys), default=0),
        literal=pattern,
    )


@dataclass(frozen=True)
class _Rules:
    keys: dict
    lists: dict
    digest: str
    epoch: int


def _load_rules(pack_id, locale):
    from app.game.lore import LORE_ROOT  # local import - avoids circular

    key = (str(LORE_ROOT), pack_id, locale)
    for _ in range(3):
        with _RULES_LOCK:
            cached = _RULES_CACHE.get(key)
            if cached is not None:
                return cached
            e0 = _EPOCH
        raw = {}
        for tier, fname in (
            ("default", f"moderation/blocklist.{locale}.txt"),
            ("always", f"moderation/always_blocklist.{locale}.txt"),
        ):
            path = LORE_ROOT / pack_id / fname
            if path.is_file():
                raw[tier] = path.read_bytes()
        if not raw:
            raise ListsUnavailable(pack_id)
        hook = _HOOKS["after_read"]
        if hook is not None:
            hook()
        keys = {}
        lists = {"default": None, "always": None}
        h = hashlib.sha256()
        for tier in ("default", "always"):
            if tier not in raw:
                continue
            lines = [ln.strip().lower() for ln in raw[tier].decode("utf-8").splitlines()]
            entries = [ln for ln in lines if ln and not ln.startswith("#")]
            keys[tier] = _build_keys(entries)
            alternation_parts = []
            for entry in entries:
                words = entry.split()
                if not words:
                    continue
                alternation_parts.append(r"\s+".join(re.escape(w) for w in words))
            if alternation_parts:
                alternation_parts.sort(key=len, reverse=True)
                lists[tier] = re.compile(
                    r"\b(" + "|".join(alternation_parts) + r")\b",
                    re.IGNORECASE,
                )
                log.info(
                    "moderation: loaded %d entries for pack=%s locale=%s tier=%s",
                    len(entries),
                    pack_id,
                    locale,
                    tier,
                )
            h.update(tier.encode() + b"\0" + str(len(raw[tier])).encode() + b"\0" + raw[tier])
        with _RULES_LOCK:
            if _EPOCH == e0:
                r = _Rules(keys, lists, h.hexdigest(), e0)
                _RULES_CACHE[key] = r
                return r
    raise ListsUnavailable(pack_id)


def _k0_hit(segs: list, k: _Keys) -> bool:
    for seg in segs:
        size = len(seg)
        for i in range(size):
            for n in range(1, min(k.k0_max_n, size - i) + 1):
                if " ".join(seg[i : i + n]) in k.k0:
                    return True
    return False


def _k1_hit(segs: list, sqs: list, k: _Keys) -> bool:
    # an entry matches when the squeezed words agree and every letter run
    # of the text is at least as long as the entry's
    for seg, sq in zip(segs, sqs):
        size = len(seg)
        for i in range(size):
            if sq[i] not in k.k1_firsts:
                continue
            for n in range(1, min(k.k1_max_n, size - i) + 1):
                vecs = k.k1.get(" ".join(sq[i : i + n]))
                if not vecs:
                    continue
                runs: tuple = ()
                for tok in seg[i : i + n]:
                    runs += _runs(tok)
                if any(all(a >= b for a, b in zip(runs, v)) for v in vecs):
                    return True
    return False


def _m_hit(runs: list, k: _Keys) -> bool:
    # letters spelled out one token at a time; grows only along key
    # prefixes, so each start costs at most lmax steps
    for units in runs:
        for s in range(len(units)):
            word, ntok = "", 0
            for e in range(s, len(units)):
                word += units[e][0]
                ntok += units[e][2]
                if len(word) > k.lmax or word not in k.prefixes:
                    break
                if ntok < 2:
                    continue
                vecs = k.mkeys.get(word)
                if vecs and any(all(u[1] >= r for u, r in zip(units[s : e + 1], v)) for v in vecs):
                    return True
    return False


# hard rules on normalised text


def _runs_pattern(src: str, *, leet: bool, optional: bool) -> str:
    """A hard-rule pattern where each run of k letters takes k to k+2
    letters (text runs are cut to 2 first), i and l stand for each other,
    digits become letters (leet) and spaces may be dropped (optional)."""

    def letter(c):
        return _LEET_DIGITS.get(c, c) if leet else c

    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            out.append(src[i : i + 2])
            i += 2
        elif c == "[":
            j = src.index("]", i + 1) + 1
            cls = src[i:j]
            out.append(cls + "?" if optional and cls == "[- ]" else cls)
            i = j
        elif c == "{":
            j = src.index("}", i) + 1
            out.append(src[i:j])
            i = j
        elif src.startswith("(?:", i):
            out.append("(?:")
            i += 3
        elif c == " " and optional:
            out.append(" ?")
            i += 1
        elif "a" <= letter(c) <= "z":
            ch = letter(c)
            j = i + 1
            while j < n and letter(src[j]) == ch:
                j += 1
            k = j - i
            cc = "[il]" if ch in "il" else ch
            if j < n and src[j] in "?*+{":
                out.append((f"{cc}{{{k - 1},{k + 1}}}" if k > 1 else "") + cc)
            else:
                out.append(f"{cc}{{{k},{k + 2}}}")
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


_GAP = re.compile(r"\[\\s\\S\]\{0,(\d+)\}\?")
_TAIL_W = re.compile(r"\\w[*+](?=[|)]|$)")
_WINDOW = 100


def _operand(src: str) -> re.Pattern:
    src = _TAIL_W.sub("", src.replace(r"\b", ""))
    return re.compile("(?=(" + _runs_pattern(src, leet=True, optional=True) + "))", re.IGNORECASE)


def _split_rule(rule: HardRule) -> tuple:
    src = rule.pattern.pattern
    m = _GAP.search(src)
    return rule, _operand(src[: m.start()]), _operand(src[m.end() :]), int(m.group(1))


_WORD_RULES = [r for r in _HARD_RULES if r.category != "doxx"]
_DERIVED_F = [
    (r, re.compile(_runs_pattern(r.pattern.pattern, leet=False, optional=False), re.IGNORECASE))
    for r in _WORD_RULES
]
_DERIVED_T = [
    (r, re.compile(_runs_pattern(r.pattern.pattern, leet=True, optional=True), re.IGNORECASE))
    for r in _WORD_RULES
]
_OPERANDS = [_split_rule(r) for r in _WORD_RULES]


def _pair_hit(text: str, spans: list, op1: re.Pattern, op2: re.Pattern, gap: int) -> bool:
    """Both operands of a rule, without word boundaries, in a window around
    a run of one-letter tokens, the second within gap of the first and one
    of them touching a run. Every list is sorted and each pointer only moves
    forward (or a bounded distance past another), so the scan is linear in
    the number of matches and spans."""
    windows: list = []
    for a, b in spans:
        lo, hi = max(0, a - _WINDOW), b + _WINDOW
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
            windows[-1][2].append((a, b))
        else:
            windows.append([lo, hi, [(a, b)]])
    for lo, hi, runs in windows:
        first = [(m.start(), m.start() + len(m.group(1))) for m in op1.finditer(text, lo, hi)]
        if not first:
            continue
        second = [(m.start(), m.start() + len(m.group(1))) for m in op2.finditer(text, lo, hi)]
        n2, nr = len(second), len(runs)
        p = q = 0
        for a1, e1 in first:
            # first starts only grow: skip the second operands and runs before a1
            while p < n2 and second[p][0] < a1:
                p += 1
            while q < nr and runs[q][1] <= a1:
                q += 1
            x = p
            while x < n2 and second[x][0] < e1:
                x += 1
            touch1 = q < nr and runs[q][0] < e1
            r = q
            while x < n2 and second[x][0] <= e1 + gap:
                a2, e2 = second[x]
                if touch1:
                    return True
                while r < nr and runs[r][1] <= a2:
                    r += 1
                if r < nr and runs[r][0] < e2:
                    return True
                x += 1
    return False


# one view of a text, prepared once for every check


class _Prepared:
    __slots__ = (
        "raw",
        "f",
        "fc",
        "t0r",
        "t0",
        "t1",
        "sq1",
        "units",
        "h1",
        "j",
        "spans",
        "t1p",
        "sq1p",
        "unitsp",
    )

    def __init__(self, raw: str):
        self.raw = raw
        self.f = f = _fold(raw)
        self.fc = _collapse(f)
        low = raw.casefold()
        self.t0r = _t0(low)
        self.t0 = self.t0r if f == low else _t0(f)
        self.t1 = _t1(f, True)
        self.sq1 = [[_squeeze(tok) for tok in seg] for seg in self.t1]
        self.units = _one_letter_runs([tok for seg in self.t1 for tok in seg])
        flat = [tok for seg in _t1(f, False) for tok in seg]
        self.h1 = _collapse(" ".join(flat))
        self.j, self.spans = _join_runs(flat)
        # the same tokens split at punctuation too; kept only when different
        t1p = _t1p(f)
        self.t1p = t1p if t1p != self.t1 else None
        self.sq1p = [[_squeeze(tok) for tok in seg] for seg in t1p]
        self.unitsp = _one_letter_runs([tok for seg in t1p for tok in seg])


def _hard_hit(p: _Prepared, skip_raw: bool = False) -> Optional[HardRule]:
    for rule in _HARD_RULES:
        if (not skip_raw and rule.pattern.search(p.raw)) or (
            p.f != p.raw and rule.pattern.search(p.f)
        ):
            return rule
    for rule, pattern in _DERIVED_F:
        if pattern.search(p.fc):
            return rule
    for rule, pattern in _DERIVED_T:
        if pattern.search(p.h1) or (p.spans and pattern.search(p.j)):
            return rule
    if p.spans:
        for rule, op1, op2, gap in _OPERANDS:
            if _pair_hit(p.j, p.spans, op1, op2, gap):
                return rule
    return None


def _tier_hit(p: _Prepared, k: Optional[_Keys]) -> bool:
    if k is None:
        return False
    if k.literal is not None and (k.literal.search(p.raw) or k.literal.search(p.f)):
        return True
    if _k0_hit(p.t0r, k) or (p.t0 is not p.t0r and _k0_hit(p.t0, k)):
        return True
    if _k1_hit(p.t1, p.sq1, k) or _m_hit(p.units, k):
        return True
    return p.t1p is not None and (_k1_hit(p.t1p, p.sq1p, k) or _m_hit(p.unitsp, k))


def _symbol_hit(p: _Prepared) -> bool:
    return any(e in p.raw or e in p.f for e in _SYMBOL_ENTRIES)


def _rule_result(rule: HardRule) -> CheckResult:
    return CheckResult(blocked=True, category=rule.category, description=rule.description)


def _list_result(category: str, pack_id: str, locale: str) -> CheckResult:
    if category == "pack_always":
        return CheckResult(
            blocked=True,
            category=category,
            description=f"pack always-blocklist hit ({pack_id}/{locale})",
        )
    return CheckResult(
        blocked=True, category=category, description=f"pack blocklist hit ({pack_id}/{locale})"
    )


def _check_view(p: _Prepared, keys: dict, pack_id: str, locale: str) -> Optional[CheckResult]:
    rule = _hard_hit(p)
    if rule is not None:
        return _rule_result(rule)
    if _tier_hit(p, keys.get("always")):
        return _list_result("pack_always", pack_id, locale)
    if _tier_hit(p, keys.get("default")) or _symbol_hit(p):
        return _list_result("pack", pack_id, locale)
    return None


# trees: saves, request bodies, parsed model records

# Values exempt from the check: the engine's own base64 blobs, only at
# these paths and only when the whole value has that shape.
SAVE_EXEMPT = (("memory", "*", "v"), ("physics_cache", "*", "v"))
_EXEMPT_VALUE = re.compile(r"[A-Za-z0-9+/]{1024}")
_CONTAINERS = (dict, list, tuple, set, frozenset)


def _scalar_texts(value) -> tuple:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (bytes, bytearray)):
        return (bytes(value).decode("utf-8", "replace"),)
    if type(value) is int:
        # a plain int has one JSON form, its digits
        text = str(value)
        return (text, text)
    return (str(value), json.dumps(value, default=str))


def _exempt(path: tuple, exempt) -> bool:
    return any(
        len(p) == len(path) and all(a == "*" or a == b for a, b in zip(p, path)) for p in exempt
    )


class TextTooLarge(Exception):
    """A request holds more text, or more values, than its budget allows."""


class TextBudget:
    """The characters of distinct strings and the values walked, shared by
    the collect_strings calls of one request; past the limit the walk stops
    with TextTooLarge."""

    def __init__(self, limit: int):
        self.limit = limit
        self.chars = 0
        self.nodes = 0
        self._counted: set = set()

    def node(self) -> None:
        self.nodes += 1
        if self.nodes > self.limit:
            raise TextTooLarge()

    def add(self, s: str) -> None:
        if s not in self._counted:
            self._counted.add(s)
            self.chars += len(s)
            if self.chars > self.limit:
                raise TextTooLarge()


_END = object()


def collect_strings(
    obj, exempt=None, joins: bool = True, budget: Optional[TextBudget] = None
) -> set:
    """Every string in obj: values, dict keys, bytes as text, other scalars
    as str() and as JSON, and the joined direct scalars of each list. The
    walk keeps a value's path only as deep as the exempt paths go and reads
    containers one item at a time; with a budget, every value and every new
    string is counted as it is reached, so an oversized request stops early."""
    out: set = set()
    depth = max((len(p) for p in exempt), default=0) if exempt else 0

    def add(s: str) -> None:
        if budget is not None:
            budget.add(s)
        out.add(s)

    def visit(value, path):
        # a scalar is recorded; a container comes back as a frame to walk
        if budget is not None:
            budget.node()
        if isinstance(value, dict):
            return [iter(value.items()), path, None, True, 0] if value else None
        if isinstance(value, _CONTAINERS):
            return [enumerate(value), path, [] if joins else None, False, 0] if value else None
        if isinstance(value, str):
            if not (path is not None and _EXEMPT_VALUE.fullmatch(value) and _exempt(path, exempt)):
                add(value)
        else:
            for text in _scalar_texts(value):
                add(text)
        return None

    stack = []
    frame = visit(obj, () if depth else None)
    if frame is not None:
        stack.append(frame)
    while stack:
        top = stack[-1]
        items, path, flat, is_dict, _ = top
        pair = next(items, _END)
        if pair is _END:
            stack.pop()
            if flat is not None and len(flat) >= 2:
                add(" ".join(flat))
            continue
        key, value = pair
        if is_dict:
            add(key if isinstance(key, str) else str(key))
        elif flat is not None and not isinstance(value, _CONTAINERS):
            text = value if isinstance(value, str) else _scalar_texts(value)[0]
            flat.append(text)
            top[4] += len(text) + 1
            if budget is not None and top[4] - 1 > budget.limit:
                # the join alone would pass the limit
                raise TextTooLarge()
        child = path + (key,) if path is not None and len(path) < depth else None
        sub = visit(value, child)
        if sub is not None:
            stack.append(sub)
    return out


def view_chars(text: str) -> int:
    """Characters of the decoded and joined views check_input runs for text
    besides text itself; admission counts them against the text cap."""
    if not text:
        return 0
    seen, total = {text}, 0
    for view in _views(text):
        if view not in seen:
            seen.add(view)
            total += len(view)
    return total


def check_tree(
    obj, *, pack_id, locale: str = "en", skip=frozenset(), exempt=None
) -> tuple[CheckResult, set[str]]:
    """check_input on every collected string not in skip; the first hit and
    the strings collected."""
    strings = collect_strings(obj, exempt=exempt)
    for s in sorted(strings):
        if s in skip:
            continue
        result = check_input(s, pack_id=pack_id, locale=locale)
        if result.blocked:
            return result, strings
    return CheckResult(blocked=False), strings


def check_decoded(obj, *, pack_id=None) -> None:
    """Raise OutputBlocked when any string of a parsed model record hits."""
    if obj is None:
        return
    pack = pack_id or current_pack()
    for s in sorted(collect_strings(obj)):
        result = check_input(s, pack_id=pack)
        if result.blocked:
            raise OutputBlocked(result.category)


# The one non-ASCII entry of the upstream list, kept as an escape.
_SYMBOL_ENTRIES = ("\U0001f595",)
