#!/usr/bin/env python3
"""The words no report of the tool's would hold, by which the API Worker
holds one back for review (website/api/review.js): website/api/held-words.json,
made from LDNOOBW's "List of Dirty, Naughty, Obscene, and Otherwise Bad
Words" (CC BY 4.0, NOTICES.md), at the commit pinned here.

    python3 tools/repo/held_words.py    fetch that commit and write the file

Only what a report can carry is kept: entries of the Latin alphabet alone, as
a report's text is printable ASCII (report-schema.json), of three letters or
more, of up to three words. The file is the source the Worker reads; this is
run again only to move the pin. tests/repo/test_held_words.py fails if a word
here is one the tool itself writes.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "website" / "api" / "held-words.json"
REPOSITORY = "LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words"
COMMIT = "5faf2ba42d7b1c0977169ec3611df25a3c08eb13"
ENTRY = re.compile(r"[a-z]+( [a-z]+){0,2}")
# The characters written for letters to slip past a list ("sh1t", "@ss"),
# read as those letters too: by the Worker, and by the test of this list.
LOOKALIKE = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"}


def entries(lines: list[str]) -> set[str]:
    """The entries of one language's list a report could hold."""
    found = set()
    for line in lines:
        entry = line.strip().lower()
        if ENTRY.fullmatch(entry) and len(entry.replace(" ", "")) >= 3:
            found.add(entry)
    return found


def fetch() -> dict[str, list[str]]:
    """Each language's list at COMMIT, read from its archive in memory, as text alone."""
    url = f"https://codeload.github.com/{REPOSITORY}/tar.gz/{COMMIT}"
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = tarfile.open(fileobj=io.BytesIO(response.read()), mode="r:gz")
    lists = {}
    for member in archive.getmembers():
        name = member.name.split("/", 1)[-1]
        if member.isfile() and "/" not in name and re.fullmatch(r"[a-z]{2,3}(-[A-Za-z0-9-]+)?", name):
            data = archive.extractfile(member)
            lists[name] = data.read().decode("utf-8", "replace").splitlines() if data else []
    return lists


def made(lists: dict[str, list[str]]) -> dict:
    words = sorted(set().union(*(entries(lines) for lines in lists.values())))
    return {
        "$comment": "Written by tools/repo/held_words.py, which says what this is: change that, not this.",
        "source": f"https://github.com/{REPOSITORY}/tree/{COMMIT}",
        "licence": "CC BY 4.0",
        "lookalike": LOOKALIKE,
        "words": words,
    }


def main(argv: list[str] | None = None) -> int:
    if (sys.argv[1:] if argv is None else argv):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    PATH.write_text(json.dumps(made(fetch()), indent=0) + "\n", encoding="utf-8")
    print(f"Wrote {PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
