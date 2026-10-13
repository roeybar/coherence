"""Every save the engine receives is checked before anything runs: all of its
text, the parsed world premise and the request's own fields, under a text cap,
with typed display fields."""

import copy
import json
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import NEW_BODY, turn_body
from app import ratelimit
from app.api import game as game_api
from app.game import lore, moderation
from app.providers import fake

PACK = "zzpackcanary"
ALWAYS = "zzalwayscanary"
BS = chr(92)
CAP = 1_000_000  # the self-host text cap before it was removed
HEXID = "".join(ch * n for ch, n in zip("b3dfacad3caf3", [3, 3, 3, 2, 2, 2, 2, 2, 3, 2, 2, 2, 4]))


@pytest.fixture(autouse=True)
def _fresh_lists():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


def _calls() -> int:
    return len(fake.seen_key_hashes)


def _played(client) -> dict:
    r = client.post("/api/game/turn", json=turn_body(client))
    assert r.status_code == 200, r.text
    return r.json()["save"]


def _assistant(save) -> dict:
    return next(e for e in reversed(save["history"]) if e.get("role") == "assistant")


def _expect(r, before, status=451, error="save_blocked"):
    assert r.status_code == status, r.text
    assert r.json()["detail"]["error"] == error
    assert _calls() == before


def _placements(save):
    def put(fn):
        s = copy.deepcopy(save)
        fn(s)
        return s

    def wp(v):
        return lambda s: s["config"].__setitem__("world_premise", '{"premise": "' + v + '"}')

    esc = "".join(BS + "u%04x" % ord(c) for c in PACK)
    return {
        "history_user": put(lambda s: s["history"][0].__setitem__("content", PACK)),
        "history_assistant": put(lambda s: _assistant(s).__setitem__("content_en", PACK)),
        "premise": put(wp(PACK)),
        "premise_escaped": put(wp(esc)),
        "premise_leet": put(wp("zzp4ckc4n4ry")),
        "undo_stack": put(
            lambda s: s.__setitem__(
                "undo_stack", [{"history": [{"role": "user", "content": PACK}]}]
            )
        ),
        "checkpoint": put(
            lambda s: s.__setitem__("checkpoint_history", [{"role": "user", "content": PACK}])
        ),
        "npc_name": put(lambda s: s["state"].__setitem__("npcs", {"n1": {"name": PACK}})),
        "npc_key": put(lambda s: s["state"].__setitem__("npcs", {PACK: {"name": "x"}})),
        "items": put(lambda s: s.__setitem__("items", [{"name": PACK}])),
        "memory": put(lambda s: s.__setitem__("memory", [{"t": PACK}])),
        "appearance": put(lambda s: s["profile"].__setitem__("appearance", PACK)),
        "seed_roster": put(lambda s: s.__setitem__("seed_roster", [{"backstory": PACK}])),
        "inventory_join": put(lambda s: s["state"].__setitem__("inventory", list(PACK))),
        "counter": put(lambda s: s["state"].__setitem__("some_counter", 48053716)),
        "id_history": put(lambda s: s["history"][0].__setitem__("content", HEXID)),
        "id_npc_key": put(lambda s: s["state"].__setitem__("npcs", {HEXID: {"name": "x"}})),
        "id_physics": put(lambda s: s.__setitem__("physics_cache", [{"narrative_hint": HEXID}])),
    }


def test_blocked_text_anywhere_in_a_save(selfhost):
    save = _played(selfhost)
    cases = _placements(save)
    for name, s in cases.items():
        before = _calls()
        _expect(selfhost.post("/api/game/check", json={"save": s}), before)

    def long_id(n):
        return HEXID[:-4] + "3" * (n - len(HEXID) + 4)

    for n, status in ((1024, 200), (1023, 451)):
        s = copy.deepcopy(save)
        s["memory"] = [{"v": long_id(n)}]
        assert len(long_id(n)) == n
        assert selfhost.post("/api/game/check", json={"save": s}).status_code == status, n
    for fn in (
        lambda s: s["history"][0].__setitem__("content_en", 48053716),
        lambda s: s.__setitem__("name", 5),
    ):
        s = copy.deepcopy(save)
        fn(s)
        _expect(selfhost.post("/api/game/check", json={"save": s}), _calls(), 400, "bad_save")
    print("PASS save_placements n=%d" % len(cases))


