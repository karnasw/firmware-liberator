import re
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "docs" / "upstream-contributions.md"
HEADER = "| Project | Pull request | Status | What it fixes |"

LINK = re.compile(r"\[[^\]]+\]\(https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<kind>pull|issues)/(?P<n>\d+)\)")
# The shields.io badge that shows a pull request's or issue's state as GitHub has it now.
BADGE = re.compile(r"!\[state\]\(https://img\.shields\.io/github/(?P<path>pulls|issues)/detail/state/"
                   r"(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<n>\d+)\?label=\)")


def entries(text):
    """The "Pull request" and "Status" cells of each row of the page's tables."""
    rows, in_table = [], False
    for line in text.splitlines():
        if line.startswith("| Project |"):
            in_table = line == HEADER
            if not in_table:
                rows.append((line, None))
        elif not line.startswith("|"):
            in_table = False
        elif in_table and not line.startswith("|---"):
            cells = line.split(" | ")
            rows.append((cells[1], cells[2]))
    return rows


def mismatch(link, badge):
    """Why a row's badge is not the state of its own pull request or issue, or None."""
    m, b = LINK.fullmatch(link or ""), BADGE.fullmatch(badge or "")
    if not m:
        return f"not one pull request or issue: {link}"
    if not b:
        return f"no state badge: {badge}"
    if b["path"] != ("pulls" if m["kind"] == "pull" else "issues"):
        return f"a {m['kind']} badged as {b['path']}: {link}"
    if (b["owner"], b["repo"], b["n"]) != (m["owner"], m["repo"], m["n"]):
        return f"the badge is another's: {link} {badge}"
    return None


class UpstreamContributionsTests(unittest.TestCase):
    def test_each_entry_shows_the_state_of_its_own_pull_request(self):
        rows = entries(PAGE.read_text(encoding="utf-8"))
        self.assertGreater(len(rows), 30)
        for link, badge in rows:
            self.assertIsNone(mismatch(link, badge))

    def test_a_badge_for_another_pull_request_is_caught(self):
        self.assertIn("another's", mismatch(
            "[#20](https://github.com/lcgamboa/qemu/pull/20)",
            "![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/19?label=)"))

    def test_an_issue_badged_as_a_pull_request_is_caught(self):
        self.assertIn("badged as", mismatch(
            "[#2](https://github.com/openshwprojects/BekenEmulator/issues/2)",
            "![state](https://img.shields.io/github/pulls/detail/state/openshwprojects/BekenEmulator/2?label=)"))

    def test_a_row_without_a_badge_is_caught(self):
        self.assertIn("no state badge", mismatch("[#20](https://github.com/lcgamboa/qemu/pull/20)", "Merged"))

    def test_a_table_without_the_status_column_is_caught(self):
        rows = entries("| Project | Pull request | What it fixes |\n|---|---|---|\n| QEMU | [#20](x) | y |\n")
        self.assertIsNotNone(mismatch(*rows[0]))


if __name__ == "__main__":
    unittest.main()
