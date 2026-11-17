#!/usr/bin/env python3
"""What came in a Python newer than OLDEST, the oldest the tool runs on, used
in Python files: what compiling and testing on a newer Python cannot see, and
CI's run on OLDEST finds only once pushed.

    python3 -m tools.repo.newer_python FILE...    (tools/repo/lint.py runs it)

It knows only what is listed here: the standard library's additions easy to
reach for out of habit. tools/repo/oldest_python.py, before a push, and CI's
unit tests on OLDEST find the rest. A line that uses one only where it is
there - behind a check of sys.version_info, say - says so with a comment
holding ALLOWED.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# The oldest Python the tool runs on (start.sh, tools/windows-python.ps1 and
# CI's oldest unit tests check for it too; tests/repo/test_newer_python.py
# holds them to this).
OLDEST = (3, 10)
ALLOWED = "newer-python: ok"

# Attributes found by their name alone, whatever they are taken from: each is
# its own enough to mean the one the standard library added.
ATTRIBUTES = {
    "enterContext": (3, 11), "enterClassContext": (3, 11), "enterAsyncContext": (3, 11),  # unittest
    "add_note": (3, 11),              # an exception's notes
    "file_digest": (3, 11),           # hashlib
    "getLevelNamesMapping": (3, 11),  # logging
    "batched": (3, 12),               # itertools
}
# Names a module added, taken from it (module.name) or imported (from module import name).
FROM_MODULES = {
    "typing": {"Self": (3, 11), "LiteralString": (3, 11), "Never": (3, 11), "assert_never": (3, 11),
               "assert_type": (3, 11), "reveal_type": (3, 11), "Required": (3, 11), "NotRequired": (3, 11),
               "TypeVarTuple": (3, 11), "Unpack": (3, 11), "dataclass_transform": (3, 11), "override": (3, 12)},
    "enum": {"StrEnum": (3, 11), "ReprEnum": (3, 11), "verify": (3, 11), "member": (3, 11),
             "nonmember": (3, 11), "global_enum": (3, 11)},
    "datetime": {"UTC": (3, 11)},
    "asyncio": {"TaskGroup": (3, 11), "timeout": (3, 11), "Runner": (3, 11)},
    "contextlib": {"chdir": (3, 11)},
    "operator": {"call": (3, 11)},
}
MODULES = {"tomllib": (3, 11), "wsgiref.types": (3, 11)}
BUILTINS = {"ExceptionGroup": (3, 11), "BaseExceptionGroup": (3, 11)}


def said(version: tuple[int, int]) -> str:
    return ".".join(map(str, version))


def found_in(source: str, name: str = "<source>") -> list[tuple[int, str, tuple[int, int]]]:
    """Each use of something newer than OLDEST: (line, what, the version it came in)."""
    tree = ast.parse(source, name)
    allowed = {number for number, line in enumerate(source.splitlines(), 1) if ALLOWED in line}
    found = []

    def newer(node: ast.AST, what: str, version: tuple[int, int]) -> None:
        if version > OLDEST and node.lineno not in allowed:
            found.append((node.lineno, what, version))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.attr in FROM_MODULES.get(node.value.id, {}):
                newer(node, f"{node.value.id}.{node.attr}", FROM_MODULES[node.value.id][node.attr])
            elif node.attr in ATTRIBUTES:
                newer(node, node.attr, ATTRIBUTES[node.attr])
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module in MODULES:
                newer(node, f"the module {node.module}", MODULES[node.module])
            for alias in node.names:
                if alias.name in FROM_MODULES.get(node.module, {}):
                    newer(node, f"{node.module}.{alias.name}", FROM_MODULES[node.module][alias.name])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in MODULES:
                    newer(node, f"the module {alias.name}", MODULES[alias.name])
        elif isinstance(node, ast.Name) and node.id in BUILTINS:
            newer(node, node.id, BUILTINS[node.id])
        # Syntax a newer Python parses: none of it is in an older one's ast.
        elif type(node).__name__ == "TryStar":
            newer(node, "except*", (3, 11))
        elif type(node).__name__ == "TypeAlias":
            newer(node, "the type statement", (3, 12))
        if getattr(node, "type_params", None):
            newer(node, "type parameters in brackets", (3, 12))
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    failed = 0
    for name in sys.argv[1:] if argv is None else argv:
        try:
            source = Path(name).read_text(encoding="utf-8")
            found = found_in(source, name)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue   # the compile linter says what does not parse
        for line, what, version in found:
            print(f"{name}:{line}: {what} came in Python {said(version)}; the tool runs on {said(OLDEST)} and "
                  f"newer (tools/repo/newer_python.py)")
            failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main())
