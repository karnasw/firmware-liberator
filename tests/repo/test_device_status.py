"""Each device's status is said once, in devices/status.json: the tool reads it
(tools/autoflash/kinds.py), and every README shows it through a badge that
reads the same file from GitHub. Nothing else may say otherwise."""
import json
import re
import unittest
from pathlib import Path
from urllib.parse import quote

from tools.autoflash import kinds

ROOT = Path(__file__).resolve().parents[2]
# A badge file as GitHub serves it from main: what a badge shows once pushed.
RAW = "https://raw.githubusercontent.com/adamsthws/firmware-liberator/main/devices/status/{}.json"
# A status badge of any device, by the key it asks for.
ANY_BADGE = re.compile(r"img\.shields\.io/endpoint\?url=" + re.escape(quote(RAW.split("{}")[0], safe=""))
                       + r"([a-z0-9-]+)\.json")


def badge(kind: kinds.Kind, link: str) -> str:
    """The Markdown of `kind`'s status badge, linked to `link`."""
    url = f"https://img.shields.io/endpoint?url={quote(RAW.format(kind.key), safe='')}"
    return f"[![Status]({url})]({link})"


class StatusFileTests(unittest.TestCase):
    def test_every_kind_has_a_status_and_nothing_else_has_one(self):
        listed = json.loads(kinds.STATUS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(set(listed), {kind.key for kind in kinds.EVERY})
        for key, status in listed.items():
            self.assertIn(status, {status.value for status in kinds.Status}, key)

    def test_a_kind_with_none_is_not_converted(self):
        made_up = kinds.Kind(**{**vars(kinds.SONOFF_S60), "key": "sonoff-made-up"})
        self.assertIs(made_up.status, kinds.Status.NOT_SUPPORTED)
        self.assertFalse(made_up.ready)

    def test_the_tool_converts_those_ready_and_no_other(self):
        self.assertEqual({kind.key for kind in kinds.READY},
                         {kind.key for kind in kinds.EVERY
                          if kind.status in (kinds.Status.SUPPORTED, kinds.Status.TESTERS_WANTED)})
        self.assertIs(kinds.SONOFF_S60.status, kinds.Status.SUPPORTED)

    def test_each_kind_has_its_own_folder_whatever_its_status(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertEqual(kind.folder, f"devices/{kind.key}")
                self.assertTrue((ROOT / kind.folder / "README.md").is_file())
        folders = {path.parent.relative_to(ROOT).as_posix() for path in ROOT.glob("devices/**/esphome.yaml")}
        self.assertEqual(folders, {kind.folder for kind in kinds.EVERY})


class StatusTests(unittest.TestCase):
    def test_a_bridge_readout_is_only_for_a_kind_it_is_proven_on(self):
        self.assertEqual({status for status in kinds.Status if status.reads_out},
                         {kinds.Status.SUPPORTED, kinds.Status.TESTERS_WANTED, kinds.Status.READOUT_WANTED})
        self.assertFalse(kinds.Status.READOUT_WANTED.converts)

    def test_each_badge_colour_is_one_the_readme_uses(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for status in kinds.Status:
            if status is not kinds.Status.NOT_SUPPORTED:  # the one colour new to the page
                with self.subTest(status=status.value):
                    self.assertIn(f"color={status.colour}", readme)


class BadgeTests(unittest.TestCase):
    def test_each_devices_readme_starts_with_its_status(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                text = (ROOT / kind.folder / "README.md").read_text(encoding="utf-8")
                lines = [line for line in text.splitlines() if line.strip()]
                self.assertTrue(lines[0].startswith("# "))
                self.assertEqual(lines[1], "## Status")
                self.assertEqual(lines[2], badge(kind, "README.md"))
                self.assertEqual(set(ANY_BADGE.findall(text)), {kind.key}, "another device's status")

    def test_the_readme_lists_every_device_in_one_table_with_its_badge(self):
        lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
        numbers = [number for number, line in enumerate(lines) if line.startswith("| [![")]
        self.assertTrue(numbers, "the README has no table of devices")
        self.assertEqual(numbers, list(range(numbers[0], numbers[-1] + 1)), "the devices are in more than one table")
        rows = [lines[number] for number in numbers]
        self.assertEqual(sorted(key for line in rows for key in ANY_BADGE.findall(line)),
                         sorted(kind.key for kind in kinds.EVERY))
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                link = f"{kind.folder}/README.md"
                expected = f"| {badge(kind, link)} | [{kind.name}]({link}) |"
                self.assertTrue(any(line.startswith(expected) for line in rows), expected)

    def test_each_badge_file_says_its_kinds_status_in_its_colour(self):
        from tools.repo import device_status
        self.assertEqual(device_status.stale(), [], "run python3 tools/repo/device_status.py")
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                shown = json.loads((device_status.BADGES / f"{kind.key}.json").read_text(encoding="utf-8"))
                self.assertEqual((shown["label"], shown["message"], shown["color"]),
                                 ("Status", kind.status.label, kind.status.colour))

    def test_each_status_has_its_own_colour(self):
        self.assertEqual(len({status.colour for status in kinds.Status}), len(kinds.Status))

    def test_only_the_badge_says_a_devices_status(self):
        # Written words would have to be changed by hand when a report comes in.
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                text = (ROOT / kind.folder / "README.md").read_text(encoding="utf-8")
                self.assertNotRegex(text, r"Tried for real|If you try one|no conversion reported|UNTESTED")

    def test_no_other_page_says_a_status_its_own_way(self):
        # The words the tool once used, before the status was kept in one place.
        for path in [ROOT / "README.md", *ROOT.glob("devices/**/README.md"), *ROOT.glob("docs/*.md"),
                     *ROOT.glob("tools/**/README.md")]:
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"converted_for_real|ready=False|Known from Sonoff's firmware only")


if __name__ == "__main__":
    unittest.main()
