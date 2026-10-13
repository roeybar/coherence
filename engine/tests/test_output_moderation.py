"""Every model reply, and every record parsed from one, is checked before any
caller uses it; a hit is 451 output_blocked with no save in the response."""

import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import SecretStr

from conftest import NEW_BODY, turn_body
from app import store
from app.game import engine, moderation, physics
from app.game import (
    campaign_clock,
    craft_gate,
    dice_gate,
    parser,
    physicalisation,
    timeskip,
    world_rule_gate,
)
from app.game.beats import builtin
from app.game.meta_questions import _PREFIXES
from app.game.moderation import OutputBlocked
from app.llm_client import call_messages
from app.providers import fake, registry

PACK = "zzpackcanary"
BS = chr(92)
WANT = {"error": "output_blocked", "provider": None, "category": "pack"}


@pytest.fixture(autouse=True)
def _fresh_lists():
    moderation.reset_pack_cache()
    yield
    moderation.reset_pack_cache()


def _calls() -> int:
    return len(fake.seen_key_hashes)


def _blocked(r):
    assert r.status_code == 451, r.text
    assert r.json()["detail"] == WANT
    assert "save" not in r.json() and PACK not in r.text


def _played(client) -> dict:
    r = client.post("/api/game/turn", json=turn_body(client))
    assert r.status_code == 200, r.text
    return r.json()["save"]


def _reply(monkeypatch, text):
    monkeypatch.setattr(fake, "DEFAULT_REPLY", text)


def test_blocked_replies_on_every_model_route(selfhost, monkeypatch):
    save = _played(selfhost)
    rewind = dict(
        save,
        checkpoint_state=dict(save["state"]),
        checkpoint_history=save["history"][:1],
        checkpoint_turn=0,
    )
    for n, reply in enumerate((f"The clerk says {PACK}.", "The clerk says " + ".".join(PACK))):
        _reply(monkeypatch, reply)
        cases = [
            ("/api/game/turn", {"save": save, "player_input": "I wait"}),
            ("/api/game/turn", {"save": save, "player_input": "skip 3 years"}),
            ("/api/game/regenerate", {"save": save}),
            ("/api/game/meta", {"save": save, "question": "what is this place"}),
            ("/api/game/rewind", {"save": rewind}),
            (
                "/api/game/new",
                {"name": "c", "config": {"pack_id": "testpack", "world_type": "custom"}},
            ),
        ]
        for path, body in cases:
            _blocked(selfhost.post(path, json=body))
        print("PASS output_routes reply=%d n=%d" % (n, len(cases)))


def _physics_case(monkeypatch, reply=None, record=None):
    if reply is not None:
        monkeypatch.setattr(fake, "RULES", [("hard science-fiction physicist", reply)] + fake.RULES)
    if record is not None:
        monkeypatch.setattr(physics, "_safe_parse_json", lambda raw: record)
    doc = {"items": [], "memory": [], "physics_cache": []}
    tok = store.bind_save(doc)
    ptok = registry.bind_request("fake", SecretStr("canary-x"))
    mtok = moderation.bind_pack("testpack")
    before = _calls()
    try:
        with pytest.raises(OutputBlocked):
            physics.physicalise("I conjure a spark")
    finally:
        moderation.unbind_pack(mtok)
        registry.unbind_request(ptok)
        store.unbind_save(tok)
    assert doc["physics_cache"] == []
    assert _calls() == before + 1


def test_parsed_physics_records_are_checked(pack, monkeypatch):
    _physics_case(monkeypatch, reply="{'narrative_hint': '" + PACK[:5] + "' '" + PACK[5:] + "'}")
    _physics_case(
        monkeypatch, reply="{'narrative_hint': '" + PACK[:5] + BS + chr(10) + PACK[5:] + "'}"
    )
    _physics_case(monkeypatch, reply="{'narrative_hint': b'" + PACK[:5] + "' b'" + PACK[5:] + "'}")
    _physics_case(monkeypatch, record={"narrative_hint": PACK})
    print("PASS physics_records n=4")


