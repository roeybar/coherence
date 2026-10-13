import ast
import sys
from pathlib import Path

import simple_creds_storage

PKG = Path(simple_creds_storage.__file__).parent


def test_imports_only_stdlib_cryptography_and_self():
    bad = []
    for f in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for n in names:
                top = n.split(".")[0]
                if top not in sys.stdlib_module_names and top not in (
                        "cryptography", "simple_creds_storage"):
                    bad.append((f.name, n))
    assert bad == []
