"""The engine imports no database, cache or vendor SDK, and every module loads."""

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import app

APP_DIR = Path(app.__file__).resolve().parent
BANNED = {"pymongo", "bson", "qdrant_client", "redis", "anthropic"}


def _module_names() -> list[str]:
    names = ["app"]
    for info in pkgutil.walk_packages([str(APP_DIR)], prefix="app."):
        names.append(info.name)
    return sorted(names)


def _imported_roots(tree: ast.AST) -> set[str]:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_banned_imports():
    hits = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _imported_roots(tree) & BANNED
        if bad:
            hits.append(f"{path.relative_to(APP_DIR)}: {sorted(bad)}")
    assert hits == []


def test_module_list_is_complete():
    names = _module_names()
    assert "app.store" in names
    assert "app.game.engine" in names
    assert "app.game.beats.builtin" in names


@pytest.mark.parametrize("name", _module_names())
def test_module_imports(name):
    importlib.import_module(name)


def test_only_creds_imports_simple_creds_storage():
    users = sorted(
        str(f.relative_to(APP_DIR))
        for f in APP_DIR.rglob("*.py")
        if "simple_creds_storage" in _imported_roots(ast.parse(f.read_text()))
    )
    assert users == ["creds.py"]
