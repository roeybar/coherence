"""A reply whose final paragraph ends on no hook gets one small cached call that writes only
the hook beat; a passing reply gets none, a blocked hook line refuses the move, and any other
failure leaves the reply as written.
The live endings, the shape boundaries, the cached prefix, every failure path, the API
turn and its placement are pinned here."""

import inspect

import pytest

from app.game import dice_gate, hook_repair, lore, moderation, parser
from app.game.beats.builtin import (
    _TRAILING_PROMPT_PATTERNS,
    DMWorldStepBeat,
    _replace_em_dashes,
    _strip_metagame_brackets,
    _strip_trailing_prompt,
    build_pipeline_for,
)
from app.game.beats.craft import CraftBeat
from app.game.hook_repair import MISS_SHAPES, PASS_SHAPES, _REPAIR_FOCUS, check_hook, repair_hook
from app.game.prompts import HOOK_LAST
from app.llm_client import ProviderError
from app.providers import fake
from conftest import NEW_BODY
from test_consult import consult_state
from test_context_window import bound, world_step_state
from test_hook_last import BRANCHES, GROUND_HOOK, on_branch, texts
from test_output_moderation import MARK, PACK, _decoder
from test_reply_complete import ACT, EMPTY, craft_state, new_testpack_save
from test_strong_gate import make_save

MISS = 'Mara sets the glass down. "Not yet."'
HOOK = 'Mara leans closer. "So who sent you?"'
C = {"<<": chr(0x201C), ">>": chr(0x201D), "`": chr(0x2019), "~": chr(0x2014)}
G = chr(0x05F4)
ABBR = chr(0x5E6) + chr(0x5D4) + G + chr(0x5DC)
Q = chr(0x5DE) + chr(0x5D9)
NO = chr(0x5DC) + chr(0x5D0)
HEBREW_WORD = "".join(chr(c) for c in (0x5E9, 0x5DC, 0x5D5, 0x5DD))
A4 = (
    "The woman watches your hands, voice low, <<That wire better be for more than tricks. "
    "Which end are you playing today?>>"
)


def c(s):
    for key, value in C.items():
        s = s.replace(key, value)
    return s


def tagged(x):
    return f"[NARRATIVE]\n{x}\n[/NARRATIVE]\n[STATE_DELTA]\n{{}}\n[/STATE_DELTA]"


def hook_calls(calls):
    return [call for call in calls if "BEAT FOCUS - hook" in texts(call)[1]]


# name | move | ending | expected; "-" is no move and a backslash-n is a line break.
LIVE_ROWS = r"""
a1 | - | The voice calls clearer through the glass: "I need to talk. Now. Got nowhere else to go." | statement
a2 | - | The woman waits, voice low, <<You can`t claim ignorance on this one; it`s personal~and it`s going to cost you.>> | statement
a3 | - | The woman shifts. <<You ever think about what happens when the rain stops? Because for me, dropping the lights isn`t an option~what I need is the opposite.>> Her voice nails the silence. | after_quote
a4 | - | The woman watches your hands, voice low, <<That wire better be for more than tricks. Which end are you playing today?>> | question
a5 | - | <<I need you to connect some dots, Mara. Are you willing to risk what comes next?>> | question
a6 | - | She takes it. <<So,>> she says, <<which lines are you ready to cross tonight?>> | question
a7 | - | She leans forward, voice low: "What won't you admit out loud?" | question
a8 | - | <<What`s your cut, Mara? And what are you prepared to lose to get it?>> | question
a9 | "Who sent you to my door at this hour?" | You lean forward. "Who sent you to my door at this hour?" Your visitor`s eyes flicker briefly\na shadow of hesitation\nbefore they straighten. "Names come cheap," they say, "but this one`s not mine to give away. Not yet." | statement
a10 | I tell the visitor, "I don't work for free. What's the job?" | You don`t flinch. <<I don't work for free. What's the job?>> The weight presses down. <<Think you`re that someone, Mara?>> | question
a11 | I pour the visitor another drink and ask, "Who's after you?" | <<It`s the kind that doesn`t announce itself,>> they say. <<Who do you think is after me?>> | question
a12 | - | The wire is toast.\n\nThe visitor`s eyes stay on you. <<Not every trick works the first time. What`s your next play, Mara?>> | question
a13 | - | A figure flicks a lighter~their nod is deliberate. "Waiting for you, Mara. Got a minute?" | question
b1 | - | "Please," the voice says, softer now, "I need help before the rain takes him for good." | statement
b2 | - | The visitor's voice barely rises. "Thanks. You ever deal with disappearances that don`t show on the books?" | question
b3 | - | They break the silence: "Who disappears with no trace but leaves something wrong behind?" | question
b4 | - | They lean in. "If you want to find him, you`ll need that. Ever tap into the city's veins like this before?" | question
b5 | - | "And forgetting suits some people just fine. What part do you play in this silence, Mara Voss?" | question
b6 | - | "So, Mara," they say quietly, "why should I trust someone who collects silence for a living?" | question
b7 | - | The visitor leans in, voice low. "You know why they watch you, don`t you?" | question
b8 | - | They lean forward. "If you're involved, you better know why they want him gone~before they come for you next." | statement
b9 | "Who sent you to my door at this hour?" | <<Who sent you to my door at this hour?>> you ask. <<No one,>> they reply, <<this was my choice.>> Something locks the room tight. <<Are you prepared to follow where this silence leads?>> | question
b10 | I tell the visitor, "I don't work for free. What's the job?" | "I don't work for free. What's the job?" They lock eyes. "And if you want to stay breathing, you`ll do better to find it first." | statement
b11 | I pour the visitor another drink and ask, "Who's after you?" | "Who`s after you?" you ask, "Who's after you?". They lean closer. "People who don`t want their ghosts found. No one you want knocking on your door at night." | statement
b12 | - | A voice cuts through the static, asking, <<Who`s tapping in so late and who`s buying?>> | question
b13 | - | A figure steps out from the shadows, voice low, <<You looking for me or watching the rain?>> | question
b14 | "Coffee?" | "You`re the type who likes answers straight," they say. "What`s your play now, Voss?" | question
b15 | "Name?" | "Name's Ryu," they say. "I ask first: what`s your interest in a phone line that wasn`t paid for?" Their eyes don`t blink, but the rain beads and rolls off their collar. | after_quote
b16 | "Go on." | You nod slowly, voice low, "Go on."\n\nRyu`s eyes stay locked on yours.\n\nTheir tone sharpens. "Are you ready to pay the price, or do you want to fry your chances before you start?" | question
"""


