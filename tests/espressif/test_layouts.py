"""The partition tables: each built table is the reviewed one, and a sound one."""
import hashlib
import unittest

from tools.espressif import layouts
from tools.espressif.safeboot import safeboot_migration


class TemporaryTableTests(unittest.TestCase):
    LAYOUT = layouts.SONOFF_TH_ELITE

    def rows(self):
        return {row[0]: row for row in self.LAYOUT.temporary_partitions()}

    def test_it_is_the_reviewed_table(self):
        self.assertEqual(hashlib.sha256(self.LAYOUT.temporary_table()).hexdigest(), self.LAYOUT.temporary_sha256)
        self.assertEqual(self.LAYOUT.leaving_sha256, self.LAYOUT.temporary_sha256)

    def test_it_is_a_valid_table_the_size_of_a_flash_sector(self):
        table = self.LAYOUT.temporary_table()
        self.assertEqual(len(table), safeboot_migration.TABLE_SIZE)
        parsed = safeboot_migration.parse_partition_sector(table)
        self.assertEqual(parsed["stored_md5"], parsed["calculated_md5"])
        self.assertEqual(parsed["entry_count"], len(self.LAYOUT.partitions()))

    def test_its_partitions_do_not_overlap_and_the_mover_fits_its_ota_0(self):
        rows = sorted(self.LAYOUT.temporary_partitions(), key=lambda row: row[3])
        for row, following in zip(rows, rows[1:]):
            self.assertLessEqual(row[3] + row[4], following[3], (row, following))
        self.assertEqual(self.rows()["ota_0"][3], layouts.MOVER_AT)
        self.assertGreaterEqual(self.rows()["ota_0"][4], 0xC0000)
        self.assertLessEqual(rows[-1][3] + rows[-1][4], 0x400000)

    def test_ota_1_is_where_the_devices_own_table_has_it(self):
        # The hub's Tasmota runs from there as the one table gives way to the other.
        own = {row[0]: row for row in self.LAYOUT.partitions()}
        self.assertEqual(self.rows()["ota_1"], own["ota_1"])
        self.assertEqual(self.rows()["nvs"][3], own["nvs"][3])

    def test_a_layout_with_ota_0_at_the_movers_place_has_no_need_of_one(self):
        for layout in (layouts.SONOFF_DUAL_OTA, layouts.SONOFF_NSPANEL):
            self.assertFalse(layout.needs_temporary)
            self.assertEqual(layout.leaving_sha256, layout.table_sha256)

    def test_a_layout_with_ota_0_elsewhere_must_have_one_reviewed(self):
        for layout in layouts.ALL:
            if layout.needs_temporary:
                self.assertEqual(hashlib.sha256(layout.temporary_table()).hexdigest(), layout.temporary_sha256)


if __name__ == "__main__":
    unittest.main()
