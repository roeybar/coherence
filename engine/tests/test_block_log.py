"""A refused request (a move or a new game) says why in the call log: one line with the block category and
either the engine frames that raised it or, for the final egress check, the fixed site game:egress, and none of
the story text. The 451 answer itself is unchanged.
No test in test_output_moderation.py drives ListsUnavailable through the API, so that
branch has no case here."""

from conftest import NEW_BODY, turn_body
from app.game import engine
from app.providers import fake
from test_output_moderation import PACK, WANT


def block_lines(call_lines):
    return [line for line in call_lines if line.startswith("output_blocked ")]


def test_refused_turn_logs_category_and_site(selfhost, monkeypatch, call_lines):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    body = turn_body(selfhost)
    monkeypatch.setattr(fake, "DEFAULT_REPLY", f"The clerk says {PACK}.")
    call_lines.clear()
    r = selfhost.post("/api/game/turn", json=body)
    assert r.status_code == 451, r.text
    assert r.json()["detail"] == WANT
    lines = block_lines(call_lines)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("output_blocked category=pack site="), line
    site = line.split("site=")[1]
    pairs = site.split(">")
    assert 1 <= len(pairs) <= 3, site
    assert all(p.count(":") == 1 and all(p.split(":")) for p in pairs), site
    assert PACK not in line and "clerk" not in line, line
    print(f"PASS a refused turn logs one line with its category and site: {line}")


def test_egress_refusal_logs_category_and_site(selfhost, monkeypatch, call_lines):
    # the reply passes every model-reply check; the opening in the returned save hits
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(engine, "_substitute_memory", lambda opening, memory: opening + " " + PACK)
    call_lines.clear()
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 451, r.text
    assert r.json()["detail"] == WANT
    lines = block_lines(call_lines)
    assert len(lines) == 1, lines
    line = lines[0]
    assert line.startswith("output_blocked category="), line
    assert "site=game:egress" in line, line
    assert PACK not in line and "clerk" not in line, line
    print(f"PASS an egress refusal of a new game logs one line with its category and site: {line}")
