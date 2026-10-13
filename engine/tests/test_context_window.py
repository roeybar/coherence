"""Whole-turn request windows preserve the stored save and recall boundary."""

import copy
import json
import random
from contextlib import contextmanager

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from app import store
from app.config import settings
from app.game import context, engine, items, lore, memory, moderation, npcs, timeskip
from app.game.beats.base import PROFILES
from app.game.beats.builtin import DMActBeat, DMWorldStepBeat, _ACT_FOCUS
from app.game.beats.craft import CraftBeat, _TIER_GUIDANCE
from app.game.beats.state import TurnState
from app.game.craft_gate import CraftAssessment
from app.game.llm import MODEL_HAIKU, cache_split_system
from app.game.prompts import HOOK_LAST, build_system_blocks
from app.game.state import CharacterProfile, GameConfig, save_to_doc
from app.providers import fake, registry


@contextmanager
def bound(provider, doc=None):
    save_token = store.bind_save({} if doc is None else doc)
    pack_token = moderation.bind_pack("testpack")
    provider_token = registry.bind_request(provider, SecretStr("canary-ctl"))
    try:
        yield
    finally:
        registry.unbind_request(provider_token)
        moderation.unbind_pack(pack_token)
        store.unbind_save(save_token)


@pytest.fixture
def fake_window(monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 3000)
    with bound("fake"):
        yield context.ceiling_for(3000)


def projection():
    rng = random.Random(104)
    history = [{"role": "assistant", "content": "The rain begins."}]
    for turn in range(1, 61):
        history.extend(
            [
                {"role": "user", "content": f"move {turn}: " + "u" * rng.randrange(90, 400)},
                {"role": "assistant", "content": "a" * rng.randrange(180, 600)},
            ]
        )
    return history


def history_chars(history):
    return sum(len(row["content"]) + context.MESSAGE_OVERHEAD for row in history)


def assemble(history, tail, start, recall=lambda start: "", memory_block=""):
    return context.assemble(
        "s" * 100,
        "v" * 20,
        history,
        lambda memory: "\n\n".join(part for part in (memory, tail) if part),
        recall,
        start=start,
        memory_block=memory_block,
        legacy_n=6,
    )


def test_est_and_provider_ceilings(monkeypatch):
    assert [context.est(n) for n in (0, 1, 3, 4)] == [0, 1, 1, 2]
    assert context.ceiling_for(70000) == (90000, 30000, 70000)
    assert context.context_ceiling() is None
    with bound("anthropic"):
        assert context.context_ceiling() == (90000, 30000, 70000)
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    with bound("fake"):
        monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
        assert context.context_ceiling() == (20300, 128, 300)
        monkeypatch.setattr(settings, "engine_fake_context_tokens", None)
        assert context.context_ceiling() is None
        monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
        monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "0")
        assert context.context_ceiling() is None
    with bound("openai"):
        assert context.context_ceiling() is None
    assert context.context_ceiling() is None
    print("PASS estimates and provider ceilings")


def test_whole_turn_prefixes_and_undo_are_pure():
    history = projection()
    before = copy.deepcopy(history)
    starts = []
    ceiling = context.ceiling_for(3000)
    for end in range(1, len(history) + 1, 2):
        prefix = history[:end]
        start = context.window_start(prefix, ceiling)
        assert start == 0 or start == len(prefix) or prefix[start]["role"] == "user"
        assert context.est(history_chars(prefix[start:])) <= 3000 or start == len(prefix)
        starts.append(start)
    assert starts == sorted(starts)
    assert context.window_start(history[:41], ceiling) == starts[20]
    assert context.window_start(history[:41], ceiling) < starts[-1]
    assert history == before
    print("PASS whole-turn boundaries, monotone growth and pure undo recomputation")


