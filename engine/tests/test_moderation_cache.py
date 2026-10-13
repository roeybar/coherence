import hashlib
from pathlib import Path

import pytest

from conftest import NEW_BODY
from app.game import lore, moderation

PACK = "zzcachepack"
TERMS = ["zzqterm" + c + "x" for c in "bdfghjknpsvwy"]
EXPECTED = (1, "a228d3052bd565ab1bf5daea8b010c7b06c5ab9a3c1ba232466bf0718230dc42")


@pytest.fixture(autouse=True)
def _fresh_cache():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


def _make_pack(root, pack_id):
    lists = root / pack_id / "moderation"
    lists.mkdir(parents=True)
    (lists / "always_blocklist.en.txt").write_text("zzqalwaysbase\n", encoding="utf-8")
    path = lists / "blocklist.en.txt"
    path.write_text("zzqbaseword\n", encoding="utf-8")
    return path


@pytest.fixture
def cache_pack(tmp_path, monkeypatch):
    path = _make_pack(tmp_path, PACK)
    monkeypatch.setattr(lore, "LORE_ROOT", tmp_path)
    monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", PACK)
    moderation.reset_pack_cache()
    yield path
    moderation.reset_pack_cache()


def _append(path, term):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(term + "\n")


def _form(term, name):
    if name == "leet":
        return term.replace("e", "3")
    if name == "spaced":
        return " ".join(term)
    return term


def _spy(monkeypatch):
    real = moderation.check_input
    seen = []

    def check(text, **kw):
        seen.append(text)
        return real(text, **kw)

    monkeypatch.setattr(moderation, "check_input", check)
    return seen


def _save(client):
    r = client.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    return r.json()["save"]


@pytest.mark.parametrize("term", TERMS)
@pytest.mark.parametrize("form", ["plain", "leet", "spaced"])
def test_list_reload_rechecks_old_text(cache_pack, term, form):
    text = "a " + _form(term, form) + " here"
    old = moderation._namespace(PACK, "en", moderation._load_rules(PACK, "en"))
    assert not moderation.check_input_cached(text, pack_id=PACK).blocked
    assert len(moderation._VERDICTS) == 1
    _append(cache_pack, term)
    moderation.reset_pack_cache()
    new = moderation._namespace(PACK, "en", moderation._load_rules(PACK, "en"))
    assert new != old
    assert moderation.check_input_cached(text, pack_id=PACK).blocked
    print("PASS list_reload %s %s" % (term, form))


def test_hosted_never_uses_cache(hosted, monkeypatch):
    save = _save(hosted)
    moderation.reset_pack_cache()
    seen = _spy(monkeypatch)
    r = hosted.post("/api/game/check", json={"save": save})
    assert r.status_code == 200, r.text
    first = list(seen)
    seen.clear()
    r = hosted.post("/api/game/check", json={"save": save})
    assert r.status_code == 200, r.text
    assert len(first) == len(seen) > 0
    assert len(set(first)) == len(first) and len(set(seen)) == len(seen)
    assert first == seen
    assert len(moderation._VERDICTS) == 0
    print("PASS hosted_uncached strings=%d" % len(first))


def test_selfhost_second_check_hits_cache(selfhost, monkeypatch):
    save = _save(selfhost)
    moderation.reset_pack_cache()
    seen = _spy(monkeypatch)
    r = selfhost.post("/api/game/check", json={"save": save})
    assert r.status_code == 200 and seen, r.text
    assert len(moderation._VERDICTS) > 0
    seen.clear()
    r = selfhost.post("/api/game/check", json={"save": save})
    assert r.status_code == 200 and not seen, r.text
    print("PASS selfhost_cache_hit calls=0")


def test_blocked_results_are_not_stored(cache_pack):
    for _ in range(2):
        assert moderation.check_input_cached("zzqbaseword", pack_id=PACK).blocked
        assert len(moderation._VERDICTS) == 0
    print("PASS blocked_not_cached")


def test_rules_version_and_code_digest():
    assert (moderation.MODERATION_RULES_VERSION, moderation.CODE_DIGEST) == EXPECTED
    assert (
        moderation.CODE_DIGEST == hashlib.sha256(Path(moderation.__file__).read_bytes()).hexdigest()
    )
    print("PASS rules_version_and_code_digest")


