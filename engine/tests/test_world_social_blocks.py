"""The social and world prompt blocks render the state fields the DM's state delta can
write, and trade is allowed with an NPC in the same scene or seen this turn."""

import random
from types import SimpleNamespace

from app.game import social_systems, trade, world_systems
from app.game.state import WorldState

ROLL = {
    "npc_id": "Vex",
    "kind": "insight",
    "dc": 14,
    "roll": 11,
    "modifier": 2,
    "total": 13,
    "outcome": "fail",
    "turn": 7,
}
INSIGHT_HEAD = (
    "RECENT NPC INSIGHT ROLLS (the DM rolled these last few turns "
    "- honour the outcomes when narrating what NPCs know / suspect):"
)
LIST_FIELDS = (
    "production_techniques",
    "player_conditions",
    "pc_only_anomalies",
    "declined_hooks",
)


def test_social_block_renders_insight_rolls_only():
    assert social_systems.render_social_systems_block(WorldState()) == ""
    state = WorldState(last_insight_rolls=[ROLL])
    want = INSIGHT_HEAD + "\n  - T7  Vex insight (roll 13 vs DC 14) -> fail"
    assert social_systems.render_social_systems_block(state) == want
    print("PASS the social block renders the insight rolls and is empty without them")


def test_fields_outside_the_schema_never_reach_the_blocks():
    state = WorldState(last_insight_rolls=[])
    delta = {
        "witnesses_geometry": [{"actor": "guard", "distance": "close", "posture": "hostile"}],
        "npc_calcification": {"vex": {"counter": 9, "calcified": True}},
        "last_insight_rolls": [ROLL],
    }
    diff = state.apply_delta(delta, schema=[{"name": "last_insight_rolls", "type": "list"}])
    assert list(diff) == ["last_insight_rolls"]
    assert state.get("witnesses_geometry") is None
    assert state.get("npc_calcification") is None
    assert social_systems.render_social_systems_block(state).startswith(INSIGHT_HEAD)
    print("PASS the state delta writes schema fields only, so only the insight block renders")


def test_world_blocks_render_delta_written_fields():
    schema = [{"name": name, "type": "list"} for name in LIST_FIELDS]
    schema.append({"name": "day_segment", "type": "str"})
    state = WorldState()
    delta = {
        "production_techniques": [{"name": "cold forging", "description": "hammered cold"}],
        "player_conditions": [
            {
                "kind": "injury",
                "summary": "cut forearm",
                "severity": "mild",
                "resolvable_via": "rest",
            },
        ],
        "pc_only_anomalies": [
            {"kind": "sound", "token": "a low hum", "trigger_pattern": "near water"},
        ],
        "declined_hooks": [{"summary": "the courier's offer", "declined_at_turn": 3}],
        "day_segment": "late dusk",
    }
    state.apply_delta(delta, schema=schema)
    blocks = {
        world_systems.render_production_techniques_block: (
            "PLAYER-ESTABLISHED BUILD TECHNIQUES (prefer these for new construction unless the "
            "player declares otherwise):\n  - cold forging: hammered cold"
        ),
        world_systems.render_player_conditions_block: (
            "PLAYER-SIDE SOFT CONDITIONS (frame, do not gate - the player can always act "
            "through them):\n  - [injury/mild] cut forearm (resolves via: rest)"
        ),
        world_systems.render_pc_only_anomalies_block: (
            "PC-ONLY SENSORY ANOMALIES (only the player perceives these - NPCs deny or "
            "fail to notice when asked):\n  - [sound] a low hum (trigger: near water)"
        ),
        world_systems.render_day_segment_block: (
            "TIME OF DAY: dusk - the world is between; doors that were open close; "
            "what hunts begins to move"
        ),
    }
    for render, want in blocks.items():
        assert render(state) == want, render.__name__
    hooks = world_systems.render_declined_hooks_block(state)
    assert hooks.startswith("DECLINED HOOKS (the player walked away from these;")
    assert hooks.endswith('\n  - T3  "the courier\'s offer"')
    print("PASS the state delta writes the world fields and each block renders them")


def test_can_trade_same_scene_or_seen_this_turn():
    state = SimpleNamespace(location="Dock 4")
    here = SimpleNamespace(location=" dock 4 ", last_seen_turn=1)
    away = SimpleNamespace(location="Old Market", last_seen_turn=1)
    assert trade.can_trade_dict(state, here, turn=5) == {"allowed": True, "reason": "ok"}
    refused = {"allowed": False, "reason": "not_same_scene"}
    assert trade.can_trade_dict(state, away, turn=5) == refused
    assert trade.can_trade_dict(state, away, turn=1) == {"allowed": True, "reason": "ok"}
    nowhere = SimpleNamespace(location="", last_seen_turn=None)
    assert trade.can_trade_dict(SimpleNamespace(location=""), nowhere, turn=None) == refused
    print("PASS trade is allowed in the same scene or with an NPC seen this turn")


def test_blocks_and_trade_hold_under_volume():
    rolls = [dict(ROLL, npc_id=f"npc{t}", turn=t) for t in range(1000)]
    lines = social_systems.render_social_systems_block(WorldState(last_insight_rolls=rolls))
    lines = lines.split("\n")
    want = [f"  - T{t}  npc{t} insight (roll 13 vs DC 14) -> fail" for t in range(995, 1000)]
    assert lines[0] == INSIGHT_HEAD
    assert lines[1:] == want
    for seg in world_systems.DAY_SEGMENTS + ("", "noonish", "LATE NIGHT"):
        shown = seg if seg in world_systems.DAY_SEGMENTS else "midday"
        text = world_systems.render_day_segment_block(WorldState(day_segment=seg))
        assert text.startswith(f"TIME OF DAY: {shown} - ")
    rng = random.Random(7)
    places = ["Dock 4", " dock 4", "Old Market", "", "DOCK 4 ", None]
    for _ in range(2000):
        here, there = rng.choice(places), rng.choice(places)
        seen, turn = rng.choice([None, 1, 2]), rng.choice([None, 1, 2])
        npc = SimpleNamespace(location=there, last_seen_turn=seen)
        got = trade.can_trade(SimpleNamespace(location=here), npc, turn=turn)
        a, b = (there or "").strip().lower(), (here or "").strip().lower()
        allowed = (a == b and bool(a)) or (turn is not None and seen == turn)
        assert got.allowed is allowed
        assert got.reason == ("ok" if allowed else "not_same_scene")
    print("PASS volume: last five of 1000 rolls, every day segment, 2000 random trades")
