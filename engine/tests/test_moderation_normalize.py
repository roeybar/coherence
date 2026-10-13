"""Normalised matching: obfuscated spellings of list entries and hard-rule
terms still hit, decoded and quoted text is checked, and the shipped texts
still pass. No list entry is ever written or printed here: test strings come
from the testpack canaries or are built at runtime from the lists."""

import json
import random
import re
import string
import time
import unicodedata
from pathlib import Path

import pytest

from app.game import lore, moderation
from app.game.moderation import ListsUnavailable, OutputBlocked

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PACK = "zzpackcanary"
ALWAYS = "zzalwayscanary"
GLITTER = "zzglitterbolt"
BS = chr(92)
ZWSP = unicodedata.lookup("ZERO WIDTH SPACE")
ACUTE = unicodedata.lookup("COMBINING ACUTE ACCENT")
DIGITS = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5", "t": "7"}
SYMBOLS = {"a": "@", "s": "$", "i": "!"}
LOOKALIKE = {
    "a": "CYRILLIC SMALL LETTER A",
    "c": "CYRILLIC SMALL LETTER ES",
    "p": "CYRILLIC SMALL LETTER ER",
    "o": "GREEK SMALL LETTER OMICRON",
    "e": "CYRILLIC SMALL LETTER IE",
    "y": "CYRILLIC SMALL LETTER U",
    "k": "GREEK SMALL LETTER KAPPA",
}
REACH = {"o": "0", "i": "1", "l": "1", "e": "3", "a": "4", "s": "5", "t": "7", "b": "8", "g": "9"}


@pytest.fixture(autouse=True)
def _fresh_lists():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(lore, "LORE_ROOT", ROOT / "lore")
    monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", "neon_rain")
    moderation.reset_pack_cache()


def _cat(text, pack_id="testpack"):
    return moderation.check_input(text, pack_id=pack_id).category


def _entries(name):
    lines = (ROOT / "lore" / "neon_rain" / "moderation" / name).read_text(encoding="utf-8")
    out = [ln.strip().lower() for ln in lines.splitlines()]
    return [e for e in out if e and not e.startswith("#")]


def _wide(s):
    return "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in s)


def _forms(w):
    return {
        "upper": w.upper(),
        "wide": _wide(w),
        "zwsp": w[:3] + ZWSP + w[3:],
        "acute": "".join(c + ACUTE for c in w),
        **{"join" + str(i): sep.join(w) for i, sep in enumerate(" .-_*")},
        "digits": "".join(DIGITS.get(c, c) for c in w),
        "symbols": "".join(SYMBOLS.get(c, c) for c in w),
        "tripled": "".join(c * 3 for c in w),
        "lookalike": "".join(unicodedata.lookup(LOOKALIKE[c]) if c in LOOKALIKE else c for c in w),
    }


def test_obfuscated_canaries_block(pack):
    n = 0
    for word, want in ((PACK, "pack"), (ALWAYS, "pack_always"), (GLITTER, "pack")):
        for name, form in _forms(word).items():
            assert _cat(f"I say {form} now") == want, (word[:4], name)
            n += 1
    for text in (
        "I " + " ".join(PACK),
        " ".join(c * 3 for c in PACK),
        GLITTER.replace("i", "1").replace("l", "1"),
        "I " + " ".join(c * 3 for c in PACK),
        "zzsplit    zzphrase",
        "ZZSPLIT ZZPHRASE",
        "zz5pl1t zzphr45e",
        "zzdash word",
        "zzdash.word",
        "zzdashword",
        "zzd45h-w0rd",
        "Q&J",
        "48053716",
        "I paid 48053716",
    ):
        assert _cat(text) is not None, n
        n += 1
    print("PASS canary_forms n=%d" % n)


def test_near_misses_pass(pack):
    texts = [
        "x" + PACK + "x",
        PACK + "s",
        "zzgliterbolt",
        "zzsplit",
        "zzphrase zzsplit",
        "zzsplit. Zzphrase",
        "zzsplit, zzphrase",
        "q j",
        "q & j",
        "q.j",
        "",
    ]
    for n, text in enumerate(texts):
        assert not moderation.check_input(text, pack_id="testpack").blocked, n
    print("PASS near_misses n=%d" % len(texts))