def live_rows():
    rows = []
    for line in LIVE_ROWS.strip().splitlines():
        name, move, ending, expected = (part.strip() for part in line.split(" | "))
        rows.append((name, "" if move == "-" else move, c(ending).replace("\\n", "\n"), expected))
    return rows


def curly(text):
    """Straight double quotes re-rendered alternately as curly open and close."""
    out, opened = [], False
    for ch in text:
        if ch == '"':
            out.append(C["<<"] if not opened else C[">>"])
            opened = not opened
        else:
            out.append(ch)
    return "".join(out)


STOCK_ROWS = [
    ('Mara asks, "' + p[0].upper() + p[1:] + '"', "", "stock") for p in _TRAILING_PROMPT_PATTERNS
]
STOCK_ROWS += [(curly(text), move, shape) for text, move, shape in STOCK_ROWS]

# (text, move, expected)
# A spoken closing line passes only on a question mark; a demand that ends in a full stop is repaired
# like a statement.
BOUNDARY_ROWS = [
    ("", "", "empty"),
    ("   ", "", "empty"),
    ('He says "Wait.', "", "unbalanced"),
    ("Rain falls.", "", "no_speech"),
    (fake.DEFAULT_REPLY, "", "no_speech"),
    ("The observer lowers her hood and waits for your answer.", "", "no_speech"),
    ("Rain falls. Who is at the door?", "", "narrator_question"),
    ("Rain falls. What do you do?", "", "stock"),
    ("Rain. What" + chr(0x2019) + "s your move?", "", "stock"),
    ('"If you' + chr(0x2019) + 're not going to answer, maybe tell me why."', "", "statement"),
    ('"Please. It' + chr(0x2019) + 's important. Just a minute."', "", "statement"),
    ('"And I need to know if you can hold the weight."', "", "statement"),
    ('"Just tell me who sent you."', "", "statement"),
    ('"So tell me."', "", "statement"),
    ('"That' + chr(0x2019) + 's all I need to know."', "", "statement"),
    ('"I need to know nothing more from you."', "", "statement"),
    ('"All I need to know is that you lied."', "", "statement"),
    ('"No, tell me nothing."', "", "statement"),
    ('"Not now, tell me tomorrow."', "", "statement"),
    ('"Not now; tell me later."', "", "statement"),
    ('"Not yet, please."', "", "statement"),
    ('"Please. No."', "", "statement"),
    ('"Please, I can' + chr(0x2019) + 't."', "", "statement"),
    ('"Please don' + "'" + 't ask me that."', "", "statement"),
    ('"I need help. But not from you."', "", "statement"),
    ('"You must be tired. I won' + "'" + 't go."', "", "statement"),
    ('"Nobody will tell me anything."', "", "statement"),
    ('"Now, what' + chr(0x2019) + 's your next move?"', "", "stock"),
    ("Rain. Now, what" + chr(0x2019) + "s your next move?", "", "stock"),
    # A vocative is not a stock prompt.
    ('"What' + chr(0x2019) + 's your next play, Mara?"', "", "question"),
    # A known false miss: a tag after the closing quote is a miss by the rule.
    ('"Who sent you?" she asks.', "", "after_quote"),
    ('"Not yet." She waits. Will you ask again?', "", "narrator_question"),
    ('You lean in. "Who sent you?"', 'I ask "Who sent you?"', "player_line"),
    ('You lean in. "Who' + chr(0x2019) + 's there?"', 'I ask "Who\'s there?"', "player_line"),
    ('"Get out!"', "", "statement"),
    ('"Get out!!"', "", "statement"),
    ('"Run!..."', "", "statement"),
    ('"Run!' + chr(0x2026) + '"', "", "statement"),
    ('"Get out of here."', "", "statement"),
    ('"Damn!"', "", "statement"),
    ('"You must be tired."', "", "statement"),
    ('"You must have known."', "", "statement"),
    ('"You' + chr(0x2019) + 've got to be kidding."', "", "statement"),
    ('"You must' + chr(0x2019) + 've known."', "", "statement"),
    ('"You must really be tired."', "", "statement"),
    ('"You must all be tired."', "", "statement"),
    ('"You have to be careful out there."', "", "statement"),
    ('"You must have the gold by sunrise."', "", "statement"),
    ('"You must not be seen."', "", "statement"),
    ('"You must leave now."', "", "statement"),
    ('"You have to tell me who sent you."', "", "statement"),
    ('"You' + chr(0x2019) + 've got to get out of here."', "", "statement"),
    ('"You coming?!"', "", "question"),
    ('"Well..."', "", "statement"),
    (G + HEBREW_WORD + "?" + G, "", "question"),
    (G + HEBREW_WORD + "." + G, "", "statement"),
    (GROUND_HOOK.split("\n")[1], "", "question"),
    # Only the final paragraph is judged; a mood line after earlier dialogue passes.
    ('"Who sent you?" she asks.\n\nRain falls.', "", "no_speech"),
    ('Rain.\n\n"Not yet."', "", "statement"),
    # Every quoted span counts as speech, so narration after it is a miss unless it ends on a
    # non-stock question.
    ('They call him "Ghost". Rain falls.', "", "after_quote"),
    ('The sign reads "Closed for good". Rain falls.', "", "after_quote"),
    ('{"zzsitemark": 1}', "", "after_quote"),
    # A quoted span is speech, so a tag after it is a miss.
    ('Mara shakes her head. "Not yet," she says.', "", "after_quote"),
    ('"No," he says.', "", "after_quote"),
    # A quoted word is speech, so this ending is a miss.
    ('She says she is going "home," and leaves.', "", "after_quote"),
    ("Rain falls. Your move.", "", "stock"),
    ("Mara says, " + C["<<"] + "What" + chr(0x2019) + "s your move" + C[">>"], "", "stock"),
    ('"Help me."', "I shout help me. Nobody comes.", "statement"),
    # Known limitation: no speaker attribution, so an NPC echoing the player counts as the player.
    (
        'Mara repeats your question back at you: "Who sent you?"',
        'I ask "Who sent you?"',
        "player_line",
    ),
    ('Who do you tell, "No."', 'I say "No."', "player_line"),
    ('"Don' + chr(0x2019) + 't move."', "", "statement"),
    ('"Do not follow me."', "", "statement"),
    ('"Don' + chr(0x2019) + 't know."', "", "statement"),
    ('"Don\'t think so."', "", "statement"),
    ('"Do not care."', "", "statement"),
    ('"Don\'t want any trouble."', "", "statement"),
    ('"Don\'t need your money."', "", "statement"),
    ('"Don\'t have it."', "", "statement"),
    ('"Do not like it."', "", "statement"),
    ('"Sit down."', "", "statement"),
    ('"Put it down."', "", "statement"),
    ('"Drop it."', "", "statement"),
    ('"Talk."', "", "statement"),
    ('"Speak up."', "", "statement"),
    ('"Move."', "", "statement"),
    ('"Run!"', "", "statement"),
    ('"Let me see it."', "", "statement"),
    ('"Let me think about it."', "", "statement"),
    ('"Swear I never saw him."', "", "statement"),
    ('"That\'s nothing you need to know."', "", "statement"),
    ('"Not something you have to see."', "", "statement"),
    ('"Nobody can help me."', "", "statement"),
    ('"You can\'t help me."', "", "statement"),
    ('"You\'d better believe it."', "", "statement"),
    ('"You must think I\'m a fool."', "", "statement"),
    ('"You must know him."', "", "statement"),
    ('"You must mean the old dock."', "", "statement"),
    ('"You have to admit it\'s clever."', "", "statement"),
    ('"You must really like the rain."', "", "statement"),
    ('"I don\'t think you need to go."', "", "statement"),
    ('"Talk is cheap."', "", "statement"),
    ('"Help is not coming."', "", "statement"),
    ('"Listen, I can\'t help you."', "", "statement"),
    ('"Wait, I never said that."', "", "statement"),
    ('"Look, I don\'t know."', "", "statement"),
    ('"Come on, you know I can\'t."', "", "statement"),
    ('"Look here, I owe you nothing."', "", "statement"),
    ('"See here, that is mine."', "", "statement"),
    ('"Come here."', "", "statement"),
    ('"Come on, tell me."', "", "statement"),
    ('"Look at me."', "", "statement"),
    ('"Please help me."', "", "statement"),
    ('"Okay, go."', "", "statement"),
    ('"Promise me."', "", "statement"),
    ('"Listen, find him before dawn."', "", "statement"),
    ('"Wait."', "", "statement"),
    ('"Listen."', "", "statement"),
    ('"Please, help me."', "", "statement"),
    ('"Hold still."', "", "statement"),
    ('"I need you to find him."', "", "statement"),
    ('"Running late."', "", "statement"),
    ('"Moved on."', "", "statement"),
    (ABBR + " " + G + Q + "?" + G, "", "question"),
    (ABBR + " " + G + NO + "." + G, "", "statement"),
    (G + Q + "?" + G + chr(0x200F), "", "question"),
    (G + Q + "?" + G + chr(0x200E), "", "question"),
    ('"Who sent you?"' + chr(0x200F), "", "question"),
    ('She says, "No thanks"', "", "statement"),
    ('She says, "No thanks".', "", "after_quote"),
    ('She shrugs. "Not my problem"', "", "statement"),
    ('He pockets the 6" ruler and says, "Not yet."', "", "unbalanced"),
    ('"You said ' + "'why?'" + '"', "", "question"),
    ('"(Really?)"', "", "question"),
    ('"Coming?..."', "", "question"),
    ('"No" she says.', "", "after_quote"),
    ('She says "No thanks" and walks off.', "", "after_quote"),
    ('He grins. "Your move"?', "", "stock"),
    ('Rain falls. What do you "do"?', "", "stock"),
    ('Rain falls. What do "you" do?', "", "stock"),
    ('Rain falls. "Your" move?', "", "stock"),
    ('Rain falls. "What do "you" do?"', "", "stock"),
    ('Rain falls. "Now, what' + chr(0x2019) + 's "your" next move?"', "", "stock"),
    ('("No thanks")', "", "after_quote"),
    ('She asks, "Coming"?', "", "narrator_question"),
    # The player's line is judged within a quote the player typed, word for word.
    ('She asks, "No?"', 'I say "I know?"', "question"),
    ('She asks, "You coming?"', 'I say "u coming"', "player_line"),
] + STOCK_ROWS

