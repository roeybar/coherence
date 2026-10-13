"""GET /api/packs lists the shipped packs with exactly id, name and description."""

from fastapi.testclient import TestClient

from app.main import build_app

_EXPECTED_KEYS = {"id", "name", "description"}


def _packs():
    r = TestClient(build_app("hosted")).get("/api/packs")
    assert r.status_code == 200, r.text
    return r.json()["packs"]


def test_ships_the_demo_pack():
    assert "neon_rain" in {p["id"] for p in _packs()}


def test_pack_entries_have_exact_keys_and_text():
    packs = _packs()
    assert packs
    for p in packs:
        assert set(p) == _EXPECTED_KEYS, p
        assert isinstance(p["name"], str) and p["name"].strip()
        assert isinstance(p["description"], str) and p["description"].strip()


def test_fixture_root_lists_testpack(selfhost):
    assert [p["id"] for p in selfhost.get("/api/packs").json()["packs"]] == ["testpack"]
