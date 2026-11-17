"""A whole stock flash made again from a read-out and an official file: made-up
data throughout, laid out as the S61's 4 MB table and a 2 MB one are."""
import struct
import tempfile
import unittest
import unittest.mock
import zlib
from pathlib import Path

from tools.beken import fota_wrap, layouts, rbl, rebuild_dump as rd

SIZE_4M = 0x400000
TABLE_4M = (("bootloader", "beken_onchip_crc", 0, 0x10000), ("app", "beken_onchip_crc", 0x10000, 0x250000),
            ("download", "beken_onchip", 0x286000, 0x15A000))
TABLE_2M = (("bootloader", "beken_onchip_crc", 0, 0x10000), ("app", "beken_onchip_crc", 0x10000, 0x110000),
            ("download", "beken_onchip", 0x132000, 0xAE000))
TAIL_4M, TAIL_2M = 0x285F9A, 0x131F9A
OFFICIAL_APP = bytes(range(251)) * 200 + b"odd"   # a short last block
SETTINGS = b"the device's own settings"


def entry(name, device, offset, length):
    body = b"01PE" + name.encode().ljust(24, b"\0") + device.encode().ljust(24, b"\0") + struct.pack("<II", offset, length)
    return body + struct.pack("<I", zlib.crc32(body))


def official_file(firmware=OFFICIAL_APP):
    """An official file: an RBL in a FOTA container with its extra header bytes,
    the RBL long enough to reach past the first sector."""
    image = rbl.pack(firmware, version="1.1.0") + b"\xa5" * 0x2000
    desc = bytearray(76)
    desc[:7] = b"MADE-UP"
    desc[0x20:0x25] = b"1.1.0"
    extra = b"\x11" * 0x3F
    base = 16 + len(extra)
    struct.pack_into(">III", desc, 0x30, 24 + 76, len(image), zlib.crc32(image))
    struct.pack_into(">I", desc, 0x3C, zlib.crc32(bytes(desc[:0x3C])))
    trailer = bytearray(24)
    trailer[1] = 1
    struct.pack_into(">I", trailer, 0x14, zlib.crc32(bytes(trailer[:0x14])))
    head = bytearray(16)
    struct.pack_into("<IH", head, 0, 0x1BEEF11E, 0)
    struct.pack_into("<H", head, 12, len(extra))
    assert base == len(head) + len(extra)
    return bytes(head) + extra + bytes(trailer) + bytes(desc) + image


def a_readout(table=TABLE_4M, size=SIZE_4M, tail=TAIL_4M, interim_size=0x4000, old_app_end=0x60000):
    """A device's flash after its conversion: its bootloader; the interim firmware
    over the start of the old stock app, whose end survives; the interim
    firmware's header; its image left in the download slot."""
    flash = bytearray(b"\xff" * size)
    logical = bytearray(b"\xff" * 0xE000) + b"".join(entry(*row) for row in table)
    logical = bytes(logical).ljust(0xF000, b"\xff")
    bootloader = rd.stripe(logical)
    flash[:len(bootloader)] = bootloader
    old_app = rd.stripe(OFFICIAL_APP.ljust(old_app_end, b"\x77"))
    flash[0x11000:0x11000 + len(old_app)] = old_app
    interim = rd.stripe(b"\x33" * interim_size)
    flash[0x11000:0x11000 + len(interim)] = interim
    header = rd.stripe(rbl.Header(algo=rbl.ALGO_AES_GZIP, timestamp=0, name="app", version="obk", sn=b"", body_crc32=0,
                                  raw_hash=0, size_raw=interim_size, size_package=0).pack())
    flash[tail:tail + len(header)] = header
    download = dict((row[0], row) for row in table)["download"][2]
    flash[download + 0x1000:download + 0x3000] = b"\x44" * 0x2000
    return flash


def a_readout_4m():
    flash = a_readout()
    flash[0x1E0000:0x1E0000 + 8] = b"old copy"      # no firmware's: kept
    flash[0x3E0000:0x3E0000 + len(SETTINGS)] = SETTINGS
    flash[0x3E2000:0x3E2000 + len(SETTINGS)] = SETTINGS
    for start, stop in rd.INTERIM_WRITES:
        flash[start:stop] = b"\x55" * (stop - start)
    return bytes(flash)