# (name, kind, payload, move, reason); the move None means the turn's own player input.
FAILURE_CASES = [
    ("provider", "raise", ProviderError(503, {"kind": "upstream"}), None, "provider"),
    ("timeout", "raise", TimeoutError(), None, "timeout"),
    ("runtime", "raise", RuntimeError("x"), None, "RuntimeError"),
    ("empty", "script", (EMPTY, "end_turn"), None, "empty"),
    ("cut", "script", (tagged("Mara says"), "max_tokens"), None, "cut"),
    ("long", "script", (tagged("A" * 500 + "?"), "end_turn"), None, "long"),
    # A beat that is a stock prompt fails the re-check.
    (
        "stock_beat",
        "script",
        (tagged('Mara asks, "What do you do?"'), "end_turn"),
        None,
        "recheck",
    ),
    (
        "marker_beat",
        "script",
        (tagged("What do you say to that?"), "end_turn"),
        'I say "No."',
        "verbatim",
    ),
    # A beat the verbatim gate lets through (the reply already holds the move's quote) but whose
    # re-check reads as the player's own line; its quoted line is also a whole quoted line of the
    # move, so the repeat guard (which runs first) refuses it.
    (
        "player_line_recheck",
        "script",
        (tagged('Mara frowns. "No."'), "end_turn"),
        'I say "No."',
        "repeat",
    ),
    ("unbalanced_beat", "script", (tagged('Mara says "Wait.'), "end_turn"), None, "recheck"),
    (
        "player_line_beat",
        "script",
        (tagged('"No."'), "end_turn"),
        'I say "No."',
        "verbatim",
    ),
    (
        "player_quote_beat",
        "script",
        (tagged('Mara leans closer. "No. Who sent you?"'), "end_turn"),
        'I say "No."',
        "verbatim",
    ),
    (
        "repeat_reply_quote",
        "script",
        (tagged('Mara leans in. "Not yet."'), "end_turn"),
        None,
        "repeat",
    ),
    # A beat that repeats the player's quoted words is caught by the verbatim gate, which runs
    # before the repeat guard.
    (
        "repeat_move",
        "script",
        (tagged('"Go on then."'), "end_turn"),
        'I say "Go on then."',
        "verbatim",
    ),
    (
        "repeat_unquoted_move",
        "script",
        (tagged('"Why not?"'), "end_turn"),
        "I ask why not?",
        "repeat",
    ),
    (
        "repeat_narrator_question",
        "script",
        (tagged("Will you follow her?"), "end_turn"),
        None,
        "repeat",
    ),
    (
        "repeat_question_style",
        "script",
        (tagged('"Waiting for someone, or running from them?"'), "end_turn"),
        None,
        "repeat",
    ),
    # A one-word quoted line that is a whole quoted line of the reply: only the quoted-line
    # rule catches it (the whole beat is not in the reply, the span and the last sentence are
    # one word).
    (
        "repeat_short_quoted_line",
        "script",
        (tagged('Mara shakes her head. "Wait."'), "end_turn"),
        None,
        "repeat",
    ),
    # A beat that is the reply's own last sentence: only the whole-beat rule catches it (two words).
    ("repeat_whole_beat", "script", (tagged("Nothing moves."), "end_turn"), None, "repeat"),
    # A beat with a paragraph whose quote marks do not pair up, though its last paragraph passes.
    (
        "unbalanced_paragraph_beat",
        "script",
        (tagged('She says, "Wait.\n\nThe door closes.'), "end_turn"),
        None,
        "recheck",
    ),
]

