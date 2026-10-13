"""App factory: config checks, hosted startup, routers, the body cap, the
game concurrency limit and the internal-error envelope."""

import logging
import threading

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import mode as mode_mod
from app import ratelimit
from app.config import settings
from app.game import state

log = logging.getLogger(__name__)
# Process-wide: at most this many game requests run at once.
slots = threading.BoundedSemaphore(settings.max_concurrent_requests)


class InFlight:
    """The body bytes held by the requests in flight. take() refuses an amount that
    would take the total over the cap; a zero amount is never refused. The lock is
    needed because the TestClient runs each request on its own thread; under
    uvicorn every caller is on the one event loop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.used = 0
        self.peak = 0

    def take(self, n: int, cap: int) -> bool:
        with self.lock:
            if n and self.used + n > cap:
                return False
            self.used += n
            self.peak = max(self.peak, self.used)
            return True

    def give(self, n: int) -> None:
        with self.lock:
            self.used -= n


# Process-wide (one uvicorn worker): the body bytes of all requests in flight
# never exceed the body cap.
inflight = InFlight()


class Unbudgeted:
    """Hosted mode: no in-flight budget, only the cap. Hosted caps a body at
    16 MiB and runs at most 4 game requests at once (max_concurrent_requests).
    The measured fit, peak about 356 MB + 7.6 x body bytes, gives about
    356 MB + 7.6 x 4 x 16 MiB = about 866 MB, under the 2g mem_limit. Self-host's
    128 MiB cap needs the budget: two near-cap bodies would peak at about
    356 MB + 7.6 x 2 x 128 MiB = about 2.4 GB, over 2g."""

    def take(self, n: int, cap: int) -> bool:
        return True

    def give(self, n: int) -> None:
        pass


UNBUDGETED = Unbudgeted()


def _err(status: int, kind: str) -> JSONResponse:
    return JSONResponse({"detail": {"error": kind, "provider": None}}, status_code=status)


class BodyCap:
    """Refuses a request body over the cap with 413 body_too_large, and one that
    would take the body bytes of all requests in flight over the cap with 503
    busy. A request holds its bytes until the app has answered it. In hosted
    mode only the cap applies (see Unbudgeted)."""

    def __init__(self, app, cap: int, budget=None, hosted=False):
        self.app = app
        self.cap = cap
        self.budget = budget
        self.hosted = hosted

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.hosted:
            budget = UNBUDGETED
        else:
            budget = self.budget if self.budget is not None else inflight
        held = 0
        try:
            length = dict(scope.get("headers") or []).get(b"content-length")
            if length is not None and length.isdigit():
                if int(length) > self.cap:
                    return await _err(413, "body_too_large")(scope, receive, send)
                if not budget.take(int(length), self.cap):
                    return await _err(503, "busy")(scope, receive, send)
                held = int(length)
            chunks, total, more = [], 0, True
            while more:
                msg = await receive()
                if msg["type"] != "http.request":
                    break
                total += len(msg.get("body", b""))
                if total > self.cap:
                    return await _err(413, "body_too_large")(scope, receive, send)
                if total > held:
                    if not budget.take(total - held, self.cap):
                        return await _err(503, "busy")(scope, receive, send)
                    held = total
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
            body = b"".join(chunks)
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        finally:
            budget.give(held)


class Busy:
    """Game requests over the concurrency limit get 503 busy, never a queue."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/game/"):
            return await self.app(scope, receive, send)
        held = slots
        if not held.acquire(blocking=False):
            return await _err(503, "busy")(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        finally:
            held.release()


class Internal:
    """An unhandled error becomes 500 internal; only its type name is logged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def tracked(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, receive, tracked)
        except Exception as exc:
            log.error(type(exc).__name__)
            if started:
                return
            await _err(500, "internal")(scope, receive, send)


async def _validation(request: Request, exc: RequestValidationError):
    return _err(422, "bad_request")


def build_app(mode: str) -> FastAPI:
    ratelimit.configure(mode)
    app = FastAPI()
    app.state.mode = mode
    app.add_exception_handler(RequestValidationError, _validation)
    from app.api import game, meta

    app.include_router(meta.router)
    app.include_router(game.router)
    if mode != "hosted":
        from app.api import keys

        app.include_router(keys.router)
    app.add_middleware(BodyCap, cap=settings.body_cap_bytes, hosted=mode == "hosted")
    app.add_middleware(Busy)
    app.add_middleware(Internal)
    return app


def create_app() -> FastAPI:
    mode = mode_mod.current_mode()
    if state.UNDO_CAP < 1:
        raise RuntimeError("UNDO_CAP must be at least 1")
    ratelimit.interval_for(mode)
    if mode == "hosted":
        mode_mod.startup()
    return build_app(mode)


app = create_app()
