import copy
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.game import engine
from app.game.beats.builtin import DMActBeat, DMWorldStepBeat, build_pipeline_for
from app.game.beats.craft import CraftBeat
from app.game.craft_gate import CraftAssessment
from app.game.parser import ReplyIncomplete
from app.game.prompts import CONSULT_SYSTEM
from app.game.state import CharacterProfile, GameConfig, save_to_doc
from app.providers import fake
from test_context_too_large_api import played
from test_context_window import bound, world_step_state
from test_consult import consult_state
from test_strong_gate import make_save


FULL = (
    "[NARRATIVE]\nYou push the door open into the hallway.\n[/NARRATIVE]\n"
    '[STATE_DELTA]\n{"location": "Hallway"}\n[/STATE_DELTA]'
)
ACT = (
    "[NARRATIVE]\nThe bell rings once.\n[/NARRATIVE]\n"
    '[STATE_DELTA]\n{"location": "Bell tower"}\n[/STATE_DELTA]'
)
CUT = (fake.CUT_REPLY, "max_tokens")
GROUND = "[NARRATIVE]\nRain on the bell.\n[/NARRATIVE]"
NPC = "[NARRATIVE]\nThe observer looks up.\n[/NARRATIVE]"


def tagged(calls):
    return [c["max_tokens"] for c in calls if "[/STATE_DELTA]" in fake._text(c["system"])]


def test_cut_reply_is_asked_again_once_and_the_complete_reply_plays(
    selfhost, monkeypatch, calls, call_lines
):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    call_lines.clear()
    monkeypatch.setattr(fake, "SCRIPT", [CUT, (FULL, "end_turn")])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "kind" not in body
    assert "You push the door open into the hallway." in body["narrative"]
    assert "wet wool" not in r.text
    assert body["save"]["state"]["location"] == "Hallway"
    assert tagged(calls) == [460, 920]
    assert fake.SCRIPT == []
    lines = [
        line for line in call_lines if "site=app.game.beats.builtin:DMWorldStepBeat.run" in line
    ]
    assert len(lines) == 2
    assert "max_tokens=460 stop=max_tokens" in lines[0]
    assert "max_tokens=920 stop=end_turn" in lines[1]
    print("PASS cut reply: one 460-to-920 retry, complete narration and delta, two call log lines")


