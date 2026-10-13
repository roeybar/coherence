"""A flagged turn turns the returned doc into the shadow fork; read-only docs are gated."""

from unittest.mock import patch

from conftest import NEW_BODY

_FLAGGED = {
    "narrative": "The street goes quiet.",
    "narrative_en": "The street goes quiet.",
    "flagged": True,
}


def _new(client):
    r = client.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    return r.json()["save"]


def _fake_turn(gs, *args, **kwargs):
    gs.turn += 1
    return dict(_FLAGGED)


def _flagged_turn(client, save):
    with patch("app.api.game.run_turn", side_effect=_fake_turn):
        r = client.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
    assert r.status_code == 200, r.text
    return r.json()["save"]


def test_flagged_turn_forks_and_carries_memory(selfhost):
    save = _new(selfhost)
    before = list(save["memory"])
    assert before
    forked = _flagged_turn(selfhost, save)
    assert forked["shadow_of"] == "local"
    assert forked["read_only"] is False
    assert len(forked["memory"]) == len(before) + 2
    for entry in before:
        assert entry in forked["memory"]


def test_second_flagged_turn_does_not_fork_again(selfhost):
    forked = _flagged_turn(selfhost, _new(selfhost))
    again = _flagged_turn(selfhost, forked)
    assert again["shadow_of"] == "local"
    assert len(again["memory"]) == len(forked["memory"])


def test_unflagged_turn_does_not_fork(selfhost):
    with patch("app.api.game.run_turn", return_value={**_FLAGGED, "flagged": False}):
        r = selfhost.post("/api/game/turn", json={"save": _new(selfhost), "player_input": "x"})
    assert r.json()["save"]["shadow_of"] is None


def test_read_only_doc_gets_gate_response(selfhost):
    save = _new(selfhost)
    save["read_only"] = True
    with patch("app.api.game.run_turn") as run:
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert not run.called
    assert body["kind"] == "read_only_gate"
    assert body["delta"] is None and body["diff"] == {}
    assert body["narrative"] == body["narrative_en"] and body["narrative"]
    assert body["save"]["read_only"] is True
