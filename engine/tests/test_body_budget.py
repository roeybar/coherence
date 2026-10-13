import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import main
from app.config import settings

CAP = 100
BUSY = {"detail": {"error": "busy", "provider": None}}
TOO_LARGE = {"detail": {"error": "body_too_large", "provider": None}}


def scope(length=None):
    headers = [(b"content-type", b"application/json")]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return {"type": "http", "method": "POST", "path": "/api/game/turn", "headers": headers}


def feeder(parts, finished=True):
    index = 0

    async def receive():
        nonlocal index
        if index < len(parts):
            part = parts[index]
            index += 1
            return {
                "type": "http.request",
                "body": part,
                "more_body": index < len(parts) or not finished,
            }
        return {"type": "http.disconnect"}

    return receive


class Sent:
    def __init__(self):
        self.msgs = []

    async def __call__(self, msg):
        self.msgs.append(msg)

    @property
    def status(self):
        return self.msgs[0]["status"]

    def json(self):
        return json.loads(
            b"".join(m.get("body", b"") for m in self.msgs if m["type"] == "http.response.body")
        )


class Inner:
    def __init__(self, budget, gate=None, boom=False):
        self.budget = budget
        self.gate = gate
        self.boom = boom
        self.bodies = []
        self.used_inside = []

    async def __call__(self, scope, receive, send):
        self.bodies.append((await receive())["body"])
        self.used_inside.append(self.budget.used)
        if self.gate is not None:
            await self.gate.wait()
        if self.boom:
            raise RuntimeError("boom")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


async def call(app, length, parts, finished=True):
    sent = Sent()
    await app(scope(length), feeder(parts, finished), sent)
    return sent


async def reached(inner):
    for _ in range(100):
        if inner.bodies:
            return
        await asyncio.sleep(0)
    raise AssertionError("the request never reached the app")


def test_the_middleware_order_is_internal_busy_bodycap():
    assert [m.cls for m in main.build_app("selfhost").user_middleware] == [
        main.Internal,
        main.Busy,
        main.BodyCap,
    ]
    print("PASS middleware order: internal, busy, body cap")


def test_content_length_is_held_until_the_app_answers():
    budget = main.InFlight()
    inner = Inner(budget)
    app = main.BodyCap(inner, cap=CAP, budget=budget)
    response = asyncio.run(call(app, 40, [b"x" * 40]))
    assert response.status == 200
    assert inner.bodies == [b"x" * 40]
    assert inner.used_inside == [40]
    assert budget.peak == 40
    assert budget.used == 0
    print("PASS content length held until the app answers")


def test_a_second_body_over_the_budget_is_busy_until_the_first_answers():
    budget = main.InFlight()

    async def scenario():
        gate = asyncio.Event()
        held = Inner(budget, gate=gate)
        app1 = main.BodyCap(held, cap=CAP, budget=budget)
        task1 = asyncio.create_task(call(app1, 70, [b"a" * 70]))
        await reached(held)
        assert budget.used == 70
        other = Inner(budget)
        app2 = main.BodyCap(other, cap=CAP, budget=budget)
        refused = await call(app2, 31, [b"b" * 31])
        fits = await call(app2, 30, [b"c" * 30])
        assert budget.used == 70
        gate.set()
        done = await task1
        again = await call(app2, 31, [b"b" * 31])
        assert refused.status == 503 and refused.json() == BUSY
        assert fits.status == done.status == again.status == 200
        assert other.bodies == [b"c" * 30, b"b" * 31]
        assert other.used_inside == [100, 31]
        assert budget.peak == 100

    asyncio.run(scenario())
    assert budget.used == 0
    print("PASS overlapping bodies: busy over budget, admitted at cap and after release")


def test_a_body_over_the_cap_is_413_idle_or_busy():
    budget = main.InFlight()
    inner = Inner(budget)
    app = main.BodyCap(inner, cap=CAP, budget=budget)
    for length, parts in ((CAP + 1, [b"x" * (CAP + 1)]), (None, [b"x" * 60, b"x" * 41])):
        response = asyncio.run(call(app, length, parts))
        assert response.status == 413 and response.json() == TOO_LARGE
    assert budget.take(70, CAP)
    try:
        for length in (CAP + 1, None):
            response = asyncio.run(call(app, length, [b"x" * (CAP + 1)]))
            assert response.status == 413 and response.json() == TOO_LARGE
    finally:
        budget.give(70)
    assert inner.bodies == []
    assert budget.used == 0
    print("PASS an oversized body is 413 idle or busy")


def test_the_bytes_are_released_when_the_app_raises():
    budget = main.InFlight()
    inner = Inner(budget, boom=True)
    app = main.BodyCap(inner, cap=CAP, budget=budget)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(call(app, 50, [b"x" * 50]))
    assert budget.used == 0
    print("PASS body bytes released after an app exception")


def test_the_bytes_are_released_when_the_request_is_cancelled():
    budget = main.InFlight()

    async def scenario():
        inner = Inner(budget, gate=asyncio.Event())
        app = main.BodyCap(inner, cap=CAP, budget=budget)
        task = asyncio.create_task(call(app, 60, [b"x" * 60]))
        await reached(inner)
        assert budget.used == 60
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert budget.used == 0
    print("PASS body bytes released after cancellation")


def test_the_bytes_are_released_when_the_client_disconnects():
    budget = main.InFlight()
    inner = Inner(budget)
    app = main.BodyCap(inner, cap=CAP, budget=budget)
    first = asyncio.run(call(app, 80, [b"x" * 30], finished=False))
    second = asyncio.run(call(app, None, [b"y" * 30], finished=False))
    assert first.status == second.status == 200
    assert inner.bodies == [b"x" * 30, b"y" * 30]
    assert inner.used_inside == [80, 30]
    assert budget.used == 0
    print("PASS body bytes released after client disconnect")