def test_doxx_with_folded_digits_and_dashes(pack):
    dashes = [
        "HYPHEN",
        "NON-BREAKING HYPHEN",
        "FIGURE DASH",
        "EN DASH",
        "EM DASH",
        "MINUS SIGN",
        "SMALL HYPHEN-MINUS",
    ]
    texts = [_wide("123-45-6789"), "123" + ZWSP + "-45-6789"]
    texts += ["123-45-6789".replace("-", unicodedata.lookup(d)) for d in dashes]
    for n, text in enumerate(texts):
        assert _cat("ledger " + text) == "doxx", n
    print("PASS doxx_forms n=%d" % len(texts))


def _operands(rule):
    halves = rule.pattern.pattern.split(r"[\s\S]", 1)
    words = [
        re.findall(r"[a-z]{3,}", re.sub(r"\\.|\[[^\]]*\]|\{[^}]*\}|\(\?:|[?*+|()]", " ", h))
        for h in halves
    ]
    if rule.category == "harm_instruction":
        a = next(w for w in words[0] if re.search(r"(.)\1", w))
        b = next(w for w in words[1] if len(w) >= 4 and not re.search(r"(.)\1", w))
        return a, b
    return words[0][0], words[1][0]


def test_hard_rules_survive_spelling(pack):
    n = 0
    for rule in moderation._WORD_RULES:
        a, b = _operands(rule)
        assert rule.pattern.search(f"{a} {b}")

        def sp(w):
            return " ".join(w)

        def tri(w):
            return "".join(c * 3 for c in w)

        def one(w):
            return w.replace("i", "1").replace("l", "1")

        forms = [
            f"{tri(a)} {tri(b)}",
            f"{sp(a)} {sp(b)}",
            f"{sp(tri(a))} {sp(tri(b))}",
            f"I {sp(a)} {sp(b)}",
            f"{sp(a)} {b}",
            f"I {sp(a)} {b}",
            f"{one(a)} {one(b)}",
        ]
        for i, text in enumerate(forms):
            assert _cat(text) == rule.category, (rule.category, i)
            n += 1
    print("PASS hard_rule_forms n=%d" % n)


def _esc(fmt, s):
    return "".join(BS + fmt(c) for c in s)


def test_decoded_and_joined_views_block(pack):
    texts = [
        _esc(lambda c: "u%04x" % ord(c), PACK),
        _esc(lambda c: "x%02x" % ord(c), PACK),
        _esc(lambda c: "N{%s}" % unicodedata.name(c), PACK),
        _esc(lambda c: "%o" % ord(c), PACK),
        _esc(lambda c: "u005cu%04x" % ord(c), PACK),
        f"'{PACK[:5]}' '{PACK[5:]}'",
        f'"{PACK[:5]}""{PACK[5:]}"',
        f"'{PACK[:5]}' # c" + chr(10) + f"'{PACK[5:]}'",
        f"'''{PACK[:5]}''' '''{PACK[5:]}'''",
        f"'{PACK[:5]}" + BS + chr(10) + f"{PACK[5:]}'",
        PACK[:5] + BS + "b" + PACK[5:],
    ]
    for n, text in enumerate(texts):
        assert _cat(text) == "pack", n
    for text in (BS + "U00110000", BS + "N{NOSUCHNAME}", chr(0xE000), BS + "ud800"):
        assert isinstance(moderation.check_input(text, pack_id="testpack"), moderation.CheckResult)
    rng = random.Random(7)
    alphabet = string.printable + chr(8) + chr(12) + chr(0x7F)
    for _ in range(200):
        s = "".join(rng.choices(alphabet, k=rng.randint(0, 40)))
        assert json.loads(json.dumps(s)) in moderation._views(json.dumps(s))[0]
    print("PASS decoded_views n=%d json_equivalence=200" % len(texts))


def test_scalars_joins_and_symbol(pack):
    assert "48053716" in moderation.collect_strings({"a": 48053716})
    moderation.check_decoded({"x": ["zzglitt", "erbolt"]})
    with pytest.raises(OutputBlocked):
        moderation.check_decoded({"x": list(PACK)})
    with pytest.raises(OutputBlocked):
        moderation.check_decoded({"x": (PACK.encode("utf-8"),)})
    sym = moderation._SYMBOL_ENTRIES[0]
    assert len(sym) == 1 and ord(sym) == 0x1F595
    for tail in (
        "",
        unicodedata.lookup("VARIATION SELECTOR-16"),
        unicodedata.lookup("EMOJI MODIFIER FITZPATRICK TYPE-4"),
    ):
        assert _cat("look " + sym + tail) == "pack"
    print("PASS scalars_and_symbol")


