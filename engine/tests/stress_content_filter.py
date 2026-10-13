import random
import string
import threading
import re
import time
import unicodedata
from pathlib import Path

import pytest

from app import main
from conftest import NEW_BODY, sid
from app.game import moderation
from app.api import game as game_api
from app.game import lore
from app.providers import fake

PACK = "zzpackcanary"
ALWAYS = "zzalwayscanary"


@pytest.fixture(autouse=True)
def _fresh_lists():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


def _check(text: str):
    return moderation.check_input(text, pack_id="testpack")


def test_threads_get_their_own_verdict(selfhost, monkeypatch):
    # isolation under load, not the concurrency limit
    monkeypatch.setattr(main, "slots", threading.BoundedSemaphore(40))
    errors: list[str] = []
    seen = {"pack": 0, "pack_always": 0, "clean": 0}
    lock = threading.Lock()
    # one turn first, so models that load lazily load once, not per thread
    warm = selfhost.post("/api/game/new", json=NEW_BODY).json()["save"]
    assert (
        selfhost.post("/api/game/turn", json={"save": warm, "player_input": "I wait"}).status_code
        == 200
    )

    def play(i):
        try:
            h = {"X-Coherence-Session": sid()}
            save = selfhost.post(
                "/api/game/new", json=NEW_BODY | {"name": "m%d" % i}, headers=h
            ).json()["save"]
            if i % 3 == 0:
                move, want = "thread-%d says %s" % (i, PACK), "pack"
            elif i % 3 == 1:
                move, want = "thread-%d says %s" % (i, ALWAYS), "pack_always"
            else:
                move, want = "thread-%d waits" % i, "clean"
            r = selfhost.post(
                "/api/game/turn", headers=h, json={"save": save, "player_input": move}
            )
            if want == "clean":
                assert r.status_code == 200, (i, r.status_code)
            else:
                assert r.status_code == 451, (i, r.status_code)
                assert r.json()["detail"]["category"] == want, i
            with lock:
                seen[want] += 1
        except Exception as e:
            errors.append(repr(e))

    ts = [threading.Thread(target=play, args=(i,)) for i in range(40)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    assert seen == {"pack": 14, "pack_always": 13, "clean": 13}
    print(
        "PASS threads n=40 pack=%d always=%d clean=%d"
        % (seen["pack"], seen["pack_always"], seen["clean"])
    )


def test_boundary_inputs(pack):
    cases = [
        ("", None),
        (" " * 10, None),
        ("x" + PACK + "x", None),
        (PACK + "s", None),
        ("rain " * 20000, None),
        (PACK.upper(), "pack"),
        (PACK + ".", "pack"),
        ("\u00e9 " + PACK, "pack"),
        ("rain " * 20000 + PACK, "pack"),
        (ALWAYS + " " + PACK, "pack_always"),
        ("123-45-6789 " + ALWAYS, "doxx"),
    ]
    for n, (text, want) in enumerate(cases):
        check = _check(text)
        assert check.blocked is (want is not None), n
        assert check.category == want, n
    print("PASS boundaries n=%d" % len(cases))


def _pad(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_lowercase + " ", k=rng.randint(0, 30)))


def _mixed_case(rng: random.Random, word: str) -> str:
    return "".join(c.upper() if rng.random() < 0.5 else c for c in word)


def test_volume(pack):
    rng = random.Random(1234)
    n = 2000
    always = packs = doxx = clean = 0
    for _ in range(n):
        text = "%s %s %s" % (_pad(rng), _mixed_case(rng, ALWAYS), _pad(rng))
        always += _check(text).category == "pack_always"
        text = "%s %s %s" % (_pad(rng), _mixed_case(rng, PACK), _pad(rng))
        packs += _check(text).category == "pack"
        text = "%s 123-45-6789 %s" % (_pad(rng), _pad(rng))
        doxx += _check(text).category == "doxx"
        text = "I %s the %s" % (
            rng.choice(("watch", "follow", "greet", "study")),
            _pad(rng).strip() or "rain",
        )
        clean += not _check(text).blocked
    assert (always, packs, doxx, clean) == (n, n, n, n)
    print("PASS volume always=%d pack=%d doxx=%d filler_clean=%d" % (always, packs, doxx, clean))