class CalibrationTests(unittest.TestCase):
    def test_the_interim_firmware_never_writes_the_factory_calibration(self):
        # The radio calibration (and the SDK's ADC values) the factory wrote at
        # 0x3e0000: no firmware this tool installs could make it again.
        start, length = next((at, size) for name, at, size in layouts.BEKEN_4M.partitions if name == "RF Firmware")
        for begin, end in rd.INTERIM_WRITES:
            self.assertTrue(end <= start or begin >= start + length, hex(begin))


class StripeTests(unittest.TestCase):
    def test_the_crc_is_beken_s(self):
        self.assertEqual(rd.crc16(b"123456789"), 0xAEE7)   # CRC-16/CMS's check value

    def test_a_short_last_block_is_filled_as_the_stock_bootloader_fills_it(self):
        out = rd.stripe(b"a" * 33)
        self.assertEqual(len(out), 68)
        block = b"a" + bytes.fromhex("0000100080800229") + b"\0" * 23
        self.assertEqual(out[34:66], block)
        self.assertEqual(out[66:68], rd.crc16(block).to_bytes(2, "big"))
        self.assertEqual(rd.stripe(b"a" * 29)[29:32], bytes.fromhex("000010"))   # as much as there is room for
        self.assertEqual(len(rd.stripe(b"a" * 32)), 34)

    def test_logical_addresses_map_to_the_flash_as_the_dumps_show(self):
        self.assertEqual(rd.physical(0x10000), 0x11000)
        self.assertEqual(rd.physical(0x25FFA0), TAIL_4M)
        self.assertEqual(rd.physical(0x11FFA0), TAIL_2M)


class TableTests(unittest.TestCase):
    def test_the_table_is_read_from_the_bootloader(self):
        table = rd.partitions(bytes(a_readout()))
        self.assertEqual(table["app"].physical(0), 0x11000)
        self.assertEqual(table["app"].physical(table["app"].length), 0x286000)
        self.assertEqual((table["download"].physical(0), table["download"].length), (0x286000, 0x15A000))

    def test_an_entry_failing_its_crc_is_no_entry(self):
        flash = bytearray(a_readout())
        logical = rd.stripe((b"\xff" * 0xE000 + entry(*TABLE_4M[0])[:-1] + b"\0" + b"".join(entry(*r) for r in TABLE_4M[1:])))
        flash[:len(logical)] = logical
        with self.assertRaisesRegex(rd.RebuildError, "bootloader partition"):
            rd.partitions(bytes(flash))