def test_tail_guard_does_not_change_base_or_stick(fake_window):
    history = projection()
    s0 = context.window_start(history, fake_window)
    tail_size = 3 * fake_window[0] + 1 - 100 - history_chars(history[s0:]) - 4 - 22
    _, _, s1, _ = assemble(history, "t" * tail_size, s0)
    assert s1 > s0
    assert context.window_start(history, fake_window) == s0
    _, _, s2, _ = assemble(history, "A small move.", s0)
    assert s2 == s0
    print("PASS a large tail moves only the call guard; the next call returns to the base")


def test_guard_rebuilds_recall_at_next_candidate(fake_window):
    history = projection()
    s0 = context.window_start(history, fake_window)
    nxt = next(c for c in context.candidates(history, fake_window[1]) if c > s0)
    tail_size = 3 * fake_window[0] + 1 - 100 - history_chars(history[s0:]) - 4 - 22
    tail = "t" * tail_size
    recalled_block = "[RELEVANT MEMORY]earlier rain"
    before_est = context.est(100 + history_chars(history[s0:]) + 4 + 22 + len(tail))
    after_est = context.est(
        100 + history_chars(history[nxt:]) + 4 + 22 + len(tail) + 2 + len(recalled_block),
    )
    assert 0 < before_est - fake_window[0] <= 3
    assert after_est <= fake_window[0]
    calls = []

    def recall(start):
        calls.append(start)
        return recalled_block

    _, messages, start, block = assemble(history, tail, s0, recall)
    assert start == nxt and calls == [nxt]
    assert block == recalled_block and recalled_block in messages[-1]["content"]
    assert context.recall_cutoff(history, 60, start) == context.turn_at(history, 60, start)
    print("PASS guard advances once and rebuilds recall at the actual sent boundary")


def test_no_candidate_sends_only_final_message(fake_window):
    history = [
        {"role": "user", "content": "I wait."},
        {"role": "assistant", "content": "a" * (3 * fake_window[2] + 1)},
    ]
    start = context.window_start(history, fake_window)
    assert start == len(history)
    _, messages, sent_start, _ = assemble(history, "I listen.", start)
    assert messages == [{"role": "user", "content": "v" * 20 + "\n\nI listen."}]
    assert sent_start == len(history)
    assert context.recall_cutoff(history, 1, start) == 2
    print("PASS oversized last turn sends no history and recalls through the previous turn")


def test_terminal_guard_is_bounded(fake_window):
    history = projection()
    calls = []

    def recall(start):
        calls.append(start)
        return ""

    with pytest.raises(context.ContextTooLarge) as error:
        assemble(history, "t" * (3 * fake_window[0] + 1), 0, recall)
    assert error.value.est_tokens > error.value.high == fake_window[0]
    assert len(calls) <= len(context.candidates(history, fake_window[1]))
    assert calls == sorted(set(calls)) and calls[-1] == len(history)
    calls.clear()
    with pytest.raises(context.ContextTooLarge):
        assemble([], "t" * (3 * fake_window[0] + 1), 0, recall)
    assert calls == []
    print("PASS terminal refusal ends within the number of candidates")


def test_floor_refuses_without_provider_calls(pack, monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [], "usage": {}})

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    gs.state.zz_archive = "x" * 280_000
    before = copy.deepcopy(save_to_doc(gs))
    with bound("anthropic"):
        with pytest.raises(context.ContextTooLarge):
            engine.run_turn(gs, "I cast a spell on the mist", "local")
    assert requests == []
    assert save_to_doc(gs) == before
    print("PASS oversized floor makes zero provider requests and leaves the save unchanged")


def test_meta_command_answers_on_oversized_save_without_provider_calls(pack, monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [], "usage": {}})

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    gs.state.zz_archive = "x" * 280_000
    before = copy.deepcopy(save_to_doc(gs))
    with bound("anthropic"):
        payload = engine.run_turn(gs, "/help", "local")
        with pytest.raises(context.ContextTooLarge):
            engine.run_turn(gs, "I cast a spell on the mist", "local")
    assert payload["kind"] == "system_reply"
    assert requests == []
    assert save_to_doc(gs) == before
    print("PASS a meta command answers on an oversized save with zero requests and no change")


