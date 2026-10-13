import threading

from fastapi import HTTPException

from app import ratelimit
from conftest import sid


def test_200_threads_one_session_one_accept(clock):
    lim = ratelimit.MessageLimiter(15, 100000)
    s, ok, start = sid(), [], threading.Barrier(200)

    def run(i):
        start.wait()
        try:
            lim.check(s, "10.1.%d.%d" % (i // 100, i % 100))
            ok.append(i)
        except HTTPException:
            pass

    ts = [threading.Thread(target=run, args=(i,)) for i in range(200)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 1