def test_every_route_checks_the_save(selfhost):
    save = _played(selfhost)
    _assistant(save)["content_en"] = "The clerk says " + PACK
    ro = dict(save, read_only=True)
    trade = {"npc_id": "n1", "action": "give", "item_id": "i1"}
    cases = [
        ("/api/game/turn", {"save": save, "player_input": "I wait"}),
        ("/api/game/undo", {"save": save}),
        ("/api/game/undo-to-turn", {"save": save, "turn_index": 0}),
        ("/api/game/rewind", {"save": save}),
        ("/api/game/meta", {"save": save, "question": "where am I"}),
        ("/api/game/regenerate", {"save": save}),
        ("/api/game/welcome-intervention/regen", {"save": save}),
        ("/api/game/welcome-intervention/dismiss", {"save": save}),
        ("/api/game/npc-trade", {"save": save, **trade}),
        ("/api/game/npc-trade", {"save": ro, **trade}),
        ("/api/game/check", {"save": save}),
    ]
    for path, body in cases:
        before = _calls()
        _expect(selfhost.post(path, json=body), before)
    clean = _played(selfhost)
    for path, body in [
        ("/api/game/turn", {"save": clean, "player_input": "I wait", "addressee": {"name": PACK}}),
        ("/api/game/turn", {"save": clean, "player_input": "I wait", "consent_token": PACK}),
        ("/api/game/npc-trade", {"save": clean, **trade, "item_id": PACK}),
    ]:
        before = _calls()
        _expect(selfhost.post(path, json=body), before, error="content_blocked")
    print("PASS save_routes n=%d request_fields=3" % len(cases))


def _counting(monkeypatch):
    seen = []
    monkeypatch.setattr(
        moderation,
        "check_input",
        lambda text, **kw: seen.append(1) or moderation.CheckResult(blocked=False),
    )
    return seen


def _sized(save, extra_chars, cap=CAP):
    s = copy.deepcopy(save)
    s["state"]["zz"] = ""
    base = sum(len(x) for x in moderation.collect_strings(s, exempt=moderation.SAVE_EXEMPT))
    s["state"]["zz"] = "q" * (cap - base + extra_chars)
    return s


def test_selfhost_has_no_text_cap(selfhost, monkeypatch):
    assert game_api._text_cap(types.SimpleNamespace(app=selfhost.app)) is None
    save = _played(selfhost)
    real = moderation.check_input
    seen = _counting(monkeypatch)
    r = selfhost.post("/api/game/check", json={"save": _sized(save, 1)})
    assert r.status_code == 200 and seen
    seen.clear()
    body = {**NEW_BODY, "config": {**NEW_BODY["config"], "world_hint_form": {"k": "q" * 1_000_001}}}
    assert selfhost.post("/api/game/new", json=body).status_code == 200 and seen
    monkeypatch.setattr(moderation, "check_input", real)
    before = _calls()
    body = {**NEW_BODY, "config": {**NEW_BODY["config"], "world_hint_form": {"k": ALWAYS}}}
    _expect(selfhost.post("/api/game/new", json=body), before, 451, "content_blocked")
    print("PASS selfhost_no_text_cap")


def test_hosted_creates_are_limited(hosted, clock, monkeypatch):
    # a canon creation needs a key but makes no model call, so it is not a
    # message: starts back to back all succeed until the action limit is full
    limit = ratelimit.actions.per_minute
    assert limit >= 2
    for _ in range(limit):
        assert hosted.post("/api/game/new", json=NEW_BODY).status_code == 200
    seen = _counting(monkeypatch)
    r = hosted.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "actions_rate_limited"
    assert not seen
    clock.t += ratelimit.ActionLimiter.WINDOW
    assert hosted.post("/api/game/new", json=NEW_BODY).status_code == 200
    print("PASS hosted_creates n=%d" % (limit + 2))


def test_hosted_text_cap_is_lower(hosted, monkeypatch):
    save = hosted.post("/api/game/new", json=NEW_BODY).json()["save"]
    cap = game_api.HOSTED_SAVE_TEXT_CAP
    assert cap < CAP
    seen = _counting(monkeypatch)
    r = hosted.post("/api/game/check", json={"save": _sized(save, 1, cap)})
    assert r.status_code == 413 and r.json()["detail"]["error"] == "text_too_large" and not seen
    assert (
        hosted.post("/api/game/check", json={"save": _sized(save, 0, cap)}).status_code == 200
        and seen
    )
    print("PASS hosted_cap cap=%d" % cap)


