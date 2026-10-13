"""Serve the fixture testpack as a pack that ships a physicalisation template."""

from pathlib import Path
from unittest.mock import patch

from app.game import lore

PACKS = Path(__file__).parent / "packs"
PACK_ID = "testpack"

_real_load_pack = lore.load_pack
_real_get_template = lore.get_template
_active = []


def _load_pack(pack_id):
    if pack_id == PACK_ID:
        with patch.object(lore, "LORE_ROOT", PACKS):
            return _real_load_pack(pack_id)
    return _real_load_pack(pack_id)


def _get_template(pack, slot, *args, **kwargs):
    if slot == "physicalisation" and (pack or {}).get("manifest", {}).get("id") == PACK_ID:
        return "Ground impossible acts in the physics of the scene."
    return _real_get_template(pack, slot, *args, **kwargs)


def start():
    lore.clear_cache()
    _active[:] = [
        patch.object(lore, "load_pack", _load_pack),
        patch.object(lore, "get_template", _get_template),
    ]
    for p in _active:
        p.start()


def stop():
    for p in _active:
        p.stop()
    _active.clear()
    lore.clear_cache()