@pytest.mark.parametrize("term", TERMS)
@pytest.mark.parametrize("form", ["plain", "leet", "spaced"])
def test_old_verdict_retained_under_a_different_namespace(cache_pack, term, form):
    text = "a " + _form(term, form) + " here"
    old = moderation._namespace(PACK, "en", moderation._load_rules(PACK, "en"))
    assert not moderation.check_input_cached(text, pack_id=PACK).blocked
    assert len(moderation._VERDICTS) == 1
    _append(cache_pack, term)
    with moderation._RULES_LOCK:
        moderation._RULES_CACHE.clear()
    assert len(moderation._VERDICTS) == 1
    assert moderation._namespace(PACK, "en", moderation._load_rules(PACK, "en")) != old
    assert moderation.check_input_cached(text, pack_id=PACK).blocked
    print("PASS old_verdict_namespace %s %s" % (term, form))


def test_pack_switch_misses_cache(cache_pack, tmp_path, monkeypatch):
    other = "zzcacheother"
    _make_pack(tmp_path, other)
    moderation.reset_pack_cache()
    text = "the quiet rain falls"
    assert not moderation.check_input_cached(text, pack_id=PACK).blocked
    seen = _spy(monkeypatch)
    assert not moderation.check_input_cached(text, pack_id=other).blocked
    assert seen == [text]
    print("PASS pack_switch calls=1")


def test_hosted_rechecks_a_selfhost_warmup(hosted, monkeypatch):
    save = _save(hosted)
    moderation.reset_pack_cache()
    for text in moderation.collect_strings(save, exempt=moderation.SAVE_EXEMPT):
        assert not moderation.check_input_cached(text, pack_id="testpack").blocked
    assert len(moderation._VERDICTS) > 0
    seen = _spy(monkeypatch)
    r = hosted.post("/api/game/check", json={"save": save})
    assert r.status_code == 200 and seen, r.text
    print("PASS hosted_after_warmup calls=%d" % len(seen))


def test_missing_rules_fail_after_a_warm_check(selfhost, monkeypatch):
    save = _save(selfhost)
    assert selfhost.post("/api/game/check", json={"save": save}).status_code == 200
    assert len(moderation._VERDICTS) > 0

    def unavailable(*args, **kw):
        raise moderation.ListsUnavailable("testpack")

    monkeypatch.setattr(moderation, "_load_rules", unavailable)
    r = selfhost.post("/api/game/check", json={"save": save})
    assert r.status_code == 500 and r.json()["detail"]["error"] == "internal", r.text
    print("PASS warm_missing_rules status=500")


def test_selfhost_save_over_one_million_chars(selfhost):
    save = _save(selfhost)
    save["state"]["zz"] = "q" * 1_000_001
    r = selfhost.post("/api/game/check", json={"save": save})
    assert r.status_code == 200, r.text
    print("PASS selfhost_large_save status=200")


def test_reset_after_read_discards_old_rules(cache_pack, monkeypatch):
    term = TERMS[0]

    def after_read():
        moderation._HOOKS["after_read"] = None
        _append(cache_pack, term)
        moderation.reset_pack_cache()

    monkeypatch.setitem(moderation._HOOKS, "after_read", after_read)
    assert moderation.check_input_cached(" ".join(term), pack_id=PACK).blocked
    assert moderation._load_rules(PACK, "en").lists["default"].search(term)
    print("PASS reset_after_read")


@pytest.mark.parametrize("warm", [False, True])
def test_reset_before_return_discards_old_pass(cache_pack, monkeypatch, warm):
    term = TERMS[0]
    text = " ".join(term)
    if warm:
        assert not moderation.check_input_cached(text, pack_id=PACK).blocked

    def before_return():
        moderation._HOOKS["before_return"] = None
        _append(cache_pack, term)
        moderation.reset_pack_cache()

    monkeypatch.setitem(moderation._HOOKS, "before_return", before_return)
    assert moderation.check_input_cached(text, pack_id=PACK).blocked
    assert len(moderation._VERDICTS) == 0
    print("PASS reset_before_return warm=%s" % warm)


def test_hostile_pack_ids_do_not_grow_rules_cache(cache_pack):
    assert not moderation.check_input_cached("hello", pack_id=PACK).blocked
    size = len(moderation._RULES_CACHE)
    for i in range(1000):
        assert not moderation.check_input_cached("hello", pack_id="hostile%d" % i).blocked
    assert len(moderation._RULES_CACHE) == size
    print("PASS hostile_packs n=1000")


def test_lone_surrogate_can_be_cached(cache_pack):
    lone = chr(0xD800) + " x"
    assert not moderation.check_input_cached(lone, pack_id=PACK).blocked
    assert not moderation.check_input_cached(lone, pack_id=PACK).blocked
    assert len(moderation._VERDICTS) == 1
    print("PASS lone_surrogate")
