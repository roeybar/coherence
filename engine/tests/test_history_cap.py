"""SAVE_HISTORY_CAP: unset means no cap; set, a save whose history (or a copy of it) is
over the cap, or a move that would take it past the cap, is refused as text_too_large
with scope history, before any model call."""

import copy
import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import NEW_BODY
from test_context_too_large_api import played
from app.api import game as game_api
from app.config import settings
from app.game import moderation

DETAIL = {"detail": {"error": "text_too_large", "provider": None, "scope": "history"}}
ROUTES = [
    "/api/game/check",
    "/api/game/turn",
    "/api/game/undo",
    "/api/game/undo-to-turn",
    "/api/game/rewind",
    "/api/game/meta",
    "/api/game/regenerate",
    "/api/game/welcome-intervention/regen",
    "/api/game/welcome-intervention/dismiss",
    "/api/game/npc-trade",
]


def _with_entries(save, n):
    s = copy.deepcopy(save)
    fill = [
        {"role": "user", "content_en": f"I look around the office, pass {i}."}
        if i % 2
        else {"role": "assistant", "content_en": f"The lamp buzzes over the desk, pass {i}."}
        for i in range(1, n)
    ]
    s["history"] = [s["history"][0], *fill] if n else []
    s["turn"] = (n - 1) // 2
    s["undo_stack"] = []
    return s


def _entries(n):
    return _with_entries({"history": [{"role": "assistant", "content_en": "The office."}]}, n)[
        "history"
    ]


def _counting(monkeypatch):
    seen = []
    monkeypatch.setattr(
        moderation,
        "check_input",
        lambda text, **kw: seen.append(1) or moderation.CheckResult(blocked=False),
    )
    return seen


def _cap(monkeypatch, n):
    monkeypatch.setattr(game_api, "SAVE_HISTORY_CAP", n)


def _body(path, save):
    extra = {
        "/api/game/turn": {"player_input": "I wait"},
        "/api/game/undo-to-turn": {"turn_index": 1},
        "/api/game/meta": {"question": "where am I"},
        "/api/game/npc-trade": {"npc_id": "x", "action": "give", "item_id": "y"},
    }
    return {"save": save, **extra.get(path, {})}


def _sized(save, extra_chars, cap):
    s = copy.deepcopy(save)
    s["state"]["zz"] = ""
    base = sum(len(x) for x in moderation.collect_strings(s, exempt=moderation.SAVE_EXEMPT))
    s["state"]["zz"] = "q" * (cap - base + extra_chars)
    return s


def test_cap_parsing():
    for raw in (None, "", "  "):
        assert game_api._history_cap(raw) is None
    assert game_api._history_cap("7") == 7
    for raw in ("0", "-1", "abc", "7.5"):
        with pytest.raises(ValueError):
            game_api._history_cap(raw)
    assert game_api.SAVE_HISTORY_CAP is None
    print("PASS cap parsing: unset and blank are no cap, bad values refuse to start")


def test_unset_cap_selfhost_unchanged(selfhost, monkeypatch, calls):
    save = played(selfhost)
    r = selfhost.post("/api/game/check", json={"save": _with_entries(save, 2001)})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    r = selfhost.post(
        "/api/game/turn", json={"save": _with_entries(save, 4001), "player_input": "I wait"}
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["save"]["history"]) == 4003
    print("PASS unset cap, selfhost: a 2001-entry save checks, a 4001-entry save plays")


def test_unset_cap_hosted_unchanged(hosted):
    save = hosted.post("/api/game/new", json=NEW_BODY).json()["save"]
    r = hosted.post("/api/game/check", json={"save": _with_entries(save, 4001)})
    assert r.status_code == 200, r.text
    r = hosted.post(
        "/api/game/check", json={"save": _sized(save, 1, game_api.HOSTED_SAVE_TEXT_CAP)}
    )
    assert r.status_code == 413
    assert r.json() == {"detail": {"error": "text_too_large", "provider": None}}
    print("PASS unset cap, hosted: a 4001-entry save checks, the text cap has no scope")


def test_import_boundary(hosted, selfhost, monkeypatch):
    base = played(selfhost)
    _cap(monkeypatch, 7)
    seen = _counting(monkeypatch)
    for client in (hosted, selfhost):
        r = client.post("/api/game/check", json={"save": _with_entries(base, 7)})
        assert r.status_code == 200, r.text
        seen.clear()
        r = client.post("/api/game/check", json={"save": _with_entries(base, 8)})
        assert r.status_code == 413 and r.json() == DETAIL
        assert seen == []
    print("PASS import boundary at cap 7: 7 checks, 8 is refused unread, hosted and selfhost")


