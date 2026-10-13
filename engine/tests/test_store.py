"""Save binding, MiniColl, and the memory and physics stores over the bound doc."""

import contextvars
import re
import threading

import numpy as np
import pytest

from app import store
from app.game import items, memory, physics


@pytest.fixture
def bound():
    doc = {}
    token = store.bind_save(doc)
    try:
        yield doc
    finally:
        store.unbind_save(token)


# MiniColl


def _coll():
    return store.MiniColl({}, "items")


def test_insert_one_sets_hex_id():
    c = _coll()
    res = c.insert_one({"name": "lamp"})
    assert re.fullmatch(r"[0-9a-f]{32}", res.inserted_id)
    assert c.find_one({"_id": res.inserted_id})["name"] == "lamp"


def test_insert_one_does_not_alias_input():
    c = _coll()
    src = {"name": "lamp", "props": {"lit": False}}
    c.insert_one(src)
    src["props"]["lit"] = True
    assert c.find_one({"name": "lamp"})["props"] == {"lit": False}


def test_find_and_find_one():
    c = _coll()
    c.insert_one({"name": "a", "holder": "player"})
    c.insert_one({"name": "b", "holder": "scene"})
    c.insert_one({"name": "c", "holder": "player"})
    assert [r["name"] for r in c.find({"holder": "player"})] == ["a", "c"]
    assert len(c.find({})) == 3
    assert c.find_one({"name": "b"})["holder"] == "scene"
    assert c.find_one({"name": "zz"}) is None


def test_find_one_and_update_returns_post_update_doc():
    c = _coll()
    rid = c.insert_one({"name": "a", "holder": "player"}).inserted_id
    out = c.find_one_and_update({"_id": rid}, {"$set": {"holder": "scene"}}, return_document=True)
    assert out["holder"] == "scene"
    assert out["_id"] == rid
    assert c.find_one({"_id": rid})["holder"] == "scene"


def test_find_one_and_update_none_when_no_match():
    c = _coll()
    c.insert_one({"name": "a"})
    assert (
        c.find_one_and_update({"_id": "nope"}, {"$set": {"name": "b"}}, return_document=True)
        is None
    )
    assert c.find_one({"name": "a"}) is not None


def test_delete_counts():
    c = _coll()
    rid = c.insert_one({"name": "a", "k": 1}).inserted_id
    c.insert_one({"name": "b", "k": 2})
    c.insert_one({"name": "c", "k": 2})
    assert c.delete_one({"_id": rid}).deleted_count == 1
    assert c.delete_one({"_id": rid}).deleted_count == 0
    assert [{k: v for k, v in r.items() if k != "_id"} for r in c.find({})] == [
        {"name": "b", "k": 2},
        {"name": "c", "k": 2},
    ]


def test_save_id_filter_key_is_ignored():
    c = _coll()
    rid = c.insert_one({"save_id": "a", "name": "x"}).inserted_id
    assert len(c.find({"save_id": "other", "name": "x"})) == 1
    assert c.find_one({"_id": rid, "save_id": "other"})["name"] == "x"


def test_results_are_copies():
    c = _coll()
    rid = c.insert_one({"name": "x", "props": {"w": 1}}).inserted_id
    got = c.find_one({"_id": rid})
    got["name"] = "changed"
    got["props"]["w"] = 99
    c.find({})[0]["props"]["w"] = 77
    upd = c.find_one_and_update({"_id": rid}, {"$set": {"holder": "p"}}, return_document=True)
    upd["holder"] = "q"
    row = c.find_one({"_id": rid})
    assert row["name"] == "x"
    assert row["props"] == {"w": 1}
    assert row["holder"] == "p"


def test_rejects_other_update_operators():
    c = _coll()
    rid = c.insert_one({"name": "x", "n": 1}).inserted_id
    with pytest.raises(ValueError):
        c.find_one_and_update({"_id": rid}, {"$inc": {"n": 1}}, return_document=True)
    with pytest.raises(ValueError):
        c.find_one_and_update(
            {"_id": rid}, {"$set": {"n": 2}, "$inc": {"n": 1}}, return_document=True
        )
    assert c.find_one({"_id": rid})["n"] == 1


def test_rejects_dict_valued_filter():
    c = _coll()
    c.insert_one({"name": "x", "n": 1})
    with pytest.raises(ValueError):
        c.find({"n": {"$gt": 0}})
    with pytest.raises(ValueError):
        c.find_one({"n": {"$gt": 0}})


