import re

import pytest
from pydantic import SecretStr

from app.config import settings
from app.game import moderation
from app.game.parser import parse_response
from app.llm_client import ProviderError, call_messages
from app.providers import fake, registry
from test_providers import KEY, Recorder


LINE = re.compile(
    r"^llm_call site=\S+ provider=\S+ model=\S+ max_tokens=\d+ stop=\S+ in=\d+ out=\d+ cache_read=\d+ cache_write=\d+ ms=\d+$"
)


@pytest.mark.parametrize(
    "stop,expected,truncated",
    [
        ("max_tokens", "max_tokens", True),
        ("model_context_window_exceeded", "model_context_window_exceeded", True),
        ("end_turn", "end_turn", False),
        ("zz-unknown", "other", False),
        (None, "", False),
    ],
)
def test_anthropic_stop_reason_reported(bound, stop, expected, truncated):
    payload = {
        "content": [{"type": "text", "text": "[NARRATIVE]cut"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    if stop is not None:
        payload["stop_reason"] = stop
    bound("anthropic", Recorder(payload=payload))
    msg = call_messages(
        model="tier:fast", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=50
    )
    assert (msg.stop_reason, msg.truncated) == (expected, truncated)
    assert [b.text for b in msg.content] == ["[NARRATIVE]cut"]
    print(f"PASS anthropic stop_reason={stop}: reported={expected}, truncated={truncated}")


@pytest.mark.parametrize("name", ["openai", "xai", "gemini"])
@pytest.mark.parametrize("finish,truncated", [("length", True), ("stop", False), ("", False)])
def test_openai_compat_finish_reason_reported(bound, name, finish, truncated):
    choice = {"message": {"content": "[NARRATIVE]cut"}}
    if finish:
        choice["finish_reason"] = finish
    payload = {"choices": [choice], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    bound(name, Recorder(payload=payload))
    msg = call_messages(
        model="tier:fast", system="s", messages=[{"role": "user", "content": "x"}], max_tokens=50
    )
    assert (msg.stop_reason, msg.truncated) == (finish, truncated)
    assert [b.text for b in msg.content] == ["[NARRATIVE]cut"]
    print(f"PASS {name} finish_reason={finish}: truncated={truncated}")


def test_fake_scripts_and_formats(pack, monkeypatch):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(fake, "SCRIPT", [])
    provider_token = registry.bind_request("fake", SecretStr(KEY))
    pack_token = moderation.bind_pack("testpack")
    try:
        msg = call_messages(model="tier:fast", system="s", messages=[])
        assert [b.text for b in msg.content] == [fake.DEFAULT_REPLY]
        assert (msg.stop_reason, msg.truncated) == ("end_turn", False)

        msg = call_messages(model="tier:fast", system="x [/STATE_DELTA]", messages=[])
        assert [b.text for b in msg.content] == [
            "[NARRATIVE]\n"
            + fake.DEFAULT_REPLY
            + "\n[/NARRATIVE]\n[STATE_DELTA]\n{}\n[/STATE_DELTA]",
        ]
        assert msg.usage.output_tokens == len(fake.DEFAULT_REPLY) // 4 + 1

        monkeypatch.setattr(fake, "SCRIPT", [(fake.CUT_REPLY, "max_tokens")])
        call_messages(model="tier:fast", system="s", messages=[])
        assert len(fake.SCRIPT) == 1
        msg = call_messages(model="tier:fast", system="x [/STATE_DELTA]", messages=[])
        assert [b.text for b in msg.content] == [fake.CUT_REPLY]
        assert (msg.stop_reason, msg.truncated) == ("max_tokens", True)
        assert fake.SCRIPT == []

        msg = call_messages(
            model="tier:fast",
            system="x [/STATE_DELTA]",
            messages=[{"role": "user", "content": "zzcutreply"}],
        )
        assert [b.text for b in msg.content] == [fake.CUT_REPLY]
        assert fake.CUT_REPLY == (
            "[NARRATIVE]\nYou unlatch the frosted-glass door and push it open into the "
            "hallway, damp with the smell of wet wool and burned"
        )
    finally:
        moderation.unbind_pack(pack_token)
        registry.unbind_request(provider_token)
    print(
        "PASS fake: plain unchanged, tagged wrapped, usage unchanged, script tagged-only, cut unwrapped"
    )


def test_parser_flags_cut_replies():
    full = (
        "[NARRATIVE]\nYou push the door open into the hallway.\n[/NARRATIVE]\n"
        '[STATE_DELTA]\n{"location": "Hallway"}\n[/STATE_DELTA]'
    )
    for text, flags in [
        (fake.CUT_REPLY, (False, False)),
        ('[NARRATIVE]\nA.\n[/NARRATIVE]\n[STATE_DELTA]\n{"loc', (True, False)),
        (full, (True, True)),
        ("Plain prose.", (False, False)),
    ]:
        result = parse_response(text, {"location"})
        assert (result.found_narrative, result.found_delta) == flags
        if text == fake.CUT_REPLY:
            assert result.narrative == fake.CUT_REPLY[len("[NARRATIVE]\n") :]
    print("PASS parser: cut narrative, cut delta and plain prose incomplete; closed tags complete")


def test_call_log_line_has_no_content(pack, monkeypatch, call_lines):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    monkeypatch.setattr(
        fake,
        "RULES",
        [
            ("canary-log-prompt-4b1e", "The lamp canary-log-reply-4b1e glows."),
        ]
        + fake.RULES,
    )
    provider_token = registry.bind_request("fake", SecretStr("canary-log-key-4b1e"))
    pack_token = moderation.bind_pack("testpack")
    try:
        msg = call_messages(
            model="tier:fast",
            system="canary-log-system-4b1e",
            messages=[{"role": "user", "content": "canary-log-prompt-4b1e"}],
            max_tokens=77,
        )
    finally:
        moderation.unbind_pack(pack_token)
        registry.unbind_request(provider_token)
    assert len(call_lines) == 1
    assert LINE.fullmatch(call_lines[0])
    assert call_lines[0].startswith(
        f"llm_call site={__name__}:test_call_log_line_has_no_content provider=fake "
        f"model=tier:fast max_tokens=77 stop=end_turn in={msg.usage.input_tokens} "
        f"out={msg.usage.output_tokens} cache_read=0 cache_write=0 ms="
    )
    for canary in ("canary-log-system", "canary-log-prompt", "canary-log-reply", "canary-log-key"):
        assert canary not in "\n".join(call_lines)
    print(
        "PASS one call log line: site, model, ceiling, stop, tokens and duration; no content or key"
    )


def test_failed_call_logs_its_kind(bound, call_lines):
    bound(
        "anthropic",
        Recorder(
            status=503,
            payload={
                "error": {"message": "canary-log-upstream-4b1e"},
            },
        ),
    )
    with pytest.raises(ProviderError):
        call_messages(
            model="tier:fast",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            max_tokens=50,
        )
    assert len(call_lines) == 1
    assert LINE.fullmatch(call_lines[0])
    assert call_lines[0].startswith(
        f"llm_call site={__name__}:test_failed_call_logs_its_kind provider=anthropic "
        f"model={settings.anthropic_model_fast} max_tokens=50 stop=error:provider_unavailable "
        "in=0 out=0 cache_read=0 cache_write=0 ms="
    )
    assert "canary-log-upstream" not in call_lines[0]
    assert KEY not in call_lines[0]
    print(
        "PASS failed call: one line with fixed kind, zero token counts, no upstream content or key"
    )
