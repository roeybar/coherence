"""Moderation runs before any model call that sees player text.

Each blocked case must return 451 with the expected category and leave the
fake provider's call record unchanged.
"""

import pytest

from conftest import NEW_BODY, turn_body
from app.game import moderation
from app.providers import fake

ALWAYS = "zzalwayscanary"
PACK = "zzpackcanary"


@pytest.fixture(autouse=True)
def _fresh_lists():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


def _calls() -> int:
    return len(fake.seen_key_hashes)


def _assert_blocked(r, before, category="pack_always", error="content_blocked"):
    assert r.status_code == 451, r.text
    assert r.json()["detail"] == {"error": error, "provider": None, "category": category}
    assert _calls() == before


def _played(client) -> dict:
    r = client.post("/api/game/turn", json=turn_body(client))
    assert r.status_code == 200, r.text
    return r.json()["save"]


def test_plain_blocked_move_makes_no_model_call(selfhost):
    body = {**turn_body(selfhost), "player_input": f"I shout {ALWAYS}"}
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/turn", json=body), before)


def test_blocked_time_skip_makes_no_model_call(selfhost):
    from app.game.timeskip import detect_timeskip

    move = f"skip 3 years and {ALWAYS}"
    assert detect_timeskip(move)
    body = {**turn_body(selfhost), "player_input": move}
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/turn", json=body), before)


def test_blocked_reframe_makes_no_model_call(selfhost):
    from app.game.reframe_gate import detect_reframe

    move = f"the war is over and {ALWAYS}"
    assert detect_reframe(move)
    body = {**turn_body(selfhost), "player_input": move}
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/turn", json=body), before)


def test_regenerate_with_blocked_last_input_makes_no_model_call(selfhost):
    save = _played(selfhost)
    assert save["history"][-2]["role"] == "user"
    save["history"][-2]["content_en"] = f"I shout {ALWAYS}"
    before = _calls()
    _assert_blocked(
        selfhost.post("/api/game/regenerate", json={"save": save}), before, error="save_blocked"
    )


def test_regenerate_path_refuses_blocked_feedback(selfhost):
    # Proves the regenerate path refuses blocked feedback, not which check
    # catches it: the replayed request is filtered again in run_turn.
    save = _played(selfhost)
    before = _calls()
    r = selfhost.post("/api/game/regenerate", json={"save": save, "feedback": ALWAYS})
    _assert_blocked(r, before)


def test_welcome_regen_path_refuses_a_blocked_original_input(selfhost):
    # Proves the regenerate path refuses it, not which check catches it.
    save = _played(selfhost)
    save["history"][-1]["intervention"] = {
        "kind": "welcome_grace_doubt",
        "matched_text": "nothing happens",
        "original_input": f"I shout {ALWAYS}",
    }
    before = _calls()
    r = selfhost.post("/api/game/welcome-intervention/regen", json={"save": save})
    _assert_blocked(r, before, error="save_blocked")


def test_blocked_meta_question_makes_no_model_call(selfhost):
    save = _played(selfhost)
    before = _calls()
    r = selfhost.post("/api/game/meta", json={"save": save, "question": f"why {ALWAYS}"})
    _assert_blocked(r, before)


def test_blocked_profile_note_makes_no_model_call(selfhost):
    body = {
        "name": "t",
        "profile": {"notes": f"I carry {ALWAYS}"},
        "config": {"pack_id": "testpack", "world_type": "custom"},
    }
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/new", json=body), before)


def test_blocked_world_hint_makes_no_model_call(selfhost):
    body = {
        **NEW_BODY,
        "world_hint": f"a city of {ALWAYS}",
        "config": {"pack_id": "testpack", "world_type": "custom"},
    }
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/new", json=body), before)


def test_allowed_moves_still_reach_the_model(selfhost):
    before = _calls()
    save = _played(selfhost)
    assert _calls() > before
    r = selfhost.post("/api/game/regenerate", json={"save": save, "feedback": "more rain"})
    assert r.status_code == 200, r.text
    r = selfhost.post("/api/game/meta", json={"save": r.json()["save"], "question": "where am I"})
    assert r.status_code == 200, r.text


def test_regenerate_checks_the_request_the_model_sees(selfhost):
    # A run of three letters becomes two only in the turn-zero rewrite.
    save = _played(selfhost)
    before = _calls()
    r = selfhost.post("/api/game/regenerate", json={"save": save, "feedback": "z" + ALWAYS})
    _assert_blocked(r, before)


def test_blocked_profile_in_the_save_makes_no_model_call(selfhost):
    body = turn_body(selfhost)
    body["save"]["profile"]["notes"] = f"I carry {ALWAYS}"
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/turn", json=body), before, error="save_blocked")


def test_blocked_history_on_rewind_makes_no_model_call(selfhost):
    save = _played(selfhost)
    save["checkpoint_state"] = dict(save["state"])
    save["checkpoint_history"] = save["history"][:1]
    save["checkpoint_turn"] = 0
    save["history"][-2]["content_en"] = f"I shout {ALWAYS}"
    before = _calls()
    _assert_blocked(
        selfhost.post("/api/game/rewind", json={"save": save}), before, error="save_blocked"
    )


def test_pack_list_move_is_refused_with_category_pack(selfhost):
    body = {**turn_body(selfhost), "player_input": f"I shout {PACK}"}
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/turn", json=body), before, category="pack")


def test_discarded_memory_does_not_block_a_new_game(selfhost):
    # A freeform world drops one_memory, so it is not filtered.
    body = {
        "name": "t",
        "config": {
            "pack_id": "testpack",
            "world_type": "freeform",
            "one_memory": f"I keep {ALWAYS}",
        },
    }
    r = selfhost.post("/api/game/new", json=body)
    assert r.status_code == 200, r.text


def test_non_english_save_is_filtered_against_the_english_lists(selfhost):
    body = {**turn_body(selfhost), "player_input": f"I shout {ALWAYS}"}
    body["save"]["config"]["locale"] = "he"
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/turn", json=body), before)


def test_blocked_profile_name_on_meta_makes_no_model_call(selfhost):
    save = _played(selfhost)
    save["profile"]["name"] = f"Ash {ALWAYS}"
    before = _calls()
    r = selfhost.post("/api/game/meta", json={"save": save, "question": "where am I"})
    _assert_blocked(r, before, error="save_blocked")


def test_blocked_memory_in_a_custom_world_makes_no_model_call(selfhost):
    body = {
        "name": "t",
        "config": {"pack_id": "testpack", "world_type": "custom", "one_memory": f"I keep {ALWAYS}"},
    }
    before = _calls()
    _assert_blocked(selfhost.post("/api/game/new", json=body), before)
