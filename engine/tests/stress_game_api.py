import json
import math
import threading

from app import main
from app.config import settings
from conftest import NEW_BODY, sid
from fixtures.make_doc import make_doc


def _bytes(doc) -> int:
    return len(json.dumps(doc, separators=(",", ":")).encode())


def test_sizes_and_fit():
    for n in (50, 100, 200):
        print("PASS size n=%d bytes=%d" % (n, _bytes(make_doc(n))))
    fit = max(
        n for n in range(10, 1001, 10) if math.ceil(_bytes(make_doc(n)) * 4 / 3) + 512 <= 5000000
    )
    print("PASS fit turns=%d" % fit)


def test_doc_100_round_trips(selfhost):
    r = selfhost.post("/api/game/turn", json={"save": make_doc(100), "player_input": "I wait"})
    assert r.status_code == 200, r.text
    assert len(r.json()["save"]["undo_stack"]) <= 25


def test_body_boundaries(selfhost):
    base = json.dumps({"save": {}, "pad": ""})
    cap = settings.body_cap_bytes
    at_cap = base[:-2] + "x" * (cap - len(base)) + '"}'
    assert len(at_cap) == cap
    h = {"Content-Type": "application/json"}
    assert selfhost.post("/api/game/undo", content=at_cap, headers=h).status_code == 200
    r = selfhost.post("/api/game/undo", content=at_cap + " ", headers=h)
    assert r.status_code == 413
    assert r.json() == {"detail": {"error": "body_too_large", "provider": None}}
    assert selfhost.post("/api/game/undo", content=b"", headers=h).status_code == 422


def test_50_sessions_5_turns_disjoint(selfhost, monkeypatch):
    # isolation under load, not the concurrency limit
    monkeypatch.setattr(main, "slots", threading.BoundedSemaphore(50))
    errors = []

    def play(i):
        try:
            h = {"X-Coherence-Session": sid()}
            body = NEW_BODY | {"name": "s%d" % i}
            save = selfhost.post("/api/game/new", json=body, headers=h).json()["save"]
            save["items"] = [{"_id": "x%d" % i, "name": "token-%d" % i, "holder": "player"}]
            for t in range(5):
                r = selfhost.post(
                    "/api/game/turn",
                    headers=h,
                    json={"save": save, "player_input": "session-%d move-%d" % (i, t)},
                )
                assert r.status_code == 200, r.text
                save = r.json()["save"]
            names = {it.get("name") for it in save["items"]}
            assert names == {"token-%d" % i}
            texts = " ".join(m["text"] for m in save["memory"])
            assert "session-%d " % i in texts
            assert all("session-%d " % j not in texts for j in range(50) if j != i)
        except Exception as e:
            errors.append(repr(e))

    ts = [threading.Thread(target=play, args=(i,)) for i in range(50)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
