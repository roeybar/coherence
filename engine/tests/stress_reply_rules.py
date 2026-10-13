"""Reply rules under load: natural-stop prose, the delta guard, item undo, the speech regex and
filtered replies."""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.game.addressee import looks_like_dialogue
from app.game.parser import ENGINE_COUNTERS, guard_delta
from app.providers import fake
from conftest import NEW_BODY
from test_context_too_large_api import played
from test_reply_complete import FilterReplies, filter_rig, gate_save, tagged

SCHEMA = Path(__file__).resolve().parents[1] / "lore" / "neon_rain" / "state_schema.json"


def norm(text):
    return " ".join(text.split()).casefold()


def test_natural_stop_prose_threaded(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    monkeypatch.setattr(fake, "SCRIPT", [("The rain keeps on.", "stop")] * 40)

    def turn(i):
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": f"I wait {i}"})
        assert r.status_code == 200, r.text
        assert "The rain keeps on." in r.json()["narrative"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(turn, range(40)))
    assert len(tagged(calls)) == 40
    assert fake.SCRIPT == []
    print("PASS 40 threaded turns of untagged prose on a natural stop: 40 calls, no retry")


def seeded_value(rng, ftype, old):
    pick = rng.randrange(6)
    if ftype == "int":
        return [old + rng.randint(-3, 3), str(old + 1), "x", None, True, old][pick]
    if ftype == "str" and isinstance(old, str):
        return [
            old.upper(),
            old.lower(),
            "  " + old + " ",
            old,
            "Elsewhere " + str(rng.random()),
            rng.randint(0, 9),
        ][pick]
    return [[], {}, "text", 3, None, [{"op": "add", "name": "x"}]][pick]


def test_guard_delta_seeded():
    fields = json.loads(SCHEMA.read_text(encoding="utf-8"))["fields"]
    rng = random.Random(9)
    for _ in range(2000):
        current = {}
        for f in fields:
            if f["type"] == "int":
                current[f["name"]] = rng.randint(0, 20)
            elif f["type"] == "str":
                current[f["name"]] = rng.choice(
                    ["Old Quarter", "Kessler Building, office 4F", "night"]
                )
            else:
                current[f["name"]] = f.get("default")
        names = rng.sample(
            [f["name"] for f in fields] + ["items", "money_delta"], rng.randint(0, 8)
        )
        types = {f["name"]: f["type"] for f in fields}
        delta = {n: seeded_value(rng, types.get(n), current.get(n)) for n in names}
        kept, dropped = guard_delta(delta, current)
        assert set(kept) | set(dropped) == set(delta)
        assert not set(kept) & set(dropped) and len(dropped) == len(set(dropped))
        for key, value in kept.items():
            old = current.get(key)
            if isinstance(old, str) and isinstance(value, str) and value != old:
                assert norm(value) != norm(old), (key, value, old)
            if key in ENGINE_COUNTERS and isinstance(old, int) and not isinstance(old, bool):
                try:
                    new = int(value)
                except (TypeError, ValueError):
                    continue
                assert old <= new <= old + 1, (key, value, old)
    print("PASS 2000 seeded deltas: no raise, no case-only rewrite kept, counters only +0 or +1")


def add_reply(i):
    return (
        "[NARRATIVE]\nYou put it together.\n[/NARRATIVE]\n[STATE_DELTA]\n"
        + json.dumps({"items": [{"op": "add", "name": f"lockpick {i}", "where": "player"}]})
        + "\n[/STATE_DELTA]"
    )


def test_item_undo_through_the_cap(selfhost, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    assert save["turn"] == 0 and save["items"] == []
    for i in range(30):
        monkeypatch.setattr(fake, "SCRIPT", [(add_reply(i), "end_turn")])
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": f"I wait {i}"})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
    assert save["turn"] == 30 and len(save["items"]) == 30
    for _ in range(25):
        snap = save["undo_stack"][-1]
        assert len(snap["items"]) == snap["turn"]
        r = selfhost.post("/api/game/undo", json={"save": save})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
    assert save["turn"] == 5 and len(save["items"]) == 5
    print(
        "PASS 30 item turns then 25 undos: turn 5 with 5 items; each snapshot held its turn's items"
    )


def phrases(rng):
    names = ["Mara", "Dex", "O'Brien", "Kessler"]
    nouns = ["guard", "clerk", "doorman", "medic"]
    words = ["hello", "goodbye", "thanks", "nothing"]
    fillers = ["how far it goes", "the difference", "them apart", "her age", "what time it is"]
    places = ["north", "to the door", "into the rain", "down the hall"]
    out = []
    for _ in range(200):
        kind = rng.randrange(8)
        if kind == 0:
            out.append((f"I tell {rng.choice(names)} to wait", True))
        elif kind == 1:
            out.append((f"ask the {rng.choice(nouns)}", True))
        elif kind == 2:
            out.append((f"I say {rng.choice(words)} to {rng.choice(names)}", True))
        elif kind == 3:
            out.append((f"I ask where {rng.choice(names)} went", True))
        elif kind == 4:
            out.append((f"I can't tell {rng.choice(fillers)}", False))
        elif kind == 5:
            out.append((f"I walk {rng.choice(places)}", False))
        elif kind == 6:
            out.append((f"say {rng.choice(words)}", False))
        else:
            out.append(("ask around", False))
    return out


def test_speech_regex_inputs():
    cases = [
        ("", False),
        ("tell", False),
        ("tell\n her", True),
        ("I walk north. " * 1430, False),
        ("I walk north. " * 1430 + "tell her", True),
    ]
    assert len(cases[3][0]) >= 20000
    cases += phrases(random.Random(4))
    for text, expected in cases:
        assert looks_like_dialogue(text) is expected, text[:80]
    print(
        f"PASS speech regex: {len(cases)} inputs incl. 20,000 characters, never raises, expected result"
    )


def test_filtered_and_natural_threaded(selfhost, monkeypatch):
    replies = FilterReplies(lambda text: "refusal" if "zzfiltered" in text else "end_turn")
    filter_rig(monkeypatch, replies)
    save = gate_save()

    def turn(i):
        move = f"I wait zzfiltered {i}" if i % 2 else f"I wait {i}"
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": move})
        if r.status_code == 451:
            assert r.json()["detail"]["category"] == "provider_filter" and "save" not in r.json()
        return r.status_code, i % 2

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(turn, range(20)))
    assert sorted(code for code, odd in results if odd) == [451] * 10
    assert sorted(code for code, odd in results if not odd) == [200] * 10
    print("PASS 20 threaded moves: 10 refusal stops are 451 output_blocked, 10 natural stops play")
