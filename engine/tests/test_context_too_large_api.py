import copy
from concurrent.futures import ThreadPoolExecutor


from conftest import NEW_BODY
from app.config import settings

BIG = " ".join(f"lamp{i}" for i in range(12_000))
DETAIL = {"detail": {"error": "context_too_large", "provider": None}}


def played(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    r = selfhost.post("/api/game/turn", json={"save": r.json()["save"], "player_input": "I wait"})
    assert r.status_code == 200, r.text
    return r.json()["save"]


def oversized(save):
    doc = copy.deepcopy(save)
    for state in [doc["state"], *(snap["state"] for snap in doc["undo_stack"])]:
        state["tutorial_stage"] = 2
        state["zz_archive"] = BIG
    return doc


def test_turn_over_ceiling_is_413_without_model_call(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    doc = oversized(save)
    calls.clear()
    r = selfhost.post(
        "/api/game/turn",
        json={
            "save": doc,
            "player_input": "I cast a spell on the mist",
        },
    )
    assert r.status_code == 413, r.text
    assert r.json() == DETAIL
    assert calls == []
    assert "save" not in r.json()
    checked = selfhost.post("/api/game/check", json={"save": doc})
    assert checked.status_code == 200, checked.text
    assert checked.json() == {"ok": True}
    print("PASS turn over ceiling: 413 context_too_large, no model call, save still checks")


def test_regenerate_over_ceiling_is_413_without_model_call(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    doc = oversized(save)
    calls.clear()
    r = selfhost.post("/api/game/regenerate", json={"save": doc})
    assert r.status_code == 413, r.text
    assert r.json() == DETAIL
    assert calls == []
    assert "save" not in r.json()
    print("PASS regenerate over ceiling: 413 context_too_large, no model call")


def test_welcome_regen_over_ceiling_is_413_without_model_call(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    doc = oversized(save)
    calls.clear()
    doc["history"][-1]["intervention"] = {
        "kind": "welcome_grace_doubt",
        "matched_text": "nothing happens",
        "original_input": "I wait",
    }
    r = selfhost.post("/api/game/welcome-intervention/regen", json={"save": doc})
    assert r.status_code == 413, r.text
    assert r.json() == DETAIL
    assert calls == []
    assert "save" not in r.json()
    print("PASS welcome regenerate over ceiling: 413 context_too_large, no model call")


def test_under_ceiling_turn_still_plays(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    calls.clear()
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait again"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "kind" not in body and isinstance(body.get("narrative"), str) and body["narrative"], body
    assert calls
    print("PASS under ceiling turn: narrative and model calls")


def test_context_too_large_threaded(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    doc = oversized(save)
    calls.clear()

    def turn(i):
        r = selfhost.post(
            "/api/game/turn",
            json={
                "save": save if i % 2 == 0 else doc,
                "player_input": f"I wait {i}",
            },
        )
        if i % 2 == 0:
            assert r.status_code == 200, r.text
            body = r.json()
            assert (
                "kind" not in body and isinstance(body.get("narrative"), str) and body["narrative"]
            ), body
        else:
            assert r.status_code == 413, r.text
            assert r.json() == DETAIL
            assert "save" not in r.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(turn, range(40)))
    print("PASS 40 threaded turns, 4 workers: 20 played, 20 refused 413 context_too_large")