def test_cut_twice_refuses_the_move(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    monkeypatch.setattr(fake, "SCRIPT", [CUT, CUT])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "provider_unavailable"
    assert "save" not in r.json()
    assert "wet wool" not in r.text
    assert tagged(calls) == [460, 920]
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
    assert r.status_code == 200, r.text
    print("PASS cut twice: 503 provider_unavailable without save or cut text; next move plays")


def test_exactly_one_retry_per_move(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    monkeypatch.setattr(fake, "SCRIPT", [CUT, CUT, CUT])
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 503, r.text
    assert tagged(calls) == [460, 920]
    assert len(fake.SCRIPT) == 1
    print("PASS one retry per move: third scripted cut remains unused")


def test_missing_state_delta_on_natural_stop_is_complete(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()
    monkeypatch.setattr(
        fake,
        "SCRIPT",
        [
            ("[NARRATIVE]\nYou open the door.\n[/NARRATIVE]", "end_turn"),
            (FULL, "end_turn"),
        ],
    )
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I open the door"})
    assert r.status_code == 200, r.text
    assert tagged(calls) == [460]
    assert r.json()["save"]["state"]["location"] == save["state"]["location"]
    assert len(fake.SCRIPT) == 1
    print("PASS missing state delta on a natural stop: one call, empty delta, no retry")


def test_refusal_restores_the_save_in_process(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    before = copy.deepcopy(save_to_doc(gs))
    monkeypatch.setattr(fake, "SCRIPT", [CUT, CUT])
    with bound("fake"):
        with pytest.raises(ReplyIncomplete):
            engine.run_turn(gs, "I open the door", "local")
    assert save_to_doc(gs) == before
    assert tagged(calls) == [460, 920]
    print("PASS refusal restores the entire save document in process after two cut replies")


def test_one_retry_spans_the_ability_beats(pack, monkeypatch, calls, caplog):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    caplog.set_level(logging.WARNING, logger="app.game.beats.builtin")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    ts.has_npcs = False
    monkeypatch.setattr(fake, "SCRIPT", [CUT, (ACT, "end_turn"), CUT])
    with bound("fake"):
        build_pipeline_for("ability").run(ts)
    assert tagged(calls) == [460, 920, 490]
    assert ts.reply_retried is True
    assert fake.SCRIPT == []
    assert ts.narrative == "The bell rings once."
    assert ts.gs.state.location == "Bell tower"
    drops = [
        record for record in caplog.records if "dropping this beat's text" in record.getMessage()
    ]
    assert len(drops) == 1
    assert "wet wool" not in caplog.text
    print(
        "PASS ability beats share one retry: complete act applied, incomplete grounding dropped once"
    )


def test_consult_ruling_is_reused_on_retry(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = True
    ts.has_npcs = False
    monkeypatch.setattr(fake, "SCRIPT", [CUT, (ACT, "end_turn")])
    with bound("fake"):
        build_pipeline_for("ability", consult=True).run(ts)
    assert len([c for c in calls if fake._text(c["system"]) == CONSULT_SYSTEM]) == 1
    assert tagged(calls) == [460, 920, 490]
    act_calls = [c for c in calls if "[/STATE_DELTA]" in fake._text(c["system"])][:2]
    for call in act_calls:
        assert "[RULING - DM only, from the rules consult]" in fake._text(
            call["messages"][-1]["content"]
        )
    assert "The bell rings once." in ts.narrative
    print("PASS one consult: its ruling is present in both act attempts and complete act is shown")


def test_cut_replies_threaded(selfhost, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    save = played(selfhost)
    calls.clear()

    def turn(i):
        move = f"I wait {i}" if i % 2 == 0 else f"I wait zzcutreply {i}"
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": move})
        if i % 2 == 0:
            assert r.status_code == 200, r.text
            assert r.json()["narrative"]
        else:
            assert r.status_code == 503, r.text
            assert r.json()["detail"]["error"] == "provider_unavailable"
            assert "save" not in r.json()
            assert "wet wool" not in r.text

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(turn, range(40)))
    assert len(tagged(calls)) == 60
    print(
        "PASS 40 threaded turns, 4 workers: 20 played, 20 cut twice and refused 503 provider_unavailable"
    )


def test_grounding_cut_twice_is_dropped_not_refused(pack, monkeypatch, calls, caplog):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    caplog.set_level(logging.WARNING, logger="app.game.beats.builtin")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    ts.has_npcs = False
    monkeypatch.setattr(fake, "SCRIPT", [(ACT, "end_turn"), CUT, CUT])
    with bound("fake"):
        build_pipeline_for("ability").run(ts)
    assert tagged(calls) == [460, 490, 980]
    assert ts.narrative == "The bell rings once."
    drops = [
        record for record in caplog.records if "dropping this beat's text" in record.getMessage()
    ]
    assert len(drops) == 1
    print("PASS grounding cut twice: one 490-to-980 retry, text dropped and act retained")


def test_npc_react_retries_once(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    ts.has_npcs = True
    monkeypatch.setattr(
        fake, "SCRIPT", [(ACT, "end_turn"), (GROUND, "end_turn"), CUT, (NPC, "end_turn")]
    )
    with bound("fake"):
        build_pipeline_for("ability").run(ts)
    assert tagged(calls) == [460, 490, 460, 920]
    assert ts.narrative == "The bell rings once.\n\nRain on the bell.\n\nThe observer looks up."
    print("PASS NPC reaction retries once; closed grounding and reaction narratives need no delta")


def test_npc_react_dropped_after_retry_spent(pack, monkeypatch, calls, caplog):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    caplog.set_level(logging.WARNING, logger="app.game.beats.builtin")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    ts.has_npcs = True
    monkeypatch.setattr(fake, "SCRIPT", [(ACT, "end_turn"), CUT, (GROUND, "end_turn"), CUT])
    with bound("fake"):
        build_pipeline_for("ability").run(ts)
    assert tagged(calls) == [460, 490, 980, 460]
    assert ts.narrative == "The bell rings once.\n\nRain on the bell."
    drops = [
        record for record in caplog.records if "dropping this beat's text" in record.getMessage()
    ]
    assert len(drops) == 1
    assert "wet wool" not in ts.narrative
    print("PASS NPC reaction drops after grounding spends the move's retry; no cut text shown")


def test_craft_retries_at_twice_its_ceiling(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
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
    monkeypatch.setattr(fake, "SCRIPT", [CUT, (FULL, "end_turn")])
    with bound("fake"):
        CraftBeat().run(craft_ts)
    assert tagged(calls) == [1024, 2048]
    assert craft_ts.gs.state.location == "Hallway"
    assert "You push the door open into the hallway." in craft_ts.narrative
    print("PASS craft retries at 1024-to-2048, shows complete narration and applies delta")


def test_custom_world_retries_at_twice_the_override(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    gs = engine.create_save("mist", CharacterProfile(name="Walker"), GameConfig(pack_id="testpack"))
    gs.state.tutorial_stage = 2
    ts = world_step_state(gs)
    ts.max_tokens_override = 2500
    monkeypatch.setattr(fake, "SCRIPT", [CUT, (FULL, "end_turn")])
    with bound("fake"):
        DMWorldStepBeat().run(ts)
    assert tagged(calls) == [2500, 5000]
    assert ts.gs.state.location == "Hallway"
    print("PASS custom world retries at twice the unchanged 2500-token override")


def test_empty_act_retry_uses_the_last_good_ceiling(pack, monkeypatch, calls):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    monkeypatch.setattr(
        fake,
        "SCRIPT",
        [
            CUT,
            ("[NARRATIVE][/NARRATIVE][STATE_DELTA]{}[/STATE_DELTA]", "end_turn"),
            (ACT, "end_turn"),
        ],
    )
    with bound("fake"):
        DMActBeat().run(ts)
    assert tagged(calls) == [460, 920, 920]
    assert ts.reply_ceiling == 920
    assert "The bell rings once." in ts.narrative
    print("PASS empty-act retry reuses last complete ceiling of 920 after the truncation retry")
