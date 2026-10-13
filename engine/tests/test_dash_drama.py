"""A spaced em, en or ASCII dash in narration becomes a newline; compound
words, numeric ranges and an unspaced dash stay as they are."""

import pytest

from app.game.beats.builtin import _replace_em_dashes

EM = chr(0x2014)
EN = chr(0x2013)


@pytest.mark.parametrize("dash", [EM, EN, "-"])
def test_spaced_dash_becomes_newline(dash):
    assert _replace_em_dashes("you stand " + dash + " alone") == "you stand\nalone"


@pytest.mark.parametrize(
    "text", ["the tax-collector waits", "it takes 10-12 days", "you stand" + EM + "alone"]
)
def test_unspaced_dash_unchanged(text):
    assert _replace_em_dashes(text) == text
