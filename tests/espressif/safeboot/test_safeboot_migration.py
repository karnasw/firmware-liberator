"""What tools/espressif/safeboot/safeboot_migration.py holds a migration to: the
source, interim and Tasmota tables, the pinned firmware, and the NVS gate."""
import hashlib
import struct
import unittest
import zlib
from unittest import mock

from tools.espressif.safeboot.safeboot_migration import (
    MD5_MAGIC,
    PARTITION_MAGIC,
    PINNED_ARTIFACTS,
    SECTOR_SIZE,
    SOURCE_PARTITIONS,
    SOURCE_TABLE_SHA256,
    OFFICIAL_PARTITIONS,
    OFFICIAL_TABLE_SHA256,
    TARGET_PARTITIONS,
    TARGET_TABLE_SHA256,
    analyze_canonical_nvs,
    official_release,
    parse_partition_sector,
    require_pinned_artifacts,
    require_tasmota_table,
    sector_aligned_size,
    sha256_bytes,
    target_table,
)


def partition_sector(entries):
    data = bytearray(b"\xff" * SECTOR_SIZE)
    offset = 0
    for label, ptype, subtype, start, size, flags in entries:
        struct.pack_into(
            "<HBBII16sI",
            data,
            offset,
            PARTITION_MAGIC,
            ptype,
            subtype,
            start,
            size,
            label.encode("ascii").ljust(16, b"\0"),
            flags,
        )
        offset += 32
    digest = hashlib.md5(data[:offset]).digest()
    data[offset : offset + 32] = struct.pack("<H", MD5_MAGIC) + b"\xff" * 14 + digest
    return bytes(data)


