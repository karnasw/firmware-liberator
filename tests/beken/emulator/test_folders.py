"""The Beken emulator's folders: an earlier checkout's, named after the S61,
moved to captures/emulation/beken/ once - never while a container uses one,
never over one already there, and nothing else in captures/ touched."""

import tempfile
import unittest
from pathlib import Path

from tools.beken.emulator import folders

OLD = {
    "s61/autoflash-s61": "beken/state",
    "s61/autoflash-s61-3": "beken/state-3",
    "s61/saved": "beken/saved",
    "s61/earlier-v2.3.0": "beken/legacy/v2.3.0",
    "s61-suite": "beken/suite",
    "s61-openbeken": "beken/suite-openbeken",
    "beken-firmwares": "beken/firmwares",
}


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.captures = Path(self.tmp.name, "captures")
        self.emulation = self.captures / "emulation"

    def make(self, *names):
        for name in names:
            path = self.emulation / name
            path.mkdir(parents=True)
            (path / "made.txt").write_text(name, encoding="utf-8")

    def migrate(self, mounted=()):
        return folders.migrate(self.emulation, sources=lambda: [Path(path) for path in mounted])

    def test_each_old_folder_goes_to_its_new_name(self):
        self.make(*OLD)
        said = self.migrate()
        for old, new in OLD.items():
            self.assertFalse((self.emulation / old).exists(), old)
            self.assertEqual((self.emulation / new / "made.txt").read_text(encoding="utf-8"), old)
        self.assertFalse((self.emulation / "s61").exists())
        self.assertEqual(len(said), 1)
        self.assertIn("s61-suite to beken/suite", said[0])

    def test_nothing_to_move_says_nothing(self):
        self.make("state", "beken/state")
        self.assertEqual(self.migrate(), [])

    def test_none_is_moved_while_a_container_uses_any(self):
        self.make("s61/autoflash-s61-1", "s61-suite")
        said = self.migrate(mounted=[self.emulation / "s61/autoflash-s61-1/work"])
        self.assertTrue((self.emulation / "s61/autoflash-s61-1/made.txt").exists())
        self.assertTrue((self.emulation / "s61-suite/made.txt").exists())
        self.assertFalse((self.emulation / "beken").exists())
        self.assertIn("while a container uses them", said[0])

    def test_a_container_on_the_checkout_alone_does_not_stop_it(self):
        # The device's container has the whole checkout at /work, read-only.
        self.make("s61-suite")
        self.migrate(mounted=[Path(self.tmp.name), self.emulation / "state-1"])
        self.assertTrue((self.emulation / "beken/suite/made.txt").exists())

    def test_none_is_moved_where_docker_cannot_say(self):
        self.make("s61-suite")
        said = folders.migrate(self.emulation, sources=lambda: None)
        self.assertTrue((self.emulation / "s61-suite/made.txt").exists())
        self.assertIn("Docker cannot say", said[0])

    def test_a_new_folder_already_there_is_never_overwritten(self):
        self.make("s61/saved", "beken/saved", "s61-suite")
        said = self.migrate()
        self.assertEqual((self.emulation / "beken/saved/made.txt").read_text(encoding="utf-8"), "beken/saved")
        self.assertEqual((self.emulation / "s61/saved/made.txt").read_text(encoding="utf-8"), "s61/saved")
        self.assertTrue((self.emulation / "beken/suite/made.txt").exists())
        self.assertIn("left s61/saved (beken/saved is there)", said[0])

    def test_what_it_does_not_know_is_left_where_it_is(self):
        self.make("s61/someone-elses", "s61/autoflash-s61")
        self.migrate()
        self.assertTrue((self.emulation / "s61/someone-elses/made.txt").exists())
        self.assertTrue((self.emulation / "beken/state/made.txt").exists())

    def test_nothing_outside_the_emulators_folders_is_touched(self):
        # A device's stock backup cannot be made again.
        backup = self.captures / "devices/12-34-56-ab-cd-ef/sonoff-backup"
        backup.mkdir(parents=True)
        (backup / "flash.bin").write_bytes(b"stock")
        readout = self.captures / "flash-readout"
        readout.mkdir()
        self.make(*OLD, "state", "saved", "suite")
        self.migrate()
        self.assertEqual((backup / "flash.bin").read_bytes(), b"stock")
        self.assertTrue(readout.is_dir())
        for name in ("state", "saved", "suite"):
            self.assertEqual((self.emulation / name / "made.txt").read_text(encoding="utf-8"), name)


class LayoutTests(unittest.TestCase):
    def test_laid_out_as_the_espressif_emulators(self):
        self.assertEqual(folders.state(), folders.EMU / "beken/state")
        self.assertEqual(folders.state(2), folders.EMU / "beken/state-2")
        self.assertEqual(folders.EMU, folders.ROOT / "captures/emulation")


if __name__ == "__main__":
    unittest.main()
