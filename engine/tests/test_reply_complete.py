"""A tagged reply plays on a natural stop with narration; cut, unstopped and empty
replies get one retry; a filtered reply is refused as output_blocked."""

import copy
import json
import logging

import httpx
import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import SecretStr

from app.game import context, engine, moderation, physicalisation
from app.game.beats import builtin
from app.game.beats.builtin import DMActBeat, DMWorldStepBeat, build_pipeline_for
from app.game.beats.craft import CraftBeat
from app.game.craft_gate import CraftAssessment
from app.game.llm import MODEL_HAIKU, cache_split_system, call_llm_reply
from app.game.parser import ReplyIncomplete
from app.game.prompts import build_system_blocks
from app.game.state import CharacterProfile, GameConfig, save_to_doc
from app.providers import fake, registry
from app.providers.openai_compat import FORMAT_REMINDER
from test_context_too_large_api import played
from test_context_window import bound, world_step_state
from test_consult import consult_state
from test_strong_gate import TurnReplies, configure_turns, make_save, text_of, turn_bindings

FULL = (
    "[NARRATIVE]\nYou push the door open into the hallway.\n[/NARRATIVE]\n"
    '[STATE_DELTA]\n{"location": "Hallway"}\n[/STATE_DELTA]'
)
EMPTY = "[NARRATIVE][/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]"
ACT = (
    "[NARRATIVE]\nThe bell rings once.\n[/NARRATIVE]\n"
    '[STATE_DELTA]\n{"location": "Bell tower"}\n[/STATE_DELTA]'
)
GROUND = "[NARRATIVE]\nRain on the bell.\n[/NARRATIVE]"
# A real untagged first reply from a live play-through, with its
# em dash written as " - " to keep this file ASCII.
DIAG = (
    "The electric arc ripples, snapping like a live wire thrown into black water. Bulbs "
    "flicker above the stairwell, sputtering under the sudden pulse, then flare briefly - "
    "before the corridor swallows the light again. Outside your door, the building seems "
    "to breathe, caught in a fragile electrical pause."
)
HISTORY = [
    {"role": "user", "content": "I look around."},
    {"role": "assistant", "content": "Rain on neon."},
    {"role": "user", "content": "I walk on."},
]
TAGGED_SYSTEM = cache_split_system("stable [/STATE_DELTA] prefix", "volatile turn part")
INCOMPLETE = "tagged reply ended without a state delta; applying an empty delta"


def tagged(calls):
    return [c["max_tokens"] for c in calls if "[/STATE_DELTA]" in fake._text(c["system"])]


def openai_reply(text, finish):
    choice = {"message": {"content": text}}
    if finish is not None:
        choice["finish_reason"] = finish
    return httpx.Response(
        200, json={"choices": [choice], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    )


def anthropic_reply(text, stop):
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def openai_beat(monkeypatch, replies):
    """The openai adapter answering from replies [(text, finish)], recording each body."""
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return openai_reply(*replies.pop(0))

    monkeypatch.setattr(registry.get_provider("openai"), "transport", httpx.MockTransport(respond))
    return bodies


def new_testpack_save():
    return engine.create_save(
        "mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack")
    )


def test_untagged_natural_stop_plays(selfhost, monkeypatch, calls, caplog):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    caplog.set_level(logging.INFO, logger="app.game.beats.builtin")
    caplog.clear()
    monkeypatch.setattr(fake, "SCRIPT", [("The stairwell bulbs sputter out.", "end_turn")])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 200, r.text
    assert "The stairwell bulbs sputter out." in r.json()["narrative"]
    assert tagged(calls) == [460]
    assert r.json()["save"]["state"]["location"] == save["state"]["location"]
    messages = [record.getMessage() for record in caplog.records]
    assert INCOMPLETE in messages
    assert not any("stairwell" in m for m in messages)
    print("PASS untagged prose with a natural stop plays in one call with an empty delta")


