"""Verbatim player quotes: the speech marker's separator, and the quotations the
narrative already carries, which no marker may be used inside or across."""

import itertools
import random

import pytest

from app.game import quoted_speech
from app.game.quoted_speech import ensure_verbatim

EM_DASH = chr(0x2014)
MOVE = 'I say "we leave at dawn"'


@pytest.mark.parametrize("sep", [",", EM_DASH, ":", ""])
def test_separator_replaces_the_paraphrase_in_place(sep):
    fixed, count = ensure_verbatim(MOVE, "You say" + sep + " we should go at dawn.")
    assert (fixed, count) == ('You say, "we leave at dawn".', 1)
    print(f"PASS a paraphrase after the separator {sep!r} is replaced in place")


@pytest.mark.parametrize(
    "text", ["You said/ that.", "You say3 x.", "You say.x y.", "You say-so it."]
)
def test_digit_slash_dot_or_hyphen_is_not_a_separator(text):
    assert quoted_speech._SPEECH_MARKER.search(text) is None
    print(f"PASS no separator is taken from {text!r}")


def test_a_present_quote_is_not_overwritten_by_a_missing_one():
    said = 'I say "wait here" and "bring the lamp"'
    fixed, count = ensure_verbatim(said, 'You say, "wait here".')
    assert count == 1
    assert 'You say, "wait here".' in fixed
    assert '"bring the lamp"' in fixed
    print("PASS a quote the narrative carries survives a repair of another")


def test_leftover_quotes_are_prepended_in_the_players_order():
    said = 'I say "first thing" then "second thing"'
    narrative = "The room is quiet."
    fixed, count = ensure_verbatim(said, narrative)
    assert count == 2
    assert fixed == ('You say, "first thing".\n\nYou say, "second thing".\n\n' + narrative)
    print("PASS two leftover quotes come out in the order the player wrote them")


def test_quotes_come_back_in_document_order_across_flavors():
    curly = chr(0x201C) + "curly line" + chr(0x201D)
    said = f'I whisper {curly} and then I say "straight line"'
    assert quoted_speech.extract_player_quotes(said) == ["curly line", "straight line"]
    print("PASS a curly quote before a straight one is returned first")


def test_present_quote_is_kept_free_marker_filled_rest_prepended():
    said = 'I say "alpha one", "beta two" and "gamma three"'
    narrative = 'You say, "alpha one". Then you add we rest.'
    fixed, count = ensure_verbatim(said, narrative)
    assert count == 2
    assert fixed == ('You say, "gamma three".\n\nYou say, "alpha one". Then you add, "beta two".')
    print("PASS present kept, second marker filled, third quote prepended")


def test_a_present_quote_with_a_full_stop_inside_is_not_cut():
    said = 'I say "Stop. Now." and "we rest here"'
    fixed, count = ensure_verbatim(said, 'You say, "Stop. Now." You add we rest.')
    assert (fixed, count) == ('You say, "Stop. Now." You add, "we rest here".', 1)
    print("PASS a present quote holding a full stop is left intact")


def test_a_present_curly_quote_with_a_full_stop_inside_is_not_cut():
    open_q, close_q = chr(0x201C), chr(0x201D)
    present = f"{open_q}Stop. Now.{close_q}"
    said = 'I say "Stop. Now." and "we rest here"'
    fixed, count = ensure_verbatim(said, f"You say, {present} You add we rest.")
    assert (fixed, count) == (f'You say, {present} You add, "we rest here".', 1)
    print("PASS a present curly quote holding a full stop is left intact")


def test_a_marker_inside_a_present_quotation_is_not_used():
    said = 'I say "Listen. You said wait. I waited." and "bring the lamp"'
    fixed, count = ensure_verbatim(said, 'You say, "Listen. You said wait. I waited."')
    assert (fixed, count) == (
        'You say, "bring the lamp".\n\nYou say, "Listen. You said wait. I waited."',
        1,
    )
    print("PASS a speech marker inside a present quotation is left alone")


