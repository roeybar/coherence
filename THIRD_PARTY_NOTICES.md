# Third-party notices

Coherence is licensed under the PolyForm Noncommercial License 1.0.0
(see `LICENSE`). This file lists third-party material that ships in this
repository under its own license. Those licenses apply to that material
only; they do not change the license of the rest of the code.

## List of Dirty, Naughty, Obscene, and Otherwise Bad Words (English list)

- File: `engine/lore/neon_rain/moderation/blocklist.en.txt`.
- Source: the `en` list from
  https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words,
  fetched 2026-05-16.
- Creator: Shutterstock, Inc. and the project's contributors.
- License: Creative Commons Attribution 4.0 International (CC BY 4.0),
  https://creativecommons.org/licenses/by/4.0/
- Changes: four entries were removed: one (an emoji) so the list is
  ASCII, and three mild-profanity entries so that they are not
  blocked. No entries were added; the list is otherwise as fetched.
  The engine still blocks the removed emoji entry:
  `engine/app/game/moderation.py` holds it as an escaped character.
- The material is provided as-is, without warranties; see the license
  for the full terms.

The `always_blocklist.en.txt` file next to this list holds 199 entries
copied from this list, under the same license; its comment lines are
part of Coherence.

## Newsreader (web font)

- Files: the font files in `web/public/fonts/` named for Newsreader. The
  license text is `web/public/fonts/OFL-Newsreader.txt`.
- The web app serves this font from its own origin; it is not loaded from
  a third-party host.
- License: SIL Open Font License, Version 1.1,
  https://openfontlicense.org/
- The font files are used as published, without modification. The
  material is provided as-is, without warranties; see the license for the
  full terms.

## IBM Plex Mono (web font)

- Files: the font files in `web/public/fonts/` named for IBM Plex Mono.
  The license text is `web/public/fonts/OFL-IBMPlexMono.txt`.
- The web app serves this font from its own origin; it is not loaded from
  a third-party host.
- License: SIL Open Font License, Version 1.1,
  https://openfontlicense.org/
- Reserved Font Name: "Plex". The font files are used as published,
  without modification, so the name stays in use as the license allows.
- The material is provided as-is, without warranties; see the license for
  the full terms.

## BAAI/bge-small-en-v1.5 (embedding model)

- Source: the BAAI/bge-small-en-v1.5 model published by the Beijing
  Academy of Artificial Intelligence on Hugging Face,
  https://huggingface.co/BAAI/bge-small-en-v1.5
- The engine image downloads the model through the fastembed library
  during the image build
  (`engine/Dockerfile`); the model files are not stored in this
  repository.
- License: MIT License, as published by its authors.
- The model is used as published, without modification. The material is
  provided as-is, without warranties; see the license for the full terms.
