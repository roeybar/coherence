"""Scan a tree, a commit or a message for text that must not be published.

  leak_scan.py                  scan the working tree (all rules)
  leak_scan.py --commit SHA     scan that commit's tree in the repository of the
                                current directory (no non-ASCII rule)
  leak_scan.py --message FILE   scan a commit message (all rules)
  --generic                     skip the deny list

Rules: non-ASCII text, non-ASCII escapes in engine/ and web/src/ code and
data files, key shapes, ticket-style ids, work markers, IPv4 addresses,
host paths, phone numbers and the deny list. The deny list is
tools/leak-deny.txt, one term per line, matched
as a case-insensitive substring in file contents and paths, and is required
unless --generic is given. Hits print as path:line: rule and never show the
matched text. Binary files (a NUL byte in the first 8 KiB, or a .woff2, .png,
.ico or .jpg suffix) have their contents skipped.

Exit codes: 0 clean, 1 a hit, 2 usage error or missing deny file.
"""

import argparse
import fnmatch
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent
DEFAULT_DENY = HERE / "leak-deny.txt"

KEY_SHAPES = {
    "key-shape anthropic": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "key-shape sk": r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}",
    "key-shape google": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "key-shape xai": r"\bxai-[A-Za-z0-9]{20,}",
    "key-shape github": r"\b(ghp|gho|ghs)_[A-Za-z0-9]{36}\b",
    "key-shape github-pat": r"\bgithub_pat_[A-Za-z0-9_]{22,}",
    "key-shape aws": r"\bAKIA[0-9A-Z]{16}\b",
    "key-shape jwt": r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}",
    "key-shape private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "key-shape mongodb": r"mongodb(\+srv)?://[^\s/:@]+:[^\s@]+@",
}
KEY_RES = [(name, re.compile(rx)) for name, rx in KEY_SHAPES.items()]

# A hash sign and 3-5 digits, not part of a longer hex run (a CSS colour).
TICKET_RE = re.compile(r"(?<![0-9A-Fa-f#])#\d{3,5}\b(?![0-9A-Fa-f])")
MARKER_RE = re.compile(r"\b(?:T" r"ODO|FIX" r"ME|XX" r"X)\b")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")
IPV4_ALLOWED = tuple(ipaddress.ip_network(cidr) for cidr in (
    "0.0.0.0/32", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "255.0.0.0/8",
))
HOST_PATH_RE = re.compile(r"(?<![\w-])/(?:home|srv)(?![\w-])|/tmp/" r"claude")
PHONE_RE = re.compile(r"(?<![\w-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)\s?|\d{3}[ .-])?\d{3}-\d{4}(?![\w-])")

# In style and script files a three or four digit run is a colour, not a
# ticket, when it follows one of : , ( = or a quote and then only words
# (as in "border: 1px solid ..."), and no comment marker comes before it.
COLOUR_SUFFIXES = {".css", ".scss", ".html", ".svg", ".js", ".jsx", ".mjs", ".ts", ".tsx"}
COLOUR_CONTEXT_RE = re.compile(r"""[:,(='"]\s*(?:[\w.%-]+\s+)*$""")


# An escape such as backslash-u plus four hex digits, or backslash-U plus
# eight, that yields a non-ASCII character puts non-ASCII text on screen or
# in a prompt from an ASCII file.
ESCAPE_RE = re.compile(r"(?<!\x5c)\x5c(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8}))")
ESCAPE_SUFFIXES = {".py", ".json", ".js", ".jsx", ".txt"}
ESCAPE_DIRS = ("engine/", "web/src/")

