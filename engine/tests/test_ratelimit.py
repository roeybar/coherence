import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import ratelimit
from app.config import settings
from app.main import build_app, create_app
from conftest import NEW_BODY, sid, turn_body

SAVE = {"save": {}}


def _turn(c, **h):
    return c.post("/api/game/turn", json=turn_body(c), headers=h)


def test_interval_window(hosted, clock):
    assert _turn(hosted).status_code == 200
    clock.t += 14.9
    r = _turn(hosted)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    assert r.json() == {"detail": {"error": "rate_limited", "provider": None, "retry_after": 1}}
    clock.t += 0.1
    assert _turn(hosted).status_code == 200


def test_same_ip_two_sessions_and_same_session_two_ips(hosted, clock):
    assert _turn(hosted).status_code == 200
    assert _turn(hosted, **{"X-Coherence-Session": sid()}).status_code == 429
    clock.t += 15
    s = sid()
    c1 = TestClient(hosted.app, client=("192.0.2.88", 1))
    c2 = TestClient(hosted.app, client=("192.0.2.99", 1))
    for c in (c1, c2):
        c.headers.update(hosted.headers)
    assert _turn(c1, **{"X-Coherence-Session": s}).status_code == 200
    assert _turn(c2, **{"X-Coherence-Session": s}).status_code == 429


def test_undo_limited_per_minute_in_hosted(hosted, clock):
    for _ in range(12):
        assert hosted.post("/api/game/undo", json=SAVE).status_code == 200
    r = hosted.post("/api/game/undo", json=SAVE)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert r.json()["detail"]["error"] == "actions_rate_limited"
    clock.t += 60
    assert hosted.post("/api/game/undo", json=SAVE).status_code == 200


def test_undo_not_limited_in_selfhost(selfhost):
    for _ in range(20):
        assert selfhost.post("/api/game/undo", json=SAVE).status_code == 200


def test_missing_session_counts_against_the_ip(hosted, clock):
    h = dict(hosted.headers)
    h.pop("x-coherence-session")
    c = TestClient(hosted.app)
    for _ in range(12):
        assert c.post("/api/game/undo", json=SAVE, headers=h).status_code == 200
    assert c.post("/api/game/undo", json=SAVE, headers=h).status_code == 429
    r = hosted.post("/api/game/undo", json=SAVE, headers={"X-Coherence-Session": sid()})
    assert r.status_code == 429 and r.json()["detail"]["error"] == "actions_rate_limited"


@pytest.mark.parametrize("value", [None, "not-a-uuid", sid().upper()])
def test_bad_session(hosted, value):
    h = dict(hosted.headers)
    h.pop("x-coherence-session")
    if value:
        h["X-Coherence-Session"] = value
    c = TestClient(hosted.app)
    r = c.post("/api/game/turn", json=turn_body(hosted), headers=h)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "bad_session"


def test_no_key_428_uses_no_slot(hosted):
    assert _turn(hosted, **{"X-Provider-Key": ""}).status_code == 428
    assert _turn(hosted).status_code == 200
    r = _turn(hosted, **{"X-Provider-Key": ""})
    assert r.status_code == 428 and r.json()["detail"]["error"] == "byok_required"


def test_hosted_interval_5_refuses(monkeypatch):
    monkeypatch.setenv("COHERENCE_MODE", "hosted")
    monkeypatch.setattr(settings, "message_interval_seconds", 5.0)
    with pytest.raises(RuntimeError):
        create_app()


def test_selfhost_default_unlimited(selfhost):
    for _ in range(20):
        assert (
            selfhost.post(
                "/api/game/new", json=NEW_BODY | {"config": {"pack_id": "testpack"}}
            ).status_code
            == 200
        )


