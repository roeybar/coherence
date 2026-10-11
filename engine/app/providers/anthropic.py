"""Anthropic Messages API. cache_control markers pass through unchanged; no sampling parameters are sent."""

import logging
from typing import Any, Optional

from pydantic import SecretStr

from app.llm_client import _Block, _Message, _Usage
from app.providers.base import Provider

URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

log = logging.getLogger(__name__)

# Per-model request fields, keyed by exact id and merged into the body.
# Ids not listed get none of these fields.
MODEL_PARAMS: dict[str, dict] = {
    "claude-haiku-5-5": {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}},
    "claude-opus-5-5": {"output_config": {"effort": "low"}},
}

_unknown_warned = False

# stop_reason values passed on as they are; any other value is reported as "other".
STOP_REASONS = (
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
)


def _int(v) -> int:
    return int(v or 0)


def _warn_unknown_once(model: str) -> None:
    global _unknown_warned
    if _unknown_warned or not model.startswith("claude-") or model in MODEL_PARAMS:
        return
    _unknown_warned = True
    log.warning("anthropic: model %s has no request settings; sending none", model)


class AnthropicProvider(Provider):
    name = "anthropic"

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
        msgs = list(messages)
        # No caller sends a prefill (supports_prefill is False), but a
        # trailing assistant turn is dropped in case one does.
        if msgs and msgs[-1].get("role") == "assistant":
            msgs = msgs[:-1]
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": msgs,
        }
        if system:
            body["system"] = system
        _warn_unknown_once(model)
        body.update(MODEL_PARAMS.get(model, {}))
        headers = {
            "x-api-key": key.get_secret_value(),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        payload = self._post(URL, headers, body)
        try:
            blocks = [
                _Block(text=c.get("text", ""))
                for c in (payload.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            u = payload.get("usage") or {}
            usage = _Usage(
                input_tokens=_int(u.get("input_tokens")),
                output_tokens=_int(u.get("output_tokens")),
                cache_read_input_tokens=_int(u.get("cache_read_input_tokens")),
                cache_creation_input_tokens=_int(u.get("cache_creation_input_tokens")),
            )
        except (AttributeError, TypeError, ValueError):
            usage = None
        if usage is None:
            # Raised outside the except so no upstream text rides along.
            raise self._error(502, 200)
        if not blocks and payload.get("stop_reason") == "max_tokens":
            log.warning("anthropic: %s stopped at max_tokens=%d with no text", model, max_tokens)
        stop = payload.get("stop_reason")
        stop_reason = stop if stop in STOP_REASONS else ("other" if stop else "")
        return _Message(
            content=blocks,
            usage=usage,
            stop_reason=stop_reason,
            truncated=stop in ("max_tokens", "model_context_window_exceeded"),
        )
