"""Model-escalation consent.

Default behaviour: every turn, including ability-classed ones, runs on
the fast model tier. The strong tier is for cases where the fast tier's
output is unusable, and the player's provider may charge more for it, so
the player is asked first.

This module:
  - mints HMAC tokens that bind a (save_id, turn_index, move) to the
    player's consent to use the strong tier on that specific move,
  - verifies tokens server-side without any database lookup,
  - resolves the per-config policy ("ask" | "auto" | "never") into
    a routing decision; auto asks like ask,
  - raises EscalationPending so the API layer can short-circuit and
    return a {kind: "escalation_consent_needed", ...} payload.

Tokens are stateless (HMAC) and short-lived (5 min). HMAC key is
derived from settings.escalation_hmac_key (ESCALATION_HMAC_KEY, or
random bytes generated at startup).
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.config import settings
from app.game.gate_base import TurnGate


def _key() -> bytes:
    raw = settings.escalation_hmac_key
    # Domain-separate so a leak of one HMAC use can't be replayed at another.
    return hashlib.sha256(b"coherence:escalation:" + raw).digest()


# token mint / verify
TOKEN_TTL_S = 300  # 5 minutes - long enough to read the modal, short
# enough that a tab left open overnight can't escalate
# a turn the player has long forgotten about.


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def mint(*, save_id: str, turn_index: int, move: str = "", now: Optional[int] = None) -> str:
    """Return a token binding (save_id, turn_index, move) to consent for
    the strong tier. Bound by exp; only the holder of the HMAC key can
    forge a valid one."""
    payload = json.dumps(
        {
            "s": save_id,
            "t": int(turn_index),
            "m": move,
            "exp": (int(now or time.time()) + TOKEN_TTL_S),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64u(payload)}.{_b64u(sig)}"


@dataclass
class _Verdict:
    ok: bool
    reason: str = ""


def verify(
    token: str, *, save_id: str, turn_index: int, move: str = "", now: Optional[int] = None
) -> _Verdict:
    """Return ok=True iff the token is well-formed, signed by us, not
    expired, and bound to the same (save_id, turn_index, move). All other
    paths return ok=False with a short reason for telemetry."""
    if not token or "." not in token:
        return _Verdict(False, "malformed")
    try:
        body, sig = token.split(".", 1)
        payload = _b64u_decode(body)
        expected = hmac.new(_key(), payload, hashlib.sha256).digest()
        provided = _b64u_decode(sig)
        if not hmac.compare_digest(expected, provided):
            return _Verdict(False, "bad_signature")
        d = json.loads(payload.decode())
    except Exception:
        return _Verdict(False, "decode_error")

    n = int(now or time.time())
    if int(d.get("exp", 0)) < n:
        return _Verdict(False, "expired")
    if d.get("s") != save_id:
        return _Verdict(False, "save_mismatch")
    if int(d.get("t", -1)) != int(turn_index):
        return _Verdict(False, "turn_mismatch")
    if d.get("m") != move:
        return _Verdict(False, "move_mismatch")
    return _Verdict(True)


# policy resolver
ALLOWED_POLICIES = ("ask", "auto", "never")


class EscalationPending(TurnGate):
    """Raised when a move needs the strong tier without player consent.
    Carries a fresh consent token bound to (save_id, turn_index, move).
    One token covers every strong site of a move until it expires TOKEN_TTL_S
    after minting; a gate chain that outlasts it re-asks once with a fresh token,
    and the client re-sends with the newest token.
    The API layer should catch
    this and return a 200 payload that prompts the frontend modal."""

    def __init__(self, *, save_id: str, turn_index: int, reason: str = "ability", move: str = ""):
        super().__init__("escalation_consent_needed")
        self.save_id = save_id
        self.turn_index = turn_index
        self.reason = reason
        self.consent_token = mint(save_id=save_id, turn_index=turn_index, move=move)

    def to_payload(self) -> dict:
        return {
            "kind": "escalation_consent_needed",
            "consent_token": self.consent_token,
            "turn_index": self.turn_index,
            "ttl_s": TOKEN_TTL_S,
            "reason": self.reason,
            "message": (
                "This move looks like an ability action. The stronger model "
                "set for your provider can work out how it plays out, and "
                "your provider may charge more for it. Continue to use it "
                "for this move, or Keep it small to stay on the usual model."
            )
            if self.reason == "ability"
            else (
                "The usual model could not settle the physics of this move. "
                "The stronger model set for your provider can work it out, "
                "and your provider may charge more for it. Continue to use "
                "it for this move, or Keep it small to stay on the usual model."
            ),
        }


@dataclass
class StrongGate:
    policy: str
    allowed: bool
    consent_token: Optional[str]
    declined: bool
    regenerated: bool
    save_id: str
    turn_index: int
    move: str | Callable[[], str]

    def need(self, reason: str) -> bool:
        if not self.allowed or self.regenerated or self.declined or self.policy == "never":
            return False
        if callable(self.move):
            self.move = self.move()
        if verify(
            self.consent_token, save_id=self.save_id, turn_index=self.turn_index, move=self.move
        ).ok:
            return True
        raise EscalationPending(
            save_id=self.save_id, turn_index=self.turn_index, reason=reason, move=self.move
        )


def normalise_policy(value: str) -> str:
    """Coerce arbitrary input to one of ALLOWED_POLICIES; default 'ask'."""
    v = (value or "").strip().lower()
    return v if v in ALLOWED_POLICIES else "ask"


def default_policy_for_tier(tier: str) -> str:
    """Default policy applied when a save's config has no explicit
    setting yet. The tier values 'pro' and 'turbo' default to 'auto';
    every other tier value defaults to 'ask'. auto asks like ask."""
    t = (tier or "").strip().lower()
    if t in ("pro", "turbo"):
        return "auto"
    return "ask"


def tier_allows_sonnet(tier: str) -> bool:
    """Hard tier gate for the strong tier:
    only the tier values 'pro' and 'turbo' may use the strong tier.
    Any other tier value cannot, whatever the save config or
    the consent modal say.

    Why hard-gate rather than just defaulting policy='never'?
    Because per-save `ability_escalation: 'auto'` would otherwise
    override the tier default. The hard gate runs before policy
    resolution, so the per-save setting cannot bypass it."""
    return (tier or "").strip().lower() in ("pro", "turbo")
