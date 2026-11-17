"""What a backup says about a device, for the run's log."""

import struct
import unittest

from tools.espressif import device_facts, flashmap


def entry(label: bytes, kind: int, subtype: int, offset: int, size: int) -> bytes:
    return b"\xaa\x50" + bytes([kind, subtype]) + struct.pack("<II", offset, size) + label.ljust(16, b"\0") + bytes(4)


def flash(app: bytes = b"") -> bytes:
    image = bytearray(b"\xff" * 0x400000)
    image[0x1000:0x1000 + 14] = b"\xe9" + bytes(13)
    table = entry(b"nvs", 1, 2, 0x9000, 0x10000) + entry(b"ota_0", 0, 0x10, 0x20000, 0x1f0000)
    image[0x8000:0x8000 + len(table)] = table
    image[0x8000 + len(table):0x8000 + len(table) + 2] = b"\xff\xff"
    image[0x20000:0x20000 + len(app)] = app
    return bytes(image)


INFO = (b"bridge 4.4.5\nchip: esp32\nchip revision: 3.1\nflash id: c84016\n"
        b"fallback slot: ota_0 at 0x20000, 0x1f0000 bytes\ngpio levels: 0=1 2=0\ngpio input off:\n")


class InfoTests(unittest.TestCase):
    def test_its_lines_are_read_by_name(self):
        info = device_facts.parse_info(INFO)
        self.assertEqual(info["chip"], "esp32")
        self.assertEqual(info["gpio input off"], "")
        self.assertNotIn("bridge 4.4.5", info)

    def test_nothing_else_reaches_the_log(self):
        self.assertEqual(device_facts.parse_info(b"bridge 4.4.5\n<html>: hi\nchip: \xff\nnot a line\n"), {})
        self.assertEqual(device_facts.parse_info(b""), {})
        many = b"bridge 4.4.5\n" + b"".join(b"name %d: x\n" % n for n in range(100))
        self.assertLess(len(device_facts.parse_info(many)), 40)

    def test_with_no_answer_there_are_no_lines(self):
        self.assertEqual(device_facts.info_lines({}), [])
        self.assertEqual(device_facts.lines(flash(), None), device_facts.lines(flash(), None, {}))


class SecurityTests(unittest.TestCase):
    def test_a_plain_classic_esp32(self):
        fuses = device_facts.security(bytes(flashmap.ESP32_EFUSE_SIZE))
        self.assertEqual((fuses["secure_boot"], fuses["flash_encryption"]), (False, False))

    def test_a_locked_classic_esp32(self):
        efuse = bytearray(flashmap.ESP32_EFUSE_SIZE)
        struct.pack_into("<I", efuse, 0, 1 << 20)  # one bit of FLASH_CRYPT_CNT: encrypted
        struct.pack_into("<I", efuse, 24, 1 << 5)  # ABS_DONE_1: secure boot v2
        fuses = device_facts.security(bytes(efuse))
        self.assertEqual((fuses["secure_boot"], fuses["flash_encryption"]), (True, True))

    def test_encryption_turned_off_again_counts_as_off(self):
        efuse = bytearray(flashmap.ESP32_EFUSE_SIZE)
        struct.pack_into("<I", efuse, 0, 3 << 20)
        self.assertFalse(device_facts.security(bytes(efuse))["flash_encryption"])

    def test_an_esp32_c3(self):
        efuse = bytearray(flashmap.esp32c3_efuse("12:34:56:ab:cd:ef", 3))
        struct.pack_into("<I", efuse, 12, 1 << 20)  # SECURE_BOOT_EN
        fuses = device_facts.security(bytes(efuse))
        self.assertEqual((fuses["revision"], fuses["secure_boot"], fuses["flash_encryption"]), ("3", True, False))


class LinesTests(unittest.TestCase):
    def test_the_layout_and_the_firmwares_own_name_are_there(self):
        app = bytearray(b"\xe9" + bytes(0x200))
        struct.pack_into("<I", app, 32, device_facts.APP_DESC_MAGIC)
        app[48:53] = b"1.2.3"
        app[80:88] = b"esp32_fw"
        app[144:149] = b"v4.1x"
        app += b"FWSW-E32EX-EXAMPLE-ESP32D0WDV3\0"
        shown = "\n".join(device_facts.lines(flash(bytes(app)), bytes(flashmap.ESP32_EFUSE_SIZE)))
        self.assertIn("Chip: esp32", shown)
        self.assertIn("Flash size: 4 MB", shown)
        self.assertIn("Partition: ota_0 type 0x00/0x10 at 0x20000, 0x1f0000 bytes", shown)
        self.assertIn("esp32_fw version 1.2.3, ESP-IDF v4.1x", shown)
        self.assertIn("FWSW-E32EX-EXAMPLE-ESP32D0WDV3", shown)
        self.assertIn("Secure boot: off", shown)

    def test_a_classic_esp32s_revision_is_at_least_without_the_bridges_word(self):
        shown = device_facts.lines(flash(), bytes(flashmap.ESP32_EFUSE_SIZE))
        self.assertIn("Chip revision: at least 0", shown)

    def test_the_bridges_exact_revision_takes_its_place(self):
        info = device_facts.parse_info(INFO)
        shown = device_facts.lines(flash(), bytes(flashmap.ESP32_EFUSE_SIZE), info)
        self.assertIn("Chip revision: 3.1", shown)
        self.assertFalse([line for line in shown if "at least" in line], shown)
        self.assertIn("Installer says flash id: c84016", shown)
        self.assertIn("Installer says gpio levels: 0=1 2=0", shown)
        self.assertIn("Installer says fallback slot: ota_0 at 0x20000, 0x1f0000 bytes", shown)

    def test_an_encrypted_flash_is_said_to_be_so_and_stops_nothing(self):
        shown = "\n".join(device_facts.lines(bytes(0x400000), None))
        self.assertIn("not known", shown)
        self.assertIn("not readable", shown)