# Lines that only match or normalise player and model text, never render it.
# A line is exempt when its file is listed here and it contains one of the
# listed snippets verbatim.
ESCAPE_ALLOW = {
    "engine/app/game/addressee.py": (
        r'r"(?<!can\u2019t\s)(?<!couldn\u2019t\s))")',
        r'r"(?<!can\u2019t\s)(?<!couldn\u2019t\s))"',
        r'''_QUOTED_RE = re.compile(r'"[^"]+"|\u201c[^\u201d]+\u201d')''',
    ),
    "engine/app/game/beats/builtin.py": (
        r'_DASH_DRAMA_RE = _re.compile(r"\s+[\u2014\u2013-]+\s+")',
    ),
    "engine/app/game/moderation.py": (
        r'_SYMBOL_ENTRIES = ("\U0001F595",)', r'_SYMBOL_ENTRIES = ("\U0001f595",)',
    ),
    "engine/app/game/physics.py": (r'repl = {"\u201c": ',),
    "engine/app/game/quoted_speech.py": (
        r're.compile("\u201c(', r're.compile("\u05f4(', r're.compile("\u2018(',
        r'r"(?:\s*[,\u2014:]\s*|\s+)"',
        r"""_OPEN_QUOTES = '"\u201c\u05f4\u2018'""",
    ),
    "engine/app/game/turn_zero.py": (r'_RUN_RE = re.compile("([a-zA-Z\u05d0-\u05ea])',),
    "engine/tests/stress_diceless_dice_optin.py": (
        r'    "\u05d0\u05e0\u05d9 ', r'    "\u79c1\u306f', r'    "\u044f ', r'    "\u0645\u0631',
    ),
    "engine/tests/stress_content_filter.py": (r'("\u00e9 " + PACK, "pack")',),
    "engine/tests/stress_narration_discipline.py": (
        r'    "I \U0001f6aa the room",', r'    "I \U0001f3c3 quickly",',
    ),
    "engine/tests/test_anti_automation.py": (r'self._human_assess("\u05dc\u05da ',),
    "engine/tests/test_diceless_dice_optin.py": (r'["x" * 2000, "\u05d0\u05d1\u05d2"]',),
    "web/src/app.jsx": (
        r'const SEG_RE = /(\[[^\]]*\]|"[^"]*"|\u201c[^\u201d]*\u201d)/;',
        r"s[0] === '\u201c'",
        r"/[.!?\u2026\u2014]/.test(prev)",
    ),
}

# Binary files are not scanned: these suffixes, or a NUL byte in the first 8 KiB.
BINARY_SUFFIXES = {".woff2", ".png", ".ico", ".jpg"}


class Usage(Exception):
    pass