def test_pack_resolution(pack, monkeypatch, tmp_path):
    assert moderation.resolve_pack("nopack") == "testpack"
    assert moderation.resolve_pack("../x") == "testpack"
    _cat("hello")
    sizes = (
        len(moderation._PACKS_WITH_LISTS),
        len(moderation._RULES_CACHE),
    )
    for i in range(1000):
        _cat("hello", pack_id="hostile%d" % i)
    assert sizes == (
        len(moderation._PACKS_WITH_LISTS),
        len(moderation._RULES_CACHE),
    )
    monkeypatch.setattr(lore, "LORE_ROOT", tmp_path)
    moderation.reset_pack_cache()
    with pytest.raises(ListsUnavailable):
        moderation.check_input("hello")
    print("PASS packs hostile=1000")


def _leet_ok(e):
    return re.fullmatch(r"[a-z -]+", e.translate(moderation._LEET))


def test_real_list_entries_block_in_every_form(real):
    counts = {}
    for name in ("always_blocklist.en.txt", "blocklist.en.txt"):
        for e in (e for e in _entries(name) if _leet_ok(e)):
            words = e.replace("-", " ").split()
            letters = "".join(words)
            forms = {
                "exact": e,
                "upper": e.upper(),
                "tripled_end": e + e[-1] * 2,
                "leet": "".join(DIGITS.get(c, c) for c in e),
                "zwsp": e[:1] + ZWSP + e[1:],
                "spaced_tripled": "I " + " ".join(c * 3 for c in letters),
                "dots": " ".join(".".join(w) for w in words),
            }
            if "-" in e:
                forms.update(
                    {
                        "joined": e.replace("-", ""),
                        "split": e.replace("-", " "),
                        "leet_split": "".join(DIGITS.get(c, c) for c in e.replace("-", " ")),
                    }
                )
            for form, text in forms.items():
                assert moderation.check_input(f"I say {text} now").blocked, (name, form)
                counts[form] = counts.get(form, 0) + 1
            if " " not in e and "-" not in e and set(e) <= set(REACH):
                digits = "".join(REACH[c] for c in e)
                assert moderation.check_input(f"I paid {digits}").blocked, (name, "digits")
                counts["digits"] = counts.get("digits", 0) + 1
    print("PASS real_list_forms " + " ".join("%s=%d" % kv for kv in sorted(counts.items())))


# hits the index gives on plain numbers, times and prices (always, main)
FALSE_POSITIVES = {"numbers": (0, 26), "times": (0, 2), "prices": (0, 0)}


def test_false_positive_counts(real):
    keys = moderation._load_rules("neon_rain", "en").keys
    texts = {
        "numbers": [str(n) for n in range(100000)],
        "times": ["%d:%02d" % (h, m) for h in range(24) for m in range(60)]
        + ["%02d:%02d" % (h, m) for h in range(10) for m in range(60)],
        "prices": ["$%d" % n for n in range(100000)],
    }
    for name, items in texts.items():
        hits = {}
        for tier, k in keys.items():
            hits[tier] = 0
            for t in items:
                segs = moderation._t1(moderation._fold(t), True)
                sqs = [[moderation._squeeze(x) for x in seg] for seg in segs]
                hits[tier] += moderation._k1_hit(segs, sqs, k)
        assert (hits.get("always", 0), hits.get("default", 0)) == FALSE_POSITIVES[name], name
        print(
            "PASS false_positives %s n=%d always=%d main=%d"
            % (name, len(items), hits.get("always", 0), hits.get("default", 0))
        )


def _lore_texts():
    out = []
    for f in sorted((ROOT / "lore" / "neon_rain").rglob("*")):
        if f.is_file() and "moderation" not in f.parts:
            s = f.read_text(encoding="utf-8")
            out.append(s)
            if f.suffix == ".json":
                out += sorted(moderation.collect_strings(json.loads(s)))
    return out