# (name, kind, payload, move, shape of the beat as added): a statement, after_quote or
# no_speech beat is added as the last paragraph.
ADDED_CASES = [
    (
        "shrug_statement_beat",
        "script",
        (tagged('Mara shrugs. "Fine."'), "end_turn"),
        None,
        "statement",
    ),
    (
        "statement_beat",
        "script",
        (tagged('Mara leans in. "You must be tired."'), "end_turn"),
        None,
        "statement",
    ),
    (
        "demand_shaped_statement_beat",
        "script",
        (tagged('Mara looks away. "Nobody can help me."'), "end_turn"),
        None,
        "statement",
    ),
    (
        "demand_beat",
        "script",
        (tagged('Mara leans in. "Get out of here."'), "end_turn"),
        None,
        "statement",
    ),
    (
        "after_quote_beat",
        "script",
        (tagged('Mara leans in. "Who sent you?" she asks.'), "end_turn"),
        None,
        "after_quote",
    ),
    ("default_reply", "default", None, None, "no_speech"),
]

# (name, kind, payload, move, expected exception): a blocked repair refuses the whole move.
BLOCK_CASES = [
    ("blocked_exc", "raise", moderation.OutputBlocked("pack"), None, moderation.OutputBlocked),
    ("no_lists", "raise", moderation.ListsUnavailable("pack"), None, moderation.ListsUnavailable),
    ("filter", "script", (tagged("x"), "content_filter"), None, moderation.OutputBlocked),
]
REFUSED = {
    moderation.OutputBlocked: "refused:blocked",
    moderation.ListsUnavailable: "refused:lists_unavailable",
}