def test_a_marker_inside_a_present_curly_quotation_is_not_used():
    open_q, close_q = chr(0x201C), chr(0x201D)
    present = f"{open_q}Listen. You said wait. I waited.{close_q}"
    said = 'I say "Listen. You said wait. I waited." and "bring the lamp"'
    fixed, count = ensure_verbatim(said, f"You say, {present}")
    assert (fixed, count) == (f'You say, "bring the lamp".\n\nYou say, {present}', 1)
    print("PASS a speech marker inside a present curly quotation is left alone")


def test_a_marker_over_a_present_quote_in_single_quotes_is_not_used():
    said = 'I say "Stop. Now." and "we rest here"'
    fixed, count = ensure_verbatim(said, "You say, 'Stop. Now.' You add we rest.")
    assert (fixed, count) == ("You say, 'Stop. Now.' You add, \"we rest here\".", 1)
    print("PASS a marker that overlaps a present single-quoted quote is not used")


def test_a_marker_inside_a_quotation_across_lines_is_not_used():
    narrative = 'The guard says, "Listen.\nYou said wait. I waited."'
    fixed, count = ensure_verbatim('I say "bring the lamp"', narrative)
    assert (fixed, count) == ('You say, "bring the lamp".\n\n' + narrative, 1)
    print("PASS a speech marker inside a quotation over a line break is left alone")


def test_a_marker_inside_a_quotation_over_500_characters_is_not_used():
    narrative = 'The guard says, "' + "x" * 520 + '. You said wait. I waited."'
    fixed, count = ensure_verbatim('I say "bring the lamp"', narrative)
    assert (fixed, count) == ('You say, "bring the lamp".\n\n' + narrative, 1)
    print("PASS a speech marker inside a quotation over 500 characters is left alone")


def test_a_marker_inside_an_ascii_single_quotation_is_not_used():
    narrative = "The guard says 'You said wait. Fine.'"
    fixed, count = ensure_verbatim('I say "bring the lamp"', narrative)
    assert (fixed, count) == ('You say, "bring the lamp".\n\n' + narrative, 1)
    print("PASS a speech marker inside an ASCII single quotation is left alone")


def test_a_marker_opening_an_ascii_single_quotation_is_not_used():
    narrative = "You say, 'We wait. Then go.'"
    fixed, count = ensure_verbatim('I say "bring the lamp"', narrative)
    assert (fixed, count) == ('You say, "bring the lamp".\n\n' + narrative, 1)
    print("PASS a speech marker opening an ASCII single quotation is left alone")


def test_a_contraction_does_not_block_a_marker():
    fixed, count = ensure_verbatim('I say "we go at dawn"', "It's late. You say we should rest.")
    assert (fixed, count) == ('It\'s late. You say, "we go at dawn".', 1)
    print("PASS an apostrophe in a contraction leaves the marker usable")


def test_a_quotation_elsewhere_leaves_the_marker_usable():
    fixed, count = ensure_verbatim(
        'I say "we go at dawn"', '"Halt!" the guard shouts. You say we should leave soon.'
    )
    assert (fixed, count) == ('"Halt!" the guard shouts. You say, "we go at dawn".', 1)
    print("PASS a quotation that does not overlap the marker leaves it usable")


CO, CC = chr(0x201C), chr(0x201D)
SO, SC = chr(0x2018), chr(0x2019)
G = chr(0x05F4)
ACRONYM = chr(0x5E6) + chr(0x5D4) + chr(0x5F4) + chr(0x5DC)
LAMP = 'I say "bring the lamp"'


def _prepended(narrative):
    return ('You say, "bring the lamp".\n\n' + narrative, 1)


def test_a_plural_possessive_does_not_close_a_single_quotation():
    narrative = "The guard says 'Read the guards' orders. You said wait. I waited.'"
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS a possessive inside an ASCII single quotation does not end it")


def test_an_apostrophe_does_not_close_a_curly_single_quotation():
    narrative = "The guard says " + SO + "I don" + SC + "t care. You said wait. I waited." + SC
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS an apostrophe inside a curly single quotation does not end it")


