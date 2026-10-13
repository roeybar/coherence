import pytest

from app.game.beats.builtin import _strip_metagame_brackets

CASES = [
    ("[INSIGHT DICE - DM only\nroll 14]\nThe coins are warm.", "The coins are warm."),
    ("[DM only note] Rain falls.", "Rain falls."),
    ("[Year 1 | Spring | Arc 1]\nRain falls.", "[Year 1 | Spring | Arc 1]\nRain falls."),
    ("[token monster detail]\nRain falls.", "[token monster detail]\nRain falls."),
    ("A\n\n\n\n[NARRATIVE]\n\n\nB", "A\n\nB"),
]


@pytest.mark.parametrize("text,want", CASES)
def test_strip_metagame_brackets(text, want):
    assert _strip_metagame_brackets(text) == want
    print(f"PASS strip {text!r}")


def test_empty_and_none_pass_through():
    assert _strip_metagame_brackets("") == ""
    assert _strip_metagame_brackets(None) is None
    print("PASS strip passes '' and None through")