# A case whose reply is not MISS: name -> (reply chunk, its shape before the repair).
OTHER_REPLY = {
    "player_line_recheck": ('You say, "No." Mara shrugs and turns away.', "after_quote"),
    "repeat_narrator_question": ('Will you follow her? Mara turns. "Not yet."', "statement"),
    "repeat_short_quoted_line": ('Mara stops. She says, "Wait."', "statement"),
    "repeat_whole_beat": ('Mara says, "Fine." Nothing moves.', "after_quote"),
    "unbalanced_paragraph_beat": ('Mara says, "Fine."', "statement"),
    "repeat_question_style": (
        "A shadow moves. "
        + C["<<"]
        + "Waiting for someone, or running from them?"
        + C[">>"]
        + " they ask.",
        "after_quote",
    ),
}


def reply_of(case):
    return OTHER_REPLY.get(case[0], (MISS, "statement"))[0]


def prepare_case(m, case):
    """The turn state for the case's reply (MISS unless listed), with its failure set up; m
    is a monkeypatch (or a context of one)."""
    _name, kind, payload, _move, _reason = case
    ts = world_step_state(new_testpack_save())
    ts.narrative_chunks = [reply_of(case)]
    if kind == "raise":

        def boom(*args, **kwargs):
            raise payload

        m.setattr(hook_repair, "call_llm_reply", boom)
    elif kind == "script":
        m.setattr(fake, "SCRIPT", [payload])
    else:
        m.setattr(fake, "RULES", [r for r in fake.RULES if r[0] != "BEAT FOCUS - hook"])
    return ts


def run_repair(ts, case):
    with bound("fake"):
        return repair_hook(ts, ts.player_input if case[3] is None else case[3])


def run_failure_case(m, case):
    """One repair of the case's reply under its failure."""
    ts = prepare_case(m, case)
    return ts, run_repair(ts, case)


def test_live_endings():
    misses = 0
    for name, move, ending, expected in live_rows():
        got = check_hook(ending, move)
        assert got == expected, (name, got, expected)
        misses += got not in PASS_SHAPES
    assert misses == 9, misses
    print(f"PASS {len(live_rows())} live endings keep their shape; {misses} of them miss")


def test_shapes_and_boundaries():
    for text, move, expected in BOUNDARY_ROWS:
        got = check_hook(text, move)
        assert got == expected, (text, move, got, expected)
    assert "unbalanced" in MISS_SHAPES and "unbalanced" not in PASS_SHAPES
    print(f"PASS {len(BOUNDARY_ROWS)} boundary texts give their shape")


def test_passing_reply_makes_no_call(pack, monkeypatch, calls):
    on_branch(monkeypatch, "no_ceiling")
    ts = world_step_state(new_testpack_save())
    ts.narrative_chunks = [c(A4)]
    with bound("fake"):
        report = repair_hook(ts, ts.player_input)
    assert len(calls) == 0
    assert ts.narrative_chunks == [c(A4)]
    assert (report.before, report.repair, report.after) == ("question", "none", "question")
    print("PASS a reply that ends on a question makes no call and is left as it is")


def test_unbalanced_reply_gets_one_hook_call(pack, monkeypatch, calls):
    on_branch(monkeypatch, "no_ceiling")
    monkeypatch.setattr(fake, "SCRIPT", [(tagged('Mara leans in. "Who sent you?"'), "end_turn")])
    ts = world_step_state(new_testpack_save())
    ts.narrative_chunks = ['He pockets the 6" ruler and says, "Not yet."']
    with bound("fake"):
        report = repair_hook(ts, "")
    assert len(hook_calls(calls)) == 1
    assert (report.before, report.repair, report.after) == ("unbalanced", "added", "question")
    print("PASS an unbalanced reply gets one hook call and the beat is added")