@pytest.mark.parametrize(
    "narrative",
    [
        "The guard says" + EM_DASH + "'You said wait. I waited.'",
        "The guard says 'You said wait.'" + EM_DASH + "he turns.",
    ],
)
def test_an_em_dash_beside_an_ascii_single_quotation_does_not_hide_it(narrative):
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print(f"PASS an em dash beside a single quotation keeps it protected: {narrative!r}")


def test_a_stray_inch_mark_does_not_shift_the_pairs():
    narrative = 'The rope is 6" thick. The guard says "You said wait. I waited."'
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS an inch mark does not leave a real quotation unprotected")


def test_a_quotation_over_several_paragraphs_is_protected_throughout():
    narrative = 'The guard says, "Listen.\n\n"You said wait. I waited.\n\n"Now go."'
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS every paragraph of a multi-paragraph quotation is protected")


def test_an_unclosed_curly_opener_protects_its_paragraph():
    narrative = "The guard says " + CO + "You said wait. I waited."
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS a curly opener with no closer protects its paragraph")


def test_gershayim_inside_acronyms_are_not_quote_marks():
    narrative = f"The {ACRONYM} guard says {G}You said wait. The {ACRONYM} waits.{G}"
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS two acronym marks do not shift the gershayim pairs")


def test_an_unclosed_single_quote_runs_to_the_end_of_its_paragraph():
    narrative = "The guard says, 'Listen. You said wait.\n\nI nod."
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS an unclosed ASCII single quote protects its paragraph")


def test_a_single_quote_does_not_run_over_a_blank_line():
    narrative = "'Tis late.\n\nYou say we should rest. The dogs' bowls are empty."
    fixed, count = ensure_verbatim('I say "we go at dawn"', narrative)
    assert (fixed, count) == (
        "'Tis late.\n\nYou say, \"we go at dawn\". The dogs' bowls are empty.",
        1,
    )
    print("PASS an elision at a paragraph start does not swallow the next paragraph")


@pytest.mark.parametrize("o,c", [('"', '"'), (CO, CC)])
def test_a_quotation_elsewhere_still_leaves_the_marker_usable(o, c):
    narrative = f"{o}Halt!{c} the guard shouts. You say we should leave soon. {o}Fine,{c} he says."
    fixed, count = ensure_verbatim('I say "we go at dawn"', narrative)
    assert (fixed, count) == (
        f'{o}Halt!{c} the guard shouts. You say, "we go at dawn". {o}Fine,{c} he says.',
        1,
    )
    print(f"PASS a marker between two quotations {o}{c} is still used")


def test_a_single_quotation_elsewhere_still_leaves_the_marker_usable():
    narrative = "'Halt!' the guard shouts. You say we should leave soon."
    fixed, count = ensure_verbatim('I say "we go at dawn"', narrative)
    assert (fixed, count) == ("'Halt!' the guard shouts. You say, \"we go at dawn\".", 1)
    print("PASS a marker after a single quotation is still used")


def test_an_acronym_mark_does_not_block_a_repair():
    narrative = f"The {ACRONYM} patrol passes. You say we should hide."
    fixed, count = ensure_verbatim('I say "we go at dawn"', narrative)
    assert (fixed, count) == (f'The {ACRONYM} patrol passes. You say, "we go at dawn".', 1)
    print("PASS an abbreviation mark leaves the marker usable")


def test_two_stray_marks_around_a_quotation_do_not_leave_it_open():
    narrative = '"Wait, he says. "You said wait. I waited." Then go"'
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS two openers in a row protect the paragraph, not a mispaired quotation")


def test_a_stray_closer_like_mark_before_a_quotation_does_not_leave_it_open():
    narrative = 'The rope is 6" thick. "You said wait. I waited." He nods.'
    assert ensure_verbatim(LAMP, narrative) == _prepended(narrative)
    print("PASS a mark that reads as a closer first protects the paragraph")


def test_an_apostrophe_inside_a_curly_single_player_quote_does_not_close_it():
    said = "I say " + SO + "I don" + SC + "t agree" + SC
    assert quoted_speech.extract_player_quotes(said) == ["I don" + SC + "t agree"]
    print("PASS an apostrophe inside a curly single player quote does not end it")


