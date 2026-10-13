"""Unlocked stores held in RAM only, per opaque session string.

Every call first drops sessions idle past the TTL. At most max_sessions are
held; a new unlock evicts the least recently used one.
"""

import hmac
import threading
import time
from collections import OrderedDict

from simple_creds_storage.filestore import CredStore, check_entry
from simple_creds_storage.secret import Secret


class Locked(Exception):
    """The session is not unlocked, or it expired."""


class Vault:
    def __init__(self, store: CredStore, idle_ttl_s: float, clock=time.monotonic,
                 max_sessions: int = 64):
        self.store = store
        self.idle_ttl_s = idle_ttl_s
        self.clock = clock
        self.max_sessions = max(1, max_sessions)
        self._sessions: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        for s in [s for s, e in self._sessions.items() if now - e[2] > self.idle_ttl_s]:
            del self._sessions[s]

    def unlock(self, session: str, passphrase: str) -> None:
        km, data = self.store.open(passphrase)
        with self._lock:
            data = self.store.read(km)
            now = self.clock()
            self._purge(now)
            self._sessions[session] = [km, data, now]
            self._sessions.move_to_end(session)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)

    def lock(self, session: str) -> None:
        with self._lock:
            self._purge(self.clock())
            self._sessions.pop(session, None)

    def session_count(self) -> int:
        with self._lock:
            self._purge(self.clock())
            return len(self._sessions)

    def _entry(self, session: str) -> list:
        now = self.clock()
        self._purge(now)
        e = self._sessions.get(session)
        if e is None:
            raise Locked()
        e[2] = now
        self._sessions.move_to_end(session)
        return e

    def status(self, session: str, names) -> dict:
        with self._lock:
            data = self._entry(session)[1]
            return {n: n in data for n in names}

    def get(self, session: str, name: str):
        with self._lock:
            value = self._entry(session)[1].get(name)
        return None if value is None else Secret(value)

    def _write(self, session: str, name: str, value) -> None:
        with self._lock:
            e = self._entry(session)
            data = dict(self.store.read(e[0]))
            if value is None:
                data.pop(name, None)
            else:
                data[name] = value
            self.store.save(e[0], data)
            e[1] = data
            # Sessions unlocked separately hold separate KeyMaterial objects for
            # one store, so each session with the same derived key gets the write.
            for s in self._sessions.values():
                if s is not e and hmac.compare_digest(s[0].key, e[0].key):
                    s[1] = dict(data)

    def put(self, session: str, name: str, value: str) -> None:
        check_entry(name, value)
        self._write(session, name, value)

    def delete(self, session: str, name: str) -> None:
        self._write(session, name, None)
