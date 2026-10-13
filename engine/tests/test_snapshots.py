"""Length references, legacy copies and checkpoint preservation."""

import copy
import json

import pytest

from app.game import engine
from app.game.dice_gate import DiceRollPending
from app.game.state import GameConfig, GameSave, WorldState, doc_to_save, save_to_doc
from conftest import NEW_BODY
from test_strong_gate import attempt, make_save, turn_bindings


def history(turns):
    rows = [{"role": "assistant", "content": "An open courtyard.", "turn": 0}]
    for turn in range(1, turns + 1):
        rows.extend(
            [
                {"role": "user", "content": f"I wait t{turn}.", "turn": turn},
                {"role": "assistant", "content": f"A bell sounds t{turn}.", "turn": turn},
            ]
        )
    return rows


def make_stack(form="new"):
    gs = GameSave(
        config=GameConfig(pack_id="testpack"),
        state=WorldState(location="courtyard", marker=3),
        history=history(3),
        turn=3,
    )
    for turn in range(3):
        snap = {"state": {"location": "courtyard", "marker": turn}, "turn": turn}
        if form == "old" or (form == "mixed" and turn % 2 == 0):
            snap["history"] = copy.deepcopy(gs.history[: 2 * turn + 1])
        else:
            snap["history_len"] = 2 * turn + 1
        gs.undo_stack.append(snap)
    gs.checkpoint_state = {"location": "courtyard", "marker": 2}
    gs.checkpoint_turn = 2
    if form == "old":
        gs.checkpoint_history = copy.deepcopy(gs.history[:5])
    else:
        gs.checkpoint_len = 5
    return gs


@pytest.fixture
def restore_rig(monkeypatch):
    monkeypatch.setattr(engine, "_moderate", lambda *args, **kw: None)
    monkeypatch.setattr(engine, "rewind_debrief", lambda *args, **kw: ("A bell faded.", None))


@pytest.mark.parametrize("form", ["old", "new", "mixed"])
@pytest.mark.parametrize("route", ["undo", "undo_to_turn", "regenerate", "rewind"])
def test_restore_routes(restore_rig, monkeypatch, form, route):
    gs = make_stack(form)
    expected_turn = 1 if route == "undo_to_turn" else 2
    expected_history = copy.deepcopy(gs.history[: 2 * expected_turn + 1])
    if route == "regenerate":

        def replay(save, player_input, save_id, **kw):
            assert save.state.marker == 2
            assert save.history == expected_history
            assert kw["regenerated"] and kw["original_input"] == "I wait t3."
            return {"ok": True}

        monkeypatch.setattr(engine, "run_turn", replay)
        out = engine.run_regenerate(gs, "local")
    elif route == "undo_to_turn":
        out = engine.run_undo_to_turn(gs, 2)
    else:
        out = getattr(engine, f"run_{route}")(gs)
    assert out["ok"]
    assert gs.turn == expected_turn
    assert gs.state.marker == expected_turn
    assert gs.history == expected_history
    if route == "rewind":
        assert gs.checkpoint_len is None
        assert gs.checkpoint_state == {} and gs.checkpoint_history == []
        assert gs.checkpoint_turn == 0


@pytest.mark.parametrize("route", ["undo", "undo_to_turn", "regenerate"])
def test_undo_below_checkpoint(restore_rig, monkeypatch, route):
    gs = make_stack()
    checkpoint_history = copy.deepcopy(gs.history)
    gs.checkpoint_len = len(gs.history)
    gs.checkpoint_state = gs.state.to_dict()
    gs.checkpoint_turn = gs.turn
    if route == "regenerate":
        monkeypatch.setattr(engine, "run_turn", lambda *args, **kw: {"ok": True})
        engine.run_regenerate(gs, "local")
    elif route == "undo_to_turn":
        engine.run_undo_to_turn(gs, 2)
    else:
        engine.run_undo(gs)
    assert gs.checkpoint_len is None
    assert gs.checkpoint_history == checkpoint_history
    assert engine.run_rewind(gs)["ok"]
    assert gs.history == checkpoint_history and gs.state.marker == 3
    assert doc_to_save(save_to_doc(gs)).history == checkpoint_history