def test_a_curly_single_player_quote_with_an_apostrophe_is_spliced_whole():
    said = "I say " + SO + "I don" + SC + "t agree" + SC
    expected = ('You say, "I don' + SC + 't agree".', 1)
    assert ensure_verbatim(said, "You say we should wait.") == expected
    print("PASS the spliced quote keeps the words after the apostrophe")


def test_two_curly_single_player_quotes_on_one_line_stay_two():
    said = "I say " + SO + "go" + SC + ", then " + SO + "wait" + SC
    assert quoted_speech.extract_player_quotes(said) == ["go", "wait"]
    print("PASS two curly single player quotes on one line are two quotes")


def test_a_plural_possessive_inside_a_curly_single_player_quote_ends_it():
    said = "I say " + SO + "the guards" + SC + " orders stand" + SC
    assert quoted_speech.extract_player_quotes(said) == ["the guards"]
    print("PASS a plural possessive inside a curly single player quote ends it")


def test_a_later_plural_possessive_does_not_extend_a_curly_single_player_quote():
    said = "I say " + SO + "Go." + SC + " Then I read the guards" + SC + " orders."
    assert quoted_speech.extract_player_quotes(said) == ["Go."]
    print("PASS a later plural possessive does not extend a curly single quote")


def test_a_plural_possessive_after_a_curly_single_player_quote_is_not_speech():
    said = "I tell her " + SO + "run" + SC + " and grab the twins" + SC + " coats"
    assert quoted_speech.extract_player_quotes(said) == ["run"]
    print("PASS action text after a curly single quote is not extracted")


def test_a_plural_possessive_after_a_curly_single_player_quote_is_not_spliced():
    said = "I tell her " + SO + "run" + SC + " and grab the twins" + SC + " coats"
    narrative = "You say we should run now."
    assert ensure_verbatim(said, narrative) == (narrative, 0)
    print("PASS the action text after a curly single quote is not spliced")


def test_no_fragment_mix_raises_or_alters_a_quotation():
    quotations = [
        '"You said wait. I waited."',
        CO + "You said wait. I waited." + CC,
        "'You said wait. I waited.'",
        SO + "You said wait. I waited." + SC,
        G + "You said wait. I waited." + G,
    ]
    fragments = quotations + [
        '"Listen. You said wait."',
        '"Listen. You said wait.',
        CO + "Listen. You said wait." + CC,
        CO + "Listen. You said wait.",
        G + "Listen. You said wait." + G,
        G + "Listen. You said wait.",
        SO + "Listen. You said wait." + SC,
        SO + "Listen. You said wait.",
        "'Listen. You said wait.'",
        "'Listen. You said wait.",
        "'tis",
        "dogs' bowls",
        "don" + SC + "t",
        '6" thick',
        ACRONYM,
        "\n\n",
        "You say we go.",
        EM_DASH,
    ]
    cases = 0
    for said in (LAMP, 'I say "a" and "b"'):
        for parts in itertools.product(fragments, repeat=3):
            narrative = " ".join(parts)
            fixed, _ = ensure_verbatim(said, narrative)
            for q in quotations:
                assert fixed.count(q) >= narrative.count(q), (narrative, said, fixed)
            cases += 1
    print(f"PASS {cases} fragment mixes: no exception, every quotation fragment intact")


APOSTROPHES = [chr(0x2018), chr(0x2019), chr(0x02BC), "'"]
DOUBLE_QUOTES = [chr(0x201C), chr(0x201D), '"']
ELLIPSIS = chr(0x2026)


def test_a_curly_apostrophe_in_the_narrative_is_the_same_quote():
    said = 'I pour the visitor another drink and ask, "Who\'s after you?"'
    narrative = '"Who' + SC + 's after you?" you ask, the words low. The rain picks up.'
    assert ensure_verbatim(said, narrative) == (narrative, 0)
    print("PASS a curly apostrophe in the narrative counts as the straight one")


