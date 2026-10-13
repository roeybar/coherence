"""The hook check keeps its shape under padding, extra paragraphs and quote styles, gives the
same answer from 16 threads, makes at most one call per move and exactly one on a miss, and
every failure path completes with the reply untouched."""

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.game.hook_repair import MISS_SHAPES, PASS_SHAPES, check_hook, repair_hook
from app.game.quoted_speech import ensure_verbatim
from app.providers import fake
from test_context_window import bound, world_step_state
from test_hook_last import BRANCHES, on_branch
from test_hook_repair import (
    ABBR,
    BLOCK_CASES,
    BOUNDARY_ROWS,
    C,
    FAILURE_CASES,
    G,
    HOOK,
    hook_calls,
    live_rows,
    prepare_case,
    reply_of,
    run_failure_case,
    run_repair,
    tagged,
)
from test_reply_complete import new_testpack_save

FIXTURES = [(ending, move, expected) for _n, move, ending, expected in live_rows()] + list(
    BOUNDARY_ROWS
)
EMPTY_TEXTS = ("", "   ")
HEBREW = re.compile("[" + chr(0x590) + "-" + chr(0x5FF) + "]")
BETWEEN_WORDS = re.compile(r'(?<=\w)"(?=\w)')
ODD = [
    None,
    '"',
    '""',
    "?",
    '"?"',
    "x" * 20000,
    "{brace} }{ %s",
    ABBR,
    chr(0x200F),
]


def fold(text):
    return text.translate({ord(ch): '"' for ch in (C["<<"], C[">>"], G)})


def restyle(text, style):
    """Straight double quotes re-rendered alternately as open and close marks."""
    marks = {"curly": (C["<<"], C[">>"]), "gershayim": (G, G)}[style]
    out, count = [], 0
    for ch in text:
        if ch == '"':
            out.append(marks[count % 2])
            count += 1
        else:
            out.append(ch)
    return "".join(out)


def test_shape_survives_padding_paragraphs_and_quote_styles():
    skipped = 0
    runs = 0
    for text, move, expected in FIXTURES:
        empty = text in EMPTY_TEXTS
        variants = [
            (text + "  \n\n", expected),
            ("x" * 4000 + " " + text, "no_speech" if empty else expected),
            ('"Earlier line?"\n\n' + text, "question" if empty else expected),
        ]
        for variant, want in variants:
            got = check_hook(variant, move)
            assert got == want, (variant[:80], move, got, want)
            runs += 1
        straight = fold(text)
        if empty or HEBREW.search(text):
            continue
        if BETWEEN_WORDS.search(straight):
            skipped += 1
            continue
        for style in ("curly", "gershayim"):
            got = check_hook(restyle(straight, style), move)
            assert got == expected, (style, text[:80], move, got, expected)
            runs += 1
        assert check_hook(straight, move) == expected, (text[:80], move)
    assert skipped == 0, skipped
    for odd in ODD:
        assert check_hook(odd, "") in PASS_SHAPES + MISS_SHAPES, repr(odd)[:40]
    print(
        f"PASS {runs} padded, repeated-paragraph and restyled endings keep their shape; odd input is safe"
    )


def test_threads_agree_with_the_serial_result():
    serial = [check_hook(text, move) for text, move, _e in FIXTURES]
    assert serial == [expected for _t, _m, expected in FIXTURES]

    def work(_):
        return [check_hook(text, move) for text, move, _e in FIXTURES]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(work, range(16 * 200)))
    assert all(result == serial for result in results)
    print(f"PASS 16 threads x 200 rounds over {len(FIXTURES)} fixtures equal the serial result")


@pytest.mark.parametrize("branch", BRANCHES)
def test_one_call_on_a_miss_and_none_on_a_pass(pack, monkeypatch, calls, branch):
    on_branch(monkeypatch, branch)
    gs = new_testpack_save()
    moves = repairs = 0
    for _round in range(3):
        for text, move, _expected in FIXTURES:
            ts = world_step_state(gs)
            ts.narrative_chunks = [text]
            miss = check_hook(ensure_verbatim(move, text)[0], move) not in PASS_SHAPES
            monkeypatch.setattr(fake, "SCRIPT", [(tagged(HOOK), "end_turn")] if miss else [])
            calls.clear()
            with bound("fake"):
                repair_hook(ts, move)
            assert len(hook_calls(calls)) == (1 if miss else 0), (text[:80], move)
            assert len(calls) <= 1
            assert fake.SCRIPT == []
            moves += 1
            repairs += miss
    assert repairs > 0
    print(f"PASS {moves} moves: {repairs} repairs, 0 on passing replies ({branch})")


def test_failure_paths_complete_with_the_reply_untouched(pack, monkeypatch, calls):
    on_branch(monkeypatch, "no_ceiling")
    for n in range(60):
        case = FAILURE_CASES[n % len(FAILURE_CASES)]
        with monkeypatch.context() as m:
            ts, report = run_failure_case(m, case)
        assert ts.narrative_chunks == [reply_of(case)], case[0]
        assert report.repair == "failed:" + case[4], (case[0], report)
    print("PASS 60 failing repairs complete and leave the reply untouched")


def test_blocked_repairs_refuse_the_move_every_time(pack, monkeypatch, calls):
    on_branch(monkeypatch, "no_ceiling")
    for n in range(30):
        case = BLOCK_CASES[n % len(BLOCK_CASES)]
        with monkeypatch.context() as m:
            ts = prepare_case(m, case)
            with pytest.raises(case[4]):
                run_repair(ts, case)
        assert ts.narrative_chunks == [reply_of(case)], case[0]
    print("PASS 30 blocked repairs each refuse the move and leave the reply untouched")