def test_minicoll_writes_into_doc():
    doc = {"items": []}
    store.MiniColl(doc, "items").insert_one({"name": "x"})
    assert [r["name"] for r in doc["items"]] == ["x"]


# binding


def test_current_save_raises_when_unbound():
    def check():
        with pytest.raises(store.StoreUnbound):
            store.current_save()

    contextvars.Context().run(check)


def test_items_add_raises_when_unbound():
    def check():
        with pytest.raises(store.StoreUnbound):
            items.add("local", "lamp", "player")

    contextvars.Context().run(check)


def test_bind_save_defaults_three_keys():
    doc = {"items": [{"name": "kept"}]}
    token = store.bind_save(doc)
    try:
        assert store.current_save() is doc
        assert doc["items"] == [{"name": "kept"}]
        assert doc["memory"] == []
        assert doc["physics_cache"] == []
    finally:
        store.unbind_save(token)


def test_unbind_restores_prior_binding():
    a, b = {"n": "a"}, {"n": "b"}
    ta = store.bind_save(a)
    try:
        tb = store.bind_save(b)
        assert store.current_save() is b
        store.unbind_save(tb)
        assert store.current_save() is a
    finally:
        store.unbind_save(ta)


def test_items_live_in_bound_doc(bound):
    added = items.add("local", "brass lantern", "player")
    assert re.fullmatch(r"[0-9a-f]{32}", added["id"])
    assert [r["name"] for r in bound["items"]] == ["brass lantern"]
    moved = items.move(added["id"], "scene", current_location="dock")
    assert moved["holder"] == "scene"
    assert items.get_item("local", added["id"])["location"] == "dock"
    assert items.remove(added["id"]) is True
    assert bound["items"] == []


# memory


def test_memory_round_trip_within_float16_tolerance(bound):
    text = "The courier hid the data chip inside a noodle bar menu."
    memory.index_turn("local", 1, "assistant", text)
    assert len(bound["memory"]) == 1
    entry = bound["memory"][0]
    assert set(entry) == {"turn", "role", "text", "v"}
    exact = np.asarray(memory._embed(text), dtype=np.float32)
    stored = memory._decode(entry["v"])
    assert stored.shape == (memory.VECTOR_SIZE,)
    assert np.allclose(stored, exact, atol=1e-3)

    memory.index_turn("local", 2, "user", "I order a bowl of ramen and ask about the weather.")
    hits = memory.recall("local", text, top_k=1)
    assert len(hits) == 1
    assert hits[0]["turn"] == 1
    assert hits[0]["content"] == text
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-2)


def test_memory_recall_excludes_and_sorts(bound):
    for turn in (3, 1, 2):
        memory.index_turn("local", turn, "assistant", f"Neon rain falls on block {turn}.")
    hits = memory.recall("local", "neon rain on the block", top_k=4)
    assert [h["turn"] for h in hits] == [1, 2, 3]
    hits = memory.recall("local", "neon rain on the block", top_k=4, exclude_after_turn=2)
    assert [h["turn"] for h in hits] == [1]


def test_memory_same_turn_and_role_replaces(bound):
    memory.index_turn("local", 1, "user", "first wording")
    memory.index_turn("local", 1, "assistant", "the reply")
    memory.index_turn("local", 1, "user", "second wording")
    assert len(bound["memory"]) == 2
    users = [e for e in bound["memory"] if e["role"] == "user"]
    assert [e["text"] for e in users] == ["second wording"]


# physics


def _record(wish: str) -> dict:
    return {"normalized_wish": physics.normalize_wish(wish), "narrative_hint": "hint"}


def test_physics_store_then_lookup_bumps_use_count(bound):
    wish = "an infinite gold pouch"
    stored = physics._store_new(_record(wish), wish_text=wish, in_tokens=10, out_tokens=5)
    assert stored["use_count"] == 1
    entry = bound["physics_cache"][0]
    assert re.fullmatch(r"[0-9a-f]{32}", entry["_id"])
    assert entry["v"]

    hit = physics.lookup("An infinite gold pouch!")
    assert hit is not None
    assert hit["normalized_wish"] == physics.normalize_wish(wish)
    assert hit["use_count"] == 2
    assert hit["match_score"] >= physics.MATCH_THRESHOLD
    assert bound["physics_cache"][0]["use_count"] == 2
    assert "An infinite gold pouch!" in bound["physics_cache"][0]["seen_variants"]
    assert physics.find_one(wish)["use_count"] == 2