def test_floor_refuses_timeskip_without_provider_calls(pack, monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"content": [], "usage": {}})

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    gs.state.zz_archive = "x" * 280_000
    before = copy.deepcopy(save_to_doc(gs))
    with bound("anthropic"):
        assert timeskip.detect_timeskip("I skip 3 weeks")
        with pytest.raises(context.ContextTooLarge):
            engine.run_turn(gs, "I skip 3 weeks", "local")
    assert requests == []
    assert save_to_doc(gs) == before
    print("PASS oversized floor refuses time-skip with zero requests and an unchanged save")


def world_step_state(gs):
    pack = lore.load_pack("testpack")
    schema = lore.state_field_defs(pack)
    return TurnState(
        gs=gs,
        save_id="local",
        player_input="I wait by the bell.",
        pre_state=gs.state.to_dict(),
        pack=pack,
        pack_schema=schema,
        valid_keys={field["name"] for field in schema},
        turn_class="standard",
        turn_model=MODEL_HAIKU,
        monster_level=0,
        has_npcs=False,
        stage=2,
        insight_dice=[1, 2, 3, 4, 5, 6, 7, 8],
        memory_block="[RELEVANT MEMORY]x",
        recalled=[],
        ctx=context.ContextEngine(),
        stakes="normal",
    )


def test_act_retry_over_ceiling_returns_stub_without_second_call(pack, monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    gs.history = []
    ts = world_step_state(gs)
    ts.turn_class = "ability"
    ts.memory_block = ""
    assert npcs.render_dm_notes_block(gs.state) == ""
    stable, volatile = build_system_blocks(
        gs.profile,
        ts.stage,
        gs.config,
        monster_level=0,
        has_npcs=False,
        turn_class="ability",
        seed_roster=gs.seed_roster,
    )
    volatile += _ACT_FOCUS
    dice = (
        "[INSIGHT DICE - DM only, hidden from player. "
        "Consume in order for NPC perception/insight checks: 1, 2, 3, 4, 5, 6, 7, 8]"
    )
    joined = "\n\n".join(
        [
            dice,
            f"[WORLD STATE]\n{json.dumps(gs.state.to_dict(), indent=2)}\n[/WORLD STATE]",
            ts.player_input,
            PROFILES["short"].directive,
        ]
    )
    high = context.ceiling_for(context.ANTHROPIC_HIST_HIGH)[0]
    padding = 3 * high - len(stable) - len(volatile) - 2 - len(joined) - 4 - 2
    assert padding > 0
    ts.items_block = "x" * padding
    attempts = []
    assemble_call = context.assemble

    def recorded_assemble(*args, **kwargs):
        try:
            assembled = assemble_call(*args, **kwargs)
        except context.ContextTooLarge as error:
            attempts.append(("refused", error.est_tokens, error.high))
            raise
        system, messages, start, _memory = assembled
        estimate = context.est(len(fake._text(system)) + history_chars(messages))
        attempts.append(("fits", estimate, start))
        return assembled

    monkeypatch.setattr(context, "assemble", recorded_assemble)
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "[NARRATIVE][/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]"}
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(respond)
    )
    with bound("anthropic"):
        result = DMActBeat().run(ts)
    assert len(attempts) == 2
    assert attempts[0] == ("fits", high, len(gs.history))
    assert attempts[1][0] == "refused" and attempts[1][1] > attempts[1][2] == high
    assert len(bodies) == 1
    assert len(bodies[0]["messages"]) == 1
    assert "BEAT FOCUS - act" in bodies[0]["messages"][-1]["content"]
    assert result.narrative == (
        "You attempt the act. The world hesitates around you "
        "for a moment, then settles. Something happened - what, "
        "you can't quite tell yet."
    )
    print("PASS fitting act call with an oversized retry returns the stub after one DM call")


