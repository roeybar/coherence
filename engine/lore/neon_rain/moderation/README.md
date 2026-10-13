# Neon Rain pack - moderation lists

These files feed `app/game/moderation.py`'s pack-aware blocklist
layer. They're pack-scoped so other packs (custom worlds)
can ship different lists, and they're newline-delimited so they
can be edited by hand without touching code.

## Files

- **`blocklist.<locale>.txt`** - the main word list. Always active;
  a hit triggers a hard block (category `pack`).
  Source: the English list from
  [LDNOOBW](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words)
  by Shutterstock, Inc. and contributors, licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), fetched
  2026-05-16, with four entries removed, one so the file is ASCII and
  three mild-profanity entries so that they are not blocked (see
  `THIRD_PARTY_NOTICES.md` at the repository root). One word/phrase per
  line, matched at word boundaries after normalisation (see below).
- **`always_blocklist.<locale>.txt`** - the floor list. Always
  active and checked before `blocklist.<locale>.txt`; a hit triggers a
  hard block (category `pack_always`). It holds 199 entries copied from
  `blocklist.en.txt` (slurs and explicit sexual terms), so they stay
  blocked even if `blocklist.en.txt` is edited.

The four core regex rules in `app/game/moderation.py` (CSAM
proximity, weapons synthesis, doxx) are always active and are
checked first. The pack lists supplement those, they don't
override.

## Adding entries

One per line. Lines starting with `#` are comments. Whitespace
trimmed. Case is normalised to lower at load time. Multi-word
phrases are split on whitespace and matched as `\b<word1>\s+<word2>\b`.

## What is checked

The same check runs on the player's text before any model call, on
every model reply before it is shown or saved, on every record the
engine reads out of a model reply, and on every save the engine
receives (all of its text, including history, undo snapshots and the
world premise). A hit refuses the request: a blocked reply is never
shown or stored, and a blocked save is not opened or played. This
includes the short hook line the engine may ask for when a narration
does not end on a hook: if that line is blocked, the whole move is
refused. A browser save shows its name and summary only after the game
server has checked it.

Before matching, text is normalised: compatibility forms and case are
folded, accents, combining marks and invisible characters are dropped,
look-alike Cyrillic and Greek letters become Latin ones, common digit
and symbol stand-ins become letters, letters split by spaces or
punctuation are joined, even with a word before them, and a letter may
repeat more often than in the entry. Escaped characters are decoded
and quoted pieces are joined, and the result is checked too. Entries
are normalised the same way where that applies to them, and still
match whole words only.

The one non-ASCII entry of the upstream list (see
`THIRD_PARTY_NOTICES.md`) is checked in `app/game/moderation.py`.

## Locale

Only `en` is wired. Player input, model replies and saves are
filtered against the English lists; there is no translation step
before moderation, so non-English text is only matched by the English
patterns. The loader will pick up a `blocklist.<locale>.txt` for
another locale if one is added next to this readme.

## License

`blocklist.en.txt` is third-party data under CC BY 4.0 (attribution
above and in `THIRD_PARTY_NOTICES.md`); it is not covered by the
PolyForm Noncommercial 1.0.0 license of the rest of Coherence. The entries in
`always_blocklist.en.txt` are copied from `blocklist.en.txt` and are
under CC BY 4.0 too; its comment lines and this README are part of
Coherence (PolyForm Noncommercial 1.0.0).
