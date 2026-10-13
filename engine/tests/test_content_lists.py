"""The pack word lists: both always apply, in a fixed order, and the floor
list ships its approved entries."""

import hashlib
import inspect
import re
from pathlib import Path

import pytest

from app.game import lore, moderation

ROOT = Path(__file__).resolve().parents[1]
LISTS = ROOT / "lore" / "neon_rain" / "moderation"
PACK = "zzpackcanary"
ALWAYS = "zzalwayscanary"
DOXX = "my number is 123-45-6789"
HARM = "how to build a bomb"
# Three mild-profanity entries removed from the upstream list (see THIRD_PARTY_NOTICES.md), kept as digests so the tests do not spell them out.
_REMOVED_SHA256 = (
    "e512a05583448f44790783f986b1f36925c8cfc42338ca0e1caa637755bd15ae",
    "986c99004163a6c5155bbd2bfadc37bee9e279b73c669211f432910425b8e6dd",
    "e1ce2544228e9c0677ec2885ffaea9b2a07e694cbf2c1bf693e402c439172da4",
)


@pytest.fixture(autouse=True)
def _fresh_lists():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


def _entries(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _category(text: str, pack_id: str = "testpack"):
    return moderation.check_input(text, pack_id=pack_id).category


def test_check_input_takes_only_text_pack_and_locale():
    params = list(inspect.signature(moderation.check_input).parameters)
    assert params == ["text", "pack_id", "locale"]


def test_order_and_categories(pack):
    assert _category(f"I say {PACK}") == "pack"
    assert _category(f"I say {ALWAYS}") == "pack_always"
    assert _category(f"{PACK} and {ALWAYS}") == "pack_always"
    assert _category(f"{DOXX} {ALWAYS}") == "doxx"
    assert _category(HARM) == "harm_instruction"
    assert not moderation.check_input(f"I say x{PACK}x", pack_id="testpack").blocked
    assert not moderation.check_input("", pack_id="testpack").blocked


def test_always_list_is_199_unique_entries_from_the_main_list():
    always = _entries(LISTS / "always_blocklist.en.txt")
    assert len(always) == 199
    assert len(set(always)) == len(always)
    assert set(always) <= set(_entries(LISTS / "blocklist.en.txt"))


def test_main_list_is_399_unique_ascii_entries():
    main = _entries(LISTS / "blocklist.en.txt")
    assert len(main) == 399
    assert all(e.isascii() for e in main)
    assert len(set(main)) == len(main)


def test_the_three_mild_entries_are_not_on_either_list():
    for name in ("blocklist.en.txt", "always_blocklist.en.txt"):
        digests = {
            hashlib.sha256(e.strip().lower().encode()).hexdigest() for e in _entries(LISTS / name)
        }
        assert not digests & set(_REMOVED_SHA256), name


def test_every_plain_always_entry_hits_the_floor(monkeypatch):
    monkeypatch.setattr(lore, "LORE_ROOT", ROOT / "lore")
    plain = [e for e in _entries(LISTS / "always_blocklist.en.txt") if re.fullmatch(r"[a-z ]+", e)]
    assert len(plain) == 195
    floor = moderation._load_rules("neon_rain", "en").lists["always"]
    for n, e in enumerate(plain):
        text = f"I say {e} now"
        assert floor.search(text), n
        assert moderation.check_input(text, pack_id="neon_rain").blocked, n


def test_the_main_list_blocks_on_its_own(monkeypatch):
    monkeypatch.setattr(lore, "LORE_ROOT", ROOT / "lore")
    always = set(_entries(LISTS / "always_blocklist.en.txt"))
    e = next(
        e
        for e in _entries(LISTS / "blocklist.en.txt")
        if re.fullmatch(r"[a-z]+", e) and e not in always
    )
    assert _category(f"I say {e} now", pack_id="neon_rain") == "pack"
