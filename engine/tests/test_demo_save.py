"""examples/demo-save.json loads, round-trips through undo, and is reproducible."""

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.game import memory, state
from conftest import sid
from tests import make_demo_save

DEMO = Path("/app/examples/demo-save.json")
# float16 embeddings differ by a few ulps between environments; same bound as test_store
VEC_ATOL = 1e-3


def _file() -> dict:
    return json.loads(DEMO.read_text(encoding="ascii"))


def test_demo_save_parses_and_loads():
    data = _file()
    assert data["format"] == "coherence-save" and data["v"] == 1
    assert data["save"]["config"]["pack_id"] == "neon_rain"
    assert state.doc_to_save(data["save"]) is not None


def test_demo_save_undo_round_trip(clock):
    from app.main import build_app

    c = TestClient(build_app("hosted"))
    r = c.post(
        "/api/game/undo",
        json={"save": _file()["save"]},
        headers={
            "X-Provider": "anthropic",
            "X-Provider-Key": "demo-not-a-key",
            "X-Coherence-Session": sid(),
        },
    )
    assert r.status_code == 200
    assert "save" in r.json()


def _stable(data: dict) -> dict:
    save = {k: v for k, v in data["save"].items() if k not in ("created", "updated")}
    return {**data, "save": save}


def _split(data: dict) -> tuple[dict, list[str]]:
    out = json.loads(json.dumps(_stable(data)))
    vecs = []
    for key in ("memory", "physics_cache"):
        for entry in out["save"][key]:
            vecs.append(entry.pop("v"))
    return out, vecs


def _same_save(new: dict, old: dict) -> None:
    a, va = _split(new)
    b, vb = _split(old)
    assert a == b
    assert len(va) == len(vb)
    for i, (p, q) in enumerate(zip(va, vb)):
        x, y = memory._decode(p), memory._decode(q)
        assert x.shape == y.shape == (memory.VECTOR_SIZE,)
        assert np.allclose(x, y, rtol=0, atol=VEC_ATOL), (
            f"vector {i}: max diff {np.abs(x - y).max()}"
        )


def test_demo_save_matches_regeneration(clock):
    _same_save(make_demo_save.build(), _file())


def test_demo_compare_fails_on_text_change():
    changed = _file()
    changed["save"]["memory"][0]["text"] += "x"
    with pytest.raises(AssertionError):
        _same_save(changed, _file())


def test_demo_compare_fails_on_vector_change():
    changed = _file()
    vec = memory._decode(changed["save"]["memory"][0]["v"])
    changed["save"]["memory"][0]["v"] = memory._encode(vec + 0.01)
    with pytest.raises(AssertionError):
        _same_save(changed, _file())
    changed["save"]["memory"][0]["v"] = memory._encode(-vec)
    with pytest.raises(AssertionError):
        _same_save(changed, _file())


def test_demo_compare_tolerates_float16_jitter():
    changed = _file()
    vec = memory._decode(changed["save"]["memory"][0]["v"]).astype(np.float16)
    changed["save"]["memory"][0]["v"] = memory._encode(np.nextafter(vec, np.float16(np.inf)))
    _same_save(changed, _file())
