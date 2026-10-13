"""Pack-supplied engine fragments: present, they sit in place; absent, the
prompt reads as if they were never there."""

from unittest import mock

import pytest

from app.game import engine, lore, prompts
from app.game import physicalisation as phys
from app.game.state import CharacterProfile, GameConfig, GameSave, WorldState

FRAGS = {
    "insight_example": "a sample insight example",
    "ability_tier_note": "Sample tier note.",
    "physics_examples_a": "'alpha', 'beta'",
    "physics_examples_b": "'gamma', 'delta'",
    "spoiler_keywords": ["Secret Fleet", "", 7],
}
LEDGER = {"tier_exposure": {"miller": {"tier_3_count": 1, "last_witnessed_turn": 4}}}
GAP_MARKS = ("{insight_example}", "{ability_tier_note}", " .", ", .", "- .")


@pytest.fixture
def testpack(pack):
    lore.clear_cache()
    p = lore.load_pack("testpack")
    p["manifest"] = dict(p["manifest"], templates={"abilities": "abilities.md"})
    yield p
    lore.clear_cache()


@pytest.fixture
def with_frags(testpack):
    testpack["manifest"]["engine_fragments"] = dict(FRAGS)
    return testpack


def _no_gaps(text):
    for mark in GAP_MARKS:
        assert mark not in text, mark


def test_npc_knowledge_with_fragment(with_frags):
    text = prompts._npc_knowledge(with_frags)
    assert "no basis to know - a sample insight example.\n" in text


def test_npc_knowledge_without_fragment(testpack):
    text = prompts._npc_knowledge(testpack)
    assert "no basis to know.\n" in text
    assert "\n  Never expose" in text
    for line in text.splitlines():
        assert line == line.rstrip()


def test_ability_reminder_with_fragment(with_frags):
    text = prompts._ability_block(with_frags, "standard")
    assert "Never name tiers to them. Sample tier note. Full ability rules" in text


def test_ability_reminder_without_fragment(testpack):
    text = prompts._ability_block(testpack, "standard")
    assert "Never name tiers to them. Full ability rules" in text
    _no_gaps(text)


def test_volatile_block_carries_both(with_frags):
    _, volatile = prompts.build_system_blocks(
        CharacterProfile(), 2, GameConfig(pack_id="testpack"), has_npcs=True
    )
    assert "a sample insight example" in volatile
    assert "Sample tier note." in volatile


def test_volatile_block_without_fragments(testpack):
    _, volatile = prompts.build_system_blocks(
        CharacterProfile(), 2, GameConfig(pack_id="testpack"), has_npcs=True
    )
    assert "no basis to know.\n" in volatile
    assert "Never name tiers to them. Full ability rules" in volatile
    assert "{insight_example}" not in volatile and "{ability_tier_note}" not in volatile


def _phys(p):
    return phys.render_physicalisation_block(
        dict(LEDGER), "scene", ability_intent=phys.INTENT_INTERNAL, pack=p
    )


def test_physicalisation_with_fragments(with_frags):
    text = _phys(with_frags)
    assert "DO NOT name it, DO NOT use the words 'alpha', 'beta'.\n" in text
    assert "nature - no 'gamma', 'delta'. They register" in text


def test_physicalisation_without_fragments(testpack):
    text = _phys(testpack)
    assert "DO NOT name it.\n" in text
    assert "DO NOT use the words" not in text
    assert "player's nature. They register what they saw" in text
    _no_gaps(text)


def test_physicalisation_without_pack():
    text = phys.render_physicalisation_block(
        dict(LEDGER), "scene", ability_intent=phys.INTENT_INTERNAL
    )
    assert "DO NOT name it.\n" in text
    assert "player's nature. They register" in text


def test_fragment_list_is_lower_case_strings_only(with_frags):
    assert lore.engine_fragment_list(with_frags, "spoiler_keywords") == ("secret fleet",)
    assert lore.engine_fragment_list(with_frags, "missing") == ()
    assert lore.engine_fragment(with_frags, "spoiler_keywords") == ""


class _Captured(Exception):
    pass


def _meta_system():
    seen = {}

    def fake(system, messages, **kw):
        seen["system"] = system
        raise _Captured()

    state = WorldState(
        tutorial_stage=2,
        threats=["the secret fleet nears", "rain"],
        rumour="A SECRET FLEET was seen",
        dm_notes=["hidden"],
    )
    gs = GameSave(state=state, config=GameConfig(pack_id="testpack"), turn=3)
    with mock.patch.object(engine, "call_llm_plain", fake), pytest.raises(_Captured):
        engine.run_meta(gs, "what now?")
    return seen["system"]


def test_meta_redacts_pack_keywords(with_frags):
    text = _meta_system()
    assert "secret fleet nears" not in text
    assert '"rumour": "(redacted)"' in text
    assert '"rain"' in text and "hidden" not in text


def test_meta_without_keywords_keeps_state(testpack):
    text = _meta_system()
    assert "the secret fleet nears" in text
    assert "A SECRET FLEET was seen" in text
    assert "(redacted)" not in text and "hidden" not in text
