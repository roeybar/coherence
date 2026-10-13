"""A pack with no world_gen template (the sample pack ships none) gets the engine's
fallback: it asks for the premise JSON the engine parses, formats safely with any
hint, and a custom start built on it round-trips through create_save."""

import json

from app.game import engine, lore, prompts
from app.game.prompts import build_system_blocks
from app.game.state import CharacterProfile, GameConfig
from app.providers import fake
from test_context_window import bound

KEYS = ["name", "era", "tone", "premise", "arc_name", "opening_scene", "world_state_overrides"]
HEBREW = "".join(chr(c) for c in range(0x5D0, 0x5EA))
HINTS = ["", "a harbour town", "a {brace} world }{ {0} {hint} %s", "x" * 5000, HEBREW]


def test_sample_pack_has_no_world_gen():
    assert lore.get_template(lore.load_pack("neon_rain"), "world_gen") is None
    assert prompts.world_gen_prompt(GameConfig(pack_id="neon_rain")) == prompts._WORLD_GEN_FALLBACK
    print("PASS the sample pack ships no world_gen template and gets the fallback")


def test_fallback_formats_with_any_hint():
    template = prompts._WORLD_GEN_FALLBACK
    assert template.isascii()
    for hint in HINTS:
        out = template.format(hint=hint)
        assert out.endswith("Player's request: " + hint), hint[:20]
        for word in (*KEYS, "human hook", "Never a list of options"):
            assert word in out, (hint[:20], word)
        example = [line for line in out.split("\n") if line.startswith('{"name"')]
        assert len(example) == 1, hint[:20]
        assert list(json.loads(example[0])) == KEYS, hint[:20]
    print(f"PASS the fallback formats with {len(HINTS)} hints and its example line parses")


def test_custom_start_round_trip(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    premise = {
        "name": "Harbour of Glass",
        "era": "late tide",
        "tone": "wet and wary",
        "premise": "A harbour town where the tide keeps a ledger. Someone owes it.",
        "arc_name": "The Pier",
        "opening_scene": 'Rain on the pier. Mara grabs your sleeve: "Are you the one they sent?"',
        "world_state_overrides": {"location": "the pier"},
    }
    monkeypatch.setattr(
        fake, "RULES", [("Reply with one JSON object only", json.dumps(premise))] + fake.RULES
    )
    with bound("fake"):
        gs = engine.create_save(
            "c",
            CharacterProfile(name="Walker"),
            GameConfig(pack_id="testpack", world_type="custom"),
            world_hint="a harbour {town} }{",
        )
    assert gs.config.world_type == "custom"
    assert gs.history[0]["content_en"] == premise["opening_scene"]
    assert gs.state.location == "the pier"
    asked = [
        c
        for c in calls
        if "Player's request: a harbour {town} }{" in fake._text(c["messages"][-1]["content"])
    ]
    assert len(calls) == 1 and len(asked) == 1
    stable = build_system_blocks(gs.profile, 2, gs.config)[0]
    assert "WORLD: " + premise["name"] in stable
    assert premise["arc_name"] in stable
    print("PASS a custom start asks once, parses the premise and plays its opening")