def canonical_nvs():
    data = bytearray(b"\xff" * 0x5000)
    page = bytearray(b"\xff" * SECTOR_SIZE)
    struct.pack_into("<II", page, 0, 0xFFFFFFFE, 7)
    struct.pack_into("<I", page, 28, zlib.crc32(page[4:28], 0xFFFFFFFF) & 0xFFFFFFFF)

    def mark_written(index):
        shift = (index % 4) * 2
        page[32 + index // 4] &= ~(0x03 << shift)
        page[32 + index // 4] |= 0x02 << shift

    def entry_crc(entry):
        struct.pack_into(
            "<I", entry, 4, zlib.crc32(entry[:4] + entry[8:32], 0xFFFFFFFF) & 0xFFFFFFFF
        )

    next_entry = 0
    for key, chunk_index in (("Settings", 0), ("sta.apinfo", 128)):
        payload = key.encode("ascii")[:4]
        entry = bytearray(b"\xff" * 32)
        struct.pack_into("<BBBB", entry, 0, 1, 0x42, 2, chunk_index)
        entry[8:24] = key.encode("ascii").ljust(16, b"\0")
        struct.pack_into("<H", entry, 24, len(payload))
        struct.pack_into("<I", entry, 28, zlib.crc32(payload, 0xFFFFFFFF) & 0xFFFFFFFF)
        entry_crc(entry)
        page[64 + next_entry * 32 : 96 + next_entry * 32] = entry
        page[96 + next_entry * 32 : 128 + next_entry * 32] = payload.ljust(32, b"\xff")
        mark_written(next_entry)
        mark_written(next_entry + 1)
        next_entry += 2

        index_entry = bytearray(b"\xff" * 32)
        struct.pack_into("<BBBB", index_entry, 0, 1, 0x48, 1, 0xFF)
        index_entry[8:24] = key.encode("ascii").ljust(16, b"\0")
        struct.pack_into("<IBBH", index_entry, 24, len(payload), 1, chunk_index, 0xFFFF)
        entry_crc(index_entry)
        page[64 + next_entry * 32 : 96 + next_entry * 32] = index_entry
        mark_written(next_entry)
        next_entry += 1
    data[:SECTOR_SIZE] = page
    return bytes(data)


class SafebootMigrationTests(unittest.TestCase):
    """The layouts, the pinned firmware and the NVS gate a migration is held to."""

    def test_partition_fixtures_and_nvs_gate(self):
        source = parse_partition_sector(partition_sector(SOURCE_PARTITIONS))
        self.assertEqual(source["sha256"], SOURCE_TABLE_SHA256)
        report = analyze_canonical_nvs(canonical_nvs())
        self.assertTrue(report["ready"])
        self.assertEqual(report["missing_keys"], [])
        self.assertEqual(sector_aligned_size(12_345), 16_384)

    def test_nvs_gate_rejects_blob_whose_index_survives_but_chunk_does_not(self):
        nvs = bytearray(canonical_nvs())
        # Erase the Settings BLOB_DATA entry and its payload while leaving its
        # BLOB_IDX record written. Key-name-only validation would miss this.
        nvs[32] |= 0x0F
        report = analyze_canonical_nvs(bytes(nvs))
        self.assertFalse(report["ready"])
        self.assertEqual(report["missing_keys"], [])
        self.assertIn("Settings", report["keys"])
        self.assertEqual(report["incomplete_keys"], ["Settings"])

    def test_pinned_artifact_gate_rejects_manifest_substitution(self):
        manifest = {
            "official_release": official_release(),
            "artifacts": {
                name: dict(expected) for name, expected in PINNED_ARTIFACTS.items()
            },
        }
        require_pinned_artifacts(manifest)
        manifest["artifacts"]["safeboot"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "safeboot sha256 differs"):
            require_pinned_artifacts(manifest)

    def test_a_bundle_of_a_release_pinned_before_is_taken_unless_current_only(self):
        # A run that installed that release at F2, carried on by a version
        # that pins a later one, finishes with its bundle.
        from tools.espressif.safeboot import safeboot_migration as sm
        earlier = {"version": "1.0.0", "files": {
            name: {"name": name, "size": 10 + index, "sha256": f"{index}" * 64}
            for index, name in enumerate(("factory", "safeboot", "app"))}}
        manifest = {
            "official_release": {"install_commit": "abc", "tasmota_commit": "def"},
            "artifacts": {"bluetooth": dict(PINNED_ARTIFACTS["bluetooth"]),
                          **{name: {"size": f["size"], "sha256": f["sha256"]}
                             for name, f in earlier["files"].items()}},
        }
        with mock.patch.dict(sm.OFFICIAL_TASMOTA, {"earlier": [earlier]}):
            self.assertEqual(sm.earlier_release(manifest), earlier)
            require_pinned_artifacts(manifest)
            with self.assertRaisesRegex(ValueError, "identity differs"):
                require_pinned_artifacts(manifest, current_only=True)
            manifest["artifacts"]["app"]["sha256"] = "f" * 64
            self.assertIsNone(sm.earlier_release(manifest))
            with self.assertRaisesRegex(ValueError, "identity differs"):
                require_pinned_artifacts(manifest)

    def test_the_interim_layout_moves_only_the_low_regions_clear_of_the_commit(self):
        import tools.espressif.safeboot.safeboot_migration as migration
        table = migration.interim_table()
        self.assertEqual(sha256_bytes(table), migration.INTERIM_TABLE_SHA256)
        source = {e["label"]: e for e in migration.parse_partition_sector(
            migration.SOURCE_TABLE_FILE.read_bytes())["entries"]}
        interim = {e["label"]: e for e in migration.parse_partition_sector(table)["entries"]}
        low = ("nvs", "reserve", "otadata", "phy_init")
        # Moved whole, each the same size and the same distance apart.
        for label in low:
            self.assertEqual(interim[label]["size"], source[label]["size"], label)
            self.assertEqual(interim[label]["offset"] - source[label]["offset"],
                             migration.MOVED_LOW_OFFSET - migration.LOW_REGIONS_OFFSET, label)
        self.assertEqual(interim["ota_1"], source["ota_1"])  # the Tasmota that starts
        moved = (migration.MOVED_LOW_OFFSET, migration.MOVED_LOW_OFFSET + migration.LOW_REGIONS_SIZE)
        # In what was ota_0, clear of everything the commit writes or reads its
        # copy from, and of the shrunk ota_0.
        self.assertGreaterEqual(moved[0], interim["ota_0"]["end"])
        self.assertEqual(moved[1], migration.OLD_OTA1_OFFSET)
        self.assertGreaterEqual(moved[0], migration.SAFEBOOT_STAGE_OFFSET + migration.SAFEBOOT_SIZE)
        self.assertGreaterEqual(moved[0], migration.SAFEBOOT_OFFSET + migration.SAFEBOOT_SIZE)
        with self.assertRaises(ValueError):
            migration.interim_table(b"\xff" * 4096)

    def test_the_table_written_is_tasmotas_with_the_identitys_sector(self):
        official = partition_sector(OFFICIAL_PARTITIONS)
        self.assertEqual(sha256_bytes(official), OFFICIAL_TABLE_SHA256)  # as the factory image has it
        table = target_table(official)
        self.assertEqual(sha256_bytes(table), TARGET_TABLE_SHA256)
        entries = {e["label"]: e for e in parse_partition_sector(table)["entries"]}
        self.assertEqual((entries["spiffs"]["offset"], entries["spiffs"]["end"]), (0x3B0000, 0x3FF000))
        self.assertEqual((entries["sonoff"]["type"], entries["sonoff"]["subtype"], entries["sonoff"]["offset"],
                          entries["sonoff"]["end"]), (0x01, 0x40, 0x3FF000, 0x400000))
        # Everything else as Tasmota has it.
        self.assertEqual([e for e in parse_partition_sector(table)["entries"][:4]],
                         parse_partition_sector(official)["entries"][:4])
        for other in (partition_sector(TARGET_PARTITIONS), partition_sector(SOURCE_PARTITIONS)):
            with self.assertRaisesRegex(ValueError, "partition entries differ|official"):
                target_table(other)


class TasmotaTableTests(unittest.TestCase):
    """A run's bundle may name either Tasmota table this tool converts to: a run
    begun before the identity's sector was added carries Tasmota's own, and G1
    resumed by this version must still take it."""

    def test_either_reviewed_table_is_taken(self):
        for partitions in (TARGET_PARTITIONS, OFFICIAL_PARTITIONS):
            with self.subTest(entries=len(partitions)):
                require_tasmota_table(parse_partition_sector(partition_sector(partitions)))

    def test_any_other_is_refused(self):
        changed = tuple(entry if entry[0] != "spiffs" else (*entry[:4], 0x040000, 0) for entry in OFFICIAL_PARTITIONS)
        with self.assertRaisesRegex(ValueError, "partition entries differ"):
            require_tasmota_table(parse_partition_sector(partition_sector(changed)))


if __name__ == "__main__":
    unittest.main()
