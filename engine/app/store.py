"""Per-request save binding.

The engine is stateless: every request carries the whole save doc. The
handler binds that doc here for the duration of the engine call, and the
item, memory and physics modules read it through current_save().

MiniColl is a small stand-in for the handful of collection calls the item
module makes. Filters are equality only; the save_id key is ignored
because the bound doc is already the scope.
"""

import contextvars
import copy
import uuid
from dataclasses import dataclass

SAVE_KEYS = ("items", "memory", "physics_cache")

_current: contextvars.ContextVar = contextvars.ContextVar("current_save", default=None)


class StoreUnbound(RuntimeError):
    """Raised when a store reader runs with no save bound."""


def bind_save(doc: dict) -> contextvars.Token:
    for key in SAVE_KEYS:
        doc.setdefault(key, [])
    return _current.set(doc)


def unbind_save(token: contextvars.Token) -> None:
    _current.reset(token)


def current_save() -> dict:
    doc = _current.get()
    if doc is None:
        raise StoreUnbound("no save is bound to this request")
    return doc


@dataclass
class InsertResult:
    inserted_id: str


@dataclass
class DeleteResult:
    deleted_count: int


def _check_filter(flt: dict) -> dict:
    out = {}
    for k, v in (flt or {}).items():
        if isinstance(v, dict):
            raise ValueError(f"unsupported filter on {k!r}: only equality is allowed")
        if k == "save_id":
            continue
        out[k] = v
    return out


def _matches(row: dict, flt: dict) -> bool:
    return all(row.get(k) == v for k, v in flt.items())


class MiniColl:
    def __init__(self, doc: dict, key: str):
        self._rows = doc.setdefault(key, [])

    def find(self, flt: dict) -> list:
        f = _check_filter(flt)
        return [copy.deepcopy(r) for r in self._rows if _matches(r, f)]

    def find_one(self, flt: dict):
        f = _check_filter(flt)
        for r in self._rows:
            if _matches(r, f):
                return copy.deepcopy(r)
        return None

    def insert_one(self, doc: dict) -> InsertResult:
        row = copy.deepcopy(doc)
        row["_id"] = uuid.uuid4().hex
        self._rows.append(row)
        return InsertResult(inserted_id=row["_id"])

    def find_one_and_update(self, flt: dict, update: dict, return_document=True):
        f = _check_filter(flt)
        ops = set(update or {})
        if ops != {"$set"}:
            raise ValueError(f"unsupported update operators: {sorted(ops)}")
        for r in self._rows:
            if _matches(r, f):
                r.update(copy.deepcopy(update["$set"]))
                return copy.deepcopy(r)
        return None

    def delete_one(self, flt: dict) -> DeleteResult:
        f = _check_filter(flt)
        for i, r in enumerate(self._rows):
            if _matches(r, f):
                del self._rows[i]
                return DeleteResult(deleted_count=1)
        return DeleteResult(deleted_count=0)