def test_bucket_cap(clock):
    lim = ratelimit.MessageLimiter(15, 1000)
    for i in range(5000):
        lim.check(sid(), "10.0.%d.%d" % (i // 250, i % 250))
    assert lim.size() <= 1000


def _ip_of(peer, xff):
    seen = {}
    app = build_app("selfhost")

    @app.get("/ipcheck")
    def ipcheck(request: ratelimit.Request):
        seen["ip"] = ratelimit.client_ip(request)

    TestClient(app, client=(peer, 5000)).get("/ipcheck", headers={"X-Forwarded-For": xff})
    return seen["ip"]


def test_client_ip():
    assert _ip_of("172.18.0.5", "192.0.2.11") == "192.0.2.11"
    assert _ip_of("172.18.0.5", "192.0.2.22") == "192.0.2.22"
    assert _ip_of("172.18.0.5", "192.0.2.66, 192.0.2.11") == "192.0.2.11"
    assert _ip_of("192.0.2.84", "192.0.2.11") == "192.0.2.84"


def test_client_ip_two_hops_behind_tunnel(monkeypatch, clock):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    a = _ip_of("172.18.0.5", "192.0.2.11, 172.18.0.9")
    b = _ip_of("172.18.0.5", "192.0.2.22, 172.18.0.9")
    assert (a, b) == ("192.0.2.11", "192.0.2.22")
    lim = ratelimit.MessageLimiter(15, 1000)
    lim.check(sid(), a)
    lim.check(sid(), b)
    with pytest.raises(HTTPException):
        lim.check(sid(), a)


def _from_web(hosted, xff):
    c = TestClient(hosted.app, client=("172.18.0.5", 1))
    c.headers.update(hosted.headers)
    c.headers["X-Forwarded-For"] = xff
    return c


def test_hosted_limit_buckets_the_two_shapes_the_web_sends(hosted, monkeypatch):
    # the web sends "<client>, <peer>"; a direct client repeats its own address
    monkeypatch.setattr(settings, "trusted_proxy_hops", 2)
    direct = _from_web(hosted, "203.0.113.7, 203.0.113.7")
    tunnel = _from_web(hosted, "198.51.100.4, 172.18.0.9")
    other = _from_web(hosted, "203.0.113.8, 203.0.113.8")
    for c in (direct, tunnel, other):
        assert _turn(c, **{"X-Coherence-Session": sid()}).status_code == 200
    for c in (direct, tunnel):
        assert _turn(c, **{"X-Coherence-Session": sid()}).status_code == 429
    edge = _from_web(hosted, "198.51.100.5, 172.18.0.9")
    for _ in range(12):
        r = edge.post("/api/game/undo", json=SAVE, headers={"X-Coherence-Session": sid()})
        assert r.status_code == 200
    r = edge.post("/api/game/undo", json=SAVE, headers={"X-Coherence-Session": sid()})
    assert r.status_code == 429 and r.json()["detail"]["error"] == "actions_rate_limited"
    elsewhere = _from_web(hosted, "203.0.113.9, 203.0.113.9")
    assert (
        elsewhere.post(
            "/api/game/undo", json=SAVE, headers={"X-Coherence-Session": sid()}
        ).status_code
        == 200
    )


def test_ipv6_bucketed_by_64(clock):
    assert ratelimit.ip_bucket("2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert ratelimit.ip_bucket("::ffff:192.0.2.34") == "192.0.2.34"
    assert ratelimit.ip_bucket("192.0.2.99") == "192.0.2.99"
    lim = ratelimit.MessageLimiter(15, 1000)
    lim.check(sid(), "2001:db8:1:2::1")
    with pytest.raises(HTTPException) as ei:
        lim.check(sid(), "2001:db8:1:2:ffff:ffff:ffff:fffe")
    assert ei.value.status_code == 429
    lim.check(sid(), "2001:db8:1:3::1")
    lim.check(sid(), "::ffff:192.0.2.34")
    lim.check(sid(), "::ffff:192.0.2.35")


def test_busy_503_without_queueing(monkeypatch, selfhost):
    import threading
    from app import main

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(main, "slots", slots)
    body = turn_body(selfhost)
    assert slots.acquire(blocking=False)
    try:
        r = selfhost.post("/api/game/turn", json=body)
        assert r.status_code == 503
        assert r.json() == {"detail": {"error": "busy", "provider": None}}
        assert selfhost.get("/api/health").status_code == 200
    finally:
        slots.release()
    assert selfhost.post("/api/game/turn", json=body).status_code == 200


def test_max_concurrent_requests_default():
    from app import main

    assert settings.max_concurrent_requests == 4
    held = [main.slots.acquire(blocking=False) for _ in range(5)]
    try:
        assert held == [True, True, True, True, False]
    finally:
        for ok in held:
            if ok:
                main.slots.release()
