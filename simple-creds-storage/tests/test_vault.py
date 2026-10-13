import json
import pickle

import pytest

from simple_creds_storage import CredStore, Locked, Secret, Vault


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    clock = Clock()
    path = tmp_path_factory.mktemp("v") / "store.json"
    v = Vault(CredStore(path), idle_ttl_s=100, clock=clock)
    v.unlock("s1", "pass-one")
    return v, clock, path


def test_put_get_status_and_iv_changes(setup):
    v, _, path = setup
    v.put("s1", "openai", "value-a")
    iv1 = json.loads(path.read_text())["cipher"]["iv"]
    v.put("s1", "xai", "value-b")
    iv2 = json.loads(path.read_text())["cipher"]["iv"]
    v.delete("s1", "xai")
    iv3 = json.loads(path.read_text())["cipher"]["iv"]
    assert len({iv1, iv2, iv3}) == 3
    assert v.status("s1", ["openai", "xai"]) == {"openai": True, "xai": False}
    assert v.get("s1", "openai").reveal() == "value-a"
    assert v.get("s1", "xai") is None


def test_secret_never_shows():
    s = Secret("value-zz")
    assert "value-zz" not in repr(s) and "value-zz" not in str(s)
    assert repr(s) == "Secret(***)"
    with pytest.raises(TypeError):
        pickle.dumps(s)


def test_unknown_session_locked(setup):
    with pytest.raises(Locked):
        setup[0].get("nobody", "openai")


def test_idle_expiry_and_lock(setup):
    v, clock, _ = setup
    v.unlock("s2", "pass-one")
    clock.t += 50
    v.status("s2", ["openai"])
    clock.t += 101
    with pytest.raises(Locked):
        v.status("s2", ["openai"])
    v.unlock("s3", "pass-one")
    v.lock("s3")
    with pytest.raises(Locked):
        v.put("s3", "openai", "x")


def test_concurrent_first_unlocks_share_one_store(tmp_path):
    import threading
    v = Vault(CredStore(tmp_path / "keys" / "store.json"), idle_ttl_s=100)
    errors, start = [], threading.Barrier(2)

    def go(s):
        start.wait()
        try:
            v.unlock(s, "same-pass")
        except Exception as e:
            errors.append(repr(e))
    ts = [threading.Thread(target=go, args=(s,)) for s in ("a", "b")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errors == []
    v.put("a", "openai", "value-a")
    v.put("b", "xai", "value-b")
    v.unlock("c", "same-pass")
    assert v.status("c", ["openai", "xai"]) == {"openai": True, "xai": True}
    assert v.get("b", "openai").reveal() == "value-a"


def test_write_refreshes_other_sessions_with_same_key(tmp_path):
    v = Vault(CredStore(tmp_path / "s.json"), idle_ttl_s=100)
    v.unlock("a", "pw")
    v.unlock("b", "pw")
    v.put("a", "openai", "value-a")
    assert v.get("b", "openai").reveal() == "value-a"
    assert v.status("b", ["openai"]) == {"openai": True}
    assert v._sessions["b"][1] is not v._sessions["a"][1]
    v.put("a", "openai", "value-a2")
    assert v.get("b", "openai").reveal() == "value-a2"
    v.delete("a", "openai")
    assert v.get("b", "openai") is None
    assert v.status("b", ["openai"]) == {"openai": False}


def test_write_between_open_and_register_reaches_new_session(tmp_path):
    v = Vault(CredStore(tmp_path / "s.json"), idle_ttl_s=100)
    v.unlock("a", "pw")
    v.put("a", "openai", "value-a")
    v.put("a", "xai", "value-x")
    real_open = v.store.open

    def open_then_write(passphrase):
        opened = real_open(passphrase)
        v.put("a", "openai", "value-a2")
        v.delete("a", "xai")
        return opened

    v.store.open = open_then_write
    v.unlock("b", "pw")
    assert v.get("b", "openai").reveal() == "value-a2"
    assert v.get("b", "xai") is None
    assert v.status("b", ["openai", "xai"]) == {"openai": True, "xai": False}


def test_expired_sessions_purged_on_any_call(tmp_path):
    clock = Clock()
    v = Vault(CredStore(tmp_path / "s.json"), idle_ttl_s=10, clock=clock)
    v.unlock("old", "pw")
    clock.t += 11
    v.lock("other")
    assert "old" not in v._sessions
    assert v.session_count() == 0


def test_session_cap_evicts_least_recent(tmp_path):
    clock = Clock()
    v = Vault(CredStore(tmp_path / "s.json"), idle_ttl_s=100, clock=clock, max_sessions=2)
    for s in ("s1", "s2"):
        v.unlock(s, "pw")
        clock.t += 1
    v.status("s1", ["x"])
    v.unlock("s3", "pw")
    assert v.session_count() == 2
    v.status("s1", ["x"])
    with pytest.raises(Locked):
        v.status("s2", ["x"])