@pytest.mark.parametrize("kind", ["world_step", "craft"])
@pytest.mark.parametrize("branch", BRANCHES)
def test_miss_makes_one_call_on_the_cached_prefix(pack, monkeypatch, calls, branch, kind):
    on_branch(monkeypatch, branch)
    monkeypatch.setattr(fake, "SCRIPT", [(tagged(MISS), "end_turn")])
    ts = world_step_state(new_testpack_save())
    with bound("fake"):
        if kind == "craft":
            ts = craft_state(ts)
            CraftBeat().run(ts)
        else:
            DMWorldStepBeat().run(ts)
        assert ts.narrative == MISS
        tokens_before = ts.turn_tokens_in
        report = repair_hook(ts, ts.player_input)
    assert len(calls) == 2
    assert calls[1]["system"] == [calls[0]["system"][0]]
    assert calls[1]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert calls[1]["model"] == calls[0]["model"]
    assert calls[1]["max_tokens"] == 150
    assert "BEAT FOCUS - hook" in texts(calls[1])[1]
    assert ts.narrative == MISS + "\n\n" + HOOK
    row = ts.by_beat[-1]
    assert row["id"] == "aux:hook_repair"
    assert ts.turn_tokens_in == tokens_before + row["tokens_in"]
    assert (report.before, report.repair, report.after) == ("statement", "added", "question")
    print(f"PASS {kind} ({branch}): a miss makes one call on the turn's cached prefix")


@pytest.mark.parametrize("case", FAILURE_CASES, ids=[case[0] for case in FAILURE_CASES])
def test_failed_repair_leaves_the_reply(pack, monkeypatch, calls, case):
    on_branch(monkeypatch, "no_ceiling")
    ts, report = run_failure_case(monkeypatch, case)
    assert ts.narrative_chunks == [reply_of(case)]
    assert report.repair == "failed:" + case[4], report
    assert report.after == report.before == OTHER_REPLY.get(case[0], (MISS, "statement"))[1]
    if case[1] != "raise":
        assert ts.by_beat[-1]["id"] == "aux:hook_repair", ts.by_beat
    print(f"PASS {case[0]}: no exception, the reply is untouched, repair={report.repair}")


@pytest.mark.parametrize("case", ADDED_CASES, ids=[case[0] for case in ADDED_CASES])
def test_statement_beat_is_added(pack, monkeypatch, calls, case):
    on_branch(monkeypatch, "no_ceiling")
    ts, report = run_failure_case(monkeypatch, case)
    beat = ts.narrative_chunks[-1]
    assert ts.narrative_chunks == [MISS, beat] and beat != MISS
    assert ts.narrative.endswith("\n\n" + beat)
    assert (report.before, report.repair, report.after, report.beat) == (
        "statement",
        "added",
        case[4],
        case[4],
    ), report
    print(f"PASS {case[0]}: a {case[4]} beat is added as the last paragraph")


@pytest.mark.parametrize("case", BLOCK_CASES, ids=[case[0] for case in BLOCK_CASES])
def test_blocked_repair_refuses_the_move(pack, monkeypatch, calls, call_lines, case):
    on_branch(monkeypatch, "no_ceiling")
    ts = prepare_case(monkeypatch, case)
    call_lines.clear()
    with pytest.raises(case[4]):
        run_repair(ts, case)
    assert ts.narrative_chunks == [reply_of(case)]
    lines = hook_lines(call_lines)
    assert len(lines) == 1, lines
    assert "repair=" + REFUSED[case[4]] + " " in lines[0], lines[0]
    print(f"PASS {case[0]}: the move is refused, the reply is untouched, repair={REFUSED[case[4]]}")


def test_repair_log_carries_beat_shape(pack, monkeypatch, calls, call_lines):
    on_branch(monkeypatch, "no_ceiling")
    # (reply, scripted beat or None for the stock hook rule, expected fragment)
    cases = [
        (MISS, tagged('Mara asks, "What do you do?"'), "repair=failed:recheck", "beat_shape=stock"),
        (MISS, tagged('Mara shrugs. "Fine."'), "repair=added", "beat_shape=statement"),
        (MISS, None, "repair=added", "beat_shape=question"),
        (c(A4), None, "repair=none", "beat_shape=-"),
    ]
    for reply, script, repair, beat in cases:
        ts = world_step_state(new_testpack_save())
        ts.narrative_chunks = [reply]
        monkeypatch.setattr(fake, "SCRIPT", [(script, "end_turn")] if script else [])
        call_lines.clear()
        with bound("fake"):
            repair_hook(ts, ts.player_input)
        lines = hook_lines(call_lines)
        assert len(lines) == 1, lines
        line = lines[0]
        assert line.startswith("hook_check path="), line
        assert repair in line and line.endswith(beat), line
        assert line.index("after_shape=") < line.index("beat_shape="), line
    print("PASS the hook_check line ends with the beat's shape, or - when no beat was judged")


