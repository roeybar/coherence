"""Undo and regenerate restore the items and the memory of the restored turn."""

import copy
import json

from app.providers import fake
from test_context_too_large_api import played

ADD = (
    "[NARRATIVE]\nYou twist the wire into a crude lockpick.\n[/NARRATIVE]\n[STATE_DELTA]\n"
    + json.dumps({"items": [{"op": "add", "name": "crude lockpick", "where": "player"}]})
    + "\n[/STATE_DELTA]"
)


def lockpicks(save):
    return [i for i in save["items"] if i.get("name") == "crude lockpick"]


def add_turn(selfhost, monkeypatch, save):
    monkeypatch.setattr(fake, "SCRIPT", [(ADD, "end_turn")])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
    assert r.status_code == 200, r.text
    assert fake.SCRIPT == []
    return r.json()["save"]


def test_undo_restores_items(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    save2 = add_turn(selfhost, monkeypatch, save)
    assert len(lockpicks(save2)) == 1
    assert save2["undo_stack"][-1]["items"] == save["items"]
    r = selfhost.post("/api/game/undo", json={"save": save2})
    assert r.status_code == 200, r.text
    undone = r.json()["save"]
    assert undone["items"] == save["items"]
    assert undone["turn"] == save["turn"]
    print("PASS undo restores the items held before the undone turn")


def test_regenerate_does_not_duplicate_items(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save2 = add_turn(selfhost, monkeypatch, played(selfhost))
    monkeypatch.setattr(fake, "SCRIPT", [(ADD, "end_turn")])
    r = selfhost.post("/api/game/regenerate", json={"save": save2})
    assert r.status_code == 200, r.text
    assert len(lockpicks(r.json()["save"])) == 1
    print("PASS regenerate restores the items first, so the regenerated add leaves one lockpick")


def test_undo_and_regenerate_drop_later_memory(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save2 = add_turn(selfhost, monkeypatch, played(selfhost))
    assert save2["turn"] == 2
    save2["memory"] = [{"turn": t, "role": "user", "text": f"note-{t}", "v": ""} for t in range(4)]
    r = selfhost.post("/api/game/undo", json={"save": copy.deepcopy(save2)})
    assert r.status_code == 200, r.text
    assert [e["text"] for e in r.json()["save"]["memory"]] == ["note-0", "note-1"]
    monkeypatch.setattr(fake, "SCRIPT", [(ADD, "end_turn")])
    r = selfhost.post("/api/game/regenerate", json={"save": copy.deepcopy(save2)})
    assert r.status_code == 200, r.text
    assert "note-3" not in [e.get("text") for e in r.json()["save"]["memory"]]
    print("PASS undo keeps memory up to the restored turn; regenerate drops the later notes")


def test_legacy_snapshot_leaves_items(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save2 = add_turn(selfhost, monkeypatch, played(selfhost))
    del save2["undo_stack"][-1]["items"]
    r = selfhost.post("/api/game/undo", json={"save": save2})
    assert r.status_code == 200, r.text
    assert len(lockpicks(r.json()["save"])) == 1
    print("PASS a snapshot without items leaves the items as they are on undo")
