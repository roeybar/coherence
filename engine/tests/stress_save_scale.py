"""Exercise preserved history windows and stream saves near the body cap."""

import copy
import http.client
import json
import os
import resource
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path


def write_body(path, base_save, turns):
    import base64

    import numpy as np

    user_words = "I wait by the bell while the quiet rain falls over the stone path. "
    assistant_words = "The quiet rain falls over the stone path. The bell rests in the still air. "

    def texts(turn):
        marker = f"t{turn:04d} "
        return ((marker + user_words * 8)[:300], (marker + assistant_words * 20)[:1200])

    def dump(stream, value):
        json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))

    with path.open("w", encoding="ascii") as stream:
        stream.write('{"save":{')
        for key, value in base_save.items():
            if key in ("history", "memory", "undo_stack", "turn"):
                continue
            dump(stream, key)
            stream.write(":")
            dump(stream, value)
            stream.write(",")
        stream.write('"history":[')
        opener = base_save["history"][0]
        dump(
            stream,
            {"role": "assistant", "content": opener.get("content_en") or opener.get("content", "")},
        )
        for turn in range(1, turns + 1):
            for role, text in zip(("user", "assistant"), texts(turn)):
                stream.write(",")
                dump(stream, {"role": role, "content": text})
        stream.write('],"memory":[')
        for turn in range(1, turns + 1):
            vector = np.random.default_rng(turn).normal(size=384).astype(np.float16)
            encoded = base64.b64encode(vector.tobytes()).decode("ascii")
            for index, (role, text) in enumerate(zip(("user", "assistant"), texts(turn))):
                if turn > 1 or index:
                    stream.write(",")
                dump(stream, {"turn": turn, "role": role, "text": text, "v": encoded})
        stream.write('],"undo_stack":[')
        for k in range(25):
            if k:
                stream.write(",")
            dump(
                stream,
                {
                    "state": base_save["state"],
                    "history_len": 1 + 2 * (turns - k),
                    "turn": turns - k,
                },
            )
        stream.write('],"turn":')
        dump(stream, turns)
        stream.write('},"player_input":"I wait by the bell"}')


def in_process(directory):
    import pytest
    from fastapi.testclient import TestClient

    from app import creds
    from app.api import game as game_api
    from app.config import settings
    from app.game import context, engine, memory
    from app.main import build_app
    from app.providers import fake
    from conftest import NEW_BODY

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(settings, "keys_dir", str(directory / "keys"))
        monkeypatch.setattr(creds, "_vault", None)
        with TestClient(build_app("selfhost")) as client:
            assert (
                client.post("/api/keys/unlock", json={"passphrase": "pass-one"}).status_code == 200
            )
            assert client.put("/api/keys/anthropic", json={"key": "canary-ctl"}).status_code == 200
            client.headers.update(
                {"X-Provider": "anthropic", "X-Coherence-Session": str(uuid.uuid4())}
            )
            response = client.post("/api/game/new", json=NEW_BODY)
            assert response.status_code == 200, response.status_code
            base = response.json()["save"]
            path = directory / "body.json"
            write_body(path, base, 4000)
            print(f"save_scale turns=4000 bytes={path.stat().st_size}", flush=True)
            with path.open(encoding="ascii") as stream:
                body = json.load(stream)
            calls, recalls, admissions = [], [], []
            complete, recall, admit = fake.FakeProvider.complete, memory.recall, game_api._admit

            def recorded_complete(self, **kwargs):
                if "[INSIGHT DICE" in fake._text(kwargs["messages"][-1]["content"]):
                    calls.append(copy.deepcopy(kwargs))
                return complete(self, **kwargs)

            def recorded_recall(*args, **kwargs):
                recalls.append(dict(kwargs))
                return recall(*args, **kwargs)

            def timed_admit(*args, **kwargs):
                started = time.perf_counter()
                try:
                    return admit(*args, **kwargs)
                finally:
                    admissions.append(time.perf_counter() - started)

            monkeypatch.setattr(fake.FakeProvider, "complete", recorded_complete)
            monkeypatch.setattr(memory, "recall", recorded_recall)
            monkeypatch.setattr(game_api, "_admit", timed_admit)
            walls = []
            for attempt in range(2):
                in_save = body["save"]
                in_history = in_save["history"]
                projected = engine._history_for_llm(in_history)
                calls.clear()
                recalls.clear()
                started = time.perf_counter()
                response = client.post("/api/game/turn", json=body)
                wall = time.perf_counter() - started
                walls.append(wall)
                assert response.status_code == 200, response.status_code
                assert wall < 120, wall
                returned = response.json()["save"]
                assert returned["turn"] == in_save["turn"] + 1
                assert len(returned["history"]) == len(in_history) + 2
                assert returned["history"][:-2] == in_history
                assert not any(
                    "HISTORY SUMMARY" in engine._history_for_llm([row])[0]["content"]
                    for row in returned["history"]
                )
                assert calls and recalls
                for call in calls:
                    chars = len(fake._text(call["system"])) + sum(
                        len(fake._text(row["content"])) + 4 for row in call["messages"]
                    )
                    assert context.est(chars) <= 90000
                    sent = [
                        {"role": row["role"], "content": fake._text(row["content"])}
                        for row in call["messages"][:-1]
                    ]
                    start = len(in_history) - len(sent)
                    assert 0 < start < len(in_history) and in_history[start]["role"] == "user"
                    assert sent == projected[start:]
                    assert recalls[-1]["exclude_after_turn"] == context.recall_cutoff(
                        in_history,
                        in_save["turn"],
                        start,
                    )
                body = {"save": returned, "player_input": "I wait by the bell"}
                print(
                    f"PASS save_scale move={attempt + 1} wall_s={wall:.3f} "
                    f"admit_s={admissions[-1]:.3f}",
                    flush=True,
                )
            assert len(admissions) == 2 and admissions[1] < admissions[0], admissions
            print(
                f"PASS save_scale wall_s={walls} admit_s={admissions} "
                f"ru_maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}",
                flush=True,
            )
    finally:
        monkeypatch.undo()
    return 0


