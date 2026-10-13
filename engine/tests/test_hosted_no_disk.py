"""Runs only in engine-test-hosted: read-only root, tmpfs /tmp, hosted mode."""

import errno
import logging
import os
import re
import subprocess
import sys

import pytest

from conftest import NEW_BODY, sid

KEY = "test-canary-key-7f3a"
TEXT = "canary-player-text-91c2"
SKIP = ("/proc", "/sys", "/dev", "/tmp")


def _walk():
    out = {}
    for root, dirs, files in os.walk("/"):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in SKIP]
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[p] = (st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture(scope="module")
def started():
    assert os.environ.get("COHERENCE_MODE") == "hosted"
    from app import mode

    before = _walk()
    return mode.startup(), before


def test_probe_failed_with_fs_errors(started):
    probe, _ = started
    assert set(probe) == {"/", "/app", "/data"}
    assert all(e == errno.EROFS for e in probe.values())
    for d in probe:
        assert os.statvfs(d).f_flag & os.ST_RDONLY
    from app.mode import tmp_is_tmpfs

    assert tmp_is_tmpfs()


def test_writes_refused():
    from app.mode import HostedWriteRefused

    for p in ("/app/x", "/tmp/../app/x"):
        with pytest.raises(HostedWriteRefused) as ei:
            open(p, "w")
        assert isinstance(ei.value, PermissionError)


def test_cycle_no_disk_no_logs(started, pack, clock, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    from fastapi.testclient import TestClient
    from app import store
    from app.main import app
    from app.game import memory

    c = TestClient(app)
    c.headers.update(
        {"X-Provider": "anthropic", "X-Provider-Key": KEY, "X-Coherence-Session": sid()}
    )
    r = c.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    save = r.json()["save"]
    for i in range(3):
        clock.t += 15
        r = c.post("/api/game/turn", json={"save": save, "player_input": f"{TEXT} step {i}"})
        assert r.status_code == 200, r.text
        save = r.json()["save"]
    assert save["memory"]
    tok = store.bind_save(save)
    try:
        hits = memory.recall("local", f"{TEXT} step 0", top_k=4)
    finally:
        store.unbind_save(tok)
    assert any(TEXT in h.get("content", "") for h in hits)
    assert c.get("/api/keys/status").status_code == 404
    assert c.post("/api/keys/unlock", json={"passphrase": "x"}).status_code == 404
    assert "simple_creds_storage" not in sys.modules and "app.creds" not in sys.modules
    logs = caplog.text + "".join(capfd.readouterr())
    assert KEY not in logs and TEXT not in logs
    assert _walk() == started[1]
    found = subprocess.run(
        ["find", "/tmp", "-type", "f", "-printf", "%p %s\n"], capture_output=True, text=True
    )
    # onnxruntime leaves empty /tmp/mat-debug-<n>.log files; nothing else may appear.
    for line in found.stdout.splitlines():
        path, size = line.rsplit(" ", 1)
        assert re.fullmatch(r"/tmp/mat-debug-[0-9]+\.log", path) and size == "0", line


MODEL_TEXT = "canary-model-text-5d81"
RAISED = "canary-raised-text-a04e"


def _no_canary(caplog, capfd, *canaries):
    logs = caplog.text + "".join(capfd.readouterr())
    for canary in canaries:
        assert canary not in logs
    for rec in caplog.records:
        assert rec.args is None and rec.exc_info is None


def _hosted_client(clock):
    from fastapi.testclient import TestClient
    from app.main import build_app

    c = TestClient(build_app("hosted"), raise_server_exceptions=False)
    c.headers.update(
        {"X-Provider": "anthropic", "X-Provider-Key": KEY, "X-Coherence-Session": sid()}
    )
    return c


def test_bad_int_field_not_logged(started, pack, clock, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    c = _hosted_client(clock)
    save = c.post("/api/game/new", json=NEW_BODY).json()["save"]
    save["state"].setdefault("npcs", {})["mira"] = {
        "first_seen_turn": TEXT,
        "last_seen_turn": TEXT,
        "witnessed": [TEXT] * 5,
    }
    clock.t += 15
    r = c.post("/api/game/turn", json={"save": save, "player_input": "I look around"})
    assert r.status_code == 200, r.text
    assert any(rec.getMessage() == "ValueError" for rec in caplog.records)
    _no_canary(caplog, capfd, TEXT, KEY)


def test_non_json_model_text_not_logged(started, pack, clock, caplog, capfd, monkeypatch):
    from app.providers import fake

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(fake, "RULES", [("", "{not json " + MODEL_TEXT)])
    c = _hosted_client(clock)
    save = c.post("/api/game/new", json=NEW_BODY).json()["save"]
    clock.t += 15
    c.post("/api/game/turn", json={"save": save, "player_input": "I check the door"})
    _no_canary(caplog, capfd, MODEL_TEXT, KEY)


def test_handler_error_is_internal_and_unlogged(started, clock, caplog, capfd):
    from fastapi.testclient import TestClient
    from app.main import build_app

    caplog.set_level(logging.DEBUG)
    app = build_app("hosted")

    @app.post("/api/game/boom")
    def boom():
        raise ValueError(RAISED)

    r = TestClient(app, raise_server_exceptions=False).post("/api/game/boom")
    assert r.status_code == 500
    assert r.json() == {"detail": {"error": "internal", "provider": None}}
    assert any(rec.getMessage() == "ValueError" for rec in caplog.records)
    _no_canary(caplog, capfd, RAISED)
