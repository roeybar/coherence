"""Tests for leak_scan.py. Positive samples are built at run time by joining
pieces, so this file passes the scan it tests."""

import subprocess
import sys
from pathlib import Path

import pytest

import leak_scan

SCRIPT = Path(leak_scan.__file__).resolve()
HASH = "#"
CANARY = "zebrawidget"


def rules(name, text, deny=None, check_ascii=True):
    return {r for _, _, r in leak_scan.scan_text(name, text, deny, check_ascii)}


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd,
                          capture_output=True, text=True)


@pytest.fixture
def deny_file(tmp_path_factory):
    f = tmp_path_factory.mktemp("cfg") / "deny.txt"
    f.write_text("# a comment\n%s\nTwo Words\n" % CANARY, encoding="utf-8")
    return f


# ---- key shapes: one positive and one near miss each ----------------------

KEY_CASES = [
    ("key-shape anthropic", "sk-" + "ant-" + "a" * 24, "sk-" + "ant-short"),
    ("key-shape sk", "sk-" + "proj-" + "b" * 24, "task-" + "z" * 30),
    ("key-shape google", "AI" + "za" + "a" * 35, "AI" + "za" + "a" * 10),
    ("key-shape xai", "xa" + "i-" + "c" * 24, "xa" + "i-" + "c" * 5),
    ("key-shape github", "gh" + "p_" + "d" * 36, "gh" + "p_" + "d" * 10),
    ("key-shape github-pat", "github" + "_pat_" + "e" * 30, "github" + "_pat_" + "e" * 5),
    ("key-shape aws", "AK" + "IA" + "A" * 16, "AK" + "IA" + "A" * 5),
    ("key-shape jwt", "ey" + "J" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, "ey" + "J" + "a" * 12),
    ("key-shape private-key", "-----BEGIN " + "RSA " + "PRIVATE KEY-----",
     "-----BEGIN " + "CERTIFICATE-----"),
    ("key-shape mongodb", "mongo" + "db://user:pw@host", "mongo" + "db://host:27017/db"),
]


@pytest.mark.parametrize("rule,positive,near_miss", KEY_CASES, ids=[c[0] for c in KEY_CASES])
def test_key_shape(rule, positive, near_miss):
    assert rule in rules("a.txt", "value = " + positive)
    assert not any(r.startswith("key-shape") for r in rules("a.txt", "value = " + near_miss))


# ---- ticket ids, including the colour near misses -------------------------

@pytest.mark.parametrize("text", [
    "see " + HASH + "123",
    "Fixes " + HASH + "4567",
    "(" + HASH + "123)",
    "x " + HASH + "12345 y",
])
def test_ticket_id_hit(text):
    assert "ticket id" in rules("notes.md", text)


@pytest.mark.parametrize("text", [
    "item " + HASH + "12",
    "item " + HASH + "123456",
    "colour " + HASH + "33363b",
    "colour " + HASH + "fff",
    "TICKET-123",
])
def test_ticket_id_near_miss(text):
    assert "ticket id" not in rules("notes.md", text)


@pytest.mark.parametrize("name,text", [
    ("a.css", "color: " + HASH + "000;"),
    ("a.css", "color: " + HASH + "33363b;"),
    ("a.css", "border: 1px solid " + HASH + "333;"),
    ("a.css", "background: linear-gradient(" + HASH + "000, " + HASH + "fff);"),
    ("a.svg", '<rect fill="' + HASH + '000"/>'),
    ("a.jsx", "const c = '" + HASH + "1234';"),
])
def test_css_colours_are_not_ticket_ids(name, text):
    assert "ticket id" not in rules(name, text)


def test_ticket_id_still_hits_in_code_comment():
    assert "ticket id" in rules("a.js", "// see " + HASH + "123")
    assert "ticket id" in rules("a.js", "// fix: " + HASH + "123")
    assert "ticket id" in rules("a.css", "/* see: " + HASH + "123 */")
    assert "ticket id" in rules("a.js", "const c = '" + HASH + "12345';")


# ---- the other rules ------------------------------------------------------