class EngineOverLimit(RuntimeError):
    pass


def check_child(child):
    code = child.poll()
    if code == -9:
        raise EngineOverLimit("engine killed: over 3 GiB container limit")
    if code is not None:
        raise RuntimeError(f"engine exited with returncode {code}")


@contextmanager
def child_engine(directory, engine_root):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env["KEYS_DIR"] = str(directory / "keys")
    with (directory / "engine.log").open("wb") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--no-access-log",
            ],
            cwd=engine_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                check_child(child)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    response.read()
                    if response.status == 200:
                        break
                except OSError:
                    pass
                finally:
                    connection.close()
                if time.monotonic() >= deadline:
                    raise RuntimeError("engine health deadline exceeded")
                time.sleep(0.1)
            yield child, port
            check_child(child)
        except Exception:
            check_child(child)
            raise
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)


def request_json(port, method, path, body, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        connection.request(
            method,
            path,
            body=json.dumps(body),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        response = connection.getresponse()
        data = response.read()
        assert response.status == 200, (path, response.status)
        return json.loads(data), response.getheader("Set-Cookie")
    finally:
        connection.close()


def unlock_child(port):
    _, cookie_header = request_json(port, "POST", "/api/keys/unlock", {"passphrase": "pass-one"})
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    headers = {
        "Cookie": "coh_session=" + cookie["coh_session"].value,
        "X-Provider": "anthropic",
        "X-Coherence-Session": str(uuid.uuid4()),
    }
    request_json(port, "PUT", "/api/keys/anthropic", {"key": "canary-ctl"}, headers)
    return headers


def peak_kb(child):
    check_child(child)
    for line in Path(f"/proc/{child.pid}/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    raise RuntimeError("engine VmHWM is unavailable")


def stream_turn(child, port, path, headers):
    started = time.perf_counter()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1800)
    try:
        connection.putrequest("POST", "/api/game/turn")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(path.stat().st_size))
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
        response = connection.getresponse()
        while response.read(1024 * 1024):
            pass
        check_child(child)
        assert response.status == 200, response.status
        return response.status, time.perf_counter() - started
    except Exception:
        check_child(child)
        raise
    finally:
        connection.close()


def near_cap(directory, engine_root, cap, pair=False):
    from conftest import NEW_BODY

    setup = directory / "setup"
    setup.mkdir()
    with child_engine(setup, engine_root) as (child, port):
        headers = unlock_child(port)
        data, _ = request_json(port, "POST", "/api/game/new", NEW_BODY, headers)
        base = data["save"]
    sample = directory / "sample.json"
    write_body(sample, base, 100)
    small_size = sample.stat().st_size
    write_body(sample, base, 1000)
    per_turn = (sample.stat().st_size - small_size) / 900
    fixed = small_size - 100 * per_turn
    files = []
    for label, target in (("40MB", 40_000_000), ("cap", cap - 435_456)):
        turns = int((target - fixed) / per_turn)
        path = directory / (label + ".json")
        write_body(path, base, turns)
        size = path.stat().st_size
        if label == "cap":
            assert cap - 1_435_456 <= size < cap, size
        else:
            assert 39_000_000 <= size <= 41_000_000, size
        files.append((label, path))
    if pair:
        return near_cap_pair(directory, engine_root, dict(files)["cap"])
    for label, path in files:
        work = directory / label
        work.mkdir()
        with child_engine(work, engine_root) as (child, port):
            headers = unlock_child(port)
            status, wall = stream_turn(child, port, path, headers)
            print(
                f"PASS near-cap {label} bytes={path.stat().st_size} status={status} "
                f"VmHWM_kB={peak_kb(child)} wall_s={wall:.3f}",
                flush=True,
            )
    return 0


def near_cap_pair(directory, engine_root, path):
    """Two near-cap moves at once: the second is refused 503 busy while the first
    is in flight, and the first plays."""
    size = path.stat().st_size
    work = directory / "pair"
    work.mkdir()
    with child_engine(work, engine_root) as (child, port):
        headers = unlock_child(port)
        flowing = threading.Event()
        first = {}

        def opened(timeout):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            connection.putrequest("POST", "/api/game/turn")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(size))
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            return connection

        def held():
            connection = None
            try:
                connection = opened(1800)
                sent = 0
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        connection.send(chunk)
                        sent += len(chunk)
                        if sent >= 32 * 1024 * 1024:
                            flowing.set()
                response = connection.getresponse()
                while response.read(1024 * 1024):
                    pass
                first["status"] = response.status
            except Exception as exc:
                first["error"] = repr(exc)
            finally:
                flowing.set()
                if connection is not None:
                    connection.close()

        thread = threading.Thread(target=held, daemon=True)
        thread.start()
        if not flowing.wait(600):
            raise RuntimeError("the first body did not start flowing")
        second = opened(300)
        try:
            response = second.getresponse()
            refused = (response.status, json.loads(response.read()))
        finally:
            second.close()
        thread.join(1800)
        if thread.is_alive():
            raise RuntimeError("the first request did not finish")
        check_child(child)
        assert refused == (503, {"detail": {"error": "busy", "provider": None}}), refused
        assert first == {"status": 200}, first
        print(
            f"PASS near-cap pair bytes={size} first=200 second=503 busy VmHWM_kB={peak_kb(child)}",
            flush=True,
        )
    return 0