def test_parser_sites_check_decoded_values(selfhost, monkeypatch):
    save = _played(selfhost)
    esc = "".join(BS + "u%04x" % ord(c) for c in PACK)
    cases = [
        'Rain. [STATE_DELTA]{"location": "' + esc + BS + BS + 'u0073"}[/STATE_DELTA]',
        'Rain. [STATE_DELTA]{"location": "' + PACK[:5] + BS + "b" + PACK[5:] + '"}[/STATE_DELTA]',
        'Rain. [STATE_DELTA]{"location":"zzp4ckc4n4ry"}[/STATE_DELTA]',
    ]
    at_block = []
    init = OutputBlocked.__init__

    def record(self, category):
        at_block.append(_calls())
        init(self, category)

    monkeypatch.setattr(OutputBlocked, "__init__", record)
    for n, reply in enumerate(cases):
        _reply(monkeypatch, reply)
        at_block.clear()
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
        assert r.status_code == 451 and r.json()["detail"]["error"] == "output_blocked", n
        assert at_block and _calls() == at_block[0], n
    print("PASS parser_sites n=%d" % len(cases))


def test_premise_paths(selfhost, monkeypatch):
    body = {
        "name": "c",
        "world_hint": "a harbour zzpremisemark",
        "config": {"pack_id": "testpack", "world_type": "custom"},
    }
    monkeypatch.setattr(
        fake,
        "RULES",
        [("zzpremisemark", '{"premise": "' + PACK[:5] + '" "' + PACK[5:] + '"}')] + fake.RULES,
    )
    _blocked(selfhost.post("/api/game/new", json=body))
    escaped = "".join(BS + "u%04x" % ord(c) for c in PACK)
    monkeypatch.setattr(
        engine, "generate_world_premise", lambda hint, config=None: '{"premise": "' + escaped + '"}'
    )
    before = _calls()
    _blocked(selfhost.post("/api/game/new", json=body))
    assert _calls() == before
    print("PASS premise n=2")


def test_call_messages_raises_through_fallbacks(pack):
    _reply_rules = [("zzunitmark", "say " + PACK)]
    old = fake.RULES
    fake.RULES = _reply_rules + old
    ptok = registry.bind_request("fake", SecretStr("canary-x"))
    mtok = moderation.bind_pack("testpack")
    try:
        with pytest.raises(OutputBlocked):
            try:
                call_messages(
                    model="m", system="s", messages=[{"role": "user", "content": "zzunitmark"}]
                )
            except Exception:
                pass
    finally:
        fake.RULES = old
        moderation.unbind_pack(mtok)
        registry.unbind_request(ptok)
    print("PASS unit_call_messages")


def test_egress_and_clean_cases(selfhost, monkeypatch):
    save = _played(selfhost)
    for prefix in sorted(_PREFIXES):
        r = selfhost.post(
            "/api/game/turn", json={"save": save, "player_input": prefix + " where am I"}
        )
        assert r.status_code == 200, prefix
    r = selfhost.post(
        "/api/game/turn", json={"save": save, "player_input": "I wait", "reframe_confirm": "cancel"}
    )
    assert r.status_code == 200
    orig = engine._substitute_memory
    monkeypatch.setattr(engine, "_substitute_memory", lambda opening, memory: opening + " " + PACK)
    _blocked(selfhost.post("/api/game/new", json=NEW_BODY))
    monkeypatch.setattr(engine, "_substitute_memory", orig)
    print("PASS egress_and_e_set prefixes=%d" % len(_PREFIXES))


def test_hosted_turn_reply_is_blocked(hosted, monkeypatch):
    body = turn_body(hosted)
    _reply(monkeypatch, f"The clerk says {PACK}.")
    _blocked(hosted.post("/api/game/turn", json=body))
    print("PASS hosted_output_blocked")


# each check site, reached with a canary only that site's parsed value holds

MARK = "zzsitemark"
CLASSIFY = "Classify the player action. Respond with exactly one word"


