"""Model call entry point used by the game modules.

call_messages keeps its keyword signature and the _Message shape the
callers read (content blocks with .text, usage with token counts). Each
call goes to the provider bound to the current request (see
app.providers.registry). ProviderError.detail holds only kind, provider
and status; a provider response body is never kept.
"""

import logging
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class _Block:
    text: str

    def __getattr__(self, k):
        if k == "type":
            return "text"
        raise AttributeError(k)


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Message:
    content: list
    usage: _Usage
    stop_reason: str = ""  # provider stop/finish reason from a fixed set; "" when absent
    truncated: bool = False  # True when the provider says the reply stopped at max_tokens


class ProviderError(Exception):
    def __init__(self, status_code: int, detail: dict):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"provider {status_code}: {detail.get('kind')}")


# One line per provider call: where it came from, the model, the ceiling, how
# it stopped, token counts and time. Never prompt or reply text, never keys.
call_log = logging.getLogger("coherence.llm")
call_log.setLevel(logging.INFO)
call_log.propagate = False
if not call_log.handlers:
    from app.mode import LOG_FORMAT, _Stderr

    _handler = _Stderr()
    _handler.setFormatter(logging.Formatter(LOG_FORMAT))
    call_log.addHandler(_handler)

# Frames that only pass a call through; the logged site is the first frame outside them.
_PASS_THROUGH = frozenset(
    {
        ("app.llm_client", "call_messages"),
        ("app.game.llm", "call_llm_reply"),
        ("app.game.llm", "call_llm_plain"),
        ("app.game.beats.builtin", "_tagged_reply"),
    }
)


def _call_site() -> str:
    frame = sys._getframe(1)
    while (
        frame is not None
        and (frame.f_globals.get("__name__"), frame.f_code.co_name) in _PASS_THROUGH
    ):
        frame = frame.f_back
    if frame is None:
        return "-"
    return f"{frame.f_globals.get('__name__', '-')}:{frame.f_code.co_qualname}"


def _log_call(
    site: str, provider: str, model: str, max_tokens: int, stop: str, usage, started: float
) -> None:
    call_log.info(
        f"llm_call site={site} provider={provider} model={model} max_tokens={max_tokens} "
        f"stop={stop} in={getattr(usage, 'input_tokens', 0)} out={getattr(usage, 'output_tokens', 0)} "
        f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} "
        f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0)} "
        f"ms={int((time.monotonic() - started) * 1000)}"
    )


def call_messages(
    *,
    model: str,
    system: Any,
    messages: list,
    max_tokens: int = 1024,
    temperature: Optional[float] = None,
) -> _Message:
    from app.providers import registry

    name = registry.current_provider()
    key = registry.current_key()
    if not name or key is None:
        raise ProviderError(
            428, {"kind": "byok_required", "provider": name or None, "status": None}
        )
    provider = registry.get_provider(name)
    resolved = registry.resolve_model(name, model)
    site = _call_site()
    started = time.monotonic()
    try:
        msg = provider.complete(
            key=key,
            model=resolved,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except ProviderError as e:
        _log_call(
            site, name, resolved, max_tokens, f"error:{(e.detail or {}).get('kind')}", None, started
        )
        raise
    _log_call(site, name, resolved, max_tokens, msg.stop_reason or "-", msg.usage, started)
    from app.game import moderation

    text = "".join(getattr(b, "text", "") or "" for b in msg.content)
    check = moderation.check_input(text, pack_id=moderation.current_pack(), locale="en")
    if check.blocked:
        raise moderation.OutputBlocked(check.category)
    return msg
