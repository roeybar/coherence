from app import ratelimit
from conftest import turn_body

INPUTS = [
    "/help",
    "/state",
    "/who",
    "/money",
    "/inventory",
    "/recap",
    "ooc: what do I carry",
    "(meta) who is here",
]


def _meta(client, save, text):
    r = client.post("/api/game/turn", json={"save": save, "player_input": text})
    assert r.status_code == 200, (text, r.status_code, r.text)
    return r.json()


def test_help_is_a_system_reply_and_changes_nothing(selfhost):
    save = turn_body(selfhost)["save"]
    body = _meta(selfhost, save, "/help")
    assert body["kind"] == "system_reply"
    assert body["title"] == "help"
    assert body["save"]["turn"] == save["turn"]
    assert len(body["save"]["history"]) == len(save["history"])
    print("PASS /help is a system_reply and leaves turn and history unchanged")


def test_meta_commands_make_no_model_call(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(ratelimit.messages, "interval", 0.0)
    save = turn_body(selfhost)["save"]
    seen = {}
    for text in ("/help", "/state"):
        calls.clear()
        body = _meta(selfhost, save, text)
        assert body["kind"] == "system_reply", text
        seen[text] = len(calls)
        assert seen[text] == 0, (text, seen[text])
    calls.clear()
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I look around"})
    assert r.status_code == 200, r.text
    seen["I look around"] = len(calls)
    assert seen["I look around"] > 0
    print(f"PASS model calls per turn: {seen}")


def test_meta_commands_stress(selfhost, monkeypatch):
    monkeypatch.setattr(ratelimit.messages, "interval", 0.0)
    save = turn_body(selfhost)["save"]
    turn, size = save["turn"], len(save["history"])
    replies = 0
    for _ in range(5):
        for text in INPUTS:
            body = _meta(selfhost, save, text)
            assert body["kind"] == "system_reply", text
            assert body["save"]["turn"] == turn, text
            assert len(body["save"]["history"]) == size, text
            save = body["save"]
            replies += 1
    assert replies == 40
    print(f"PASS {replies} meta commands: all system_reply, turn unchanged")