def test_repeat_guard_needs_whole_words():
    assert (
        hook_repair._repeats('Mara frowns. "So what?"', "She had it all, also what? Nothing.", "")
        is False
    )
    assert (
        hook_repair._repeats('Mara frowns. "So what?"', "He shrugs. So what? Nothing.", "") is True
    )
    said = "They ask, " + C["<<"] + "Waiting for someone, or running from them?" + C[">>"]
    assert hook_repair._repeats('"Waiting for someone, or running from them?"', said, "") is True
    said = 'She says, "I don' + chr(0x2019) + 't know who sent me."'
    assert hook_repair._repeats('"I don\'t know who sent me."', said, "") is True
    # An unquoted narrator-question beat repeats the reply's sentence; the same words in
    # another sentence do not.
    assert hook_repair._repeats("Will you ask again?", "He waits. Will you ask again?", "") is True
    assert (
        hook_repair._repeats("Will you ask her again?", "He waits. Will you ask again?", "")
        is False
    )
    print("PASS the repeat guard matches whole words in any quote style")


def test_beat_is_post_processed(pack, monkeypatch, calls):
    on_branch(monkeypatch, "no_ceiling")
    narrative = 'Mara leans in - [DM NOTE x] "Who sent you?"'
    monkeypatch.setattr(fake, "SCRIPT", [(tagged(narrative), "end_turn")])
    ts = world_step_state(new_testpack_save())
    ts.narrative_chunks = [MISS]
    with bound("fake"):
        report = repair_hook(ts, ts.player_input)
    assert report.repair == "added", report
    beat = ts.narrative_chunks[-1]
    assert (
        beat
        == _strip_metagame_brackets(_strip_trailing_prompt(_replace_em_dashes(narrative))).strip()
    )
    assert "[DM" not in beat and " - " not in beat
    print("PASS the beat goes through the same clean-up as the other beats before it is added")


def new_game(selfhost):
    r = selfhost.post("/api/game/new", json=NEW_BODY)
    assert r.status_code == 200, r.text
    return r.json()["save"]


def hook_lines(call_lines):
    return [line for line in call_lines if line.startswith("hook_check ")]


TURN_CASES = [
    "added",
    "pass",
    "raw_block",
    "decoded_block",
    "narrator_question_splice",
    "prepend",
    "quoted_npc",
]


@pytest.mark.parametrize("case", TURN_CASES)
def test_turn_api(selfhost, monkeypatch, calls, call_lines, case):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    move, reply, repair_calls = "I wait", MISS, 1
    expect = MISS
    fragment = "repair=refused:blocked"
    if case == "added":
        expect = MISS + "\n\n" + HOOK
        fragment = "before=miss shape=statement repair=added after=pass after_shape=question"
    elif case == "pass":
        reply = expect = c(A4)
        repair_calls = 0
        fragment = "before=pass shape=question repair=none"
    elif case == "raw_block":
        rule = ("BEAT FOCUS - hook", "The clerk says " + PACK + ".")
        monkeypatch.setattr(fake, "RULES", [rule] + fake.RULES)
    elif case == "decoded_block":
        _decoder(monkeypatch, parser, {"location": PACK})
        delta = '{"' + MARK + '": 1}'
        rule = (
            "BEAT FOCUS - hook",
            "[NARRATIVE]\n" + HOOK + "\n[/NARRATIVE]\n[STATE_DELTA]\n" + delta + "\n[/STATE_DELTA]",
        )
        monkeypatch.setattr(fake, "RULES", [rule] + fake.RULES)
    elif case == "narrator_question_splice":
        # Pins the current output of a separate known defect in the verbatim splice (a missing
        # player quote spliced into a narrator question); when that defect is fixed this
        # expectation changes.
        move = 'I say "No."'
        reply = "Mara studies the ledger. Who do you tell about the locked door?"
        expect = 'Mara studies the ledger. Who do you tell, "No."' + "\n\n" + HOOK
        fragment = "before=miss shape=player_line repair=added after=pass after_shape=question"
    elif case == "prepend":
        move = 'I say "We go at dawn."'
        expect = 'You say, "We go at dawn."\n\n' + MISS + "\n\n" + HOOK
        fragment = "shape=statement repair=added"
    else:
        move = 'I say "No."'
        reply = 'Mara studies you. "What do you say to the man at the door?"'
        expect = 'You say, "No."\n\n' + reply
        repair_calls = 0
        fragment = "before=pass shape=question repair=none"
    monkeypatch.setattr(fake, "DEFAULT_REPLY", reply)
    save = new_game(selfhost)
    calls.clear()
    call_lines.clear()
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": move})
    if case in ("raw_block", "decoded_block"):
        assert r.status_code == 451, r.text
        assert r.json()["detail"]["error"] == "output_blocked", r.text
        assert PACK not in r.text and "save" not in r.json()
        lines = hook_lines(call_lines)
        assert len(lines) == 1, lines
        assert fragment in lines[0], lines[0]
        blocks = [line for line in call_lines if line.startswith("output_blocked category=")]
        assert len(blocks) == 1, blocks
        site = blocks[0].split("site=")[1]
        assert "hook_repair:" in site or "llm_client:" in site, site
        assert PACK not in blocks[0] and len(hook_calls(calls)) == 1
        print(f"PASS turn api ({case}): {lines[0]} | {blocks[0]}")
        return
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["narrative"] == expect
    assert body["save"]["history"][-1]["content_en"] == expect
    assert len(hook_calls(calls)) == repair_calls
    lines = hook_lines(call_lines)
    assert len(lines) == 1, lines
    assert fragment in lines[0], lines[0]
    if case == "added":
        assert any(b["id"] == "aux:hook_repair" and b["tokens_in"] > 0 for b in body["beats"])
    print(f"PASS turn api ({case}): {lines[0]}")


