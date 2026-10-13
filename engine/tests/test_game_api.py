from conftest import NEW_BODY


def test_new_turns_undo_rewind(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    for i in range(3):
        r = selfhost.post(
            "/api/game/turn", json={"save": save, "player_input": f"I look around {i}"}
        )
        assert r.status_code == 200, r.text
        save = r.json()["save"]
    assert save["turn"] >= 3 and save["memory"]
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200 and r.json()["ok"]
    r = selfhost.post("/api/game/rewind", json={"save": r.json()["save"]})
    assert r.status_code == 200, r.text


def test_bad_save_and_bad_request(selfhost):
    r = selfhost.post("/api/game/undo", json={"save": {"profile": {"bogus": 1}}})
    assert r.status_code == 400 and r.json() == {"detail": {"error": "bad_save", "provider": None}}
    r = selfhost.post("/api/game/undo", json={"save": 5, "x": "echo-input-zz"})
    assert r.status_code == 422 and "echo-input-zz" not in r.text
    assert r.json()["detail"]["error"] == "bad_request"


def test_bad_pack_and_intervention(selfhost):
    r = selfhost.post("/api/game/new", json={"config": {"pack_id": "nopack"}})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_pack"
    r = selfhost.post("/api/game/welcome-intervention/regen", json={"save": {}})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "intervention_missing"


def test_packs_config_health(selfhost):
    assert selfhost.get("/api/packs").json()["packs"][0]["id"] == "testpack"
    cfg = selfhost.get("/api/config").json()
    assert cfg["mode"] == "selfhost" and cfg["tuned"] == "anthropic"
    assert selfhost.get("/api/health").status_code == 200


def test_read_only_and_trade(selfhost):
    r = selfhost.post("/api/game/undo", json={"save": {"read_only": True}})
    assert r.json()["kind"] == "read_only_gate" and "save" in r.json()
    r = selfhost.post(
        "/api/game/npc-trade",
        json={"save": {"read_only": True}, "npc_id": "n", "action": "give", "item_id": "i"},
    )
    assert r.json() == {"ok": False, "reason": "read_only"}


def test_world_type_defaults_to_canon(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["save"]["config"]["world_type"] == "canon"


def test_old_world_type_value_is_rejected(selfhost):
    for value in ("coherence", "default"):
        body = {**NEW_BODY, "config": {**NEW_BODY["config"], "world_type": value}}
        r = selfhost.post("/api/game/new", json=body)
        assert r.status_code == 422 and r.json()["detail"]["error"] == "bad_request"