def _site_blocks(monkeypatch):
    """(model calls so far, file, function) for every block raised by a check
    site, recorded where the block is made."""
    seen = []
    init = OutputBlocked.__init__

    def record(self, category):
        f = sys._getframe(2)
        seen.append((_calls(), Path(f.f_code.co_filename).name, f.f_code.co_name))
        init(self, category)

    monkeypatch.setattr(OutputBlocked, "__init__", record)
    return seen


def _decoder(monkeypatch, module, record):
    """The module's JSON parser hands back record for a reply holding MARK."""
    real = json.loads

    def loads(s, *a, **kw):
        value = real(s, *a, **kw)
        return record if isinstance(value, dict) and MARK in value else value

    stub = types.SimpleNamespace(
        dumps=json.dumps,
        dump=json.dump,
        load=json.load,
        JSONDecodeError=json.JSONDecodeError,
        loads=loads,
    )
    monkeypatch.setattr(module, "json", stub)


def _premise(monkeypatch, pick):
    """engine._parse_world_premise returns a canary record for the call pick
    selects (by calling function and count); every other call is real."""
    real = engine._parse_world_premise
    calls = []

    def parse(raw):
        value = real(raw)
        if MARK in (raw or ""):
            calls.append(sys._getframe(1).f_code.co_name)
            if pick(calls[-1], calls.count(calls[-1])):
                return {"premise": PACK, "opening_scene": PACK, "name": PACK}
        return value

    monkeypatch.setattr(engine, "_parse_world_premise", parse)
    monkeypatch.setattr(
        engine,
        "generate_world_premise",
        lambda hint, config=None: '{"' + MARK + '": 1, "premise": "a harbour"}',
    )


def _blocked_at(r, seen, where):
    assert r.status_code == 451 and r.json()["detail"]["error"] == "output_blocked", (where, r.text)
    assert seen and seen[0][1:] == where, (where, seen)
    assert seen[0][0] == _calls(), where


CUSTOM = {"name": "c", "config": {"pack_id": "testpack", "world_type": "custom"}}


def _plain_classifier(m):
    """The world-rule classifier reads call_llm_plain's result as text, but
    the helper returns (text, usage), so its parse never runs; hand it the
    text so its check site is reached."""
    from app.game import llm

    real = llm.call_llm_plain

    def plain(*a, **kw):
        out = real(*a, **kw)
        return out[0] if kw.get("system") is world_rule_gate._CLASSIFIER_SYSTEM else out

    m.setattr(llm, "call_llm_plain", plain)