@pytest.mark.parametrize("route", ["undo", "undo_to_turn", "regenerate"])
@pytest.mark.parametrize("restored_len", [0, 3, 9])
def test_full_copy_restore_materialises_current_checkpoint(
    restore_rig, monkeypatch, route, restored_len
):
    gs = make_stack()
    current = [{"role": "assistant", "content": "A compressed summary."}] + history(3)[-4:]
    gs.history = current
    gs.checkpoint_len = 3
    gs.checkpoint_history = [{"role": "assistant", "content": "An obsolete checkpoint."}]
    restored = history(4)[:restored_len]
    gs.undo_stack = [
        {
            "state": {"location": "courtyard", "marker": 2},
            "history": restored,
            "history_len": -1,
            "turn": 2,
        }
    ]
    if route == "regenerate":
        monkeypatch.setattr(engine, "run_turn", lambda *args, **kw: {"ok": True})
        out = engine.run_regenerate(gs, "local")
    elif route == "undo_to_turn":
        out = engine.run_undo_to_turn(gs, 3)
    else:
        out = engine.run_undo(gs)
    assert out["ok"] and gs.history == restored
    assert gs.checkpoint_len is None
    assert gs.checkpoint_history == current[:3]
    assert engine.run_rewind(gs)["ok"]
    assert gs.history == current[:3]


def test_materialise_is_copy_on_write_and_idempotent():
    gs = make_stack("mixed")
    gs.undo_stack[0]["history"] = []
    gs.undo_stack[0]["history_len"] = -1
    originals = list(gs.undo_stack)
    before = copy.deepcopy(originals)
    expected = [
        copy.deepcopy(s["history"])
        if isinstance(s.get("history"), list)
        else gs.history[: s["history_len"]]
        for s in originals
    ]
    engine._materialise(gs)
    once = copy.deepcopy(save_to_doc(gs))
    assert originals == before
    for snap, old in zip(gs.undo_stack, originals):
        if isinstance(old.get("history"), list):
            assert snap is old
        else:
            assert snap is not old and "history_len" not in snap
    assert [s["history"] for s in gs.undo_stack] == expected
    assert gs.undo_stack[0]["history_len"] == -1
    assert gs.checkpoint_len is None and gs.checkpoint_history == gs.history[:5]
    engine._materialise(gs)
    assert save_to_doc(gs) == once
    assert save_to_doc(doc_to_save(once)) == once


@pytest.mark.parametrize("entry", [None, 3, [], "legacy", {}, {"state": {}, "history": None}])
def test_non_length_entries_are_preserved(entry):
    doc = save_to_doc(make_stack())
    doc["undo_stack"].insert(0, entry)
    gs = doc_to_save(doc)
    before = copy.deepcopy(entry)
    engine._materialise(gs)
    assert gs.undo_stack[0] is entry
    assert entry == before
    assert "history" in gs.undo_stack[-1] and "history_len" not in gs.undo_stack[-1]


@pytest.mark.parametrize("n", [-1, True, False, 8, 1.5, "3", None])
def test_bad_snapshot_length(n):
    doc = save_to_doc(make_stack())
    doc["undo_stack"][-1]["history_len"] = n
    with pytest.raises(ValueError, match="history_len"):
        doc_to_save(doc)


@pytest.mark.parametrize("n", [-1, True, False, 8, 1.5, "3"])
def test_bad_checkpoint_length(n):
    doc = save_to_doc(make_stack())
    doc["checkpoint_len"] = n
    with pytest.raises(ValueError, match="checkpoint_len"):
        doc_to_save(doc)


