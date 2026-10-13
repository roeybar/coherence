"""POST /api/game/npc-trade on a stateless doc with a seeded item."""

from conftest import NEW_BODY
from fastapi.encoders import jsonable_encoder

from app import store
from app.game import items


def _seeded(client):
    r = client.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    tok = store.bind_save(save)
    try:
        item = items.add("local", "brass key", "player")
    finally:
        store.unbind_save(tok)
    state = save["state"]
    state.setdefault("npcs", {})["guard-guard"] = {
        "npc_id": "guard-guard",
        "name": "Guard",
        "role": "guard",
        "persona": "stoic",
        "location": state.get("location", ""),
        "witnessed": [],
        "disposition": 30,
        "first_seen_turn": save.get("turn", 0),
        "last_seen_turn": save.get("turn", 0),
        "color_hue": 35,
        "reveal_state": {},
        "bound_status": "",
    }
    return jsonable_encoder(save), item["id"]


def _trade(client, save, item_id, action="give", npc_id="guard-guard"):
    return client.post(
        "/api/game/npc-trade",
        json={"save": save, "npc_id": npc_id, "action": action, "item_id": item_id},
    )


def test_give_moves_seeded_item_to_npc(selfhost):
    save, item_id = _seeded(selfhost)
    r = _trade(selfhost, save, item_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["reason"] == "ok"
    assert body["item"]["holder"].startswith("npc:")
    held = [i for i in body["save"]["items"] if str(i.get("_id")) == item_id]
    assert held and held[0]["holder"] == body["item"]["holder"]


def test_unknown_npc_and_unknown_item(selfhost):
    save, item_id = _seeded(selfhost)
    assert _trade(selfhost, save, item_id, npc_id="nobody").json()["reason"] == "npc_not_found"
    assert _trade(selfhost, save, "no-such-item").json()["reason"] == "item_not_found"


def test_take_of_player_item_is_refused(selfhost):
    save, item_id = _seeded(selfhost)
    body = _trade(selfhost, save, item_id, action="take").json()
    assert body["ok"] is False and body["reason"] == "not_npc_held"


def test_read_only_doc_is_refused(selfhost):
    save, item_id = _seeded(selfhost)
    save["read_only"] = True
    assert _trade(selfhost, save, item_id).json() == {"ok": False, "reason": "read_only"}
