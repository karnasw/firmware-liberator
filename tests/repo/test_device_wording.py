"""The project says "device", not "plug" (CONTRIBUTING.md, Writing docs): it
began with one smart plug and converts switches, relays and panels too. Every
text file git tracks is read, code and docs alike; "plug" may stand only where
it is someone else's word, or a name an earlier release gave something the
tool still reads.

Left out: archive/, kept as it was written; released sections of CHANGELOG.md,
which are history; and what is not this project's text."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Where "plug" is someone else's word.
THEIRS = [
    r"Sonoff_S61s_EUPlug_WBBK_01P_V1", r"Sonoff_S60TPG_UK_Plug_ESP32", r"iPlug",   # Sonoff's names
    r"[Ss]mart[- ]plugs?",                                # what the S60 and S61 are sold as
    r"mdastPlugins",                                      # Astro's option
    r"emulator-public-plugs", r"public-plugs-(?:[0-9a-f]{12}|<hash>)\.tar\.xz",   # a release already published
    r"kitchen-plug",                                      # a name someone gave theirs
    r"[\"'`“][Pp]lugs?[\"'`”]",                           # the word itself, spoken of
]
# A cable is unplugged, and a lamp plugged in; a wall switch is neither.
CABLES = [r"\b[Uu]nplug(?:ged|ging|s)?\b", r"\b[Pp]lugg(?:ed|ing)\b"]
# What releases up to 2.3.0 called things, read still or said to be gone: the
# folder and file of each device's records, a run's inputs, an emulator
# instance's files, the options, and what those releases print (the fault
# suites run them).
EARLIER = [
    r"\bplugs/", r"/plugs?\b", r"\bplug\.json\b", r"\bplug_(?:ip|mac|kind|busy_seconds)\b",
    r"--plug(?:-ip)?(?![\w-])",
    r"\((?:\?:)?plug\|device\)", r"\((?:\?:)?PLUG\|DEVICE\)",
]
ALLOWED = re.compile("|".join(THEIRS + CABLES + EARLIER))
PLUG = re.compile(r"plug", re.IGNORECASE)

LEFT_OUT = ("archive/", "website/node_modules/")
NOT_OURS = {
    "LICENSE",
    "website/package-lock.json",    # packages' names
    # As sent to OpenBeken, where an S61 is a plug: the pull request's own words.
    "tools/beken/openbeken/openbk7231t_app-bk7238-4m-flash.patch",
    "tests/repo/test_device_wording.py",
}


def strays(text: str) -> list[str]:
    """Each line of `text` that says plug where nothing allows it."""
    return [line.strip() for line in text.splitlines() if PLUG.search(ALLOWED.sub("", line))]


def top_section(changelog: str) -> str:
    return "## " + changelog.split("\n## ")[1]


def pages() -> dict[str, str]:
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    found = {}
    for name in tracked.splitlines():
        if name.startswith(LEFT_OUT) or name in NOT_OURS:
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue   # firmware, a picture, a file deleted and not yet committed
        found[name] = top_section(text) if name == "CHANGELOG.md" else text
    return found


class DeviceWordingTests(unittest.TestCase):
    def test_nothing_says_plug_but_where_it_is_someone_elses_word(self):
        for name, text in pages().items():
            with self.subTest(file=name):
                self.assertEqual(strays(text), [])

    def test_no_file_is_named_for_a_plug(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertEqual([name for name in tracked.splitlines()
                          if PLUG.search(name) and not name.startswith(LEFT_OUT)], [])

    def test_it_tells_the_projects_word_from_someone_elses(self):
        for line in ("Find the plug on your network.", "plug_dir = root / mac", "class PlugFinder:", "PLUG_IP=$NET.50",
                     "a plug in the account", "plugs = find_all()", "emu.sh --plug-dir DIR"):
            self.assertEqual(strays(line), [line], line)
        for line in ("The S60 is a smart plug with a power meter.", "FWSW ... Sonoff_S61s_EUPlug_WBBK_01P_V1",
                     "releases/tag/emulator-public-plugs", 'earlier = captures / "plugs"', "plugs/12-34-56-ab-cd-ef/runs",
                     'inputs.pop("plug_ip")', "(device / \"plug.json\")", "the cable was unplugged mid-run",
                     r'STARTS = (r"Backing up the (?:plug|device)\'s Sonoff firmware", 12)',
                     'Say "device", not "plug"', 'if [ -f "$STATE/plug" ]; then', "colours", "a devices folder",
                     "`--plug-ip` is `--device-ip`, and the hidden `--plug` is gone"):
            self.assertEqual(strays(line), [], line)

    def test_only_the_changelogs_top_section_counts(self):
        self.assertEqual(top_section("# Changelog\n\n## 2.0.0\na device\n\n## 1.0.0\na plug\n"), "## 2.0.0\na device\n")


if __name__ == "__main__":
    unittest.main()