@pytest.mark.parametrize("n", [-1, True, 8, "ignored"])
def test_full_history_wins_over_invalid_length(n):
    doc = save_to_doc(make_stack("old"))
    for snap in doc["undo_stack"]:
        snap["history_len"] = n
    doc["undo_stack"][-1]["history"] = []
    gs = doc_to_save(doc)
    assert engine.run_undo(gs)["ok"] and gs.history == []


@pytest.mark.parametrize("n", [0, 7])
def test_length_boundaries_round_trip(n):
    doc = save_to_doc(make_stack())
    doc["undo_stack"][-1]["history_len"] = n
    doc["checkpoint_len"] = n
    gs = doc_to_save(doc)
    assert save_to_doc(gs) == doc


def test_legacy_checkpoint_omits_length_and_migration_stays_full_copy():
    doc = save_to_doc(make_stack("old"))
    assert "checkpoint_len" not in doc
    doc["undo_stack"] = []
    doc["previous_state"] = {"location": "courtyard", "marker": 2}
    doc["previous_history"] = history(2)
    gs = doc_to_save(doc)
    assert gs.undo_stack == [
        {"state": doc["previous_state"], "history": doc["previous_history"], "turn": 2}
    ]


def test_lost_checkpoint_length_refuses_without_mutation(restore_rig):
    gs = make_stack()
    gs.checkpoint_len = None
    before = copy.deepcopy(save_to_doc(gs))
    assert engine.run_rewind(gs) == {"ok": False, "reason": "no rewind point"}
    assert save_to_doc(gs) == before


def test_zero_checkpoint_length_is_a_rewind_point(restore_rig):
    gs = make_stack()
    gs.checkpoint_len = 0
    assert engine.run_rewind(gs)["ok"]
    assert gs.history == [] and gs.checkpoint_len is None


def test_snapshot_size_is_independent_of_history_text():
    gs = make_stack()
    first = engine._snapshot(gs)
    gs.history = [{"role": "assistant", "content": "x" * 1000000}] * len(gs.history)
    second = engine._snapshot(gs)
    assert set(first) == {"state", "history_len", "turn"}
    assert first == second
    assert len(json.dumps(first)) == len(json.dumps(second))
    gs.history += gs.history * 100
    large = engine._snapshot(gs)
    assert len(json.dumps(large)) - len(json.dumps(first)) <= 3


@pytest.mark.parametrize(
    "field, n, invalid_history",
    [
        ("history_len", -1, None),
        ("checkpoint_len", -1, None),
        ("history_len", True, None),
        ("checkpoint_len", True, None),
        ("history_len", 8, None),
        ("checkpoint_len", 8, None),
        ("history_len", 5, {str(turn): row for turn, row in enumerate(history(3))}),
        ("history_len", 5, "invalid"),
    ],
)
def test_bad_lengths_return_400(selfhost, field, n, invalid_history):
    doc = save_to_doc(make_stack())
    if invalid_history is not None:
        doc["history"] = invalid_history
    if field == "history_len":
        doc["undo_stack"][-1][field] = n
    else:
        doc[field] = n
    r = selfhost.post("/api/game/undo", json={"save": doc})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "bad_save"


