"""A gate exception on /api/game/turn returns the input in save.state.pending_input."""

from unittest.mock import patch

from conftest import NEW_BODY

from app.game.dice_gate import DiceAssessment, DiceRollPending


def _new(client):
    r = client.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    return r.json()["save"]


def _gated_turn(client, save, text):
    gate = DiceRollPending(DiceAssessment(dc=13, skill="brawn", stat="STR", flavor="braced"))
    with patch("app.api.game.run_turn", side_effect=gate):
        return client.post("/api/game/turn", json={"save": save, "player_input": text})


def test_gate_stores_pending_input(selfhost):
    save = _new(selfhost)
    r = _gated_turn(selfhost, save, "I attack the guard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "dice_roll_needed"
    pi = body["save"]["state"]["pending_input"]
    assert pi["gate_kind"] == "dice_roll"
    assert pi["text"] == "I attack the guard"
    assert pi["at_turn"] == save["turn"]
    assert pi["created_at"]


def test_pending_input_survives_the_round_trip(selfhost):
    save = _gated_turn(selfhost, _new(selfhost), "I climb the wall").json()["save"]
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200, r.text
    assert r.json()["save"]["state"]["pending_input"]["text"] == "I climb the wall"


def test_new_save_has_no_pending_input(selfhost):
    assert not _new(selfhost)["state"].get("pending_input")
