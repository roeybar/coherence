"""Seeded fake-provider operations compared with a full-copy save."""

import copy
import random
import sys
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store
from app.game import context, engine, lore, moderation
from app.game.state import CharacterProfile, GameConfig, WorldState, doc_to_save, save_to_doc
from app.providers import fake, registry


def full_snapshot(gs):
    return {
        "state": gs.state.to_dict(),
        "history": list(gs.history),
        "turn": gs.turn,
        "items": copy.deepcopy(store.current_save()["items"]),
    }


def full_restore(gs, snap):
    gs.state = WorldState(**snap["state"])
    gs.history = list(snap["history"])
    gs.turn = snap.get("turn", max(0, gs.turn - 1))
    doc = store.current_save()
    if isinstance(snap.get("items"), list):
        doc["items"][:] = copy.deepcopy(snap["items"])
    doc["memory"][:] = [
        e
        for e in doc["memory"]
        if not (isinstance(e, dict) and type(e.get("turn")) is int and e["turn"] > gs.turn)
    ]


def freeze_checkpoint(gs):
    if gs.checkpoint_len is not None:
        gs.checkpoint_history = gs.history[: gs.checkpoint_len]
        gs.checkpoint_len = None


@contextmanager
def bindings(doc):
    stok = store.bind_save(doc)
    mtok = moderation.bind_pack("testpack")
    ptok = registry.bind_request("fake", SecretStr("canary-snapshots"))
    try:
        yield
    finally:
        registry.unbind_request(ptok)
        moderation.unbind_pack(mtok)
        store.unbind_save(stok)


def apply(gs, doc, operation, index, target, *, full_copy=False):
    with bindings(doc):
        with (
            patch.object(engine, "_snapshot", full_snapshot if full_copy else engine._snapshot),
            patch.object(engine, "_restore", full_restore if full_copy else engine._restore),
        ):
            if operation == "turn":
                out = engine.run_turn(
                    gs, f"I wait by the bell s{index:03d}.", "local", ua="Mozilla/5.0"
                )
                assert out["narrative"]
                assert "kind" not in out
            elif operation == "undo":
                out = engine.run_undo(gs)
            elif operation == "undo_to_turn":
                out = engine.run_undo_to_turn(gs, target)
            elif operation == "regenerate":
                out = engine.run_regenerate(gs, "local", ua="Mozilla/5.0")
            elif operation == "rewind":
                out = engine.run_rewind(gs)
            else:
                compressed = context.ContextEngine().maybe_compress(gs.history)
                changed = compressed is not gs.history
                if changed:
                    if full_copy:
                        freeze_checkpoint(gs)
                    else:
                        engine._materialise(gs)
                    gs.history = compressed
                out = {"compressed": changed}
        if full_copy:
            freeze_checkpoint(gs)
    return {
        key: out[key] for key in ("ok", "reason", "turn", "compressed", "regenerated") if key in out
    }


def compare(gs, reference, index, operation):
    assert gs.turn == reference.turn, f"turn at {index}: {operation}"
    assert gs.history == reference.history, f"history at {index}: {operation}"
    assert gs.state.to_dict() == reference.state.to_dict(), f"state at {index}: {operation}"
    expected = save_to_doc(reference)
    actual = copy.deepcopy(save_to_doc(gs))
    for snap in actual["undo_stack"]:
        if not isinstance(snap.get("history"), list):
            snap["history"] = actual["history"][: snap["history_len"]]
        snap.pop("history_len", None)
    if actual.get("checkpoint_len") is not None:
        actual["checkpoint_history"] = actual["history"][: actual.pop("checkpoint_len")]
    assert actual == expected, f"full save at {index}: {operation}"
    assert save_to_doc(doc_to_save(save_to_doc(gs))) == save_to_doc(gs)