def test_physics_same_normalized_wish_bumps_in_place(bound):
    wish = "a horse that flies"
    physics._store_new(_record(wish), wish_text=wish, in_tokens=1, out_tokens=1)
    again = physics._store_new(
        _record(wish), wish_text="A horse that flies.", in_tokens=1, out_tokens=1
    )
    assert len(bound["physics_cache"]) == 1
    assert again["use_count"] == 2


def test_physics_lookup_miss_returns_none(bound):
    physics._store_new(
        _record("an infinite gold pouch"),
        wish_text="an infinite gold pouch",
        in_tokens=1,
        out_tokens=1,
    )
    assert physics.lookup("I quietly reboot the city power grid with a paperclip") is None
    assert bound["physics_cache"][0]["use_count"] == 1


def test_physics_cache_capped_dropping_oldest(bound):
    cap = physics.PHYSICS_CACHE_MAX
    assert cap == 64
    for i in range(cap + 1):
        wish = f"wish number {i}"
        physics._store_new(_record(wish), wish_text=wish, in_tokens=1, out_tokens=1)
    cache = bound["physics_cache"]
    assert len(cache) == cap
    assert cache[0]["normalized_wish"] == "wish number 1"
    assert cache[-1]["normalized_wish"] == f"wish number {cap}"


# stresser: concurrent requests each see only their own bound doc


def test_stress_concurrent_bindings_are_isolated():
    n_threads, rounds = 50, 20
    errors: list[str] = []
    docs: dict[int, dict] = {}
    start = threading.Barrier(n_threads)

    def work(tid: int) -> None:
        doc = {}
        docs[tid] = doc
        token = store.bind_save(doc)
        try:
            start.wait()
            for r in range(rounds):
                items.add("local", f"t{tid}-add-{r}", "player", turn=r)
                items.apply_ops(
                    "local",
                    [
                        {"op": "add", "name": f"t{tid}-op-{r}", "where": "scene"},
                    ],
                    current_location="market",
                    turn=r,
                )
                view = items.visible_to_player("local", "market")
                names = [i["name"] for i in view["carried"] + view["scene"]]
                foreign = [n for n in names if not n.startswith(f"t{tid}-")]
                if foreign:
                    errors.append(f"thread {tid} saw {foreign[:3]}")
                if len(names) != 2 * (r + 1):
                    errors.append(f"thread {tid} round {r} saw {len(names)} items")
        except Exception as e:  # recorded and asserted below
            errors.append(f"thread {tid}: {e!r}")
        finally:
            store.unbind_save(token)

    threads = [
        threading.Thread(target=contextvars.copy_context().run, args=(work, tid))
        for tid in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    for tid, doc in docs.items():
        assert len(doc["items"]) == 2 * rounds
        assert all(r["name"].startswith(f"t{tid}-") for r in doc["items"])


def test_api_keeps_seeded_lists_and_defaults(selfhost):
    seeded = {
        "items": [{"_id": "a1", "name": "kite"}],
        "memory": [{"turn": 0, "role": "user", "text": "x", "v": ""}],
        "physics_cache": [{"_id": "p", "normalized_wish": "w"}],
    }
    out = selfhost.post("/api/game/undo", json={"save": dict(seeded)}).json()["save"]
    for k in seeded:
        assert out[k] == seeded[k]
    out = selfhost.post("/api/game/undo", json={"save": {}}).json()["save"]
    assert out["items"] == [] and out["memory"] == [] and out["physics_cache"] == []


def test_api_unknown_profile_key_and_physics_trim(selfhost):
    r = selfhost.post("/api/game/undo", json={"save": {"profile": {"nope": 1}}})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_save"
    pc = [{"_id": str(i), "normalized_wish": "w%d" % i} for i in range(500)]
    out = selfhost.post("/api/game/undo", json={"save": {"physics_cache": pc}}).json()["save"]
    assert out["physics_cache"] == pc[-64:]


def test_api_concurrent_turns_keep_own_items(selfhost):
    from conftest import turn_body

    res = {}

    def go(name):
        save = turn_body(selfhost)["save"]
        save["items"] = [{"_id": name, "name": name, "holder": "player"}]
        res[name] = selfhost.post("/api/game/turn", json={"save": save, "player_input": "hold"})

    ts = [threading.Thread(target=go, args=(n,)) for n in ("alpha", "beta")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(res) == ["alpha", "beta"]
    for name, r in res.items():
        assert r.status_code == 200, r.text
        assert {i["name"] for i in r.json()["save"]["items"]} == {name}
