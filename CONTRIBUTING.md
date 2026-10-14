# Contributing

Thanks for your interest. Bug reports and small, focused pull requests are
welcome.

## Before you start

- Open an issue first for anything larger than a small fix, so the change can
  be discussed before you spend time on it.
- Never put an API key, a passphrase or a real save file in an issue, a test
  or a commit.

## Running the checks

Everything runs in containers. Use the test project name so the cleanup can
never touch the volumes of your own install:

```sh
docker compose -p coherence-standalone-test --profile test run --rm engine-test
docker compose -p coherence-standalone-test --profile test run --rm web-test
docker compose -p coherence-standalone-test --profile test down -v
```

`scripts/test.sh` runs the full set, including the browser tests and the
hosted-mode check.

## Style

- Python: `ruff check` and `ruff format` pass; names follow the `N` rules.
- JavaScript: `oxlint` passes (`npm run check` in `web/`).
- Text files are ASCII only. Run `python tools/leak_scan.py --generic` (in the
  test container) before you open a pull request.
- Most of the web client's text lives in `web/src/copy.js`; a few labels are still in `web/src/app.jsx` and `web/src/api/client.js`, and the engine sends some messages of its own.
- Keep comments short and plain.

## Pull requests

- One change per pull request, with a test that fails without it.
- Describe what changed and why in a few lines.
- By contributing you agree that your work is licensed under the PolyForm
  Noncommercial License 1.0.0, like the rest of the project.
