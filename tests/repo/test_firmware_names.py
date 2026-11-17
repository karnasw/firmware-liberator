"""Each device's firmware name, the one its stock updater checks, is said once,
as its kind's wrapper_model in tools/autoflash/kinds.py, which the tool wraps
its firmware with; every device's README shows it in its table, the same."""
import re
import unittest
from pathlib import Path

from tools.autoflash import kinds

ROOT = Path(__file__).resolve().parents[2]
ROW = re.compile(r"(?m)^\| Firmware name \| (.*) \|$")


class FirmwareNameTests(unittest.TestCase):
    def test_each_devices_readme_names_its_firmware_as_the_tool_does(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                text = (ROOT / kind.folder / "README.md").read_text(encoding="utf-8")
                self.assertEqual(ROW.findall(text), [f"`{kind.wrapper_model}`"])

    def test_it_is_in_the_devices_table_after_its_chip(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                text = (ROOT / kind.folder / "README.md").read_text(encoding="utf-8")
                self.assertRegex(text, r"(?m)^\| Chip \|.*\|\n\| Firmware name \|")

    def test_no_other_readme_names_one(self):
        folders = {kind.folder for kind in kinds.EVERY}
        for readme in ROOT.glob("devices/**/README.md"):
            if readme.parent.relative_to(ROOT).as_posix() not in folders:
                with self.subTest(readme=str(readme.relative_to(ROOT))):
                    self.assertEqual(ROW.findall(readme.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
