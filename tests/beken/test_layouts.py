"""Reading a Beken app's partition table, in made-up apps: names, then entries
pointing at them, at the places asked for. No firmware is here."""
import re
import struct
import unittest
from pathlib import Path

from tools.autoflash import kinds
from tools.beken import layouts

FOUR_MB = (("Bootloader", 0x0, 0xF000), ("Application", 0x11000, 0x250000), ("ota", 0x286000, 0x15A000),
           ("RF Firmware", 0x3E0000, 0x1000), ("NET info", 0x3E1000, 0x1000), ("NVS", 0x3E2000, 0xB000),
           ("KEY", 0x3ED000, 0x1000))
TWO_MB = (("Bootloader", 0x0, 0xF000), ("Application", 0x11000, 0x121000), ("ota", 0x132000, 0xAE000),
          ("RF Firmware", 0x1E0000, 0x1000), ("NET info", 0x1E1000, 0x1000))


def an_app(*tables, base=layouts.APP_BASE, spaced=False):
    """An app holding these tables: the names first, shared, then each table
    with 0xff words around it, which nothing could read as an entry."""
    names = sorted({name for table in tables for name, _, _ in table})
    app, at = bytearray(b"\xff" * 4 + b"\0"), {}
    for name in names:
        at[name] = base + len(app)
        app += name.encode("ascii") + b"\0"
    app += b"\0" * (-len(app) % 4)
    for table in tables:
        app += b"\xff" * 20
        for name, start, length in table:
            app += struct.pack("<5I", 0, at[name], start, length, 1)
        app += b"\xff" * (40 if spaced else 20)
    return bytes(app)


class ReadingTests(unittest.TestCase):
    def test_the_table_is_read_by_its_entries_descriptions(self):
        table = layouts.table_of(an_app(FOUR_MB))
        self.assertEqual([(part.description, part.start, part.length) for part in table], list(FOUR_MB))
        self.assertEqual(str(table[2]), "ota 0x286000+0x15a000")

    def test_it_follows_where_the_app_runs(self):
        table = layouts.table_of(an_app(FOUR_MB, base=0x20000), base=0x20000)
        self.assertEqual(len(table), len(FOUR_MB))
        with self.assertRaises(layouts.LayoutError):
            layouts.table_of(an_app(FOUR_MB, base=0x20000))

    def test_no_table_is_refused(self):
        with self.assertRaisesRegex(layouts.LayoutError, "no partition table"):
            layouts.table_of(b"Application\0ota\0nothing refers to these" + b"\xff" * 64)

    def test_two_that_differ_are_refused(self):
        with self.assertRaisesRegex(layouts.LayoutError, "holds 2 partition tables"):
            layouts.table_of(an_app(FOUR_MB, TWO_MB))

    def test_the_same_table_twice_is_one(self):
        self.assertEqual(len(layouts.table_of(an_app(FOUR_MB, FOUR_MB))), len(FOUR_MB))

    def test_a_name_ending_another_string_is_not_that_name(self):
        app = b"\0xota\0" + b"\0" * 2
        pointer = layouts.APP_BASE + 2 + 1   # "ota" inside "xota"
        app += struct.pack("<5I", 0, pointer, 1, 1, 1) * 3 + b"\xff" * 20
        with self.assertRaises(layouts.LayoutError):
            layouts.table_of(app)


class KnownTests(unittest.TestCase):
    def test_the_4_mb_table_is_known(self):
        layout, table = layouts.known(an_app(FOUR_MB))
        self.assertIs(layout, layouts.BEKEN_4M)
        self.assertEqual(layout.flash_size, 0x400000)
        self.assertEqual(len(table), len(FOUR_MB))
        self.assertIs(layouts.of("beken-4m"), layouts.BEKEN_4M)

    def test_the_2_mb_table_is_not(self):
        with self.assertRaisesRegex(layouts.LayoutError, "not one this tool knows.*NET info 0x1e1000"):
            layouts.known(an_app(TWO_MB))

    def test_any_place_the_conversion_depends_on_moved_is_refused(self):
        for name, start, length in FOUR_MB:
            if name in ("Bootloader", "KEY"):  # which it neither writes nor reads
                continue
            for moved in ((name, start + 0x1000, length), (name, start, length + 0x1000)):
                table = tuple(moved if entry[0] == name else entry for entry in FOUR_MB)
                with self.subTest(moved=moved), self.assertRaises(layouts.LayoutError):
                    layouts.known(an_app(table))

    def test_one_missing_is_refused(self):
        table = tuple(entry for entry in FOUR_MB if entry[0] != "NET info")
        with self.assertRaises(layouts.LayoutError):
            layouts.known(an_app(table))

    def test_the_name_is_taken_in_either_case(self):
        table = tuple(("OTA" if name == "ota" else name, start, length) for name, start, length in FOUR_MB)
        self.assertIs(layouts.known(an_app(table))[0], layouts.BEKEN_4M)



class CalibrationTests(unittest.TestCase):
    def test_each_beken_esphome_keeps_the_factory_calibration_where_the_sdk_reads_it(self):
        # LibreTiny points the SDK's radio calibration at board_flash.calibration:
        # it is the device's own sector, and nothing else of ESPHome's is there.
        start, length = next((at, size) for name, at, size in layouts.BEKEN_4M.partitions if name == "RF Firmware")
        root = Path(__file__).resolve().parents[2]
        for kind in kinds.EVERY:
            if kind.family != "beken":
                continue
            with self.subTest(kind=kind.key):
                config = (root / kind.folder / "esphome.yaml").read_text(encoding="utf-8")
                parts = {name: (int(at, 16), int(size, 16)) for name, at, size in
                         re.findall(r'board_flash\.(\w+): "0x([0-9A-Fa-f]+)\+0x([0-9A-Fa-f]+)"', config)}
                self.assertEqual(parts.pop("calibration"), (start, length))
                for name, (at, size) in parts.items():
                    self.assertTrue(at + size <= start or at >= start + length, name)


if __name__ == "__main__":
    unittest.main()