def test_missing_lists_are_500(selfhost, monkeypatch, tmp_path):
    save = _played(selfhost)

    def boom(*a, **kw):
        raise moderation.ListsUnavailable("x")

    orig = moderation._load_rules
    monkeypatch.setattr(moderation, "_load_rules", boom)
    trade = {"npc_id": "n1", "action": "give", "item_id": "i1"}
    for path, body in [
        ("/api/game/check", {"save": save}),
        ("/api/game/turn", {"save": save, "player_input": "I wait"}),
        ("/api/game/new", NEW_BODY),
        ("/api/game/npc-trade", {"save": dict(save, read_only=True), **trade}),
        ("/api/game/undo", {"save": save}),
    ]:
        r = selfhost.post(path, json=body)
        assert r.status_code == 500 and r.json()["detail"]["error"] == "internal", path
    monkeypatch.setattr(moderation, "_load_rules", orig)
    monkeypatch.setattr(lore, "LORE_ROOT", tmp_path)
    c = TestClient(selfhost.app, raise_server_exceptions=False)
    c.headers.update(selfhost.headers)
    moderation.reset_pack_cache()
    r = c.post("/api/game/check", json={"save": save})
    assert r.status_code == 500 and r.json()["detail"]["error"] == "internal"
    print("PASS envelope n=6")


def test_clean_saves_pass(selfhost):
    save = _played(selfhost)
    save["state"]["ids"] = [
        str(uuid.uuid4()),
        uuid.uuid4().hex,
        datetime.now(timezone.utc).isoformat(),
    ]
    assert selfhost.post("/api/game/check", json={"save": save}).status_code == 200
    root = Path(__file__).resolve().parents[2] / "examples" / "demo-save.json"
    path = root if root.is_file() else Path("/app/examples/demo-save.json")
    demo = json.loads(path.read_text(encoding="utf-8"))
    r = selfhost.post("/api/game/check", json={"save": demo.get("save", demo)})
    assert r.status_code == 200, r.text
    print("PASS clean_saves")


def _rss_kb() -> int:
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _deep_wide(cases):
    import time

    for name, client, leaf in cases:
        inner = ",".join([leaf] * 1_000_000)
        body = '{"save": {"state": {"deep": ' + "[" * 900 + inner + "]" * 900 + "}}}"
        before = _rss_kb()
        t0 = time.perf_counter()
        r = client.post(
            "/api/game/check", content=body, headers={"content-type": "application/json"}
        )
        ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 413 and r.json()["detail"]["error"] == "text_too_large", (
            name,
            r.text[:200],
        )
        assert ms < 2000, (name, ms)
        print(
            "CASE deep_wide %s depth=900 leaves=1000000 status=413 ms=%.1f peak_rss_growth_kb=%d"
            % (name, ms, _rss_kb() - before)
        )
    return len(cases)


def test_deep_wide_saves_are_checked_without_a_cap(selfhost):
    import time

    for name, leaf in (("scalars", "0"), ("lists", "[]")):
        inner = ",".join([leaf] * 1_000_000)
        body = '{"save": {"state": {"deep": ' + "[" * 900 + inner + "]" * 900 + "}}}"
        t0 = time.perf_counter()
        r = selfhost.post(
            "/api/game/check", content=body, headers={"content-type": "application/json"}
        )
        ms = (time.perf_counter() - t0) * 1000
        assert r.status_code == 200, (name, r.text[:200])
        assert ms < 120000, (name, ms)
        print("CASE deep_wide %s status=200 ms=%.1f" % (name, ms))
    print("PASS deep_wide_saves_selfhost n=2")


def test_deep_wide_saves_stop_at_the_hosted_cap(hosted):
    n = _deep_wide([("scalars_hosted", hosted, "0"), ("lists_hosted", hosted, "[]")])
    print("PASS deep_wide_saves_hosted n=%d" % n)


def test_numeric_request_fields_are_checked(selfhost):
    save = _played(selfhost)
    for field in ("craft_roll", "dice_roll"):
        before = _calls()
        r = selfhost.post(
            "/api/game/turn",
            json={"save": save, "player_input": "I forge a blade", field: 48053716},
        )
        _expect(r, before, error="content_blocked")
    print("PASS numeric_fields n=2")


def test_create_checks_the_save_it_returns(selfhost, monkeypatch):
    real = game_api.save_to_doc
    calls = []

    def rebuilt(gs):
        doc = real(gs)
        calls.append(1)
        # only the save create builds after the request's own egress check
        return dict(doc, name=PACK) if len(calls) > 1 else doc

    monkeypatch.setattr(game_api, "save_to_doc", rebuilt)
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 451 and r.json()["detail"]["error"] == "output_blocked", r.text
    assert len(calls) == 2 and PACK not in r.text
    print("PASS create_final_check")


def test_egress_skips_only_matched_text(selfhost, monkeypatch):
    from app.game import engine

    # with the engine's own input check off, only egress can see request
    # text that admission counted but did not match
    monkeypatch.setattr(engine, "_moderate", lambda config, text: None)
    body = {**NEW_BODY, "profile": {"name": "Mira", "appearance": "a coat and " + PACK}}
    r = selfhost.post("/api/game/new", json=body)
    assert r.status_code == 451 and r.json()["detail"]["error"] == "output_blocked", r.text
    print("PASS egress_skip")
