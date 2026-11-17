#!/usr/bin/env python3
"""The unit tests on the oldest Python the tool runs on (newer_python.OLDEST),
as CI runs them there, before a push rather than after it.

    python3 tools/repo/oldest_python.py

What HEAD holds - what a push sends, not what is uncommitted - is cloned into
a folder of its own and tested in IMAGE, with the packages CI installs
(requirements/ci.txt) in a venv inside it: nothing of this checkout or this
computer is written, but the image pulled the first time. Needs Docker. A
newer Python passes what the oldest cannot run; tools/repo/newer_python.py
finds the commonest of it at each commit, and this the rest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.repo import newer_python  # noqa: E402

# The official image of the oldest Python, pinned to what was tested with it;
# its tag names newer_python.OLDEST (tests/repo/test_oldest_python.py). It has
# git, which some tests read the checkout with.
IMAGE = "python:3.10.22-trixie@sha256:c10791e6f69086d5216f7b5d3b839929c454e24305867ed9bc5152f0355b5423"

# Inside the container: CI's packages in a venv of its own, then every test.
SCRIPT = ("python -m venv /tmp/venv"
          " && /tmp/venv/bin/pip install -q --require-hashes --no-deps -r requirements/ci.txt"
          " && git config --global --add safe.directory /tree"
          " && /tmp/venv/bin/python tests/run.py")


def clone_head(into: Path) -> str:
    """This checkout's HEAD, cloned into `into` (a worktree's own HEAD, not the
    main checkout's): the commit."""
    subprocess.run(["git", "init", "-q", str(into)], check=True)
    subprocess.run(["git", "-C", str(into), "fetch", "-q", str(ROOT), "HEAD"], check=True)
    subprocess.run(["git", "-C", str(into), "checkout", "-q", "FETCH_HEAD"], check=True)
    return subprocess.run(["git", "-C", str(into), "rev-parse", "--short", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def docker_command(tree: Path) -> list[str]:
    """The run of every test on the oldest Python, on `tree`, as you."""
    user = ["--user", f"{os.getuid()}:{os.getgid()}"] if hasattr(os, "getuid") else []
    return ["docker", "run", "--rm", *user, "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{tree}:/tree", "-w", "/tree", IMAGE, "bash", "-c", SCRIPT]


def main(argv: list[str] | None = None) -> int:
    if (sys.argv[1:] if argv is None else argv):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("oldest_python: Docker is needed - the oldest Python runs in its official image", file=sys.stderr)
        return 2
    oldest = newer_python.said(newer_python.OLDEST)
    with tempfile.TemporaryDirectory(prefix="oldest-python-") as tmp:
        tree = Path(tmp, "tree")
        commit = clone_head(tree)
        print(f"The unit tests on Python {oldest}, on {commit} (HEAD)...", flush=True)
        done = subprocess.run(docker_command(tree))
    print(f"Python {oldest}: {'passed' if done.returncode == 0 else 'FAILED'}", flush=True)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