def test_a_straight_apostrophe_in_the_narrative_is_the_same_quote():
    said = "I ask " + CO + "Who" + SC + "s there?" + CC
    assert quoted_speech.extract_player_quotes(said) == ["Who" + SC + "s there?"]
    narrative = '"Who\'s there?" you ask, the words low.'
    assert ensure_verbatim(said, narrative) == (narrative, 0)
    print("PASS a straight apostrophe in the narrative counts as the curly one")


def test_curly_double_quotes_around_a_straight_quoted_player_line_are_present():
    narrative = CO + "we leave at dawn" + CC + " he repeats."
    assert ensure_verbatim(MOVE, narrative) == (narrative, 0)
    print("PASS curly double quotes in the narrative carry a straight-quoted line")


def test_a_question_quote_gets_no_stray_full_stop_when_prepended():
    narrative = "The figure steps closer."
    fixed, count = ensure_verbatim('"Coffee?"', narrative)
    assert (fixed, count) == ('You say, "Coffee?"\n\nThe figure steps closer.', 1)
    print("PASS a prepended quote ending in ? gets no full stop after it")


@pytest.mark.parametrize("end", ["?", "!", ELLIPSIS])
def test_a_paired_splice_adds_no_terminator_after_a_closed_quote(end):
    quote = "Really" + end
    for narrative in ("You say we should rest.", "You say we should rest!"):
        fixed, count = ensure_verbatim(f'I say "{quote}"', narrative)
        assert (fixed, count) == (f'You say, "{quote}"', 1)
    print(f"PASS a paired splice of a quote ending in {end!r} adds no terminator")


@pytest.mark.parametrize("end", [".", "!", "?"])
def test_a_quote_with_no_end_punctuation_keeps_the_paraphrase_terminator(end):
    fixed, count = ensure_verbatim(MOVE, "You say we should rest" + end)
    assert (fixed, count) == ('You say, "we leave at dawn"' + end, 1)
    print(f"PASS an unpunctuated quote still takes the paraphrase terminator {end!r}")


def test_a_present_quote_with_a_curly_apostrophe_is_protected_from_a_marker():
    said = 'I say "Don\'t go. You said wait." and "bring the lamp"'
    narrative = "The guard mutters Don" + SC + "t go. You said wait. Then he leaves."
    assert ensure_verbatim(said, narrative) == _prepended(narrative)
    print("PASS a marker over a present quote written with a curly apostrophe is not used")


def test_every_mix_of_apostrophe_and_double_quote_styles_compares_alike():
    same = diff = 0
    for in_ap, out_ap, open_q, close_q, outer in itertools.product(
        APOSTROPHES, APOSTROPHES, DOUBLE_QUOTES, DOUBLE_QUOTES, ['""', CO + CC]
    ):
        said = f"I say {outer[0]}Who{in_ap}s after you?{outer[1]}"
        assert quoted_speech.extract_player_quotes(said) == ["Who" + in_ap + "s after you?"]
        narrative = f"{open_q}Who{out_ap}s after you?{close_q} you ask, the words low."
        assert ensure_verbatim(said, narrative) == (narrative, 0), (said, narrative)
        same += 1
        other = f"The figure leans in. {open_q}Who{out_ap}s after me?{close_q} the words low."
        fixed, count = ensure_verbatim(said, other)
        assert count == 1, (said, other)
        assert fixed == f'You say, "Who{in_ap}s after you?"\n\n' + other, (said, other)
        diff += 1
    assert same + diff >= 500
    print(f"PASS {same + diff} style mixes: {same} unchanged, {diff} spliced exactly once")


def test_hebrew_gershayim_and_ascii_double_quote_compare_alike():
    abbr = chr(0x5E6) + chr(0x5D4) + "{}" + chr(0x5DC)
    # the ASCII mark sits inside a curly-quoted line so the line is still one quote
    said_ascii = "I say " + CO + "the " + abbr.format('"') + " patrol is near" + CC
    said_g = 'I say "the ' + abbr.format(G) + ' patrol is near"'
    narr_ascii = "He mutters the " + abbr.format('"') + " patrol is near today."
    narr_g = "He mutters the " + abbr.format(G) + " patrol is near today."
    assert ensure_verbatim(said_ascii, narr_g) == (narr_g, 0)
    assert ensure_verbatim(said_g, narr_ascii) == (narr_ascii, 0)
    print("PASS gershayim and an ASCII double quote in an abbreviation compare alike")


