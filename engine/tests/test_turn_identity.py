from conftest import NEW_BODY


def _text(entry) -> str:
    return str(entry.get("content_en") or entry.get("content") or "")


def _turn_of(save, i):
    h = save["history"]
    if i == 0 and h[0]["role"] == "assistant":
        return 0
    return save["turn"] - (len(h) - 1 - i) // 2


def test_turns_survive_compression(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    shrinks = 0
    prev = len(save["history"])
    for n in range(1, 18):
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": f"I wait t{n:02d}"})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
        h = save["history"]
        assert save["turn"] == n
        assert h[-1]["role"] == "assistant"
        for i in range(1, len(h)):
            if h[i]["role"] != "user":
                continue
            assert f"t{_turn_of(save, i):02d}" in _text(h[i])
            assert _turn_of(save, i + 1) == _turn_of(save, i)
        if len(h) < prev:
            shrinks += 1
        prev = len(h)
    assert shrinks == 2
    assert len(save["history"]) == 11

    target = _turn_of(save, 5)
    assert target == 15
    snap = next(s for s in save["undo_stack"] if s["turn"] == 14)
    users = [e for e in snap["history"] if e["role"] == "user"]
    assert "t14" in _text(users[-1])

    r = selfhost.post("/api/game/undo-to-turn", json={"save": save, "turn_index": target})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"]
    assert body["turn"] == 14
    texts = [_text(e) for e in body["save"]["history"]]
    assert any("t14" in t for t in texts)
    for gone in ("t15", "t16", "t17"):
        assert not any(gone in t for t in texts)


def test_undo_after_compression_with_length_snapshots(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    for n in range(1, 18):
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": f"I wait u{n:02d}"})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
    assert "history_len" in save["undo_stack"][-1]
    assert "history" in save["undo_stack"][-2]
    for turn in (16, 15):
        r = selfhost.post("/api/game/undo", json={"save": save})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["turn"] == turn
        save = body["save"]
        users = [e for e in save["history"] if e["role"] == "user"]
        assert f"u{turn:02d}" in _text(users[-1])
        assert not any(f"u{turn + 1:02d}" in _text(e) for e in save["history"])


def test_regenerate_with_length_snapshot(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait regen01"})
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    before = save["history"][:-2]
    assert save["undo_stack"][-1]["history_len"] == len(before)
    r = selfhost.post("/api/game/regenerate", json={"save": save})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original_input"] == "I wait regen01"
    assert body["save"]["turn"] == 1
    assert body["save"]["history"][:-2] == before
    assert "regen01" in _text(body["save"]["history"][-2])
    assert "REGENERATION REQUEST" not in _text(body["save"]["history"][-2])
    assert body["save"]["undo_stack"][-1]["history_len"] == len(before)


def test_turns_survive_window_jump(selfhost, monkeypatch):
    import copy

    from app.config import settings
    from app.game import context, engine
    from app.providers import fake

    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")

    def no_compression(self, history):
        raise AssertionError("stored history must not be compressed under a ceiling")

    monkeypatch.setattr(context.ContextEngine, "maybe_compress", no_compression)
    calls = []
    complete = fake.FakeProvider.complete

    def recorded(self, **kwargs):
        if "[INSIGHT DICE" in fake._text(kwargs["messages"][-1]["content"]):
            calls.append(copy.deepcopy(kwargs))
        return complete(self, **kwargs)

    monkeypatch.setattr(fake.FakeProvider, "complete", recorded)
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    jumped = False
    for n in range(1, 24):
        previous = copy.deepcopy(save["history"])
        calls.clear()
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": f"I wait w{n:02d}"})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
        h = save["history"]
        assert save["turn"] == n and len(h) == len(previous) + 2
        assert h[:-2] == previous
        assert not any("HISTORY SUMMARY" in _text(entry) for entry in h)
        for i in range(1, len(h)):
            if h[i]["role"] != "user":
                continue
            assert f"w{_turn_of(save, i):02d}" in _text(h[i])
            assert _turn_of(save, i + 1) == _turn_of(save, i)
        assert calls
        projected = engine._history_for_llm(previous)
        for call in calls:
            sent = [
                {"role": row["role"], "content": fake._text(row["content"])}
                for row in call["messages"][:-1]
            ]
            start = len(projected) - len(sent)
            assert sent == projected[start:]
            assert start == 0 or start == len(projected) or projected[start]["role"] == "user"
            jumped = jumped or start > 0
            chars = len(fake._text(call["system"])) + sum(
                len(fake._text(row["content"])) + 4 for row in call["messages"]
            )
            assert context.est(chars) <= 20300
    assert jumped
    full = copy.deepcopy(save)
    r = selfhost.post("/api/game/undo-to-turn", json={"save": full, "turn_index": 20})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["turn"] == 19
    texts = [_text(entry) for entry in body["save"]["history"]]
    assert any("w19" in text for text in texts)
    assert all(not any(f"w{n:02d}" in text for text in texts) for n in range(20, 24))

    r = selfhost.post("/api/game/undo-to-turn", json={"save": full, "turn_index": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["turn"] == 2
    undone = body["save"]
    projected = engine._history_for_llm(undone["history"])
    expected_start = context.window_start(projected, context.ceiling_for(300))
    calls.clear()
    r = selfhost.post("/api/game/turn", json={"save": undone, "player_input": "I wait w03"})
    assert r.status_code == 200, r.text
    assert calls
    for call in calls:
        sent = [
            {"role": row["role"], "content": fake._text(row["content"])}
            for row in call["messages"][:-1]
        ]
        assert sent == projected[expected_start:]
    print("PASS 23 turns preserve identity across window jumps; undo recomputes the earlier window")