def test_every_check_site_blocks_its_own_value(selfhost, monkeypatch):
    save = _played(selfhost)
    n = 0

    def turn(text="I wait", **kw):
        return selfhost.post("/api/game/turn", json={"save": save, "player_input": text, **kw})

    reply = '{"' + MARK + '": 1}'
    cases = [
        (
            parser,
            {"location": PACK},
            "Rain. [STATE_DELTA]" + reply + "[/STATE_DELTA]",
            {},
            ("parser.py", "parse_response"),
        ),
        (
            campaign_clock,
            {"event": PACK, "suggested_moves": []},
            reply,
            {},
            ("campaign_clock.py", "estimate_event_duration"),
        ),
        (
            craft_gate,
            {"dc": 10, "target": PACK},
            reply,
            {"craft_roll": 10},
            ("craft_gate.py", "assess_craft_dc"),
        ),
        (
            dice_gate,
            {"dc": 10, "skill": PACK},
            reply,
            {"dice_roll": 10},
            ("dice_gate.py", "assess_dice_dc"),
        ),
        (
            timeskip,
            {"interrupt": True, "reason": PACK},
            reply,
            {"text": "skip 3 years"},
            ("timeskip.py", "_parse_interrupt_json"),
        ),
        (
            world_rule_gate,
            {"is_paradigm_shift": True, "rule_text": PACK},
            reply,
            {"text": "Swords are banned in the city", "plain": True},
            ("world_rule_gate.py", "classify_rule_shift"),
        ),
        (
            physicalisation,
            {"scope": PACK, "confidence": "high"},
            reply,
            {"text": "I conjure a spark", "ability": True},
            ("physicalisation.py", "_parse_classifier_json"),
        ),
    ]
    for module, record, text, kw, where in cases:
        with monkeypatch.context() as m:
            seen = _site_blocks(m)
            _decoder(m, module, record)
            m.setattr(fake, "DEFAULT_REPLY", text)
            if kw.pop("plain", False):
                _plain_classifier(m)
            if kw.pop("ability", False):
                m.setattr(fake, "RULES", [(CLASSIFY, "ability")] + fake.RULES)
            _blocked_at(turn(kw.pop("text", "I wait"), **kw), seen, where)
            n += 1
    with monkeypatch.context() as m:
        seen = _site_blocks(m)
        real = builtin.parse_response

        def parse(raw, keys):
            result = real(raw, keys)
            if MARK in raw:
                result.delta = {"location": PACK}
            return result

        m.setattr(builtin, "parse_response", parse)
        m.setattr(fake, "DEFAULT_REPLY", "Rain falls on " + MARK + ".")
        _blocked_at(turn(), seen, ("builtin.py", "run"))
        n += 1
    for pick, where in (
        (lambda f, k: f == "create_save" and k == 1, "create_save"),
        (lambda f, k: f == "create_save" and k == 3, "create_save"),
    ):
        with monkeypatch.context() as m:
            seen = _site_blocks(m)
            _premise(m, pick)
            _blocked_at(selfhost.post("/api/game/new", json=CUSTOM), seen, ("engine.py", where))
            n += 1
    with monkeypatch.context() as m:
        _premise(m, lambda f, k: False)
        custom = selfhost.post("/api/game/new", json=CUSTOM)
        assert custom.status_code == 200, custom.text
        seen = _site_blocks(m)
        _premise(m, lambda f, k: f == "build_system_blocks")
        r = selfhost.post(
            "/api/game/turn", json={"save": custom.json()["save"], "player_input": "I wait"}
        )
        _blocked_at(r, seen, ("prompts.py", "build_system_blocks"))
        n += 1
    print("PASS check_sites n=%d" % n)


def test_text_cut_after_the_last_check_is_checked(selfhost, monkeypatch):
    save = _played(selfhost)
    n = 0
    # a bracket tag the narrative clean-up removes joins the halves
    with monkeypatch.context() as m:
        seen = _site_blocks(m)
        m.setattr(fake, "DEFAULT_REPLY", "Rain falls. " + PACK[:5] + "[AB]" + PACK[5:] + " glows.")
        r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
        _blocked_at(r, seen, ("builtin.py", "run"))
        n += 1
    # a 60-character cut drops the letters that kept the word apart
    target = ("a" * (60 - len(PACK) - 1)) + " " + PACK + "qq more words"
    assert target[:60].endswith(" " + PACK) and not moderation.check_input(target).blocked
    with monkeypatch.context() as m:
        seen = _site_blocks(m)
        m.setattr(
            fake,
            "RULES",
            [("Assess the crafting attempt", json.dumps({"dc": 10, "target": target}))]
            + fake.RULES,
        )
        r = selfhost.post(
            "/api/game/turn",
            json={"save": save, "player_input": "I forge a blade", "craft_roll": 12},
        )
        _blocked_at(r, seen, ("craft_gate.py", "assess_craft_dc"))
        n += 1
    # the same for a 40-character cut of a roll's skill
    skill = ("b" * (40 - len(PACK) - 1)) + " " + PACK + "qq"
    with monkeypatch.context() as m:
        seen = _site_blocks(m)
        m.setattr(
            fake,
            "RULES",
            [
                (
                    "Determine what dice roll this action requires",
                    json.dumps({"dc": 10, "skill": skill}),
                )
            ]
            + fake.RULES,
        )
        r = selfhost.post(
            "/api/game/turn",
            json={"save": save, "player_input": "I punch the guard", "dice_roll": 12},
        )
        _blocked_at(r, seen, ("dice_gate.py", "assess_dice_dc"))
        n += 1
    print("PASS cut_after_check n=%d" % n)
