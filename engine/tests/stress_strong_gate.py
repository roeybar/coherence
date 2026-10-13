"""Threaded consent chains with isolated request and save bindings."""

import itertools
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.game import engine, escalation, lore, moderation, physicalisation
from app.game.escalation import EscalationPending
from test_strong_gate import (
    MOVE,
    TurnReplies,
    attempt,
    configure_turns,
    make_save,
    turn_bindings,
)


def prepare(index, policy, declined, token_kind, trigger):
    gs = make_save(policy)
    doc = {"items": [], "memory": [], "physics_cache": []}
    move = f"{MOVE} Trial {index}."
    replies = TurnReplies(trigger)
    # Use the wrapper's real lazy digest to prepare tokens for every policy.
    with patch.object(
        engine, "_run_turn_inner", side_effect=lambda *args, strong_gate, **kw: strong_gate
    ):
        with turn_bindings(replies, doc):
            gate = engine.run_turn(gs, move, "local")
    digest = gate.move()
    now = int(time.time())
    token = escalation.mint(save_id="local", turn_index=gs.turn, move=digest, now=now)
    assert escalation.verify(token, save_id="local", turn_index=gs.turn, move=digest).ok
    if token_kind == "none":
        token = None
    elif token_kind == "forged":
        token = token.split(".")[0] + ".AAAA"
    elif token_kind == "expired":
        token = escalation.mint(
            save_id="local", turn_index=gs.turn, move=digest, now=now - escalation.TOKEN_TTL_S - 2
        )
    elif token_kind == "wrong_move":
        token = escalation.mint(save_id="local", turn_index=gs.turn, move=digest + "different")
    return index, policy, declined, token_kind, trigger, gs, doc, move, replies, digest, token


def run_chain(case):
    index, policy, declined, token_kind, trigger, gs, doc, move, replies, digest, token = case
    consent_count = 0
    kw = {"consent_token": token, "decline_escalation": declined}
    completed = False
    for _ in range(4):
        before_strong = len(replies.strong)
        consent_ok = escalation.verify(
            kw["consent_token"], save_id="local", turn_index=gs.turn, move=digest
        ).ok
        permitted = consent_ok and not declined and policy != "never"
        try:
            out = attempt(gs, replies, doc, move, **kw)
        except EscalationPending as gate:
            consent_count += 1
            assert not permitted, f"unexpected consent gate in move {index}"
            kw["consent_token"] = gate.to_payload()["consent_token"]
            assert escalation.verify(
                kw["consent_token"], save_id="local", turn_index=gs.turn, move=digest
            ).ok
        except physicalisation.AbilityIntentAmbiguous:
            kw["ability_intent"] = "external"
        else:
            assert out["narrative"] and gs.turn == 2, f"incomplete move {index}"
            completed = True
        if not permitted:
            assert len(replies.strong) == before_strong, f"unconsented strong body in move {index}"
        assert consent_count <= 1, f"second consent gate in move {index}"
        if completed:
            break
    assert completed, f"move {index} did not complete within its gate chain"
    if declined or policy == "never":
        assert not replies.strong and consent_count == 0, f"fast-only guard failed in move {index}"
    else:
        assert replies.strong, f"consented move {index} made no strong call"
        assert consent_count == (0 if token_kind == "valid" else 1)
    return consent_count, len(replies.strong), policy, declined, token_kind, trigger


def main():
    monkeypatch = pytest.MonkeyPatch()
    failures = []
    results = []
    try:
        monkeypatch.setattr(lore, "LORE_ROOT", Path(__file__).parent / "fixtures" / "packs")
        monkeypatch.setattr(moderation, "DEFAULT_PACK_ID", "testpack")
        moderation.reset_pack_cache()
        configure_turns(monkeypatch)
        combinations = list(
            itertools.product(
                ("ask", "auto", "never", ""),
                (False, True),
                ("none", "valid", "forged", "expired", "wrong_move"),
                ("tier2", "physics", "ability"),
            )
        )
        cases = [prepare(index, *combinations[index % len(combinations)]) for index in range(300)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [(case, pool.submit(run_chain, case)) for case in cases]
            for case, future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append((case[:5], type(exc).__name__, str(exc)))
    finally:
        monkeypatch.undo()
        lore.clear_cache()
        moderation.reset_pack_cache()
    if failures:
        for case, name, message in failures:
            print(f"FAIL {case}: {name}: {message}")
        print(f"FAIL {len(failures)} of 300 threaded moves")
        return 1
    covered = {(row[2], row[3], row[4], row[5]) for row in results}
    assert len(results) == 300 and len(covered) == 120
    print("PASS 300 threaded moves, 8 workers, all 120 policy/decline/token/trigger combinations")
    print("PASS strong bodies require a verified move token and no decline or never")
    print("PASS at most one consent prompt per move chain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
