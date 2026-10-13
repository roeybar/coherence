"""tell / ask / say need an addressee, and only NPCs the save knows are offered."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.game.addressee import AddresseeKind, classify_addressee, looks_like_dialogue

OPENING = Path(__file__).resolve().parents[1] / "lore" / "neon_rain" / "prompts" / "opening.md"

MOVE_2 = (
    "I open the door and let the visitor in. Then I try to kill the hallway lights - "
    "or the whole floor's power, I can't tell how far it will reach."
)

NOT_DIALOGUE = [
    MOVE_2,
    "I can't tell how far it goes",
    "I can't tell the difference",
    "I can't tell them apart",
    "I cannot tell her age",
    "ask around",
    "say nothing",
    "I walk north",
]

DIALOGUE = [
    "I tell her to wait.",
    "Tell Mara",
    "tell MARA",
    "tell O'Brien",
    "TELL HER",
    "say to Dex",
    "I say hello to Mara",
    "say goodbye to Dex",
    "tell me what you saw",
    "tell us",
    "ask the guard",
    "ask a guard",
    "ask my contact",
    "I ask where Kessler went",
    "ask if he's armed",
    "ask about the job",
]


def save_with(dispositions, npcs, narration):
    state = SimpleNamespace(npc_dispositions=dispositions, npcs=npcs)
    return SimpleNamespace(state=state, history=[{"role": "assistant", "content_en": narration}])


def test_neon_rain_opening_raises_no_modal():
    text = OPENING.read_text(encoding="utf-8")
    assert "Kessler" in text
    gs = save_with({}, {}, text)
    assert classify_addressee("I tell her to come in.", gs).kind == AddresseeKind.NONE
    print("PASS the neon_rain opening names no NPC the save knows, so no addressee modal")


@pytest.mark.parametrize("text", NOT_DIALOGUE)
def test_not_dialogue(text):
    assert looks_like_dialogue(text) is False
    print(f"PASS not dialogue: {text}")


@pytest.mark.parametrize("text", DIALOGUE)
def test_dialogue(text):
    assert looks_like_dialogue(text) is True
    print(f"PASS dialogue: {text}")


def test_curly_double_quotes_count_as_dialogue():
    # No speech verb here, so the quoted-speech pattern is the only route.
    curly = chr(0x201C) + "We leave at dawn" + chr(0x201D)
    assert looks_like_dialogue(curly) is True
    assert looks_like_dialogue('"We leave at dawn"') is True
    assert looks_like_dialogue("I walk north") is False
    print("PASS curly and straight double quotes are dialogue, a plain action is not")


def test_options_are_only_known_npcs():
    gs = save_with(
        {"Mara": 2},
        {"npc:dex": {"name": "Dex"}},
        "Rain. Mara waits by the door while Dex counts credits in the Office.",
    )
    result = classify_addressee("I tell her to wait.", gs)
    assert result.kind == AddresseeKind.AMBIGUOUS
    assert result.names == ["Mara", "Dex"]
    print("PASS the modal offers Mara and Dex only, never narration words")