def test_zero_byte_requests_are_never_refused():
    budget = main.InFlight()
    inner = Inner(budget)
    app = main.BodyCap(inner, cap=CAP, budget=budget)
    assert budget.take(CAP, CAP)
    try:
        for length, parts in ((0, [b""]), (None, [b""]), (None, [])):
            assert asyncio.run(call(app, length, parts)).status == 200
        assert inner.used_inside == [CAP, CAP, CAP]
        response = asyncio.run(call(app, 1, [b"x"]))
        assert response.status == 503 and response.json() == BUSY
    finally:
        budget.give(CAP)
    assert budget.used == 0
    print("PASS zero bytes admitted at full budget; one byte is busy")


def test_a_body_without_length_is_counted_as_it_arrives():
    budget = main.InFlight()
    inner = Inner(budget)
    app = main.BodyCap(inner, cap=CAP, budget=budget)
    response = asyncio.run(call(app, None, [b"a" * 20, b"b" * 20, b"c" * 20]))
    assert response.status == 200
    assert inner.bodies == [b"a" * 20 + b"b" * 20 + b"c" * 20]
    assert inner.used_inside == [60]
    assert budget.take(50, CAP)
    try:
        response = asyncio.run(call(app, None, [b"a" * 30, b"b" * 30]))
        assert response.status == 503 and response.json() == BUSY
        assert budget.used == 50
    finally:
        budget.give(50)
    assert len(inner.bodies) == 1
    assert budget.used == 0
    print("PASS body without length charged as it arrives and released on busy")


def test_the_counter_is_exact_across_threads():
    budget = main.InFlight()

    def worker():
        for _ in range(2000):
            if budget.take(7, 10_000):
                budget.give(7)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(worker) for _ in range(8)]
        for future in futures:
            future.result()
    assert budget.used == 0
    assert 0 < budget.peak <= 10_000
    print("PASS counter exact across 8 threads with 2000 reservations each")


def test_stress_mixed_bodies_through_the_app(request, monkeypatch):
    budget = main.InFlight()
    monkeypatch.setattr(main, "inflight", budget)
    monkeypatch.setattr(main, "slots", threading.BoundedSemaphore(8))
    monkeypatch.setattr(settings, "body_cap_bytes", 1_000_000)
    client = request.getfixturevalue("selfhost")
    pads = (None, 200, 300_000, 600_000, 950_000, 1_000_000)

    def post(i):
        pad = pads[i % 6]
        if pad is None:
            return 0, client.get("/api/health").status_code, None
        body = json.dumps({"save": {}, "pad": "x" * pad})
        response = client.post(
            "/api/game/undo", content=body, headers={"Content-Type": "application/json"}
        )
        return len(body), response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(post, i) for i in range(48)]
        results = [future.result(timeout=120) for future in futures]
    ok = busy = too_large = 0
    for size, status, body in results:
        if size == 0:
            assert status == 200
            ok += 1
        elif size > 1_000_000:
            assert status == 413 and body == TOO_LARGE
            too_large += 1
        elif status == 503:
            assert body == BUSY
            busy += 1
        else:
            assert status == 200, (size, status, body)
            ok += 1
    assert too_large == 8
    assert budget.used == 0
    assert 0 < budget.peak <= 1_000_000
    print(
        f"PASS 48 mixed requests, 8 workers: ok={ok} busy={busy} "
        f"too_large=8 peak={budget.peak} cap=1000000"
    )


def test_hosted_bodycap_ignores_the_budget():
    budget = main.InFlight()
    assert budget.take(CAP, CAP)
    inner = Inner(budget)
    app = main.BodyCap(inner, cap=CAP, budget=budget, hosted=True)
    try:
        assert asyncio.run(call(app, 40, [b"x" * 40])).status == 200
        assert inner.used_inside == [CAP]
        assert asyncio.run(call(app, None, [b"y" * 40])).status == 200
        response = asyncio.run(call(app, CAP + 1, [b"x" * (CAP + 1)]))
        assert response.status == 413 and response.json() == TOO_LARGE
        assert budget.used == CAP
    finally:
        budget.give(CAP)
    assert budget.used == 0
    print("PASS hosted body cap ignores the budget and still refuses oversized bodies")


def test_the_budget_is_off_in_hosted_mode_only(request, monkeypatch):
    monkeypatch.setattr(settings, "body_cap_bytes", 1_000_000)
    budget = main.InFlight()
    monkeypatch.setattr(main, "inflight", budget)
    selfhost = request.getfixturevalue("selfhost")
    hosted = request.getfixturevalue("hosted")
    assert budget.take(1_000_000, 1_000_000)
    try:
        small = json.dumps({"save": {}})
        big = json.dumps({"save": {}, "pad": "x" * 1_000_000})
        headers = {"Content-Type": "application/json"}
        response = hosted.post("/api/game/undo", content=small, headers=headers)
        assert response.status_code == 200, response.text
        response = selfhost.post("/api/game/undo", content=small, headers=headers)
        assert response.status_code == 503 and response.json() == BUSY
        response = hosted.post("/api/game/undo", content=big, headers=headers)
        assert response.status_code == 413 and response.json() == TOO_LARGE
    finally:
        budget.give(1_000_000)
    assert budget.used == 0
    print("PASS hosted answers a small body with the budget full; self-host says busy")
