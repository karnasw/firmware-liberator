"""What a dump says of itself: its chip, its partitions, its MAC."""

from __future__ import annotations

import struct
import unittest

from tools.espressif import flashmap
from tools.espressif.emulator import copy_device


def entry(label: bytes, kind: int, subtype: int, offset: int, size: int) -> bytes:
    return b"\xaa\x50" + bytes([kind, subtype]) + struct.pack("<II", offset, size) + label.ljust(16, b"\0") + bytes(4)


def flash_with(bootloader_at: int, chip_id: int) -> bytes:
    data = bytearray(b"\xff" * 0x9000)
    data[bootloader_at] = 0xE9
    struct.pack_into("<H", data, bootloader_at + 12, chip_id)
    table = entry(b"nvs", 1, 2, 0x9000, 0x40000) + entry(b"ota_0", 0, 0x10, 0x60000, 0x1000)
    data[0x8000:0x8000 + len(table)] = table
    return bytes(data)


class Dump(unittest.TestCase):
    def test_each_chip_is_told_by_where_its_bootloader_is(self):
        self.assertEqual(flashmap.chip_of(flash_with(0x1000, 0)), "esp32")
        self.assertEqual(flashmap.chip_of(flash_with(0, 5)), "esp32c3")

    def test_a_bootloader_for_another_chip_where_it_is_found_is_refused(self):
        with self.assertRaises(ValueError):
            flashmap.chip_of(flash_with(0x1000, 5))

    def test_an_encrypted_dump_is_said_to_be_one(self):
        with self.assertRaisesRegex(ValueError, "encrypted"):
            flashmap.chip_of(b"\x37" * 0x2000)
        with self.assertRaisesRegex(ValueError, "encrypted"):
            flashmap.partitions(b"\x37" * 0x9000)

    def test_settings_and_slots_come_from_its_own_table(self):
        flash = flash_with(0x1000, 0)
        self.assertEqual((flashmap.nvs(flash).offset, flashmap.nvs(flash).size), (0x9000, 0x40000))
        self.assertEqual([slot.offset for slot in flashmap.app_slots(flash)], [0x60000])

    def test_a_table_built_to_be_at_0xc000_is_read_there(self):
        # The BASICR4's firmware reads its table there (CONFIG_PARTITION_TABLE_OFFSET).
        flash = bytearray(flash_with(0, 5)) + b"\xff" * 0x4000
        flash[0xC000:0xC040], flash[0x8000:0x8040] = flash[0x8000:0x8040], b"\xff" * 0x40
        self.assertEqual(flashmap.table_at(bytes(flash)), 0xC000)
        self.assertEqual([slot.offset for slot in flashmap.app_slots(bytes(flash))], [0x60000])


class Mac(unittest.TestCase):
    MAC = "12:34:56:ab:cd:ef"

    def test_a_classic_esp32s_reads_back(self):
        efuse = flashmap.esp32_efuse(self.MAC)
        self.assertEqual(len(efuse), flashmap.ESP32_EFUSE_SIZE)
        self.assertEqual(flashmap.mac_of_efuse(efuse), self.MAC)

    def test_a_classic_esp32s_has_espefuses_crc(self):
        # esp_efuse_mac_get_default aborts the firmware when this is wrong: the
        # first emulated boot of a classic ESP32 found it. Dallas/Maxim's check
        # value for "123456789" is 0xA1.
        self.assertEqual(flashmap.crc8(b"123456789"), 0xA1)
        self.assertEqual(flashmap.esp32_efuse(self.MAC)[10], flashmap.crc8(bytes.fromhex("123456abcdef")))

    def test_an_esp32c3s_is_where_it_was(self):
        efuse = bytearray(1024)
        efuse[0x18:0x1E] = bytes.fromhex("123456abcdef")[::-1]
        self.assertEqual(flashmap.mac_of_efuse(bytes(efuse)), self.MAC)

    def test_a_test_copy_of_either_takes_the_new_mac(self):
        for efuse in (flashmap.esp32_efuse("12:34:56:00:00:01"), bytes(1024)):
            self.assertEqual(flashmap.mac_of_efuse(copy_device.with_mac(efuse, self.MAC)), self.MAC)


class Esp32C3Efuse(unittest.TestCase):
    def test_it_holds_the_mac_and_the_revision_its_firmware_needs(self):
        efuse = flashmap.esp32c3_efuse("12:34:56:ab:cd:ef", 3)
        self.assertEqual(len(efuse), flashmap.ESP32C3_EFUSE_SIZE)
        self.assertEqual(flashmap.mac_of_efuse(efuse), "12:34:56:ab:cd:ef")
        self.assertEqual((int.from_bytes(efuse[0x24:0x28], "little") >> 18) & 7, 3)

    def test_it_holds_adc_calibration_as_every_chip_does(self):
        # BLK_VERSION_MAJOR, block 2's bits 128-129: in the word EFUSE_RD_SYS_PART1_DATA4.
        efuse = flashmap.esp32c3_efuse("12:34:56:ab:cd:ef")
        self.assertEqual(int.from_bytes(efuse[0x40:0x44], "little") & 3, 1)

    def test_the_revision_is_read_from_the_apps_header(self):
        flash = bytearray(0x20000)
        flash[0x10000 + 14] = 3
        self.assertEqual(flashmap.min_chip_revision(bytes(flash), flashmap.Partition("ota_0", 0, 0x10, 0x10000, 0x10000)), 3)



if __name__ == "__main__":
    unittest.main()