@pytest.mark.parametrize("word", ["TO" + "DO", "FIX" + "ME", "XX" + "X"])
def test_work_marker(word):
    assert "work marker" in rules("a.py", "# " + word + " later")
    assert "work marker" not in rules("a.py", "# " + word.lower() + " later")


def test_work_marker_near_miss():
    assert "work marker" not in rules("a.py", "x = " + "XX" + "XX")


@pytest.mark.parametrize("check_ascii", [True, False])
@pytest.mark.parametrize("rule,text", [
    ("ipv4", ".".join(["8"] * 4)),
    ("host path", "/" + "home" + "/user/x"),
    ("host path", "/" + "srv" + "/x"),
    ("host path", "/tmp/" + "claude" + "/x"),
    ("phone", "555" + "-" + "0142"),
    ("phone", "(212) " + "555" + "-" + "0199"),
    ("phone", "+1 212 " + "555" + "-" + "0199"),
])
def test_literal_rule_hit_once_per_line(rule, text, check_ascii):
    assert leak_scan.scan_text("a.md", "plain\n" + text + " " + text,
                               check_ascii=check_ascii) == [("a.md", 2, rule)]


@pytest.mark.parametrize("text", [
    "127.0.0.5",
    "10.1.2.3",
    "172.18.0.5",
    "192.168.5.3",
    "192.0.2.11",
    "198.51.100.4",
    "203.0.113.7",
    "0.0.0.0",
    "255.255.255.0",
    "1.2.3",
    "256.1.2.3",
    "/application/x",
    "/app/engine",
    "/homepage",
    "4417",
    "12-B",
])
def test_literal_rule_near_miss(text):
    assert not rules("a.md", text)


def test_non_ascii():
    assert "non-ASCII" in rules("a.md", "caf" + chr(0xE9))
    assert "non-ASCII" not in rules("a.md", "cafe")
    assert "non-ASCII" not in rules("a.md", "caf" + chr(0xE9), check_ascii=False)


def test_deny_list(deny_file):
    deny = leak_scan.load_deny(deny_file)
    assert "deny list" in rules("a.md", "a %s here" % CANARY.upper(), deny)
    assert "deny list" in rules("a.md", "the two words appear", deny)
    assert "deny list" in rules("a.md", "%ss and x%sfoo" % (CANARY, CANARY), deny)
    assert "deny list" not in rules("a.md", "zebra widget", deny)
    assert "deny list" not in rules("a.md", "a comment", deny)
    assert "deny list" not in rules("a.md", CANARY, None)


def test_deny_list_missing_is_a_usage_error(tmp_path):
    with pytest.raises(leak_scan.Usage):
        leak_scan.load_deny(tmp_path / "absent.txt")


def test_own_sources_are_clean():
    for path in (SCRIPT, Path(__file__).resolve()):
        text = path.read_text(encoding="utf-8")
        assert leak_scan.scan_text(path.name, text) == []


# ---- tree mode ------------------------------------------------------------