def test_entry_cap_before_text_cap(hosted, monkeypatch):
    save = hosted.post("/api/game/new", json=NEW_BODY).json()["save"]
    _cap(monkeypatch, 7)
    s = _sized(_with_entries(save, 8), 1, game_api.HOSTED_SAVE_TEXT_CAP)
    r = hosted.post("/api/game/check", json={"save": s})
    assert r.status_code == 413 and r.json() == DETAIL
    print("PASS a save over both caps is refused with scope history")


def test_copies_count(selfhost, monkeypatch):
    save = _with_entries(played(selfhost), 3)
    _cap(monkeypatch, 7)
    for n, status in ((8, 413), (7, 200)):
        s = dict(save, checkpoint_history=_entries(n))
        r = selfhost.post("/api/game/check", json={"save": s})
        assert r.status_code == status, (n, r.text)
        s = dict(
            save,
            undo_stack=[{"state": copy.deepcopy(save["state"]), "turn": 1, "history": _entries(n)}],
        )
        r = selfhost.post("/api/game/check", json={"save": s})
        assert r.status_code == status, (n, r.text)
        if status == 413:
            assert r.json() == DETAIL
    print("PASS the checkpoint copy and an undo copy count against the cap")


def test_crossing_move(hosted, clock, monkeypatch, calls):
    save = hosted.post("/api/game/new", json=NEW_BODY).json()["save"]
    _cap(monkeypatch, 7)
    clock.t += 15
    r = hosted.post(
        "/api/game/turn", json={"save": _with_entries(save, 5), "player_input": "I wait"}
    )
    assert r.status_code == 200, r.text
    seven = r.json()["save"]
    assert len(seven["history"]) == 7
    calls.clear()
    clock.t += 15
    r = hosted.post("/api/game/turn", json={"save": seven, "player_input": "I wait"})
    assert r.status_code == 413 and r.json() == DETAIL
    assert calls == [] and "save" not in r.json()
    assert hosted.post("/api/game/check", json={"save": seven}).status_code == 200
    _cap(monkeypatch, 8)
    calls.clear()
    clock.t += 15
    r = hosted.post("/api/game/turn", json={"save": seven, "player_input": "I wait"})
    assert r.status_code == 413 and r.json() == DETAIL
    assert calls == []
    _cap(monkeypatch, 9)
    clock.t += 15
    r = hosted.post("/api/game/turn", json={"save": seven, "player_input": "I wait"})
    assert r.status_code == 200, r.text
    assert len(r.json()["save"]["history"]) == 9
    print("PASS the crossing move is refused before the model, at caps 7, 8; plays at 9")


def test_every_route_refuses_over_cap(selfhost, monkeypatch, calls):
    save = _with_entries(played(selfhost), 9)
    _cap(monkeypatch, 7)
    calls.clear()
    cases = [(p, _body(p, save)) for p in ROUTES]
    cases.append(("/api/game/npc-trade", _body("/api/game/npc-trade", dict(save, read_only=True))))
    for path, body in cases:
        r = selfhost.post(path, json=body)
        assert r.status_code == 413 and r.json() == DETAIL, (path, r.text)
    assert calls == []
    print("PASS every route refuses a 9-entry save at cap 7 with no model call n=%d" % len(cases))


def test_non_growing_routes_play_at_cap(selfhost, monkeypatch, calls):
    save = played(selfhost)
    for _ in range(2):
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
    assert len(save["history"]) == 7
    _cap(monkeypatch, 7)
    r = selfhost.post("/api/game/regenerate", json={"save": save})
    assert r.status_code == 200, r.text
    assert len(r.json()["save"]["history"]) == 7
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200, r.text
    assert len(r.json()["save"]["history"]) == 5
    calls.clear()
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
    assert r.status_code == 413 and r.json() == DETAIL
    assert calls == []
    print("PASS at the cap: regenerate and undo play, a move is refused")