def test_api_round_trips_twice_after_compression(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    doc = r.json()["save"]
    shrinks = 0
    for turn in range(1, 18):
        prev = len(doc["history"])
        doc["checkpoint_state"] = copy.deepcopy(doc["state"])
        doc["checkpoint_turn"] = doc["turn"]
        doc["checkpoint_history"] = []
        doc["checkpoint_len"] = prev
        r = selfhost.post(
            "/api/game/turn", json={"save": doc, "player_input": f"I wait c{turn:02d}"}
        )
        assert r.status_code == 200, r.text
        doc = r.json()["save"]
        if len(doc["history"]) < prev:
            shrinks += 1
            assert doc.get("checkpoint_len") is None
            assert len(doc["checkpoint_history"]) == prev
            for _ in range(2):
                r = selfhost.post(
                    "/api/game/undo-to-turn", json={"save": doc, "turn_index": doc["turn"] + 1}
                )
                assert r.status_code == 200, r.text
                doc = r.json()["save"]
    assert shrinks == 2


def test_api_response_drops_cleared_checkpoint_len(selfhost, monkeypatch):
    monkeypatch.setattr(engine, "rewind_debrief", lambda *args, **kw: ("A bell faded.", None))
    doc = save_to_doc(make_stack("new"))
    r = selfhost.post("/api/game/undo", json={"save": doc})
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    assert save["checkpoint_len"] == 5
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    assert "checkpoint_len" not in save
    assert save["checkpoint_history"] == doc["history"][:5]
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200, r.text

    r = selfhost.post("/api/game/rewind", json={"save": doc})
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    assert "checkpoint_len" not in save
    assert save["history"] == doc["history"][:5]
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("form", ["old", "new", "mixed"])
def test_materialised_fork_through_api(selfhost, monkeypatch, form):
    gs = make_stack(form)
    doc = copy.deepcopy(save_to_doc(gs))
    snap = doc["undo_stack"][1]
    fork_history = (
        snap["history"]
        if isinstance(snap.get("history"), list)
        else doc["history"][: snap["history_len"]]
    )
    fork = {
        **doc,
        "name": doc["name"] + " (fork)",
        "state": snap["state"],
        "history": fork_history,
        "turn": snap["turn"],
        "undo_stack": doc["undo_stack"][:1],
    }
    if doc.get("checkpoint_len") is not None:
        fork["checkpoint_history"] = doc["history"][: doc["checkpoint_len"]]
        fork.pop("checkpoint_len")
    monkeypatch.setattr(engine, "rewind_debrief", lambda *args, **kw: ("A bell faded.", None))
    r = selfhost.post("/api/game/rewind", json={"save": fork})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["save"]["history"] == doc["history"][:5]
    assert out["save"]["state"] == doc["checkpoint_state"]
    assert out["save"].get("checkpoint_len") is None


@pytest.mark.parametrize("compress", [False, True])
def test_checkpoint_writing_ability_dice_gate_rolls_back(gate_rig, compress):
    gate_rig.turn_class = "ability"
    gate_rig.checkpoint = True
    gate_rig.post_dice = True
    gs, doc = make_save("never"), {}
    if compress:
        gs.history = history(11)
        gs.turn = 11
    gs.checkpoint_state = {"location": "An earlier courtyard."}
    gs.checkpoint_turn = 0
    gs.checkpoint_len = 1
    gs.undo_stack = [{"state": copy.deepcopy(gs.checkpoint_state), "history_len": 1, "turn": 0}]
    originals = list(gs.undo_stack)
    original_values = copy.deepcopy(originals)
    baseline = copy.deepcopy(gs)
    before = copy.deepcopy(save_to_doc(gs))
    with pytest.raises(DiceRollPending):
        attempt(gs, gate_rig, doc)
    assert save_to_doc(gs) == before
    assert gs.checkpoint_len == 1
    assert gs.checkpoint_state == before["checkpoint_state"]
    assert gs.checkpoint_turn == before["checkpoint_turn"]
    assert originals == original_values
    assert gs.undo_stack[0] is originals[0]
    assert doc["physics_cache"] == []
    attempt(gs, gate_rig, doc, dice_roll=20)
    attempt(baseline, gate_rig, {}, dice_roll=20)
    with turn_bindings(gate_rig, doc):
        assert engine.run_rewind(gs)["ok"]
    with turn_bindings(gate_rig, {}):
        assert engine.run_rewind(baseline)["ok"]
    assert gs.history == baseline.history
    assert gs.state.to_dict() == baseline.state.to_dict()
    assert gs.turn == baseline.turn and gs.checkpoint_len is None