def test_placement_dice_api(selfhost, monkeypatch, calls, call_lines):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    # A move whose post-DM dice gate fires wastes no repair call.
    monkeypatch.setattr(fake, "DEFAULT_REPLY", MISS)
    # The post-DM marker is stripped before the gate reads it today, so the test feeds the
    # marker to the real detector to pin that a gated move makes no repair call.
    real = dice_gate.detect_dice_in_narrative
    monkeypatch.setattr(
        dice_gate,
        "detect_dice_in_narrative",
        lambda narrative: real(narrative + " [ROLL:Persuasion:13]"),
    )
    save = new_game(selfhost)
    save["config"]["dice_mechanics"] = True
    calls.clear()
    call_lines.clear()
    r = selfhost.post("/api/game/turn", json={"save": save, "player_input": "I wait"})
    sites = [line.split("site=")[1].split(" ")[0] for line in call_lines if "site=" in line]
    print(f"dice gate move: {len(calls)} calls, sites {sites}")
    assert "dice_roll" in r.text, r.text
    assert any("DMWorldStepBeat" in s for s in sites), sites
    assert not any("dice_gate" in site for site in sites), sites
    assert hook_calls(calls) == []
    assert hook_lines(call_lines) == []
    print("PASS a move whose dice gate fires makes no repair call and logs no hook_check line")


def test_placement_roster_api(selfhost, monkeypatch, calls, call_lines):
    monkeypatch.setenv("ENGINE_FAKE_PROVIDER", "1")
    # The repair is built on the stable block the turn's own call sent, roster included.
    monkeypatch.setattr(fake, "DEFAULT_REPLY", MISS)
    save = new_game(selfhost)
    roster = {
        "player_kit": {},
        "npcs": [
            {
                "npc_id": "zz1",
                "name": "Zed",
                "archetype": "fixer",
                "appearance": "grey coat",
                "backstory": "-",
                "origin_alignment": "-",
                "core_personality": "calm",
            }
        ],
    }
    monkeypatch.setattr(lore, "get_seed_roster", lambda pack: roster)
    save["seed_roster"] = {}

    def move(doc):
        calls.clear()
        r = selfhost.post("/api/game/turn", json={"save": doc, "player_input": "I wait"})
        assert r.status_code == 200, r.text
        turns = [c_ for c_ in calls if "[HOOK CHECK]" in texts(c_)[1]]
        repairs = hook_calls(calls)
        assert len(turns) == 1 and len(repairs) == 1
        return r, turns[0], repairs[0]

    r, turn1, rep1 = move(save)
    assert rep1["system"] == [turn1["system"][0]]
    assert r.json()["save"]["seed_roster"]["npcs"][0]["name"] == "Zed"
    r, turn2, rep2 = move(r.json()["save"])
    assert turn2["system"][0] != turn1["system"][0]
    assert rep2["system"] == [turn2["system"][0]]
    print("PASS the repair follows the turn's stable block and roster")


ACT_MISS = ACT.replace("The bell rings once.", 'The bell rings once. The observer says, "Not yet."')
ABILITY_SCRIPTS = {
    "npc_statement": [ACT, GROUND_HOOK, tagged('The observer says, "Not yet."')],
    "npc_dropped": [ACT, tagged('Rain on the bell. The observer says, "Not yet."'), EMPTY],
    "grounding_dropped": [ACT_MISS, EMPTY, EMPTY, EMPTY],
}


@pytest.mark.parametrize("case", list(ABILITY_SCRIPTS))
@pytest.mark.parametrize("branch", BRANCHES)
def test_ability_split_judges_the_last_nonempty_chunk(pack, monkeypatch, calls, branch, case):
    on_branch(monkeypatch, branch)
    ts = consult_state(make_save(), "I ring the bell")
    ts.consult = False
    ts.has_npcs = True
    monkeypatch.setattr(fake, "SCRIPT", [(text, "end_turn") for text in ABILITY_SCRIPTS[case]])
    with bound("fake"):
        build_pipeline_for("ability", consult=ts.consult).run(ts)
        assert fake.SCRIPT == []
        report = repair_hook(ts, ts.player_input)
    assert report.before == "statement", (report, ts.narrative)
    repairs = hook_calls(calls)
    assert len(repairs) == 1
    acts = [call for call in calls if "BEAT FOCUS - act" in "\n".join(texts(call))]
    assert len(acts) == 1
    assert repairs[0]["system"] == [acts[0]["system"][0]]
    assert ts.narrative.endswith("\n\n" + HOOK)
    print(f"PASS ability split ({case}, {branch}): the last non-empty chunk is judged and repaired")


def test_repair_prompt_forbids_a_new_voice():
    assert "Never bring in a new or unnamed voice." in _REPAIR_FOCUS
    print("PASS the repair prompt forbids a new or unnamed voice")


def test_prompt_is_ascii():
    assert _REPAIR_FOCUS.isascii()
    assert HOOK_LAST.isascii()
    assert chr(92) + "u" not in inspect.getsource(hook_repair)
    print("PASS the repair prompt and the hook closer are ASCII, and the module has no escapes")