# normalised matching under load; real lists where named, testpack otherwise

ROOT = Path(__file__).resolve().parents[1]


def _timed(text):
    t0 = time.perf_counter()
    check = _check(text)
    return check, (time.perf_counter() - t0) * 1000


def _real(monkeypatch):
    monkeypatch.setattr(lore, "LORE_ROOT", ROOT / "lore")
    monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", "neon_rain")
    moderation.reset_pack_cache()


def _stem_and_letter():
    src = moderation._HARD_RULES[0].pattern.pattern
    stem = re.findall(r"([a-z]+)\\w\*", src)[0]
    first = re.findall(r"\(([a-z]{3,})\|", src)[0][0]
    return stem, first


def _long_cases(canaries: bool) -> list:
    stem, first = _stem_and_letter()
    zwsp = unicodedata.lookup("ZERO WIDTH SPACE")
    prose = "The rain keeps falling on the market and the lamps burn low. "
    cases = [
        ("rain " * 20000, None),
        ("a" * 100000, None),
        ("a " * 50000, None),
        ("a." * 50000, None),
        ("a b " * 25000, None),
        (unicodedata.lookup("CYRILLIC SMALL LETTER ZHE") * 100000, None),
        ("z" * 100000, None),
        ((" ".join(stem) + " ") * 12500, None),
        ((first + " ") * 50000, None),
    ]
    if canaries:
        cases += [
            ("rain " * 20000 + "zzp4ckc4n4ry", "pack"),
            (prose * 1600 + zwsp.join(PACK), "pack"),
            (("I " + " ".join(PACK) + " ") * 3500, "pack"),
        ]
    return cases


def _long_run(name, cases, check, verdicts=None):
    worst = 0.0
    check("warm")
    for n, (text, want) in enumerate(cases):
        want = (verdicts or {}).get(n, want)
        t0 = time.perf_counter()
        verdict = check(text).category
        ms = (time.perf_counter() - t0) * 1000
        assert verdict == want, (name, n)
        assert ms < 2000, (name, n, ms)
        worst = max(worst, ms)
        print(
            "CASE long_input %s %d chars=%d verdict=%s ms=%.1f" % (name, n, len(text), verdict, ms)
        )
    check("a b " * 12500)
    t0 = time.perf_counter()
    assert check("a b " * 12500).category is None
    t50 = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    assert check("a b " * 25000).category is None
    t100 = (time.perf_counter() - t0) * 1000
    assert t100 / max(t50, 0.001) < 3, (name, t50, t100)
    print(
        "PASS normalized_long_inputs %s n=%d max_ms=%.1f scale_50k=%.1f scale_100k=%.1f"
        % (name, len(cases), worst, t50, t100)
    )


def test_normalized_long_inputs(pack):
    _long_run("testpack", _long_cases(True), _check)


def test_normalized_long_inputs_real_lists(monkeypatch):
    _real(monkeypatch)
    _long_run(
        "neon_rain",
        _long_cases(False),
        lambda text: moderation.check_input(text, pack_id="neon_rain"),
        # the spelled stem forms a word of the main list, so it blocks there
        {7: "pack"},
    )