def main():
    monkeypatch = pytest.MonkeyPatch()
    counts = Counter()
    shrinks = 0
    checkpoint_sequences = 0
    survived_turn = False
    restored_above_checkpoint = False
    successes = Counter()
    rng = random.Random(731)
    initial_random_state = random.getstate()
    try:
        monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
        monkeypatch.setattr(lore, "LORE_ROOT", Path(__file__).parent / "fixtures" / "packs")
        monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", "testpack")
        monkeypatch.setattr(engine, "_now_iso", lambda: "2026-01-01T00:00:00+00:00")
        monkeypatch.setattr(engine, "index_turn", lambda *args, **kw: None)
        monkeypatch.setattr(
            fake,
            "DEFAULT_REPLY",
            "[NARRATIVE]A bell sounds in the courtyard.[/NARRATIVE]"
            '[STATE_DELTA]{"checkpoint_worthy": true}[/STATE_DELTA]',
        )
        lore.clear_cache()
        moderation.reset_pack_cache()
        doc = {}
        with bindings(doc):
            gs = engine.create_save(
                "bells",
                CharacterProfile(name="Walker"),
                GameConfig(pack_id="testpack", one_memory="a red kite"),
            )
        reference, reference_doc = copy.deepcopy(gs), copy.deepcopy(doc)
        operations = ("turn",) * 6 + ("undo", "undo_to_turn", "regenerate", "rewind", "compress")
        opening = ["turn"] * 26 + ["regenerate", "compress", "undo", "rewind", "undo_to_turn"]
        for index in range(500):
            operation = opening[index] if index < len(opening) else rng.choice(operations)
            target = max(1, gs.turn - rng.randrange(3))
            if operation in ("turn", "regenerate"):
                checkpoint_worthy = "true" if rng.randrange(5) == 0 else "false"
                monkeypatch.setattr(
                    fake,
                    "DEFAULT_REPLY",
                    "[NARRATIVE]A bell sounds in the courtyard.[/NARRATIVE]"
                    '[STATE_DELTA]{"checkpoint_worthy": ' + checkpoint_worthy + "}[/STATE_DELTA]",
                )
            before = len(gs.history)
            checkpoint = (gs.checkpoint_len, gs.checkpoint_turn, copy.deepcopy(gs.checkpoint_state))
            random.seed(index)
            out = apply(gs, doc, operation, index, target)
            random.seed(index)
            expected = apply(reference, reference_doc, operation, index, target, full_copy=True)
            assert out == expected, f"result at {index}: {operation}"
            compare(gs, reference, index, operation)
            counts[operation] += 1
            if out.get("ok", True):
                successes[operation] += 1
            compressed = (
                operation in ("turn", "regenerate", "compress") and len(gs.history) < before
            )
            if compressed:
                shrinks += 1
            if restored_above_checkpoint and (
                compressed
                or (operation == "regenerate" and out.get("regenerated"))
                or (operation == "rewind" and out.get("ok"))
            ):
                checkpoint_sequences += 1
                restored_above_checkpoint = False
            if gs.checkpoint_len is not None and checkpoint == (
                gs.checkpoint_len,
                gs.checkpoint_turn,
                gs.checkpoint_state,
            ):
                if operation == "turn" and gs.turn > gs.checkpoint_turn:
                    survived_turn = True
                if (
                    survived_turn
                    and operation in ("undo", "undo_to_turn")
                    and out.get("ok")
                    and gs.turn > gs.checkpoint_turn
                    and len(gs.history) > gs.checkpoint_len
                ):
                    restored_above_checkpoint = True
            else:
                survived_turn = False
                restored_above_checkpoint = False
            gs = doc_to_save(copy.deepcopy(save_to_doc(gs)))
            reference = doc_to_save(copy.deepcopy(save_to_doc(reference)))
        assert sum(counts.values()) == 500 and len(counts) == 6
        assert all(successes[op] for op in set(operations))
        assert shrinks >= 2, f"only {shrinks} compressions"
        assert checkpoint_sequences >= 1, (
            f"only {checkpoint_sequences} surviving checkpoint sequences"
        )
    except Exception as exc:
        print(f"FAIL operation {sum(counts.values())}: {type(exc).__name__}: {exc}")
        return 1
    finally:
        monkeypatch.undo()
        random.setstate(initial_random_state)
        lore.clear_cache()
        moderation.reset_pack_cache()
    print("PASS 500 seeded fake-provider operations against a full-copy save")
    print("PASS restored history, state, checkpoint and undo stack match after every operation")
    print(
        f"PASS {shrinks} count compressions; {checkpoint_sequences} surviving checkpoint sequences; "
        f"operations: {dict(sorted(counts.items()))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
