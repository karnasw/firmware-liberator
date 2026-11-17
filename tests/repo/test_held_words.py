"""held_words: the list the API holds reports by, never a word the tool writes."""

from __future__ import annotations

import json
import re
import unittest

from tests.autoflash.test_failures import raised
from tools.autoflash import failures, kinds, report, routes
from tools.repo import held_words

LIST = json.loads(held_words.PATH.read_text(encoding="utf-8"))


def holds(text: str) -> bool:
    """As website/api/review.js reads it: whole words, and with lookalikes as letters."""
    lower = text.lower()
    for variant in (lower, "".join(LIST["lookalike"].get(char, char) for char in lower)):
        tokens = [token for token in re.split(r"[^a-z]+", variant) if token]
        joined = f" {' '.join(tokens)} "
        if any(f" {word} " in joined for word in LIST["words"]):
            return True
    return False


class HeldWordsTests(unittest.TestCase):
    def test_it_is_the_list_at_the_commit_pinned_and_says_so(self):
        self.assertEqual(LIST["source"], f"https://github.com/{held_words.REPOSITORY}/tree/{held_words.COMMIT}")
        self.assertEqual(LIST["licence"], "CC BY 4.0")
        self.assertEqual(LIST["lookalike"], held_words.LOOKALIKE)

    def test_only_what_a_report_could_carry(self):
        self.assertEqual(LIST["words"], sorted(set(LIST["words"])))
        for word in LIST["words"]:
            self.assertRegex(word, held_words.ENTRY)
            self.assertGreaterEqual(len(word.replace(" ", "")), 3, word)

    def test_made_as_written(self):
        self.assertEqual(held_words.entries(["  Shout ", "ab", "two words", "x-y", "café", "one two three four"]),
                         {"shout", "two words"})

    def test_no_word_the_tool_itself_writes_is_held(self):
        written = [message for _, message in raised()]
        written += [" ".join((kind.name, kind.key, kind.short, kind.stock_model)) for kind in kinds.EVERY]
        written += list(failures.CLASSES) + list(failures.CLASSES.values())
        written += list(routes.FIRMWARES.values()) + list(report.CONVERTED_FROM.values())
        written += ["Ubuntu 24.04.1 LTS", "Debian GNU/Linux 12 (bookworm)", "Fedora Linux 41 (Workstation Edition)",
                    "Linux Mint 22", "openSUSE Tumbleweed", "Raspbian GNU/Linux 11 (bullseye)", "Arch Linux",
                    "11 (build 10.0.22631)", "macOS 15.1", "Manjaro Linux", "Kali GNU/Linux Rolling", "Zorin OS 17"]
        self.assertEqual([text[:60] for text in written if holds(text)], [])