def test_openai_body_is_legacy_and_anthropic_uses_full_history(pack, monkeypatch):
    monkeypatch.delenv("ENGINE_FAKE_PROVIDER", raising=False)
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    gs.history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content_en": f"Rain {i}."}
        for i in range(20)
    ]
    ts = world_step_state(gs)
    assert npcs.render_dm_notes_block(gs.state) == ""
    stable, volatile = build_system_blocks(
        gs.profile,
        ts.stage,
        gs.config,
        monster_level=0,
        has_npcs=False,
        turn_class="standard",
        seed_roster=gs.seed_roster,
    )
    dice = (
        "[INSIGHT DICE - DM only, hidden from player. "
        "Consume in order for NPC perception/insight checks: 1, 2, 3, 4, 5, 6, 7, 8]"
    )
    joined = "\n\n".join(
        [
            dice,
            ts.memory_block,
            f"[WORLD STATE]\n{json.dumps(gs.state.to_dict(), indent=2)}\n[/WORLD STATE]",
            ts.player_input,
            PROFILES["medium"].directive,
            HOOK_LAST,
        ]
    )
    projected = engine._history_for_llm(gs.history)
    bodies = []
    replies = []

    def openai_response(request):
        bodies.append(json.loads(request.content))
        reply = (
            replies.pop(0)
            if replies
            else "[NARRATIVE]Rain.[/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]"
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = registry.get_provider("openai")
    monkeypatch.setattr(provider, "transport", httpx.MockTransport(openai_response))
    with bound("openai"):
        DMWorldStepBeat().run(ts)
    body = bodies.pop()
    assert body == provider.build_body(
        model=settings.openai_model_fast,
        system=cache_split_system(stable, volatile),
        messages=projected[-6:] + [{"role": "user", "content": joined}],
        max_tokens=body["max_tokens"],
        temperature=None,
    )
    assert len(body["messages"]) == 8

    act_ts = world_step_state(gs)
    act_ts.turn_class = "ability"
    act_ts.items_block = "[ITEMS]a bell[/ITEMS]"
    act_ts.consult_ruling = "The bell rings."
    act_stable, act_volatile = build_system_blocks(
        gs.profile,
        act_ts.stage,
        gs.config,
        monster_level=0,
        has_npcs=False,
        turn_class="ability",
        seed_roster=gs.seed_roster,
    )
    act_volatile += _ACT_FOCUS
    act_joined = "\n\n".join(
        [
            dice,
            act_ts.memory_block,
            act_ts.items_block,
            f"[WORLD STATE]\n{json.dumps(gs.state.to_dict(), indent=2)}\n[/WORLD STATE]",
            f"[RULING - DM only, from the rules consult]\n{act_ts.consult_ruling}\n[/RULING]",
            act_ts.player_input,
            PROFILES["short"].directive,
        ]
    )
    stricter_volatile = act_volatile + (
        "\n\nCRITICAL: your previous attempt produced an empty narrative. "
        "You MUST write at least one short paragraph (2-4 sentences) "
        "describing what physically happens when the player attempts "
        "the act. Even if the attempt fails or produces an unexpected "
        "result, render the moment - never emit empty narrative."
    )
    replies.extend(
        [
            "[NARRATIVE][/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]",
            "[NARRATIVE]Rain.[/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]",
        ]
    )
    with bound("openai"):
        DMActBeat().run(act_ts)
    assert len(bodies) == 2 and replies == []
    for body, expected_volatile in zip(bodies, (act_volatile, stricter_volatile)):
        assert body == provider.build_body(
            model=settings.openai_model_fast,
            system=cache_split_system(act_stable, expected_volatile),
            messages=projected[-6:] + [{"role": "user", "content": act_joined}],
            max_tokens=body["max_tokens"],
            temperature=None,
        )
        assert len(body["messages"]) == 8
    bodies.clear()

    craft_ts = world_step_state(gs)
    craft_ts.turn_class = "craft"
    craft_ts.items_block = "[ITEMS]iron[/ITEMS]"
    craft_ts.craft_roll = 12
    craft_ts.craft_context = CraftAssessment(
        dc=12,
        target="bell",
        inputs_committed=["iron"],
        flavor="The work needs care.",
    )
    craft_block = (
        "[CRAFT RESULT - DM only, hidden from player]\n"
        "Target: bell\n"
        "Inputs: iron\n"
        "DC: 12 (moderate) | Roll: 12 | Outcome: SUCCESS\n"
        "[/CRAFT RESULT]\n\n" + _TIER_GUIDANCE["success"]
    )
    craft_volatile = volatile + "\n\n" + craft_block
    craft_joined = "\n\n".join(
        [
            dice,
            craft_ts.memory_block,
            craft_ts.items_block,
            f"[WORLD STATE]\n{json.dumps(gs.state.to_dict(), indent=2)}\n[/WORLD STATE]",
            craft_ts.player_input,
            HOOK_LAST,
        ]
    )
    with bound("openai"):
        CraftBeat().run(craft_ts)
    assert len(bodies) == 1
    body = bodies.pop()
    assert body == provider.build_body(
        model=settings.openai_model_fast,
        system=cache_split_system(stable, craft_volatile),
        messages=projected[-6:] + [{"role": "user", "content": craft_joined}],
        max_tokens=body["max_tokens"],
        temperature=None,
    )
    assert len(body["messages"]) == 8

    def anthropic_response(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": "[NARRATIVE]Rain.[/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]",
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(anthropic_response)
    )
    with bound("anthropic"):
        DMWorldStepBeat().run(world_step_state(gs))
    body = bodies.pop()
    assert body["system"] == cache_split_system(stable, "") and len(body["system"]) == 1
    sent = [
        {"role": row["role"], "content": fake._text(row["content"])} for row in body["messages"]
    ]
    assert sent[:-1] == projected
    assert sent[-1]["content"] == volatile + "\n\n" + joined
    print(
        "PASS OpenAI world-step, act, retry and craft bodies remain identical; Anthropic sends full history"
    )


def test_recall_excludes_window_before_ranking(monkeypatch):
    e0 = np.zeros(memory.VECTOR_SIZE)
    e0[0] = 1
    earlier = np.zeros(memory.VECTOR_SIZE)
    earlier[:2] = [0.5, 0.866]
    entries = [
        {"turn": turn, "role": role, "text": "recent rain", "v": memory._encode(e0)}
        for turn in range(10, 18)
        for role in ("user", "assistant")
    ]
    entries += [
        {"turn": turn, "role": "user", "text": "earlier rain", "v": memory._encode(earlier)}
        for turn in range(1, 5)
    ]
    monkeypatch.setattr(memory, "_embed", lambda text: e0)
    token = store.bind_save({"memory": entries})
    try:
        hits = memory.recall("local", "rain", exclude_after_turn=10)
    finally:
        store.unbind_save(token)
    assert [hit["turn"] for hit in hits] == [1, 2, 3, 4]
    print("PASS recall filters before ranking, preserving older eligible turns")


def test_residual_refuses_without_dm_call_and_restores_save(pack, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(settings, "engine_fake_context_tokens", 300)
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    gs.turn = 1
    gs.history = []
    before = copy.deepcopy(save_to_doc(gs))
    calls = []
    complete = fake.FakeProvider.complete

    def recorded(self, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return complete(self, **kwargs)

    monkeypatch.setattr(fake.FakeProvider, "complete", recorded)
    monkeypatch.setattr(
        items, "format_block", lambda *args, **kwargs: "[ITEMS]\n" + "rain " * 15000
    )
    with bound("fake"):
        context.check_floor(gs, "I wait by the bell")
        with pytest.raises(context.ContextTooLarge) as error:
            engine.run_turn(gs, "I wait by the bell", "local")
    assert error.value.high == 20300 and error.value.est_tokens > 20300
    assert calls
    for call in calls:
        last = fake._text(call["messages"][-1]["content"])
        system = fake._text(call["system"])
        assert "[INSIGHT DICE" not in last
        assert "BEAT FOCUS - act" not in system + last
    assert save_to_doc(gs) == before
    print(
        "PASS floor fits but items exceed the ceiling: classifiers only, no DM call, save unchanged"
    )
