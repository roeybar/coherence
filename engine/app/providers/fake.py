"""Deterministic test provider. Reachable only with ENGINE_FAKE_PROVIDER=1.

It reports Anthropic capabilities, so the cache path is the
one exercised. Each call records sha256(key) in seen_key_hashes.
A call whose system text asks for the tagged format gets a reply that has
no format tags of its own wrapped in that format. Scripted replies are sent as written.
"""

import hashlib
import threading
from typing import Any, Optional, Union

from pydantic import SecretStr

from app.llm_client import _Block, _Message, _Usage
from app.providers.base import Provider

# A reply cut at the ceiling, as seen in play; reported with stop max_tokens.
CUT_REPLY = (
    "[NARRATIVE]\nYou unlatch the frosted-glass door and push it open into the "
    "hallway, damp with the smell of wet wool and burned"
)

# (marker, reply): the first rule whose marker is in the system text or the
# last user message picks the reply. A tuple marker needs every one of its
# strings; the tuple rules come first.
RULES: list[tuple[Union[str, tuple[str, ...]], str]] = [
    (("Classify the player action. Respond with exactly one word", "zzability"), "ability"),
    (
        ("You classify the SCOPE of a player's attempted physical alteration", "zzability"),
        '{"scope": "local", "confidence": "low"}',
    ),
    (
        ("You are the second-opinion scope classifier", "zzability"),
        '{"scope": "local", "confidence": "high"}',
    ),
    ("Classify the player action. Respond with exactly one word", "standard"),
    # The hook repair's request carries the move, so its rule comes before the move's marker.
    ("BEAT FOCUS - hook", 'Mara leans closer. "So who sent you?"'),
    ("zzhookmiss", 'Mara sets the glass down. "Not yet."'),
    ("zzreplycanary", "A clerk reads 123-45-6789 aloud from the ledger."),
    ("zzcutreply", CUT_REPLY),
]
DEFAULT_REPLY = "The street hums around you. Nothing answers yet."

# Test-only: (reply, stop_reason) pairs handed out in order to tagged calls,
# before any rule. Empty (the default) changes nothing.
SCRIPT: list[tuple[str, str]] = []
TAGGED = "[/STATE_DELTA]"
FORMAT_TAGS = ("[NARRATIVE]", "[/NARRATIVE]", "[STATE_DELTA]")

seen_key_hashes: list[str] = []
_lock = threading.Lock()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def reply_for(system: Any, messages: list) -> str:
    users = [m for m in messages if m.get("role") == "user"]
    haystack = _text(system) + "\n" + (_text(users[-1].get("content")) if users else "")
    for marker, reply in RULES:
        parts = marker if isinstance(marker, tuple) else (marker,)
        if all(part in haystack for part in parts):
            return reply
    return DEFAULT_REPLY


class FakeProvider(Provider):
    name = "fake"

    def complete(
        self,
        *,
        key: SecretStr,
        model: str,
        system: Any,
        messages: list,
        max_tokens: int,
        temperature: Optional[float],
    ) -> _Message:
        digest = hashlib.sha256(key.get_secret_value().encode("utf-8")).hexdigest()
        tagged = TAGGED in _text(system)
        with _lock:
            seen_key_hashes.append(digest)
            scripted = SCRIPT.pop(0) if tagged and SCRIPT else None
        if scripted is not None:
            text, stop = scripted
        else:
            text = reply_for(system, messages)
            stop = "max_tokens" if text == CUT_REPLY else "end_turn"
        in_chars = len(_text(system)) + sum(len(_text(m.get("content"))) for m in messages)
        usage = _Usage(input_tokens=in_chars // 4 + 1, output_tokens=len(text) // 4 + 1)
        if tagged and scripted is None and not any(tag in text for tag in FORMAT_TAGS):
            text = f"[NARRATIVE]\n{text}\n[/NARRATIVE]\n[STATE_DELTA]\n{{}}\n[/STATE_DELTA]"
        return _Message(
            content=[_Block(text=text)],
            usage=usage,
            stop_reason=stop,
            truncated=stop == "max_tokens",
        )