def test_hebrew_geresh_and_ascii_apostrophe_compare_alike():
    word = chr(0x5D2) + "{}" + chr(0x5D5) + chr(0x5D6)
    said_ascii = 'I say "the ' + word.format("'") + ' gate is shut"'
    said_g = 'I say "the ' + word.format(chr(0x05F3)) + ' gate is shut"'
    narr_ascii = "He mutters the " + word.format("'") + " gate is shut today."
    narr_g = "He mutters the " + word.format(chr(0x05F3)) + " gate is shut today."
    assert ensure_verbatim(said_ascii, narr_g) == (narr_g, 0)
    assert ensure_verbatim(said_g, narr_ascii) == (narr_ascii, 0)
    print("PASS geresh and an ASCII apostrophe compare alike")


def test_a_model_that_fixes_the_players_spelling_is_not_prepended_to():
    move = (
        "i reply to Lena, \"finding people is what i do and shouldn't be a problem, "
        "do i still get paid if i find ur husband and he doesn't want to return?\""
    )
    narrative = (
        "You reply to Lena, \"Finding people is what I do and shouldn't be a problem, "
        "do I still get paid if I find your husband and he doesn't want to return?\""
    )
    assert ensure_verbatim(move, narrative) == (narrative, 0)
    print("PASS a quote with the player's spelling fixed counts as present, count 0")


@pytest.mark.parametrize(
    "quote, narrative",
    [
        ("I will not go with you", 'Mara nods. "I will go with you," you say.'),
        ("I owe you fifty credits", 'You say, "I owe you fifteen credits."'),
        ("Tell Mara I am coming", 'You say, "Tell Lena I am coming."'),
    ],
)
def test_a_changed_word_is_not_a_match(quote, narrative):
    assert quoted_speech.find_missing_quotes([quote], narrative) == [quote]
    assert ensure_verbatim(f'I say "{quote}"', narrative)[1] == 1
    print(f"PASS a changed, added or dropped word leaves {quote!r} missing")


@pytest.mark.parametrize(
    "quote, narrative",
    [
        ("can u meet me at the docks", 'You ask, "Can you meet me at the docks?"'),
        ("r we still on for tonight", 'You ask, "Are we still on for tonight?"'),
    ],
)
def test_a_chat_spelling_written_out_is_present(quote, narrative):
    assert quoted_speech.find_missing_quotes([quote], narrative) == []
    assert ensure_verbatim(f'I say "{quote}"', narrative) == (narrative, 0)
    print(f"PASS {quote!r} counts as present when the chat spelling is written out")


def test_a_question_never_matches_a_statement():
    missing = quoted_speech.find_missing_quotes
    assert missing(["you're leaving?"], "Mara frowns. You're leaving.") == ["you're leaving?"]
    assert missing(["you're leaving?"], 'Mara frowns. "You\'re leaving?"') == []
    print("PASS a question mark is kept, so a question is missing from a statement")


def test_apostrophes_and_quote_styles_do_not_hide_a_present_quote():
    missing = quoted_speech.find_missing_quotes
    assert missing(["i cant find ur key"], 'You say, "I can\'t find your key."') == []
    curly = "You say, " + chr(0x201C) + "I can" + chr(0x2019) + "t find your key." + chr(0x201D)
    assert missing(["i can't find ur key"], curly) == []
    print("PASS a dropped apostrophe and a curly apostrophe still find the quote")