def test_stress_boundary_threaded(selfhost, monkeypatch, calls):
    base = played(selfhost)
    _cap(monkeypatch, 7)
    rng = random.Random(7)
    cases = []
    for _ in range(160):
        h, c, u = (rng.randint(0, 12) for _ in range(3))
        s = _with_entries(base, h)
        s["checkpoint_history"] = _entries(c)
        s["undo_stack"] = [
            {"state": copy.deepcopy(base["state"]), "turn": 0, "history": _entries(u)}
        ]
        cases.append((s, 413 if max(h, c, u) > 7 else 200))

    def check(case):
        s, want = case
        r = selfhost.post("/api/game/check", json={"save": s})
        assert r.status_code == want, (want, r.text)
        return want

    def turn(case):
        s, want = case
        r = selfhost.post("/api/game/turn", json={"save": s, "player_input": "I wait"})
        assert r.status_code == want, (want, r.text)
        return want

    with ThreadPoolExecutor(4) as pool:  # max_concurrent_requests is 4
        got = list(pool.map(check, cases))
        calls.clear()
        refused = list(pool.map(turn, [(_with_entries(base, 7), 413)] * 12))
        assert calls == []
        played_ok = list(
            pool.map(turn, [(_with_entries(base, rng.choice((1, 3, 5))), 200) for _ in range(12)])
        )
    print(
        "PASS threaded boundary: %d checks (%d refused), %d refused moves, %d played"
        % (len(got), got.count(413), len(refused), len(played_ok))
    )


def test_volume_and_odd_shapes(selfhost, monkeypatch):
    base = played(selfhost)
    _cap(monkeypatch, 7)
    seen = _counting(monkeypatch)
    r = selfhost.post("/api/game/check", json={"save": _with_entries(base, 100_000)})
    assert r.status_code == 413 and r.json() == DETAIL
    assert seen == []
    shapes = [dict(base, history={"a": 1}), dict(base, history=list(range(9)))]
    for s in shapes:
        capped = selfhost.post("/api/game/check", json={"save": s}).status_code
        _cap(monkeypatch, None)
        uncapped = selfhost.post("/api/game/check", json={"save": s}).status_code
        _cap(monkeypatch, 7)
        assert capped == uncapped, (capped, uncapped)
    print("PASS a %d-entry save is refused unread; odd shapes answer as without a cap" % 100_000)


def test_regen_precheck(selfhost, monkeypatch, calls):
    base = played(selfhost)
    organic = base
    for _ in range(2):
        r = selfhost.post("/api/game/turn", json={"save": organic, "player_input": "I wait"})
        assert r.status_code == 200, r.text
        organic = r.json()["save"]
    assert len(organic["history"]) == 7
    save = _with_entries(base, 7)
    state = copy.deepcopy(save["state"])
    _cap(monkeypatch, 7)
    calls.clear()
    s = dict(save, undo_stack=[{"state": state, "turn": 3, "history_len": 7}])
    for path in ("/api/game/regenerate", "/api/game/welcome-intervention/regen"):
        r = selfhost.post(path, json={"save": s})
        assert r.status_code == 413 and r.json() == DETAIL, (path, r.text)
    s = dict(save, undo_stack=[{"state": state, "turn": 3, "history": _entries(7)}])
    r = selfhost.post("/api/game/regenerate", json={"save": s})
    assert r.status_code == 413 and r.json() == DETAIL
    s = dict(save, undo_stack=[], previous_state=state, previous_history=_entries(7))
    r = selfhost.post("/api/game/regenerate", json={"save": s})
    assert r.status_code == 413 and r.json() == DETAIL
    assert calls == []
    assert game_api._regen_growth(None) == 0
    assert game_api._regen_growth(dict(save, undo_stack=[])) == 0
    assert game_api._regen_growth(organic) == 0
    print("PASS a regenerate that would pass the cap is refused before the model")


def test_previous_history_counts(selfhost, monkeypatch):
    save = _with_entries(played(selfhost), 5)
    state = copy.deepcopy(save["state"])
    _cap(monkeypatch, 7)
    s = dict(save, previous_state=state, previous_history=_entries(8))
    for path in ("/api/game/check", "/api/game/undo"):
        r = selfhost.post(path, json={"save": s})
        assert r.status_code == 413 and r.json() == DETAIL, (path, r.text)
    s = dict(save, previous_state=state, previous_history=_entries(7))
    assert selfhost.post("/api/game/check", json={"save": s}).status_code == 200
    print("PASS the legacy previous_history counts against the cap")


def test_backstop(selfhost, monkeypatch):
    save = _with_entries(played(selfhost), 5)

    def padded(gs):
        gs.history = gs.history + _entries(8)[len(gs.history) :]
        return {"ok": True}

    monkeypatch.setattr(game_api, "run_undo", padded)
    _cap(monkeypatch, 7)
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 413 and r.json() == DETAIL
    assert "save" not in r.json()
    _cap(monkeypatch, None)
    r = selfhost.post("/api/game/undo", json={"save": save})
    assert r.status_code == 200, r.text
    print("PASS the backstop refuses a route that left the history over the cap")