def test_longest_key_prefix_spelled_out(monkeypatch):
    _real(monkeypatch)
    keys = moderation._load_rules("neon_rain", "en").keys
    stem = max((k for t in keys.values() for k in t.mkeys), key=len)[:-1]
    text = (" ".join(stem) + " ") * (100000 // (2 * len(stem)))
    t0 = time.perf_counter()
    moderation.check_input("warm", pack_id="neon_rain")
    cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    check = moderation.check_input(text, pack_id="neon_rain")
    ms = (time.perf_counter() - t0) * 1000
    assert ms < 2000, ms
    assert check.category is None
    print(
        "PASS prefix_spelled chars=%d verdict=%s ms=%.1f first_call_ms=%.1f"
        % (len(text), check.category, ms, cold)
    )


def test_threads_normalized(pack):
    errors = []
    texts = [
        ("I " + " ".join(PACK), "pack"),
        ("zzp4ckc4n4ry", "pack"),
        (" ".join(ALWAYS), "pack_always"),
        ("I watch the rain", None),
    ]

    done = []

    def run(i):
        try:
            for j in range(100):
                text, want = texts[(i + j) % len(texts)]
                if _check(text).category != want:
                    errors.append((i, j))
                done.append(1)
        except BaseException as e:
            errors.append((i, type(e).__name__))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(done) == 1600
    print("PASS threads_normalized n=1600")


def test_output_block_under_load(selfhost, monkeypatch):
    monkeypatch.setattr(main, "slots", threading.BoundedSemaphore(40))
    monkeypatch.setattr(fake, "RULES", [("zzloadcanary", "The clerk reads " + PACK)] + fake.RULES)
    saves = [selfhost.post("/api/game/new", json=NEW_BODY).json()["save"] for _ in range(20)]
    codes = []

    crashed = []

    def play(i):
        try:
            move = "I ask about zzloadcanary" if i % 2 else "I wait"
            r = selfhost.post(
                "/api/game/turn",
                json={"save": saves[i], "player_input": move},
                headers={"X-Coherence-Session": sid()},
            )
            codes.append((i % 2, r.status_code, "save" in r.json()))
        except BaseException as e:
            crashed.append(type(e).__name__)

    threads = [threading.Thread(target=play, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert crashed == [] and len(codes) == 20
    assert sorted(codes) == [(0, 200, True)] * 10 + [(1, 451, False)] * 10
    monkeypatch.setattr(main, "slots", threading.BoundedSemaphore(4))
    body = {"save": saves[0], "player_input": "I ask about zzloadcanary"}
    for _ in range(8):
        assert selfhost.post("/api/game/turn", json=body).status_code == 451
    late = []

    def again():
        try:
            late.append(
                selfhost.post(
                    "/api/game/turn", json=body, headers={"X-Coherence-Session": sid()}
                ).status_code
            )
        except BaseException as e:
            crashed.append(type(e).__name__)

    threads = [threading.Thread(target=again) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert crashed == []
    assert late == [451] * 4
    got = [main.slots.acquire(blocking=False) for _ in range(4)]
    for ok in got:
        if ok:
            main.slots.release()
    assert all(got)
    print("PASS output_block_under_load n=32")


def _words(n, letters="qzjvk", length=8):
    out, i = [], 0
    while len(out) < n:
        x, w = i, ""
        for _ in range(length):
            w += letters[x % len(letters)]
            x //= len(letters)
        out.append(w)
        i += 1
    return out


def _safe_letters(keys):
    """Letters no list key can be spelled from, at least 8 (pads with 2)."""
    words = [k.replace(" ", "") for t in keys.values() for k in list(t.k0) + list(t.mkeys)]
    # l is read as i, so it is left out with the rest of what could spell a key
    safe = set(string.ascii_lowercase) - {"l"}
    while True:
        bad = [w for w in words if set(w) <= safe]
        if not bad:
            break
        safe.discard(max(safe, key=lambda c: sum(c in w for w in bad)))
    return "".join(sorted(safe)) + "2" * max(0, 8 - len(safe))


def _chunks(text, size=10000):
    return [{"role": "user", "content": text[i : i + size]} for i in range(0, len(text), size)]


def _hard_safe(letters):
    """The letters minus enough of them that no word of a hard rule can be
    spelled from the rest."""
    src = re.sub(r"\\.", " ", " ".join(r.pattern.pattern for r in moderation._HARD_RULES))
    words = set(re.findall(r"[a-z]{2,}", src))
    safe = set(letters)
    while True:
        bad = [w for w in words if set(w) <= safe]
        if not bad:
            return sorted(safe)
        safe.discard(max(safe, key=lambda c: sum(c in w for w in bad)))


def _post_ms(client, save):
    t0 = time.perf_counter()
    r = client.post("/api/game/check", json={"save": save})
    return r, (time.perf_counter() - t0) * 1000


def test_save_check_volume(hosted, monkeypatch):
    _real(monkeypatch)
    rng = random.Random(99)
    keys = moderation._load_rules("neon_rain", "en").keys
    stem = max((k for t in keys.values() for k in t.mkeys), key=len)[:-1]
    moderation.check_input("warm", pack_id="neon_rain")
    cap = game_api.HOSTED_SAVE_TEXT_CAP
    safe = _safe_letters(keys)
    # every history case fills the cap exactly: the base save's own strings count
    room = cap - sum(
        len(x) for x in moderation.collect_strings({"history": [{"role": "user", "content": ""}]})
    )
    filler = " ".join(_words(room // 8 + 1))[:room]
    # letters from which no key can be spelled, so no early hit cuts the scan short
    alpha = _hard_safe([c for c in safe if c.isalpha()])
    letters = " ".join(rng.choice(alpha) for _ in range(room // 2 + 1))[:room]
    # the spelled prefix in every chunk, each chunk ending in its own tag so none repeats
    tags = _words(room // 10000 + 1, safe)
    period = " ".join(stem) + " "
    body = period * (10000 // len(period) + 1)
    worst = "".join(body[: 10000 - len(tags[i]) - 1] + " " + tags[i] for i in range(len(tags)))[
        :room
    ]
    worst_chunks = _chunks(worst)
    assert len({c["content"] for c in worst_chunks}) == len(worst_chunks)
    room_keys = cap - sum(len(x) for x in moderation.collect_strings({"state": {"": ""}}))
    dict_keys = {w: "" for w in _words(room_keys // 6, safe, 6)}
    # every chunk decodes in three stages and holds a comma and a comment, so
    # each string is checked in all of its views; the views count against the cap
    esc = chr(92) + "u005cu005cu0061"
    piece = " ".join(_words(30, safe, 6)) + " " + esc + " "
    esc_chunks, used = (
        [],
        sum(
            len(x)
            for x in moderation.collect_strings({"history": [{"role": "user", "content": ""}]})
        ),
    )
    for tag in tags:
        text = tag + " " + (piece * (2000 // len(piece) + 1))[:2000] + ", " + tag + " # " + tag
        cost = len(text) + moderation.view_chars(text)
        if used + cost > cap:
            break
        esc_chunks.append({"role": "user", "content": text})
        used += cost
    esc_over = _chunks((piece * (room // len(piece) + 1))[: room - 4] + ", #")
    print("CASE volume escapes chunks=%d counted_with_views=%d" % (len(esc_chunks), used))
    cases = [
        ("filler_cap", {"history": _chunks(filler)}, 200, 10000),
        ("single_letters_cap", {"history": _chunks(letters)}, 200, 10000),
        ("prefix_worst_cap", {"history": worst_chunks}, 200, 10000),
        ("dict_keys_%d" % len(dict_keys), {"state": dict_keys}, 200, 10000),
        ("escapes_cap", {"history": esc_chunks}, 200, 10000),
        ("escapes_over_cap", {"history": esc_over}, 413, 2000),
        ("over_cap", {"history": _chunks("q" * (room + 1), room + 1)}, 413, 2000),
    ]
    for name, save, want, limit in cases:
        size = sum(len(x) for x in moderation.collect_strings(save))
        assert size <= cap or want == 413, (name, size)
        r, ms = _post_ms(hosted, save)
        if want is not None:
            assert r.status_code == want, (name, r.status_code, r.text[:120])
        assert r.status_code in (200, 413, 451), (name, r.status_code)
        assert ms < limit, (name, ms)
        print("CASE volume %s chars=%d status=%d ms=%.1f" % (name, size, r.status_code, ms))
    print("PASS save_check_volume n=%d cap=%d" % (len(cases), cap))