@pytest.mark.parametrize(
    "quote, narrative, present",
    [
        ("no", "She does not move.", False),
        ("no", "You know the drill.", False),
        ("go", "Years ago, the docks burned.", False),
        ("yes", "Yesterday it rained.", False),
        ("run", "You keep running.", False),
        ("I wait", 'You say, "I waited."', False),
        ("?", 'She asks, "Who?"', False),
        ("no", 'You say, "No." She nods.', True),
        ("go", "Go now.", True),
    ],
)
def test_a_short_quote_inside_a_longer_word_is_missing(quote, narrative, present):
    assert (quoted_speech.find_missing_quotes([quote], narrative) == []) is present
    print(f"PASS {quote!r} against {narrative!r}: {'present' if present else 'missing'}")


def test_a_protected_span_does_not_start_inside_a_word():
    narrative = "You know it. You say no."
    spans = quoted_speech._protected_spans(narrative, ["no"])
    assert all(start != 5 for start, _end in spans), spans
    assert (narrative.rindex("no"), len(narrative) - 1) in spans, spans
    print("PASS the span of a present quote never starts inside a longer word")


@pytest.mark.parametrize(
    "line, quote, within",
    [
        ("You coming?", "u coming", True),
        ("Who sent you", "Who sent you?", True),
        ("sent you", "Who sent you? Tell me.", True),
        ("No?", "I know?", False),
        ("?", "Who?", False),
        ("Who sent them?", "Who sent you?", False),
    ],
)
def test_a_line_is_within_a_quote_by_whole_words_in_order(line, quote, within):
    assert quoted_speech.line_within_quote(line, quote) is within
    print(f"PASS {line!r} within {quote!r}: {within}")


@pytest.mark.parametrize(
    "quote, narrative, present",
    [
        ("where are you??", 'You ask, "Where are you?"', True),
        ("u there???", 'You ask, "You there?"', True),
        ("where are you ?!?", 'You ask, "Where are you?!"', True),
        ("what no way", 'You say, "What? No way."', True),
        ("really youre leaving", 'You say, "Really? You\'re leaving."', True),
        ("you're leaving?", "Mara frowns. You're leaving.", False),
        ("?!", 'You say, "Who?"', False),
        ("hey u there?", 'You say, "Hey? You there?"', True),
        ("what why would you do that?", 'You say, "What? Why would you do that?"', True),
        ("really? you're leaving", 'You say, "Really. You\'re leaving?"', False),
        ("where are you ? ?", 'You ask, "Where are you?"', True),
    ],
)
def test_a_question_mark_run_counts_as_one_and_only_the_players_own_is_matched(
    quote, narrative, present
):
    assert (quoted_speech.find_missing_quotes([quote], narrative) == []) is present
    print(f"PASS {quote!r} against {narrative!r}: {'present' if present else 'missing'}")


def test_a_short_quote_with_a_chat_spelling_is_present_and_another_still_missing():
    assert quoted_speech.find_missing_quotes(["u ok"], "Mara says: You ok?") == []
    assert quoted_speech.find_missing_quotes(["go now"], "Mara says go home.") == ["go now"]
    assert quoted_speech.find_missing_quotes(["go now"], "Mara says go now.") == []
    print("PASS a short quote counts by its words with chat spellings expanded; go now is missing")


WORDS = (
    "rain street door window glass table lamp coin ledger wire bell street river harbor "
    "market guard captain stranger letter key lock shadow candle wine bread knife coat "
    "boots road bridge tower gate wall roof stair cellar attic garden field forest hill "
    "stone mud snow wind fog night dawn noon dusk storm cloud star moon sun fire smoke "
    "ash dust blood bone skin hand eye face voice word name story song dream debt favor "
    "price deal risk plan trap secret promise warning rumor truth lie fear hope anger "
    "walk run wait watch listen speak ask tell give take hold drop open close break "
    "build carry follow leave stay return fall climb hide seek find lose pay owe trade"
).split()


def test_unrelated_narrative_rarely_matches_a_long_quote():
    rng = random.Random(55)
    narrative = " ".join(rng.choice(WORDS) for _ in range(400))
    present = 0
    for _ in range(200):
        quote = " ".join(rng.choice(WORDS) for _ in range(rng.randint(5, 25)))
        present += quoted_speech._quote_present(quote, narrative)
    assert present <= 1, present
    print(f"PASS 200 unrelated sentences against 400 words: {present} false present")