def make_tree(root):
    bad = "key = " + "sk-" + "ant-" + "q" * 24 + "\n"
    (root / ".gitignore").write_text(
        ".env\nignored.txt\nskipdir/\n/anchored.txt\n*.log\n!keep.log\n", encoding="utf-8")
    for name in (".env", "ignored.txt", "anchored.txt", "drop.log"):
        (root / name).write_text(bad, encoding="utf-8")
    (root / "skipdir").mkdir()
    (root / "skipdir" / "x.txt").write_text(bad, encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text(bad, encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\0" + bytes([0xC3, 0xA9]) * 10)
    (root / "fine.md").write_text("plain text\n", encoding="utf-8")


def test_tree_skips_ignored_binary_and_git(tmp_path, deny_file):
    make_tree(tmp_path)
    r = cli("--root", str(tmp_path), "--deny", str(deny_file))
    assert (r.returncode, r.stdout) == (0, "")


def test_tree_skips_binary_files(tmp_path, deny_file):
    body = ("key = " + "sk-" + "ant-" + "q" * 24 + " " + CANARY + "\n").encode() + bytes([0xC3, 0xA9])
    for name in ("a.woff2", "a.png", "a.ico", "a.jpg", "A.PNG"):
        (tmp_path / name).write_bytes(body)
    (tmp_path / "nul.dat").write_bytes(b"\0" + body)
    r = cli("--root", str(tmp_path), "--deny", str(deny_file))
    assert (r.returncode, r.stdout) == (0, "")


def test_tree_scans_nul_after_the_first_8_kib(tmp_path, deny_file):
    body = b"a" * 9000 + b"\0\nkey = " + ("sk-" + "ant-" + "q" * 24).encode() + b"\n"
    (tmp_path / "late.dat").write_bytes(body)
    (tmp_path / "other.txt").write_bytes(body.replace(b"\0", b""))
    r = cli("--root", str(tmp_path), "--deny", str(deny_file))
    assert r.returncode == 1 and "late.dat:2: key-shape anthropic" in r.stdout


def test_tree_negated_ignore_is_scanned(tmp_path, deny_file):
    make_tree(tmp_path)
    (tmp_path / "keep.log").write_text("key = " + "sk-" + "ant-" + "q" * 24 + "\n", encoding="utf-8")
    r = cli("--root", str(tmp_path), "--deny", str(deny_file))
    assert r.returncode == 1
    assert "keep.log:1: key-shape anthropic" in r.stdout
    assert "q" * 24 not in r.stdout


def test_tree_hit_in_nested_file(tmp_path, deny_file):
    make_tree(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "n.md").write_text("ok\ncaf" + chr(0xE9) + "\n", encoding="utf-8")
    r = cli("--root", str(tmp_path), "--deny", str(deny_file))
    assert r.returncode == 1 and "sub/n.md:2: non-ASCII" in r.stdout


def test_tree_deny_list_hit_hides_the_term(tmp_path, deny_file):
    (tmp_path / "a.md").write_text("about %s today\n" % CANARY, encoding="utf-8")
    (tmp_path / ("%s.txt" % CANARY)).write_text("x\n", encoding="utf-8")
    r = cli("--root", str(tmp_path), "--deny", str(deny_file))
    assert r.returncode == 1
    assert "a.md:1: deny list" in r.stdout and ".txt:0: deny list (path)" in r.stdout
    assert CANARY not in r.stdout.replace(CANARY + ".txt", "")


def test_missing_deny_file_exits_2_unless_generic(tmp_path):
    (tmp_path / "a.md").write_text("fine\n", encoding="utf-8")
    absent = str(tmp_path / "absent.txt")
    assert cli("--root", str(tmp_path), "--deny", absent).returncode == 2
    assert cli("--root", str(tmp_path), "--deny", absent, "--generic").returncode == 0


def test_generic_skips_the_deny_list(tmp_path, deny_file):
    (tmp_path / "a.md").write_text("about %s\n" % CANARY, encoding="utf-8")
    assert cli("--root", str(tmp_path), "--deny", str(deny_file)).returncode == 1
    assert cli("--root", str(tmp_path), "--deny", str(deny_file), "--generic").returncode == 0


@pytest.mark.parametrize("args", [
    ["--bogus"],
    ["--commit", "HEAD", "--message", "x"],
    ["--message"],
])
def test_usage_errors_exit_2(args):
    assert cli(*args, "--generic").returncode == 2


def test_missing_root_exits_2(tmp_path):
    assert cli("--root", str(tmp_path / "nope"), "--generic").returncode == 2


# ---- message mode ---------------------------------------------------------

@pytest.mark.parametrize("text,code", [
    ("Add the save file loader\n", 0),
    ("Add the loader " + HASH + "123\n", 1),
    ("Add the loader " + chr(0x2014) + " done\n", 1),
    ("Add the loader " + "TO" + "DO\n", 1),
    ("Add the loader for " + CANARY + "\n", 1),
])
def test_message_mode(tmp_path, deny_file, text, code):
    f = tmp_path / "m.txt"
    f.write_text(text, encoding="utf-8")
    assert cli("--message", str(f), "--deny", str(deny_file)).returncode == code


def test_message_generic_and_unreadable(tmp_path, deny_file):
    f = tmp_path / "m.txt"
    f.write_text("about %s\n" % CANARY, encoding="utf-8")
    assert cli("--message", str(f), "--deny", str(deny_file), "--generic").returncode == 0
    assert cli("--message", str(tmp_path / "gone.txt"), "--generic").returncode == 2


# ---- commit mode ----------------------------------------------------------

def git(repo, *args):
    env = {"HOME": str(repo), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    p = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
    return p.stdout.strip()


def commit_files(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, data in files.items():
        (repo / name).write_bytes(data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c")
    return repo, git(repo, "rev-parse", "HEAD")


def test_commit_clean_and_hits(tmp_path, deny_file):
    repo, sha = commit_files(tmp_path, {"a.md": b"plain\n", "bin.dat": b"\0\xc3\xa9"})
    assert cli("--commit", sha, "--deny", str(deny_file), cwd=repo).returncode == 0
    key = ("key = " + "sk-" + "ant-" + "q" * 24 + "\n").encode()
    for i, (data, code) in enumerate([
        (key, 1),
        (("see " + HASH + "123\n").encode(), 1),
        (("about %s\n" % CANARY).encode(), 1),
        (b"caf\xc3\xa9\n", 0),
    ]):
        sub = tmp_path / ("case%d" % i)
        sub.mkdir()
        repo2, sha2 = commit_files(sub, {"f.md": data})
        r = cli("--commit", sha2, "--deny", str(deny_file), cwd=repo2)
        assert r.returncode == code, (i, r.stdout)


def test_commit_generic_and_errors(tmp_path, deny_file):
    repo, sha = commit_files(tmp_path, {"a.md": ("about %s\n" % CANARY).encode()})
    assert cli("--commit", sha, "--deny", str(deny_file), "--generic", cwd=repo).returncode == 0
    assert cli("--commit", sha, "--deny", str(tmp_path / "absent.txt"), cwd=repo).returncode == 2
    assert cli("--commit", "0" * 40, "--generic", cwd=repo).returncode == 2
    outside = tmp_path / "plain"
    outside.mkdir()
    assert cli("--commit", sha, "--generic", cwd=outside).returncode == 2


# ---- escapes that yield non-ASCII text --------------------------------------

BS = "\\"


def test_non_ascii_escape_is_a_hit_in_engine_and_web_src_code():
    line = "x = '" + BS + "u00e9'"
    for name in ("engine/app/a.py", "engine/lore/p.json", "web/src/app.jsx", "web/src/a.js", "engine/t.txt"):
        assert "non-ASCII escape" in rules(name, line), name


def test_eight_digit_escape_is_a_hit_unless_listed():
    line = "    (\"key\", \"" + BS + "U0001f511\"),"
    assert "non-ASCII escape" in rules("engine/app/game/items.py", line)
    assert "non-ASCII escape" in rules("engine/tests/stress_narration_discipline.py", line)
    assert "non-ASCII escape" not in rules("engine/app/a.py", "x = '" + BS + "U00000041'")
    listed = "    \"I " + BS + "U0001f6aa the room\","
    assert "non-ASCII escape" not in rules("engine/tests/stress_narration_discipline.py", listed)


def test_ascii_escapes_and_other_paths_are_clean():
    assert "non-ASCII escape" not in rules("engine/app/a.py", "x = '" + BS + "u0041 " + BS + "u007f'")
    assert "non-ASCII escape" not in rules("engine/app/a.py", "x = '" + BS + BS + "u00e9'")
    assert "non-ASCII escape" not in rules("tools/a.py", "x = '" + BS + "u00e9'")
    assert "non-ASCII escape" not in rules("web/e2e/a.ts", "x = '" + BS + "u00e9'")
    assert "non-ASCII escape" not in rules("engine/app/a.py", "x = '" + BS + "u00e9'", check_ascii=False)


def test_allow_list_exempts_only_its_listed_lines():
    allowed = "    repl = {\"" + BS + "u201c\": '\"', }"
    assert "non-ASCII escape" not in rules("engine/app/game/physics.py", allowed)
    assert "non-ASCII escape" in rules("engine/app/game/physics.py", "x = '" + BS + "u2014'")
    assert "non-ASCII escape" in rules("engine/app/game/items.py", allowed)
