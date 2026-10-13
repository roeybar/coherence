"""A time skip that stops early tells the player on a plain line: a failed period call
shows a fixed message and the exception goes to the log only; the classifier's own
reason is still shown as the system notice."""

import logging

from app.game import engine, timeskip
from app.llm_client import ProviderError
from test_suggested_moves import _make_gs

NOTICE = "--- attention needed ---\n"
OK_REPLY = "The harvest fails.\nEVENTS: harvest fails"


def raiser(exc, ok=0):
    """A period stub: the first `ok` calls return a reply, the next one raises `exc`."""
    seen = []

    def advance(state, unit, index, total):
        seen.append(index)
        if len(seen) <= ok:
            return OK_REPLY, {}
        raise exc

    return advance


def calm(monkeypatch):
    monkeypatch.setattr(
        timeskip, "classify_interrupt", lambda s, c: ({"interrupt": False, "reason": ""}, None)
    )


def test_error_stop_plain_line(monkeypatch, caplog):
    calm(monkeypatch)
    monkeypatch.setattr(
        timeskip, "call_period_advance", raiser(RuntimeError("zzboom upstream 503"), ok=1)
    )
    skip = {"span_n": 3, "span_unit": "year"}
    with caplog.at_level(logging.WARNING, logger="timeskip"):
        result = timeskip.run_timeskip({}, skip=skip)
    assert result["interrupted"]
    assert result["completed_periods"] == 1
    assert result["interrupt_reason"] == timeskip._SKIP_ERROR_LINE
    out = timeskip.compose_timeskip_response(skip, result)
    assert "The harvest fails." in out
    assert out.endswith(NOTICE + timeskip._SKIP_ERROR_LINE)
    assert "zzboom" not in out and "engine error" not in out
    assert "zzboom" not in caplog.text and "RuntimeError" in caplog.text
    assert timeskip._SKIP_ERROR_LINE.isascii()
    print("PASS a failed period ends the skip on a plain line and logs the exception type only")


def test_classifier_stop_keeps_reason(monkeypatch):
    monkeypatch.setattr(timeskip, "call_period_advance", raiser(RuntimeError("unused"), ok=3))
    monkeypatch.setattr(
        timeskip,
        "classify_interrupt",
        lambda s, c: ({"interrupt": True, "reason": "A rival claims the farm."}, None),
    )
    skip = {"span_n": 3, "span_unit": "year"}
    out = timeskip.compose_timeskip_response(skip, timeskip.run_timeskip({}, skip=skip))
    assert out.endswith(NOTICE + "A rival claims the farm.")
    print("PASS the classifier's own reason is still shown as the attention notice")


def test_engine_turn_hides_the_error(monkeypatch):
    from app.game import campaign_clock

    monkeypatch.setattr(timeskip, "call_period_advance", raiser(RuntimeError("zzboom")))
    monkeypatch.setattr(campaign_clock, "estimate_event_duration", lambda *a, **k: None)
    gs = _make_gs()
    out = engine._run_timeskip_turn(gs, "s", "skip 2 years", {"span_n": 2, "span_unit": "year"})
    line = timeskip._SKIP_ERROR_LINE
    shown = {
        "narrative": out["narrative"],
        "interrupt_reason": out["timeskip_meta"]["interrupt_reason"],
        "dm_notes": gs.state.dm_notes[-1],
        "history": gs.history[-1]["content"],
    }
    for where, text in shown.items():
        assert "zzboom" not in text, where
        assert line in text, where
    print("PASS the engine turn shows the plain line in the narrative, notes, meta and history")


def test_error_stop_under_volume(monkeypatch, caplog):
    calm(monkeypatch)
    secret = "test-canary-key-timeskip"
    errors = [
        RuntimeError('he said "no" {brace} }{ ' + secret),
        ValueError("{0} {hint} %s"),
        KeyError("k"),
        TimeoutError("read timed out"),
        ProviderError(503, {"kind": "upstream"}),
    ]
    runs = 0
    for exc in errors:
        for span_n in (1, 2, 5):
            for idx in range(span_n):
                for _ in range(5):
                    caplog.clear()
                    monkeypatch.setattr(timeskip, "call_period_advance", raiser(exc, ok=idx))
                    skip = {"span_n": span_n, "span_unit": "year"}
                    with caplog.at_level(logging.WARNING, logger="timeskip"):
                        result = timeskip.run_timeskip({}, skip=skip)
                    assert result["completed_periods"] == idx, (exc, span_n, idx)
                    out = timeskip.compose_timeskip_response(skip, result)
                    if str(exc):
                        assert str(exc) not in out, (exc, span_n, idx)
                    assert secret not in out and "{brace}" not in out
                    assert out.endswith(timeskip._SKIP_ERROR_LINE)
                    assert secret not in caplog.text and type(exc).__name__ in caplog.text
                    if isinstance(exc, ProviderError):
                        assert "kind=upstream" in caplog.text
                    runs += 1
    print(
        f"PASS {runs} failed-period skips end on the plain line with the exception unseen "
        "and the log holds the exception type and provider kind only"
    )


def test_classifier_error_logs_type_only(monkeypatch, caplog):
    secret = "test-canary-key-timeskip"

    def boom(*args, **kwargs):
        raise RuntimeError("zzclassifier " + secret)

    monkeypatch.setattr(timeskip, "call_llm_plain", boom)
    with caplog.at_level(logging.WARNING, logger="timeskip"):
        verdict, usage = timeskip.classify_interrupt("The harvest fails.", ["a rival appears"])
    assert verdict["interrupt"] is False and usage is None
    assert secret not in caplog.text and secret not in verdict["reason"]
    assert "zzclassifier" not in caplog.text and "zzclassifier" not in verdict["reason"]
    assert "RuntimeError" in caplog.text and "RuntimeError" in verdict["reason"]
    print("PASS a failed classifier call logs and returns the exception type only")
