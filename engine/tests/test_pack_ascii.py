"""The demo pack is ASCII and its JSON parses to the original objects."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "lore/neon_rain"


def test_pack_json_objects_unchanged():
    lines = (ROOT / "tests/fixtures/pack_json.sha256").read_text(encoding="ascii").split("\n")
    for line in filter(None, lines):
        digest, rel = line.split("  ")
        obj = json.loads((PACK / rel).read_text(encoding="ascii"))
        canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == digest, rel


def test_pack_is_ascii():
    for p in PACK.rglob("*"):
        if p.is_file():
            assert not re.search(rb"[^\x00-\x7f]", p.read_bytes()), p