def test_corpus_passes(real):
    design = sorted(
        moderation.collect_strings(
            json.loads((FIXTURES / "design_story.json").read_text(encoding="utf-8"))
        )
    )
    typographic = [
        t.replace(" - ", " " + unicodedata.lookup("EM DASH") + " ").replace(
            "'", unicodedata.lookup("RIGHT SINGLE QUOTATION MARK")
        )
        for t in design
    ]
    prose = [
        ln
        for ln in (FIXTURES / "prose_en.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    opening = (FIXTURES / "packs" / "testpack" / "opening.md").read_text(encoding="utf-8")
    from app.api.game import _RO_EN

    corpora = {
        "design": design,
        "typographic": typographic,
        "prose_lines": prose,
        "prose_whole": [chr(10).join(prose)],
        "lore": _lore_texts(),
        "testpack_opening": [opening],
        "read_only": [_RO_EN],
    }
    for name, texts in corpora.items():
        bad = [i for i, t in enumerate(texts) if moderation.check_input(t).blocked]
        assert bad == [], (name, bad)
        print("PASS corpus %s n=%d" % (name, len(texts)))
    demo = (
        json.loads((ROOT.parent / "examples" / "demo-save.json").read_text(encoding="utf-8"))
        if (ROOT.parent / "examples" / "demo-save.json").is_file()
        else json.loads(Path("/app/examples/demo-save.json").read_text(encoding="utf-8"))
    )
    result, strings = moderation.check_tree(
        demo, pack_id="neon_rain", exempt=moderation.SAVE_EXEMPT
    )
    assert not result.blocked
    print("PASS corpus demo_save n=%d" % len(strings))


def test_punctuation_does_not_glue_words(pack):
    leet = "".join(DIGITS.get(c, c) for c in PACK)
    texts = [
        "x:" + leet,
        "x," + "".join(c * 3 for c in PACK),
        "x." + leet,
        "x;" + PACK,
        "(x)" + PACK,
        "zzsplit,zzphrase",
    ]
    for n, text in enumerate(texts):
        assert _cat(f"I say {text} now") is not None, n
    for n, text in enumerate(
        [
            "x" + PACK + "x",
            PACK + "s",
            "zzsplit. Zzphrase",
            "zzsplit, zzphrase",
            "q.j",
            PACK[:5] + "!",
        ]
    ):
        assert _cat(text) is None, ("near", n)
    print("PASS punctuation_splits n=%d" % len(texts))


def test_trailing_bang_is_a_letter(real):
    n = 0
    for name in ("always_blocklist.en.txt", "blocklist.en.txt"):
        for e in _entries(name):
            leet = e.translate(moderation._LEET)
            if re.fullmatch(r"[a-z]+", leet) and leet[-1] in "il" and len(leet) > 2:
                assert moderation.check_input(f"I say {e[:-1]}! now").blocked, (name, n)
                n += 1
    assert n > 0
    times = ["%d:%02d" % (h, m) for h in range(24) for m in range(60)]
    print(
        "PASS trailing_bang n=%d times_blocked_full_check=%d of %d"
        % (n, sum(moderation.check_input(t).blocked for t in times), len(times))
    )


def _scan_ms(n, gap):
    op1 = re.compile("(?=(qx))")
    op2 = re.compile("(?=(zv))")
    # one merged window: runs every 60 characters, each operand pair within
    # the gap but never touching a run, then every op2 before every op1
    unit = "qx zv ww ww ww ww ww ww ww ww ww ww ww ww ww ww ww ww ww ww "
    text = unit * n + "zv " * n + "qx " * n
    spans = [(i * len(unit) + 6, i * len(unit) + 8) for i in range(n)]
    spans += [(len(unit) * n + i * 90, len(unit) * n + i * 90 + 2) for i in range(2 * n // 30)]
    t0 = time.perf_counter()
    hit = moderation._pair_hit(text, spans, op1, op2, gap)
    return hit, (time.perf_counter() - t0) * 1000


def test_operand_scan_is_linear():
    hit, t1 = _scan_ms(4000, 40)
    assert hit is False
    hit, t2 = _scan_ms(8000, 40)
    assert hit is False
    assert t2 / max(t1, 0.001) < 3, (t1, t2)
    touching = moderation._pair_hit(
        "qx zv", [(0, 2)], re.compile("(?=(qx))"), re.compile("(?=(zv))"), 40
    )
    assert touching is True
    print(
        "PASS operand_scan matches_4k ms=%.1f matches_8k ms=%.1f ratio=%.2f"
        % (t1, t2, t2 / max(t1, 0.001))
    )
