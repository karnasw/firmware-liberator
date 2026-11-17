"""The website at firmwareliberator.com (website/), as far as Python can see it:
the one line's redirects, against what each release carries; its address; and
llms.txt and each line saying what the tool does, against the devices and
firmware the tool has. What the build makes is checked by website/checks/."""

import re
import unittest
from pathlib import Path

from tools.autoflash import kinds, routes
from tools.repo.github_settings import REPOSITORY, WEBSITE

ROOT = Path(__file__).resolve().parents[2]
LLMS = ROOT / "website/public/llms.txt"


def llms_device_line(kind: kinds.Kind) -> str:
    """How llms.txt's line for `kind` starts: its name and page, its status
    (devices/status.json), and for one the tool converts, what to
    (routes.firmwares_of). What the device is follows, in words of its own."""
    line = f"- [{kind.name}]({WEBSITE}{kind.folder}): {kind.status.label}."
    if kind.ready:
        offered = [label for name, label in routes.firmwares_of(kind).items() if name != "sonoff"]
        line += f" Converts to {' or '.join(offered)}."
    return line + " "


class SiteTests(unittest.TestCase):
    def test_each_redirect_names_a_file_every_release_carries(self):
        # The README's one line fetches firmwareliberator.com/liberate.sh: _redirects
        # sends it on to that file of the latest release, so the file must be one
        # the release workflow uploads.
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        uploaded = release.split("gh release create", 1)[1].split()
        latest = f"https://github.com/{REPOSITORY}/releases/latest/download/"
        redirects = (ROOT / "website/public/_redirects").read_text(encoding="utf-8")
        lines = [line.split() for line in redirects.splitlines() if line.strip() and not line.startswith("#")]
        self.assertEqual({source for source, *_ in lines}, {"/liberate.sh", "/liberate.ps1"})
        for source, target, status in lines:
            self.assertEqual(target, latest + source.lstrip("/"))
            self.assertIn(source.lstrip("/"), uploaded)
            self.assertEqual(status, "302", "a permanent redirect would be cached past the next release")

    def test_the_site_links_to_this_repository(self):
        site = (ROOT / "website/src/site.mjs").read_text(encoding="utf-8")
        self.assertIn(f'export const REPOSITORY = "{REPOSITORY}";', site)

    def test_the_api_reads_this_repositorys_downloads(self):
        downloads = (ROOT / "website/api/downloads.js").read_text(encoding="utf-8")
        self.assertIn(f'export const REPOSITORY = "{REPOSITORY}";', downloads)

    def test_the_site_is_at_the_address_the_repository_gives_it(self):
        site = (ROOT / "website/src/site.mjs").read_text(encoding="utf-8")
        self.assertIn(f'export const SITE = "{WEBSITE.rstrip("/")}";', site)
        robots = (ROOT / "website/public/robots.txt").read_text(encoding="utf-8")
        self.assertIn(f"\nSitemap: {WEBSITE}sitemap-index.xml\n", robots)


class WhatTheToolDoesTests(unittest.TestCase):
    # Each place that says in a line what the tool does, for people searching
    # and for AI tools: each must name every maker and firmware it has, so a
    # firmware or maker added is never left out of one.
    PLACES = {
        "the front page's title": ("website/src/layouts/Page.astro", r"(?m)^const HOME_TITLE = (.+);$"),
        "the front page's description": ("website/src/layouts/Page.astro", r"(?m)^const DESCRIPTION = (.+);$"),
        "llms.txt's summary": ("website/public/llms.txt", r"(?m)^> (.+)$"),
        "the README's opening": ("README.md", r"\A# [^\n]+\n(.+?)\n"),
    }

    def words(self) -> set[str]:
        return ({label for name, label in routes.FIRMWARES.items() if name != "sonoff"}
                | {kind.maker for kind in kinds.EVERY})

    def test_each_place_names_every_maker_and_firmware(self):
        for place, (path, pattern) in self.PLACES.items():
            with self.subTest(place=place):
                found = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"))
                self.assertIsNotNone(found, f"{place} not found in {path}")
                for word in self.words():
                    self.assertIn(word, found.group(1), place)

    def test_so_does_the_repositorys_description_on_github(self):
        from tools.repo.github_settings import REPOSITORY_SETTINGS
        for word in self.words():
            self.assertIn(word, REPOSITORY_SETTINGS["description"])


class LlmsTxtTests(unittest.TestCase):
    # llms.txt is written by hand, for AI tools: these keep it saying what the
    # tool does as devices and firmware are added or change status.

    def devices(self) -> list[str]:
        section = LLMS.read_text(encoding="utf-8").split("\n## Devices\n", 1)[1].split("\n## ", 1)[0]
        return [line for line in section.splitlines() if line.startswith("- ")]

    def test_it_lists_every_device_once_in_the_tools_order(self):
        names = [re.match(r"- \[([^\]]+)\]", line).group(1) for line in self.devices()]
        self.assertEqual(names, [kind.name for kind in kinds.EVERY])

    def test_each_devices_line_says_its_page_status_and_firmware(self):
        lines = self.devices()
        for kind, line in zip(kinds.EVERY, lines):
            with self.subTest(kind=kind.key):
                expected = llms_device_line(kind)
                self.assertTrue(line.startswith(expected), f"\n{line}\nshould start\n{expected}")
                self.assertGreater(len(line), len(expected), "and say what the device is")

    def test_a_device_changing_status_or_firmware_changes_its_line(self):
        line = llms_device_line(kinds.SONOFF_S60)
        self.assertEqual(line, f"- [Sonoff S60]({WEBSITE}devices/sonoff-s60): Supported. Converts to Tasmota or ESPHome or Supla. ")
        moved = kinds.Kind(**{**vars(kinds.SONOFF_S60), "key": "sonoff-made-up", "name": "Sonoff Made Up"})
        self.assertEqual(llms_device_line(moved),
                         f"- [Sonoff Made Up]({WEBSITE}devices/sonoff-made-up): Not supported yet. ")
        beken = kinds.Kind(**{**vars(kinds.SONOFF_S60), "family": "beken"})
        self.assertIn("Converts to OpenBeken or ESPHome.", llms_device_line(beken))


if __name__ == "__main__":
    unittest.main()