def main():
    os.environ["LORE_ROOT"] = str(Path(__file__).resolve().parent / "fixtures" / "packs")
    os.environ["DEFAULT_PACK_ID"] = "testpack"
    engine_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(engine_root))
    from app.config import settings
    from app.game.context import context_ceiling
    from app.providers import registry
    from pydantic import SecretStr

    if settings.body_cap_bytes < 134217728:
        print("FAIL body cap must be at least 134217728", flush=True)
        return 2
    if not registry.fake_enabled():
        print("FAIL fake provider must be enabled", flush=True)
        return 2
    token = registry.bind_request("fake", SecretStr("canary-ctl"))
    try:
        ceiling = context_ceiling()
    finally:
        registry.unbind_request(token)
    if ceiling != (90000, 30000, 70000):
        print("FAIL fake context ceiling must be (90000, 30000, 70000)", flush=True)
        return 2
    try:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            if "--near-cap-pair" in sys.argv[1:]:
                return near_cap(directory, engine_root, settings.body_cap_bytes, pair=True)
            if "--near-cap" in sys.argv[1:]:
                return near_cap(directory, engine_root, settings.body_cap_bytes)
            return in_process(directory)
    except EngineOverLimit as exc:
        print(f"FAIL {exc}", flush=True)
        return 137
    except Exception as exc:
        print(f"FAIL save_scale {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
