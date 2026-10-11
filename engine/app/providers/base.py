"""Provider interface and the shared HTTP error mapping."""

from typing import Any, Optional

import httpx
from pydantic import SecretStr

from app.llm_client import ProviderError, _Message

TIER_FAST = "tier:fast"
TIER_STRONG = "tier:strong"

TIMEOUT_S = 120.0


def kind_for_status(status: int) -> str:
    if status in (401, 403):
        return "byok_rejected"
    if status == 429:
        return "provider_rate_limited"
    return "provider_unavailable"


class Provider:
    name: str = ""

    def __init__(self, transport: Optional[httpx.BaseTransport] = None):
        # Tests swap in an httpx.MockTransport here.
        self.transport = transport

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
        raise NotImplementedError

    def _error(self, status_code: int, status: Optional[int]) -> ProviderError:
        kind = kind_for_status(status) if status is not None else "provider_unavailable"
        return ProviderError(status_code, {"kind": kind, "provider": self.name, "status": status})

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        """POST and return the JSON body. Any failure raises ProviderError
        built from the status alone; the response body is dropped.
        Errors are raised outside the except blocks so no __context__
        carries upstream text."""
        r = None
        try:
            with httpx.Client(timeout=TIMEOUT_S, transport=self.transport) as c:
                r = c.post(url, json=body, headers=headers)
        except httpx.HTTPError:
            pass
        if r is None:
            raise self._error(503, None)
        if r.status_code >= 400:
            raise self._error(r.status_code, r.status_code)
        payload = None
        try:
            payload = r.json()
        except ValueError:
            pass
        if not isinstance(payload, dict):
            raise self._error(502, r.status_code)
        return payload
