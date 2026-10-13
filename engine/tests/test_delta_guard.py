"""Delta rewrites the move did not make are ignored."""

import logging

from app.game import engine
from app.game.beats.builtin import DMWorldStepBeat
from app.game.parser import guard_delta
from app.game.state import CharacterProfile, GameConfig
from app.providers import fake
from test_context_window import bound, world_step_state

CURRENT = {
    "cycle": 3,
    "day": 2,
    "heat": 5,
    "composure": 6,
    "district": "Old Quarter",
    "location": "Kessler Building, office 4F, Meridian Old Quarter",
}


def test_observed_corruption():
    kept, dropped = guard_delta(
        {
            "cycle": 1,
            "heat": 0,
            "composure": 10,
            "district": "old quarter",
            "location": "office 4F",
        },
        CURRENT,
    )
    assert kept == {"heat": 0, "composure": 10, "location": "office 4F"}
    assert set(dropped) == {"cycle", "district"}
    print("PASS observed corruption: case-only district and a lower cycle are ignored")


def test_counters_stay_or_step_up_by_one():
    assert guard_delta({"cycle": 3}, CURRENT) == ({"cycle": 3}, [])
    assert guard_delta({"cycle": 4}, CURRENT) == ({"cycle": 4}, [])
    assert guard_delta({"cycle": 5}, CURRENT) == ({}, ["cycle"])
    assert guard_delta({"day": 1}, CURRENT) == ({}, ["day"])
    assert guard_delta({"chapter": "x"}, {"chapter": 2}) == ({"chapter": "x"}, [])
    print(
        "PASS counters: +0 and +1 kept, a jump or a decrease ignored, a non-int left to apply_delta"
    )


def test_legitimate_changes_are_kept_whole():
    delta = {
        "heat": 6,
        "district": "Neon Row",
        "location": "Kessler Building, stairwell",
        "items": [{"op": "add", "name": "x"}],
    }
    kept, dropped = guard_delta(delta, CURRENT)
    assert kept == delta and dropped == []
    print("PASS legitimate changes are kept whole")


def test_world_step_wiring(pack, monkeypatch, caplog):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    caplog.set_level(logging.INFO, logger="app.game.beats.builtin")
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    location, cycle = gs.state.location, gs.state.cycle
    assert location != location.lower()
    ts = world_step_state(gs)
    reply = (
        '[NARRATIVE]\nThe bell is quiet.\n[/NARRATIVE]\n[STATE_DELTA]\n{"location": "'
        + location.lower()
        + '", "cycle": 9}\n[/STATE_DELTA]'
    )
    monkeypatch.setattr(fake, "SCRIPT", [(reply, "end_turn")])
    with bound("fake"):
        DMWorldStepBeat().run(ts)
    assert ts.gs.state.location == location
    assert ts.gs.state.cycle == cycle
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("state delta:")]
    assert lines == ["state delta: ignored rewrites of cycle,location"]
    print("PASS world step: a lowercased location and a cycle jump are ignored and logged by name")