def load_deny(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise Usage("deny file not found: %s" % path) from None
    terms = [t.strip() for t in raw.splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    if not terms:
        return None
    body = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(body, re.IGNORECASE)


def scan_text(name, text, deny=None, check_ascii=True):
    """Return [(name, line number, rule)] for one text."""
    suffix = Path(name).suffix.lower()
    escapes = check_ascii and suffix in ESCAPE_SUFFIXES and name.startswith(ESCAPE_DIRS)
    allowed = ESCAPE_ALLOW.get(name, ())
    hits = []
    for n, line in enumerate(text.split("\n"), 1):
        if check_ascii and not line.isascii():
            hits.append((name, n, "non-ASCII"))
        if (escapes and any(int(u or w, 16) > 0x7f for u, w in ESCAPE_RE.findall(line))
                and not any(a in line for a in allowed)):
            hits.append((name, n, "non-ASCII escape"))
        for rule, rx in KEY_RES:
            if rx.search(line):
                hits.append((name, n, rule))
        for m in TICKET_RE.finditer(line):
            before = line[:m.start()]
            colour = (suffix in COLOUR_SUFFIXES and len(m.group()) <= 5
                      and "//" not in before and "/*" not in before
                      and COLOUR_CONTEXT_RE.search(before))
            if not colour:
                hits.append((name, n, "ticket id"))
                break
        if MARKER_RE.search(line):
            hits.append((name, n, "work marker"))
        for m in IPV4_RE.finditer(line):
            octets = tuple(int(part) for part in m.groups())
            if any(octet > 255 for octet in octets):
                continue
            address = ipaddress.IPv4Address(bytes(octets))
            if not any(address in network for network in IPV4_ALLOWED):
                hits.append((name, n, "ipv4"))
                break
        if HOST_PATH_RE.search(line):
            hits.append((name, n, "host path"))
        if PHONE_RE.search(line):
            hits.append((name, n, "phone"))
        if deny is not None and deny.search(line):
            hits.append((name, n, "deny list"))
    return hits


def scan_blob(name, data, deny, check_ascii):
    hits = []
    if deny is not None and deny.search(name):
        hits.append((name, 0, "deny list (path)"))
    if Path(name).suffix.lower() in BINARY_SUFFIXES or b"\0" in data[:8192]:
        return hits
    return hits + scan_text(name, data.decode("utf-8", "replace"), deny, check_ascii)


def load_ignore(root):
    rules = []
    try:
        lines = (Path(root) / ".gitignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rules
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        line = line.lstrip("!")
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        rules.append((neg, dir_only, anchored, line.lstrip("/")))
    return rules


def is_ignored(rel, is_dir, rules):
    result = False
    base = rel.rsplit("/", 1)[-1]
    for neg, dir_only, anchored, pat in rules:
        if dir_only and not is_dir:
            continue
        if fnmatch.fnmatchcase(rel if anchored else base, pat):
            result = not neg
    return result


def scan_tree(root, deny):
    root = Path(root)
    rules = load_ignore(root)
    hits = []
    for dirpath, dirs, files in os.walk(root):
        here = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if here == "." else here + "/"
        dirs[:] = sorted(d for d in dirs
                         if d != ".git" and not is_ignored(prefix + d, True, rules))
        for f in sorted(files):
            rel = prefix + f
            if is_ignored(rel, False, rules):
                continue
            try:
                data = (root / rel).read_bytes()
            except OSError:
                continue
            hits += scan_blob(rel, data, deny, True)
    return hits


def git(*args, data=None):
    p = subprocess.run(["git", *args], input=data, capture_output=True)
    if p.returncode:
        raise Usage("git %s failed: %s" % (args[0], p.stderr.decode("utf-8", "replace").strip()))
    return p.stdout


def scan_commit(sha, deny):
    listing = git("ls-tree", "-r", "-z", sha)
    entries = []
    for rec in listing.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, _kind, blob = meta.decode().split(" ")
        if mode != "160000":
            entries.append((path.decode("utf-8", "replace"), blob))
    if not entries:
        return []
    out = git("cat-file", "--batch", data="".join(b + "\n" for _, b in entries).encode())
    hits, pos = [], 0
    for name, _blob in entries:
        end = out.index(b"\n", pos)
        head = out[pos:end].split(b" ")
        if head[-1] == b"missing":
            raise Usage("blob missing in %s" % sha)
        size = int(head[2])
        hits += scan_blob(name, out[end + 1:end + 1 + size], deny, False)
        pos = end + 1 + size + 1
    return hits


def run(argv):
    ap = argparse.ArgumentParser(prog="leak_scan.py", description="Scan for text that must not be published.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--commit", metavar="SHA")
    mode.add_argument("--message", metavar="FILE", type=Path)
    ap.add_argument("--generic", action="store_true", help="skip the deny list")
    ap.add_argument("--deny", type=Path, default=DEFAULT_DENY, help=argparse.SUPPRESS)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    deny = None if args.generic else load_deny(args.deny)
    if args.commit:
        return scan_commit(args.commit, deny)
    if args.message:
        try:
            text = args.message.read_bytes().decode("utf-8", "replace")
        except OSError as e:
            raise Usage("cannot read message file: %s" % e.strerror) from None
        return scan_text("message", text, deny, True)
    if not args.root.is_dir():
        raise Usage("not a directory: %s" % args.root)
    return scan_tree(args.root, deny)


def main(argv=None):
    try:
        hits = run(sys.argv[1:] if argv is None else argv)
    except Usage as e:
        print("leak_scan: %s" % e, file=sys.stderr)
        return 2
    for name, line, rule in hits:
        print("%s:%d: %s" % (name, line, rule))
    if hits:
        print("%d hit(s)" % len(hits))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
