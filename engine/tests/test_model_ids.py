"""Anthropic model ids in the shipped env file and the fields sent per id."""

import json
import re
from pathlib import Path

import httpx
from pydantic import SecretStr

import app
from app.config import settings
from app.llm_client import call_messages
from app.providers import registry

# Older ids and their dated suffix; the class keeps the literal out of the tree.
OLD_ID = re.compile(r"claude-(haiku|sonnet|opus)-[0-4]|2025100[1]|Haiku 4\.5")


def test_env_example_ids():
    assert settings.anthropic_model_fast == "claude-haiku-5-5"
    assert settings.anthropic_model_strong == "claude-opus-5-5"
    print("PASS model ids: fast=claude-haiku-5-5 strong=claude-opus-5-5")


def test_unknown_id_gets_no_thinking_or_temperature(monkeypatch):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "usage": {}})

    monkeypatch.setattr(
        registry.get_provider("anthropic"), "transport", httpx.MockTransport(handler)
    )
    tok = registry.bind_request("anthropic", SecretStr("test-key-ids"))
    try:
        call_messages(
            model="claude-test-unknown",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            temperature=0.0,
        )
    finally:
        registry.unbind_request(tok)
    assert len(sent) == 1
    assert sent[0]["model"] == "claude-test-unknown"
    assert "thinking" not in sent[0]
    assert "temperature" not in sent[0]
    print("PASS unknown id: no thinking, no temperature")


def test_no_old_ids_in_engine_source():
    root = Path(app.__file__).parent
    files = sorted(root.rglob("*.py"))
    hits = [
        f"{p.relative_to(root)}:{n}"
        for p in files
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if OLD_ID.search(line)
    ]
    assert files
    assert hits == []
    print("PASS no old model ids in %d engine source files" % len(files))