def test_empty_narration_retries_at_the_same_ceiling(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    monkeypatch.setattr(fake, "SCRIPT", [(EMPTY, "end_turn"), (FULL, "end_turn")])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 200, r.text
    assert tagged(calls) == [460, 460]
    assert r.json()["save"]["state"]["location"] == "Hallway"
    print("PASS empty narration is asked once more at the same ceiling and the full reply plays")


def test_empty_narration_twice_refuses(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    monkeypatch.setattr(fake, "SCRIPT", [(EMPTY, "end_turn"), (EMPTY, "end_turn")])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "provider_unavailable"
    assert "save" not in r.json()
    assert tagged(calls) == [460, 460]
    print("PASS empty narration twice refuses the move as 503 provider_unavailable without a save")


def test_diagnostic_reply_plays_on_openai(pack, monkeypatch):
    bodies = openai_beat(monkeypatch, [(DIAG, "stop")])
    gs = new_testpack_save()
    location = gs.state.location
    ts = world_step_state(gs)
    with bound("openai"):
        DMWorldStepBeat().run(ts)
    assert len(bodies) == 1
    assert ts.gs.state.location == location
    assert ts.narrative == builtin._replace_em_dashes(DIAG)
    assert bodies[0]["messages"][-1]["content"].endswith("\n\n" + FORMAT_REMINDER)
    print(
        "PASS diagnostic rep 1 plays on openai in one call and the last user message carries the reminder"
    )


def test_openai_length_retries_at_twice_the_ceiling(pack, monkeypatch):
    bodies = openai_beat(monkeypatch, [("[NARRATIVE]The arc", "length"), (FULL, "stop")])
    ts = world_step_state(new_testpack_save())
    with bound("openai"):
        DMWorldStepBeat().run(ts)
    ceiling = bodies[0]["max_tokens"]
    assert ceiling > 0
    assert [b["max_tokens"] for b in bodies] == [ceiling, 2 * ceiling]
    assert ts.gs.state.location == "Hallway"
    print("PASS openai finish length is asked once more at twice the ceiling")


def run_reply(monkeypatch, provider, payload):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(registry.get_provider(provider), "transport", httpx.MockTransport(handler))
    tok = registry.bind_request(provider, SecretStr("test-key-reply"))
    try:
        result = call_llm_reply(TAGGED_SYSTEM, list(HISTORY), model=MODEL_HAIKU)
    finally:
        registry.unbind_request(tok)
    assert len(sent) == 1
    return result, sent[0]


@pytest.mark.parametrize("provider", ["openai", "xai", "gemini"])
def test_openai_compatible_body_ends_with_the_reminder(monkeypatch, provider):
    payload = {
        "choices": [{"message": {"content": "the rest"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    (raw, usage, stop, truncated), body = run_reply(monkeypatch, provider, payload)
    assert body["messages"][0] == {
        "role": "system",
        "content": "stable [/STATE_DELTA] prefix\n\nvolatile turn part",
    }
    assert body["messages"][1:-1] == HISTORY[:-1]
    assert body["messages"][-1] == {"role": "user", "content": "I walk on.\n\n" + FORMAT_REMINDER}
    assert (raw, stop, truncated) == ("the rest", "stop", False)
    assert usage is not None
    print(f"PASS {provider}: a tagged call ends with the format reminder; stop reason returned")


def test_anthropic_body_has_no_reminder(monkeypatch):
    payload = {
        "content": [{"type": "text", "text": "the rest"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    (raw, _usage, stop, truncated), body = run_reply(monkeypatch, "anthropic", payload)
    assert FORMAT_REMINDER not in json.dumps(body)
    assert body["messages"][-1] == HISTORY[-1]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert (raw, stop, truncated) == ("the rest", "end_turn", False)
    print("PASS anthropic: no reminder, last message unchanged, both cache markers in place")


def test_fake_cut_reply_is_max_tokens(selfhost, monkeypatch, calls, call_lines):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    call_lines.clear()
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait zzcutreply"})
    assert r.status_code == 503, r.text
    assert tagged(calls) == [460, 920]
    lines = [
        line for line in call_lines if "site=app.game.beats.builtin:DMWorldStepBeat.run" in line
    ]
    assert len(lines) == 2
    assert "max_tokens=460 stop=max_tokens" in lines[0]
    assert "max_tokens=920 stop=max_tokens" in lines[1]
    print("PASS the fake cut reply is reported as max_tokens, asked again at 920, then refused 503")


def test_no_stop_reason_counts_as_cut(pack, monkeypatch):
    bodies = openai_beat(monkeypatch, [(FULL, None), (FULL, None)])
    ts = world_step_state(new_testpack_save())
    with bound("openai"):
        with pytest.raises(ReplyIncomplete):
            DMWorldStepBeat().run(ts)
    assert len(bodies) == 2
    assert bodies[1]["max_tokens"] == 2 * bodies[0]["max_tokens"]
    print("PASS a reply without a stop reason counts as cut: one retry, then ReplyIncomplete")


class FilterReplies(TurnReplies):
    """The strong-gate rig's replies; the tagged DM reply is FULL, stopped as `stop`."""

    def __init__(self, stop):
        super().__init__(turn_class="standard")
        self.stop = stop
        self.tagged = 0

    def __call__(self, request):
        body = json.loads(request.content)
        if "[/STATE_DELTA]" in text_of(body.get("system")):
            self.tagged += 1
            stop = self.stop
            if callable(stop):
                stop = stop(text_of(body["messages"][-1]["content"]))
            return anthropic_reply(FULL, stop)
        return super().__call__(request)


def openai_rig(replies):
    """The same rig behind the openai adapter: openai bodies in, openai replies out."""

    def respond(request):
        body = json.loads(request.content)
        messages = body["messages"]
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        rest = messages[1:] if system else messages
        if "[/STATE_DELTA]" in system:
            replies.tagged += 1
            return openai_reply(FULL, replies.stop)
        inner = replies(
            httpx.Request(
                "POST",
                "http://rig.invalid",
                json={"model": body["model"], "system": system, "messages": rest},
            )
        )
        return openai_reply("".join(b.get("text", "") for b in inner.json()["content"]), "stop")

    return respond


def filter_rig(monkeypatch, replies):
    """configure_turns, with a standard turn, no physicalisation and these replies."""
    configure_turns(monkeypatch)
    monkeypatch.setattr(engine, "classify_turn", lambda text, config=None: ("standard", None))
    monkeypatch.setattr(
        physicalisation,
        "classify_scope",
        lambda text: ({"scope": "none", "confidence": "high"}, None),
    )
    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(replies)
    )
    monkeypatch.setattr(
        registry.get_provider("openai"), "transport", httpx.MockTransport(openai_rig(replies))
    )


def gate_save():
    return jsonable_encoder(
        {**save_to_doc(make_save()), "items": [], "memory": [], "physics_cache": []}
    )


@pytest.mark.parametrize(
    "provider,blocked,natural",
    [
        ("anthropic", "refusal", "end_turn"),
        ("openai", "content_filter", "stop"),
    ],
)
def test_filtered_reply_is_output_blocked(selfhost, monkeypatch, provider, blocked, natural):
    replies = FilterReplies(blocked)
    filter_rig(monkeypatch, replies)
    headers = {}
    if provider == "openai":
        assert (
            selfhost.put("/api/keys/openai", json={"key": "canary-key-sh-openai"}).status_code
            == 200
        )
        headers = {"X-Provider": "openai"}
    save = gate_save()
    r = selfhost.post(
        "/api/game/turn", json={"save": save, "player_input": "I wait"}, headers=headers
    )
    assert r.status_code == 451, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "output_blocked"
    assert detail["category"] == "provider_filter"
    assert "save" not in r.json()
    assert replies.tagged == 1
    replies.stop, replies.tagged = natural, 0
    r = selfhost.post(
        "/api/game/turn", json={"save": save, "player_input": "I wait"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["save"]["turn"] == save["turn"] + 1
    print(
        f"PASS {provider} {blocked}: 451 output_blocked provider_filter, no retry, no save; "
        f"the same save then plays on {natural}"
    )


def test_empty_npc_reaction_is_one_quiet_call(pack, monkeypatch, calls, caplog):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    caplog.set_level(logging.WARNING, logger="app.game.beats.builtin")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    ts.has_npcs = True
    monkeypatch.setattr(
        fake,
        "SCRIPT",
        [
            (ACT, "end_turn"),
            (GROUND, "end_turn"),
            ("[NARRATIVE]\n[/NARRATIVE]\n[STATE_DELTA]\n{}\n[/STATE_DELTA]", "end_turn"),
        ],
    )
    with bound("fake"):
        build_pipeline_for("ability").run(ts)
    assert tagged(calls) == [460, 490, 460]
    assert ts.narrative == "The bell rings once.\n\nRain on the bell."
    assert not any("dropping this beat's text" in r.getMessage() for r in caplog.records)
    print("PASS an empty NPC reaction is one call, shows nothing and is not retried or dropped")


def stage_one_state():
    gs = new_testpack_save()
    gs.state.tutorial_stage = 1
    gs.history = [
        {"role": "assistant" if i % 2 == 0 else "user", "content_en": f"Rain {i}."}
        for i in range(3)
    ]
    ts = world_step_state(gs)
    ts.stage = 1
    return gs, ts


def craft_state(ts):
    ts.turn_class = "craft"
    ts.items_block = "[ITEMS]iron[/ITEMS]"
    ts.craft_roll = 12
    ts.craft_context = CraftAssessment(
        dc=12,
        target="bell",
        inputs_committed=["iron"],
        flavor="The work needs care.",
    )
    return ts


@pytest.mark.parametrize("beat", ["world_step", "act", "craft"])
def test_world_state_at_stage_one(pack, monkeypatch, beat):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return anthropic_reply(
            "[NARRATIVE]Rain.[/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]", "end_turn"
        )

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    gs, ts = stage_one_state()
    block = f"[WORLD STATE]\n{json.dumps(gs.state.to_dict(), indent=2)}\n[/WORLD STATE]"
    if beat == "act":
        ts.turn_class = "ability"
        runner = DMActBeat()
    elif beat == "craft":
        craft_state(ts)
        runner = CraftBeat()
    else:
        runner = DMWorldStepBeat()
    stable, _volatile = build_system_blocks(
        gs.profile,
        ts.stage,
        gs.config,
        monster_level=ts.monster_level,
        has_npcs=ts.has_npcs,
        turn_class="standard" if beat == "craft" else ts.turn_class,
        seed_roster=gs.seed_roster,
    )
    with bound("anthropic"):
        runner.run(ts)
    assert len(bodies) == 1
    body = bodies[0]
    assert block + "\n\n" + ts.player_input in fake._text(body["messages"][-1]["content"])
    assert body["system"] == cache_split_system(stable, "")
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in b for b in body["system"][1:])
    marked = [i for i, m in enumerate(body["messages"]) if "cache_control" in json.dumps(m)]
    assert marked == [len(body["messages"]) - 2]
    print(
        f"PASS {beat} at stage 1 sends [WORLD STATE] before the player input; cache markers unchanged"
    )


def test_floor_refuses_at_stage_one_without_provider_calls(pack, monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [], "usage": {}})

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    gs = new_testpack_save()
    gs.state.tutorial_stage = 1
    gs.state.zz_archive = "x" * 280_000
    before = copy.deepcopy(save_to_doc(gs))
    with bound("anthropic"):
        with pytest.raises(context.ContextTooLarge):
            engine.run_turn(gs, "I cast a spell on the mist", "local")
    assert requests == []
    assert save_to_doc(gs) == before
    print("PASS an oversized stage-1 state is refused before any provider request")


def test_tool_calls_stop_plays(pack, monkeypatch):
    bodies = openai_beat(monkeypatch, [(FULL, "tool_calls")])
    ts = world_step_state(new_testpack_save())
    with bound("openai"):
        DMWorldStepBeat().run(ts)
    assert len(bodies) == 1
    assert ts.gs.state.location == "Hallway"
    assert "You push the door open into the hallway." in ts.narrative
    print(
        "PASS openai finish tool_calls with tagged narration plays in one call (not a truncation stop)"
    )


NPC_FILTERED = ("[NARRATIVE]\nThe observer looks up.\n[/NARRATIVE]", "refusal")


class SequenceReplies(FilterReplies):
    """Tagged replies handed out in order as (text, stop); other calls as the gate rig answers."""

    def __init__(self, tagged_replies):
        super().__init__("end_turn")
        self.queue = list(tagged_replies)

    def __call__(self, request):
        body = json.loads(request.content)
        if "[/STATE_DELTA]" in text_of(body.get("system")):
            self.tagged += 1
            return anthropic_reply(*self.queue.pop(0))
        return TurnReplies.__call__(self, request)


def ability_save():
    gs = make_save("never")
    gs.state.npc_dispositions = {"Observer": 1}
    return gs


def test_filtered_npc_reaction_restores_the_move(selfhost, monkeypatch):
    replies = SequenceReplies([(ACT, "end_turn"), (GROUND, "end_turn"), NPC_FILTERED])
    filter_rig(monkeypatch, replies)
    monkeypatch.setattr(engine, "classify_turn", lambda text, config=None: ("ability", None))
    gs = ability_save()
    assert gs.state.location != "Bell tower"
    save = jsonable_encoder({**save_to_doc(gs), "items": [], "memory": [], "physics_cache": []})
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I ring the bell"})
    assert r.status_code == 451, r.text
    assert r.json()["detail"]["category"] == "provider_filter"
    assert "save" not in r.json()
    assert replies.tagged == 3

    replies = SequenceReplies([(ACT, "end_turn"), (GROUND, "end_turn"), NPC_FILTERED])
    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(replies)
    )
    gs = ability_save()
    before = copy.deepcopy(save_to_doc(gs))
    doc = {"items": [], "memory": [], "physics_cache": []}
    with turn_bindings(replies, doc):
        with pytest.raises(moderation.OutputBlocked):
            engine.run_turn(gs, "I ring the bell", "local")
    assert replies.tagged == 3
    assert gs.state.to_dict() == before["state"]
    assert save_to_doc(gs) == before
    print(
        "PASS a filtered NPC reaction after the act applied its delta: 451 with no save, "
        "and the in-process save is restored"
    )


def test_filtered_droppable_beat_refuses_the_move(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    save["state"]["npc_dispositions"] = {"Observer": 1}
    save["config"]["ability_escalation"] = "never"
    calls.clear()
    monkeypatch.setattr(engine, "classify_turn", lambda text, config=None: ("ability", None))
    monkeypatch.setattr(
        physicalisation,
        "classify_scope",
        lambda text: ({"scope": "none", "confidence": "high"}, None),
    )
    monkeypatch.setattr(fake, "SCRIPT", [(ACT, "end_turn"), (GROUND, "end_turn"), NPC_FILTERED])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I ring the bell"})
    assert r.status_code == 451, r.text
    assert r.json()["detail"]["error"] == "output_blocked"
    assert "save" not in r.json()
    assert tagged(calls) == [460, 490, 460]
    assert fake.SCRIPT == []
    print("PASS a filtered stop on the droppable NPC reaction refuses the whole move with 451")


def test_craft_cut_then_complete_plays(pack, monkeypatch):
    bodies = openai_beat(
        monkeypatch, [("[NARRATIVE]You heat the iron and", "length"), (FULL, "stop")]
    )
    ts = craft_state(world_step_state(new_testpack_save()))
    with bound("openai"):
        CraftBeat().run(ts)
    ceiling = bodies[0]["max_tokens"]
    assert [b["max_tokens"] for b in bodies] == [ceiling, 2 * ceiling]
    assert ts.gs.state.location == "Hallway"
    assert "You push the door open into the hallway." in ts.narrative
    print(
        "PASS craft: a cut reply is asked once more at twice the ceiling and the complete reply plays"
    )
