"""Client address, the error envelope, and the in-RAM message and unlock limits.

A message is accepted only if both its session key and its IP key last
accepted at least the interval ago. A rejection changes nothing.
"""

import ipaddress
import math
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Optional

from fastapi import HTTPException, Request

from app.config import settings

clock = time.monotonic

SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def api_error(
    status: int, kind: str, provider: Optional[str] = None, retry_after: Optional[int] = None
) -> HTTPException:
    detail = {"error": kind, "provider": provider}
    headers = None
    if retry_after is not None:
        detail["retry_after"] = retry_after
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(status_code=status, detail=detail, headers=headers)


def _networks():
    return [
        ipaddress.ip_network(c.strip())
        for c in settings.trusted_proxy_cidrs.split(",")
        if c.strip()
    ]


def client_ip(request: Request) -> str:
    """The peer, or the XFF entry TRUSTED_PROXY_HOPS from the right when
    the peer is a trusted proxy."""
    peer = request.client.host if request.client else ""
    try:
        trusted = any(ipaddress.ip_address(peer) in n for n in _networks())
    except ValueError:
        trusted = False
    hops = settings.trusted_proxy_hops
    xff = request.headers.get("x-forwarded-for", "")
    entries = [e.strip() for e in xff.split(",") if e.strip()]
    if trusted and hops >= 1 and len(entries) >= hops:
        return entries[-hops]
    return peer


def ip_bucket(ip: str) -> str:
    """The limiter key for an address: IPv6 by its /64, IPv4 as is."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return ip


class MessageLimiter:
    def __init__(self, interval: float, max_keys: int):
        self.interval = interval
        self.max_keys = max_keys
        self._last: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def check(self, session: Optional[str], ip: str) -> None:
        if self.interval <= 0:
            return
        if not session or not SESSION_RE.match(session):
            raise api_error(400, "bad_session")
        keys = ("s:" + session, "ip:" + ip_bucket(ip))
        with self._lock:
            now = clock()
            while self._last:
                k, t = next(iter(self._last.items()))
                if now - t < self.interval:
                    break
                self._last.popitem(last=False)
            wait = max(
                (self.interval - (now - self._last[k]) for k in keys if k in self._last),
                default=0.0,
            )
            if wait > 0:
                retry = max(1, math.ceil(wait))
                raise api_error(429, "rate_limited", retry_after=retry)
            for k in keys:
                self._last[k] = now
                self._last.move_to_end(k)
            while len(self._last) > self.max_keys:
                self._last.popitem(last=False)

    def size(self) -> int:
        return len(self._last)


class UnlockLimiter:
    WINDOW = 60.0

    def __init__(self, per_minute: int, max_keys: int):
        self.per_minute = per_minute
        self.max_keys = max_keys
        self._hits: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def check(self, ip: str) -> None:
        ip = ip_bucket(ip)
        with self._lock:
            now = clock()
            q = self._hits.setdefault(ip, deque())
            while q and now - q[0] >= self.WINDOW:
                q.popleft()
            if len(q) >= self.per_minute:
                raise api_error(
                    429, "rate_limited", retry_after=max(1, math.ceil(self.WINDOW - (now - q[0])))
                )
            q.append(now)
            self._hits.move_to_end(ip)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)


class ActionLimiter:
    """Requests that make no model call: a sliding window per session key and
    per IP key. A request without a valid session counts against its IP only."""

    WINDOW = 60.0

    def __init__(self, per_minute: int, max_keys: int):
        self.per_minute = per_minute
        self.max_keys = max_keys
        self._hits: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def check(self, session: Optional[str], ip: str) -> None:
        if self.per_minute <= 0:
            return
        keys = ["ip:" + ip_bucket(ip)]
        if session and SESSION_RE.match(session):
            keys.insert(0, "s:" + session)
        with self._lock:
            now = clock()
            queues = []
            for k in keys:
                q = self._hits.setdefault(k, deque())
                while q and now - q[0] >= self.WINDOW:
                    q.popleft()
                self._hits.move_to_end(k)
                queues.append(q)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
            full = [q for q in queues if len(q) >= self.per_minute]
            if full:
                wait = max(self.WINDOW - (now - q[0]) for q in full)
                raise api_error(429, "actions_rate_limited", retry_after=max(1, math.ceil(wait)))
            for q in queues:
                q.append(now)


ACTIONS_PER_MINUTE = 12


def interval_for(mode: str) -> float:
    v = settings.message_interval_seconds
    if v is None:
        v = 15.0 if mode == "hosted" else 0.0
    if mode == "hosted" and v < 15:
        raise RuntimeError("hosted mode needs MESSAGE_INTERVAL_SECONDS of at least 15")
    return v


messages = MessageLimiter(0.0, settings.rate_limit_max_keys)
unlocks = UnlockLimiter(settings.unlock_per_minute, settings.rate_limit_max_keys)
actions = ActionLimiter(0, settings.rate_limit_max_keys)


def configure(mode: str) -> None:
    global messages, unlocks, actions
    messages = MessageLimiter(interval_for(mode), settings.rate_limit_max_keys)
    unlocks = UnlockLimiter(settings.unlock_per_minute, settings.rate_limit_max_keys)
    actions = ActionLimiter(
        ACTIONS_PER_MINUTE if mode == "hosted" else 0, settings.rate_limit_max_keys
    )


def check_message(session: Optional[str], ip: str) -> None:
    messages.check(session, ip)


def check_action(session: Optional[str], ip: str) -> None:
    actions.check(session, ip)
