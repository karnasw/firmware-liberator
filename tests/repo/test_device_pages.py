"""Every device's README.md and esphome.md are laid out alike, so a reader
finds the same thing in the same place on each and a new device follows them:
the same sections in the same order, the same first rows in its table, and
the same bullets, by chip family, under Files."""
import json
import re
import unittest
from pathlib import Path

from tools.autoflash import kinds

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8"))
# A README's sections: a section only one device needs goes between The device and Files.
README_FIRST = {True: ["Status", "Testing", "The device"], False: ["Status", "Where it stops", "The device"]}
README_LAST = "Files"
# The first rows of the table under The device, in this order; the device's pins follow.
TABLE_FIRST = ["Chip", "Firmware name", "Flash", "Stock model"]
# The bullets under Files, by chip family, in this order; then Research, where it has a research.md.
FILES = {
    "espressif": ["Tasmota", "ESPHome", "Flash layout", "Calibration", "Back to Sonoff", "How it is converted"],
    "beken": ["OpenBeken", "ESPHome", "Flash layout", "Calibration", "Back to Sonoff", "How it is converted"],
}
# An esphome.md's sections: a section only one device needs goes after these.
ESPHOME_FIRST = ["What it reports", "Adopted into Home Assistant", "Changing it"]


def headings(text: str, level: int) -> list[str]:
    return re.findall(rf"(?m)^{'#' * level} (.+)$", text)


def section(text: str, heading: str) -> str:
    """The text under a `## heading`, to the next one."""
    found = re.search(rf"(?ms)^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text)
    return found.group(1) if found else ""


def table(text: str) -> list[tuple[str, str]]:
    """The rows of the tables in `text`, as (name, value)."""
    return re.findall(r"(?m)^\| (.+?) \| (.+) \|$", text)


class ReadmeTests(unittest.TestCase):
    def readme(self, kind: kinds.Kind) -> str:
        return (ROOT / kind.folder / "README.md").read_text(encoding="utf-8")

    def test_each_is_titled_with_its_devices_name(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertEqual(headings(self.readme(kind), 1), [kind.name])

    def test_each_has_the_same_sections_in_the_same_order(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                found = headings(self.readme(kind), 2)
                first = README_FIRST[kind.ready]
                self.assertEqual(found[:len(first)], first)
                self.assertEqual(found[-1], README_LAST)
                self.assertEqual(found.count(README_LAST), 1)

    def test_each_table_starts_with_the_same_rows(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                rows = table(section(self.readme(kind), "The device"))
                self.assertEqual([name for name, _ in rows[:len(TABLE_FIRST)]], TABLE_FIRST)
                cells = dict(rows)
                self.assertNotIn("MB", cells["Chip"], "the flash size has a row of its own")
                self.assertTrue(cells["Stock model"].startswith(f"`{kind.stock_model}`" if kind.stock_model
                                                                else "not known"), cells["Stock model"])

    def test_each_says_where_its_pins_come_from(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertRegex(section(self.readme(kind), "The device"), r"(?m)^Pins (from|checked)")

    def test_each_says_what_it_is_in_a_sentence(self):
        # Not "Sonoff's the POWCT: ...", which a template once left behind.
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertNotRegex(section(self.readme(kind), "The device"), r"Sonoff's the\b")

    def test_each_lists_its_families_files_in_the_same_order(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                files = section(self.readme(kind), README_LAST)
                bullets = re.findall(r"(?m)^- \*\*(.+?)\*\*", files)
                research = (ROOT / kind.folder / "research.md").is_file()
                self.assertEqual(bullets, FILES[kind.family] + ["Research"] * research)
                if research:
                    self.assertIn("[`research.md`](research.md)", files)


class EsphomePageTests(unittest.TestCase):
    def page(self, kind: kinds.Kind) -> str:
        return (ROOT / kind.folder / "esphome.md").read_text(encoding="utf-8")

    def test_each_is_titled_with_its_devices_name(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertEqual(headings(self.page(kind), 1), [f"ESPHome for the {kind.name}"])

    def test_each_has_the_same_sections_in_the_same_order(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertEqual(headings(self.page(kind), 2)[:len(ESPHOME_FIRST)], ESPHOME_FIRST)

    def test_each_beken_page_names_its_build_as_update_firmware_rewrites_it(self):
        # tools/repo/update_firmware.py finds these two to say a new build.
        for device, pins in FIRMWARE["beken"].items():
            if "esphome" not in pins:
                continue
            with self.subTest(device=device):
                pin = pins["esphome"]
                changing = section(self.page(kinds.find(device)), "Changing it")
                self.assertIn(f"ESPHome {pin['esphome']} built the current image, `{pin['file']}`,", changing)
                self.assertIn(f"update_firmware.py esphome {pin['esphome']} --device {device}", changing)

    def test_each_beken_image_has_a_paragraph_in_the_notices_update_firmware_rewrites(self):
        # beken_esphome_doc_texts rewrites the image's own paragraph in NOTICES.md, and stops
        # at one not in its form ("`<file>` is built with ESPHome X and LibreTiny Y", then the
        # two releases' links).
        from tools.repo import update_firmware
        for device, pins in FIRMWARE["beken"].items():
            if "esphome" not in pins:
                continue
            with self.subTest(device=device):
                old = pins["esphome"]
                new = {**old, "file": f"artifacts/esphome-{device}-2099.1.0-1.rbl", "esphome": "2099.1.0",
                       "libretiny": "9.9.9", "sha256": "0" * 64}
                notices = update_firmware.beken_esphome_doc_texts(device, old, new, changelog=False)[0][
                    update_firmware.NOTICES]
                paragraph = " ".join(notices.split(f"`{new['file']}`", 1)[1].split("\n\n", 1)[0].split())
                self.assertTrue(paragraph.startswith("is built with ESPHome 2099.1.0 and LibreTiny 9.9.9"), paragraph)
                self.assertIn("[ESPHome 2099.1.0 release](https://github.com/esphome/esphome/tree/2099.1.0)",
                              paragraph)
                self.assertIn("[LibreTiny 9.9.9](https://github.com/libretiny-eu/libretiny/tree/v9.9.9)", paragraph)


if __name__ == "__main__":
    unittest.main()
