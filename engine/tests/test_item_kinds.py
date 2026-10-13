"""Item kind words: the "emoji" field holds an ASCII kind word, never a glyph."""

import pytest

from app import store
from app.game import items

GLYPH = chr(0x1F5E1)


@pytest.fixture
def bound():
    doc = {}
    token = store.bind_save(doc)
    try:
        yield doc
    finally:
        store.unbind_save(token)


def test_model_glyph_is_normalised_to_the_name_kind(bound):
    ops = [
        {"op": "add", "name": "rusted dagger", "where": "carried", "emoji": GLYPH},
        {"op": "add", "name": "glass vial", "where": "carried", "emoji": "sparkles"},
        {"op": "add", "name": "odd bauble", "where": "carried", "emoji": GLYPH},
    ]
    out = items.apply_ops("local", ops, current_location="dock")
    assert [a["emoji"] for a in out["applied"]] == ["weapon", "potion", "item"]
    assert [r["emoji"] for r in bound["items"]] == ["weapon", "potion", "item"]


def test_model_kind_word_is_kept(bound):
    out = items.apply_ops(
        "local",
        [
            {"op": "add", "name": "rusted dagger", "where": "carried", "emoji": " Potion "},
            {"op": "add", "name": "rusted dagger two", "where": "carried"},
        ],
        current_location="dock",
    )
    assert [a["emoji"] for a in out["applied"]] == ["potion", "weapon"]


def test_old_save_glyph_reads_as_kind_word():
    doc = {"_id": "a1", "name": "coil of rope", "holder": "player", "emoji": GLYPH}
    assert items._public(doc)["emoji"] == "rope"
    assert items._public({"_id": "a2", "name": "thing", "emoji": ""})["emoji"] == "item"


def test_every_hint_row_gives_a_kind_word():
    kinds = [k for _, k in items._EMOJI_HINTS]
    assert len(kinds) == 17 and all(k.isascii() and k.isalpha() for k in kinds)
    assert items._default_emoji({"name": "nothing matches"}) == "item"


def test_prompt_block_is_ascii_and_lists_kind_words():
    block = items.format_block([], [{"name": "brass lantern", "emoji": GLYPH}])
    assert block.isascii()
    assert "  light brass lantern" in block
    assert "weapon|shield|armor|bow|container|coin|scroll|key|jewel" in block
    assert "Set emoji to one of the kind words above" in block
