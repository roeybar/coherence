"""OpenAI-compatible chat completions for OpenAI, xAI and Gemini.

The request carries no cache_control: list content is flattened to
strings and the system blocks are joined in order, stable prefix first,
so the providers' automatic prefix caching applies.
"""

from typing import Any, Optional

from pydantic import SecretStr

from app.llm_client import _Block, _Message, _Usage
from app.providers.base import Provider

BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "xai": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

# finish_reason values passed on as they are; any other value is reported as "other".
FINISH_REASONS = ("stop", "length", "content_filter", "tool_calls", "function_call")

# Tagged calls carry this closing tag in their system text (prompts FORMAT block).
TAGGED_MARKER = "[/STATE_DELTA]"
# The model imitates its own untagged history; the last user message restates the format.
FORMAT_REMINDER = (
    "FORMAT REMINDER: put the narration inside [NARRATIVE] and [/NARRATIVE], "
    "then end with [STATE_DELTA] and [/STATE_DELTA] around a JSON object of only "
    "the fields this move changed ({} if nothing changed). Do not restate a "
    "field whose value did not change."
)


def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict) and b.get("type", "text") == "text":
                parts.append(str(b.get("text", "")))
        return "\n\n".join(p for p in parts if p)
    return ""


def _int(v) -> int:
    return int(v or 0)


class OpenAICompatProvider(Provider):
    def __init__(self, name: str, transport=None):
        super().__init__(transport)
        self.name = name
        self.base_url = BASE_URLS[name]

    def build_body(
        self,
        *,
        model: str,
        system: Any,
        messages: list,
        max_tokens: int,
        temperature: Optional[float],
    ) -> dict:
        out = []
        sys_text = _flatten(system) if system else ""
        if sys_text:
            out.append({"role": "system", "content": sys_text})
        msgs = list(messages)
        # No caller sends a prefill here (supports_prefill is False), but a
        # trailing assistant turn is dropped in case one does.
        if msgs and msgs[-1].get("role") == "assistant":
            msgs = msgs[:-1]
        for m in msgs:
            out.append({"role": m.get("role"), "content": _flatten(m.get("content"))})
        if TAGGED_MARKER in sys_text and out and out[-1]["role"] == "user":
            out[-1] = {"role": "user", "content": f"{out[-1]['content']}\n\n{FORMAT_REMINDER}"}
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": out}
        if temperature is not None:
            body["temperature"] = temperature
        return body

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
        body = self.build_body(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        headers = {
            "authorization": f"Bearer {key.get_secret_value()}",
            "content-type": "application/json",
        }
        payload = self._post(f"{self.base_url}/chat/completions", headers, body)
        try:
            choices = payload.get("choices") or []
            text = ""
            finish = None
            if choices:
                text = (choices[0].get("message") or {}).get("content") or ""
                finish = choices[0].get("finish_reason")
            u = payload.get("usage") or {}
            cached = _int((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
            usage = _Usage(
                input_tokens=max(_int(u.get("prompt_tokens")) - cached, 0),
                output_tokens=_int(u.get("completion_tokens")),
                cache_read_input_tokens=cached,
            )
        except (AttributeError, TypeError, ValueError):
            usage = None
        if usage is None:
            # Raised outside the except so no upstream text rides along.
            raise self._error(502, 200)
        stop_reason = finish if finish in FINISH_REASONS else ("other" if finish else "")
        return _Message(
            content=[_Block(text=str(text))],
            usage=usage,
            stop_reason=stop_reason,
            truncated=finish == "length",
        )