class OfficialFileTests(unittest.TestCase):
    def test_the_rbl_is_found_past_the_containers_extra_header(self):
        name, version, image = fota_wrap.unwrap(official_file())[0]
        self.assertEqual((name, version, rbl.unpack(image)), ("MADE-UP", "1.1.0", OFFICIAL_APP))

    def test_the_tools_own_container_unwraps_too(self):
        image = rbl.pack(b"x" * 100, version="1")
        self.assertEqual(fota_wrap.unwrap(fota_wrap.wrap(image, version="9.9.9")), [(fota_wrap.STAGING_NAME, "9.9.9", image)])

    def test_a_damaged_container_is_refused(self):
        good = official_file()
        for where, message in ((0, "magic"), (16 + 0x3F + 1, "trailer"), (16 + 0x3F + 24, "descriptor"), (len(good) - 1, "image 0")):
            damaged = bytearray(good)
            damaged[where] ^= 0xFF
            with self.subTest(where=where), self.assertRaisesRegex(ValueError, message):
                fota_wrap.unwrap(bytes(damaged))

    def test_a_file_that_is_an_rbl_is_taken_as_it_is(self):
        image = rbl.pack(b"x" * 100, version="1")
        self.assertEqual(rd.official_image(image), image)


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.image = fota_wrap.unwrap(official_file())[0][2]
        self.flash = rd.rebuild(a_readout_4m(), official_file())

    def test_the_official_app_stands_in_the_interim_firmware_s_place(self):
        striped = rd.stripe(OFFICIAL_APP)
        self.assertEqual(self.flash[0x11000:0x11000 + len(striped)], striped)
        self.assertEqual(self.flash[TAIL_4M:TAIL_4M + 102], rd.stripe(self.image[:96]))

    def test_the_interim_firmware_is_gone_past_a_shorter_official_one(self):
        readout = bytearray(a_readout_4m())
        short = official_file(b"short" * 100)
        flash = rd.rebuild(bytes(readout), short)
        end = 0x11000 + len(rd.stripe(b"short" * 100))
        self.assertEqual(set(flash[end:0x11000 + rd.physical(0x4000)]), {0xFF})

    def test_the_download_slot_is_as_the_bootloader_leaves_it(self):
        self.assertEqual(set(self.flash[0x286000:0x287000]), {0xFF})
        self.assertEqual(self.flash[0x287000:0x286000 + len(self.image)], self.image[0x1000:])
        self.assertEqual(set(self.flash[0x286000 + len(self.image):0x3E0000]), {0xFF})

    def test_what_the_device_keeps_is_kept_and_the_interim_firmware_s_writes_are_not(self):
        self.assertEqual(self.flash[:0x10000], a_readout_4m()[:0x10000])
        self.assertEqual(self.flash[0x3E0000:0x3E0000 + len(SETTINGS)], SETTINGS)
        self.assertEqual(self.flash[0x3E2000:0x3E2000 + len(SETTINGS)], SETTINGS)
        self.assertEqual(self.flash[0x1E0000:0x1E0000 + 8], b"old copy")
        for start, stop in rd.INTERIM_WRITES:
            self.assertEqual(set(self.flash[start:stop]), {0xFF}, hex(start))

    def test_a_table_without_known_interim_writes_needs_them_given(self):
        readout = bytes(a_readout(TABLE_2M, 0x200000, TAIL_2M))
        with self.assertRaisesRegex(rd.RebuildError, "4 MB table"):
            rd.rebuild(readout, official_file())
        flash = rd.rebuild(readout, official_file(), interim_writes=((0x1E1000, 0x1E2000),))
        self.assertEqual(flash[TAIL_2M:TAIL_2M + 102], rd.stripe(self.image[:96]))
        self.assertEqual(flash[0x133000:0x132000 + len(self.image)], self.image[0x1000:])

    def test_a_firmware_too_big_for_the_device_is_refused(self):
        readout = bytes(a_readout(TABLE_2M, 0x200000, TAIL_2M))
        too_big = rbl.pack(b"\0" * 0x110000, version="1")
        with self.assertRaisesRegex(rd.RebuildError, "does not fit"):
            rd.rebuild(readout, too_big, interim_writes=())

    def test_an_image_for_something_else_is_refused(self):
        with self.assertRaisesRegex(rd.RebuildError, "'bootloader'"):
            rd.rebuild(a_readout_4m(), rbl.pack(b"x" * 100, name="bootloader", version="1"))


class SurvivingAppTests(unittest.TestCase):
    def test_the_old_app_s_end_says_whether_the_file_is_the_version_the_device_ran(self):
        readout = bytes(a_readout(old_app_end=len(OFFICIAL_APP)))
        same, compared = rd.surviving_old_app(readout, official_file())
        self.assertGreater(compared, 0)
        self.assertEqual(same, compared)
        other = bytes(reversed(OFFICIAL_APP))
        same, compared = rd.surviving_old_app(readout, official_file(other))
        self.assertLess(same, compared)


class MainTests(unittest.TestCase):
    def test_it_writes_the_flash_and_compares_it_region_by_region(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "readout").write_bytes(a_readout_4m())
            (root / "official").write_bytes(official_file())
            answer = bytearray(rd.rebuild(a_readout_4m(), official_file()))
            answer[0x3E2000] ^= 0xFF
            (root / "answer").write_bytes(answer)
            with unittest.mock.patch("builtins.print") as shown:
                code = rd.main([str(root / "readout"), str(root / "official"), str(root / "out"),
                                "--compare", str(root / "answer")])
            self.assertEqual(code, 0)
            self.assertEqual((root / "out").read_bytes(), rd.rebuild(a_readout_4m(), official_file()))
            lines = "\n".join(str(call.args[0]) for call in shown.call_args_list)
            self.assertRegex(lines, r"EasyFlash app store\s+1 bytes differ")
            self.assertRegex(lines, r"bootloader\s+same")

    def test_something_unusable_is_said_and_not_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "readout").write_bytes(b"\xff" * 0x20000)
            (root / "official").write_bytes(official_file())
            with unittest.mock.patch("builtins.print") as shown:
                self.assertEqual(rd.main([str(root / "readout"), str(root / "official"), str(root / "out")]), 1)
            self.assertIn("partition", str(shown.call_args.args[0]))
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
