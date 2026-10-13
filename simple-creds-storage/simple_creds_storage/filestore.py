"""One encrypted JSON file of name -> value.

AES-256-GCM with a PBKDF2-SHA256 key; the header is the AAD. The header
is validated before any key derivation. Writes go to an O_EXCL temp file,
are fsynced, then replace the old file. A new store is linked into place
only if no file exists yet.
"""

import base64
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

FORMAT = "simple-creds-storage"
VERSION = 1
ITERATIONS = 600_000
MAX_ITERATIONS = 2_000_000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_VALUE = 4096
_create_lock = threading.Lock()


class WrongPassphraseOrCorrupt(Exception):
    """The passphrase is wrong or the file is damaged."""


class InvalidName(ValueError):
    """A name outside the allowed pattern."""


@dataclass(repr=False, eq=False)
class KeyMaterial:
    key: bytes
    salt: bytes
    iterations: int


def check_entry(name, value) -> None:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise InvalidName("invalid name")
    if not isinstance(value, str) or len(value) > MAX_VALUE:
        raise ValueError("value must be a string of at most 4096 characters")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s) -> bytes:
    if not isinstance(s, str):
        raise ValueError("not base64 text")
    return base64.b64decode(s, validate=True)


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(passphrase.encode("utf-8"))


def _aad(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


class CredStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def _parse(self):
        try:
            doc = json.loads(self.path.read_text("utf-8"))
            header = {k: doc[k] for k in ("format", "v", "kdf", "cipher")}
            kdf, cipher = doc["kdf"], doc["cipher"]
            ok = (doc["format"] == FORMAT and doc["v"] == VERSION
                  and kdf["name"] == "PBKDF2" and kdf["hash"] == "SHA-256"
                  and cipher["name"] == "AES-GCM"
                  and type(kdf["iterations"]) is int
                  and ITERATIONS <= kdf["iterations"] <= MAX_ITERATIONS)
            salt, iv, ct = _unb64(kdf["salt"]), _unb64(cipher["iv"]), _unb64(doc["ct"])
        except (OSError, ValueError, KeyError, TypeError):
            raise WrongPassphraseOrCorrupt() from None
        if not ok or len(salt) != 16 or len(iv) != 12 or len(ct) < 16:
            raise WrongPassphraseOrCorrupt()
        return salt, iv, kdf["iterations"], ct, _aad(header)

    @staticmethod
    def _decrypt(key: bytes, iv: bytes, ct: bytes, aad: bytes) -> dict:
        try:
            data = json.loads(AESGCM(key).decrypt(iv, ct, aad))
        except (InvalidTag, ValueError):
            raise WrongPassphraseOrCorrupt() from None
        if not isinstance(data, dict):
            raise WrongPassphraseOrCorrupt()
        return data

    def open(self, passphrase: str):
        """Derive the key and decrypt. Creates an empty store if absent.

        Creation runs under a process lock and re-checks for the file; the
        final file is only ever created if it does not exist yet, so two
        first unlocks end up on the same store.
        """
        if not self.exists():
            with _create_lock:
                if not self.exists():
                    salt = secrets.token_bytes(16)
                    km = KeyMaterial(_derive(passphrase, salt, ITERATIONS), salt, ITERATIONS)
                    if self._write(self._body(km, {}), exclusive=True):
                        return km, {}
        salt, iv, iterations, ct, aad = self._parse()
        km = KeyMaterial(_derive(passphrase, salt, iterations), salt, iterations)
        return km, self._decrypt(km.key, iv, ct, aad)

    def read(self, km: KeyMaterial) -> dict:
        """Decrypt with already-derived key material."""
        _, iv, _, ct, aad = self._parse()
        return self._decrypt(km.key, iv, ct, aad)

    def save(self, km: KeyMaterial, data: dict) -> None:
        self._write(self._body(km, data), exclusive=False)

    @staticmethod
    def _body(km: KeyMaterial, data: dict) -> bytes:
        for name, value in data.items():
            check_entry(name, value)
        iv = secrets.token_bytes(12)
        header = {
            "format": FORMAT, "v": VERSION,
            "kdf": {"name": "PBKDF2", "hash": "SHA-256",
                    "iterations": km.iterations, "salt": _b64(km.salt)},
            "cipher": {"name": "AES-GCM", "iv": _b64(iv)},
        }
        plain = json.dumps(data, sort_keys=True).encode("utf-8")
        ct = AESGCM(km.key).encrypt(iv, plain, _aad(header))
        return json.dumps({**header, "ct": _b64(ct)}).encode("utf-8")

    def _write(self, body: bytes, exclusive: bool) -> bool:
        """Writes the file; with exclusive, returns False if it already exists."""
        d = self.path.parent
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(os.stat(d).st_mode) != 0o700:
            os.chmod(d, 0o700)
        tmp = d / (self.path.name + "." + secrets.token_hex(8) + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            if not exclusive:
                os.replace(tmp, self.path)
                return True
            try:
                os.link(tmp, self.path)
            except FileExistsError:
                return False
            return True
        finally:
            if tmp.exists():
                tmp.unlink()
