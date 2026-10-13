"""Hosted startup checks, run in the writable engine-test container."""

import logging

import pytest

from app import mode


def test_hosted_startup_refuses_on_writable_root():
    assert not mode._hooked
    with pytest.raises(RuntimeError, match="read-only"):
        mode.startup()
    assert not mode._hooked


def test_tmp_must_be_tmpfs(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay rw 0 0\noverlay /tmp overlay rw 0 0\n")
    assert not mode.tmp_is_tmpfs(str(mounts))
    mounts.write_text("overlay / overlay ro 0 0\ntmpfs /tmp tmpfs rw,size=65536k 0 0\n")
    assert mode.tmp_is_tmpfs(str(mounts))
    assert not mode.tmp_is_tmpfs(str(tmp_path / "absent"))


def _line(record):
    return logging.Formatter(mode.LOG_FORMAT).format(mode.redact(record))


def test_redact_keeps_type_or_event_only():
    secret = "canary-redact-3e1b"
    try:
        int(secret)
    except ValueError as e:
        err = e
    rec = logging.LogRecord("game", logging.WARNING, __file__, 1, "rebake failed: %s", (err,), None)
    assert _line(rec) == "game WARNING ValueError"
    rec = logging.LogRecord("game", logging.INFO, __file__, 1, "escalating for %r", (secret,), None)
    assert _line(rec) == "game INFO escalating for %r"
    rec = logging.LogRecord(
        "game",
        logging.ERROR,
        __file__,
        1,
        "parse failed",
        None,
        (type(err), err, err.__traceback__),
    )
    line = _line(rec)
    assert line == "game ERROR ValueError" and secret not in line
