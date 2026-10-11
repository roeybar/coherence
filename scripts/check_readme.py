"""Check README.md against the rest of the repository.

Run from the repository root inside the test container. Exit 0 when
every check passes, 1 otherwise.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADINGS = [
    "What it is", "Why it is interesting", "Screenshot", "Architecture", "Quickstart",
    "Configuration", "Tests", "simple-creds-storage", "Status", "License",
]
EXTRA_VARS = ["MESSAGE_INTERVAL_SECONDS"]
TEST_PROJECT = "coherence-standalone-test"
REQUIRED_FILES = [
    "LICENSE", "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md", ".gitignore",
    ".github/workflows/ci.yml", ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md", ".github/ISSUE_TEMPLATE/config.yml",
]


def sections(text):
    parts = re.split(r"^## (.+)$", text, flags=re.M)
    return {parts[i].strip(): parts[i + 1] for i in range(1, len(parts), 2)}


def check(readme, env_text):
    errors = []
    if not readme.isascii():
        errors.append("README.md is not ASCII")
    found = [h.strip() for h in re.findall(r"^## (.+)$", readme, flags=re.M)]
    if found != HEADINGS:
        errors.append("H2 headings are %r, expected %r" % (found, HEADINGS))
    secs = sections(readme)

    m = re.search(r"<!-- quickstart -->(.*?)<!-- /quickstart -->", readme, flags=re.S)
    if not m:
        errors.append("quickstart markers not found")
    else:
        cmds = [ln for ln in m.group(1).splitlines()
                if ln.strip() and not ln.strip().startswith(("```", "#"))]
        if not 1 <= len(cmds) <= 3:
            errors.append("quickstart has %d commands, expected 1 to 3" % len(cmds))

    config = secs.get("Configuration", "")
    names = re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", env_text, flags=re.M)
    for name in sorted(set(names + EXTRA_VARS)):
        if "`%s`" % name not in config:
            errors.append("variable not documented in Configuration: %s" % name)

    for line in secs.get("Tests", "").splitlines():
        if "docker compose" in line and ("-p " + TEST_PROJECT) not in line:
            errors.append("Tests command without -p %s: %s" % (TEST_PROJECT, line.strip()))

    for target in re.findall(r"!?\[[^\]]*\]\(([^)\s]+)\)", readme):
        if not re.match(r"[a-z]+:", target) and not (ROOT / target).is_file():
            errors.append("link does not resolve: %s" % target)

    for placeholder in re.findall(r"\{[A-Z_]+\}", readme):
        errors.append("placeholder left in README: %s" % placeholder)
    return errors


def main():
    errors = check((ROOT / "README.md").read_text(encoding="utf-8"),
                   (ROOT / ".env.example").read_text(encoding="utf-8"))
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).is_file():
            errors.append("missing file: %s" % rel)
    for e in errors:
        print("check_readme: %s" % e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
