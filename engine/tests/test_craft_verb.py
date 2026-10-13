"""Shaping one thing into another counts as crafting."""

import pytest

from app.game.craft_gate import detect_craft

CARRIED = {"carried": [{"name": "wire"}]}

CRAFT = [
    "I twist the wire into a crude lockpick.",
    "I melt the coins down into a bar",
    "I make a torch",
]

NOT_CRAFT = [
    "I walk into the bar",
    "I cut into the cake",
    "I wind into the crowd",
    "I cut the rope and climb into the vent",
    "strip off my jacket and dive into the river",
    "tear open the envelope and look into it",
    "I bend the bar, then crawl into the duct",
    "I cut through the crowd into the alley",
    "I wind my way into the market",
    "we file past the guards into the hall",
    "I tear through the hedge into the yard",
]


@pytest.mark.parametrize("text", CRAFT)
def test_shaping_into_is_craft(text):
    assert detect_craft(text, CARRIED) is True
    print(f"PASS craft: {text}")


@pytest.mark.parametrize("text", NOT_CRAFT)
def test_movement_into_is_not_craft(text):
    assert detect_craft(text, CARRIED) is False
    print(f"PASS not craft: {text}")
