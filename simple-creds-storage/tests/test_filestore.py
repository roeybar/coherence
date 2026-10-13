import base64
import json
import os
import stat

import pytest

from simple_creds_storage import CredStore, InvalidName, WrongPassphraseOrCorrupt
from simple_creds_storage.filestore import check_entry


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    path = tmp_path_factory.mktemp("s") / "keys" / "store.json"
    store = CredStore(path)
    km, data = store.open("pass-one")
    assert data == {}
    store.save(km, {"openai": "value-a"})
    return path, store, km


def test_round_trip(made):
    path, store, _ = made
    _, data = CredStore(path).open("pass-one")
    assert data == {"openai": "value-a"}
    assert b"value-a" not in path.read_bytes()


def test_wrong_passphrase(made):
    with pytest.raises(WrongPassphraseOrCorrupt):
        CredStore(made[0]).open("pass-two")


def _flip(doc, where):
    def flip(b64):
        raw = bytearray(base64.b64decode(b64))
        raw[0] ^= 1
        return base64.b64encode(bytes(raw)).decode()
    if where == "salt":
        doc["kdf"]["salt"] = flip(doc["kdf"]["salt"])
    elif where == "iv":
        doc["cipher"]["iv"] = flip(doc["cipher"]["iv"])
    elif where == "ct":
        doc["ct"] = flip(doc["ct"])
    elif where == "tag":
        raw = bytearray(base64.b64decode(doc["ct"]))
        raw[-1] ^= 1
        doc["ct"] = base64.b64encode(bytes(raw)).decode()
    elif where == "header":
        doc["cipher"]["name"] = "AES-GCM "
    return doc


@pytest.mark.parametrize("where", ["salt", "iv", "ct", "tag", "header"])
def test_byte_flip(made, tmp_path, where):
    doc = _flip(json.loads(made[0].read_text()), where)
    p = tmp_path / "f.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(WrongPassphraseOrCorrupt):
        CredStore(p).open("pass-one")


@pytest.mark.parametrize("iters", [599999, 2000001])
def test_iterations_rejected_before_derive(made, tmp_path, monkeypatch, iters):
    import simple_creds_storage.filestore as fs
    doc = json.loads(made[0].read_text())
    doc["kdf"]["iterations"] = iters
    p = tmp_path / "f.json"
    p.write_text(json.dumps(doc))

    def no_derive(*a):
        raise AssertionError("derive ran")
    monkeypatch.setattr(fs, "_derive", no_derive)
    with pytest.raises(WrongPassphraseOrCorrupt):
        CredStore(p).open("pass-one")


def test_modes(made):
    path = made[0]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_atomic_replace_leaves_no_temp(made):
    path, store, km = made
    before = os.stat(path).st_ino
    store.save(km, {"openai": "value-a"})
    assert os.stat(path).st_ino != before
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_invalid_name_and_long_value():
    with pytest.raises(InvalidName):
        check_entry("Bad Name", "x")
    with pytest.raises(InvalidName):
        check_entry("a" * 33, "x")
    with pytest.raises(ValueError):
        check_entry("ok", "x" * 4097)
    check_entry("ok", "x" * 4096)


def test_create_never_replaces_an_existing_store(tmp_path, monkeypatch):
    path = tmp_path / "k" / "store.json"
    first, _ = CredStore(path).open("pw")
    late = CredStore(path)
    monkeypatch.setattr(late, "exists", lambda: False)
    km, data = late.open("pw")
    assert data == {} and km.salt == first.salt and km.key == first.key
    assert [p.name for p in path.parent.iterdir()] == ["store.json"]


def test_chmod_only_when_mode_differs(tmp_path, monkeypatch):
    import simple_creds_storage.filestore as fs
    d = tmp_path / "k"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)

    def refuse(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(fs.os, "chmod", refuse)
    km, _ = CredStore(d / "store.json").open("pw")
    assert CredStore(d / "store.json").read(km) == {}
