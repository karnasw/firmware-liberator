"""The marker sent after the interim firmware (tools/beken/interim_marker.py)."""
import re
import struct
import unittest
import zlib
from pathlib import Path

from tools.beken import interim_marker, rbl, uf2


ROOT = Path(__file__).resolve().parents[2]


APP = bytes(range(256)) * 40   # an app, as an image installs it


def an_image(size: int, app: bytes = APP) -> bytes:
    """An rt_ota image of `size` bytes installing `app`: what follows an RBL's
    body is no part of it, so filler makes it any size."""
    image = rbl.pack(app, version="1.1.0")
    return image + b"i" * (size - len(image))


class InterimMarkerTests(unittest.TestCase):
    """The sector B2 sends after the interim firmware, which the S61 import
    module looks for in the download slot on its first start, and acts on
    only for the app running."""

    def test_it_follows_the_image_at_the_next_4_kb_boundary(self):
        for size, at in ((0x1000, 0x1000), (0x1001, 0x2000), (508736, 0x7D000)):
            with self.subTest(size=size):
                image = an_image(size)
                sent = interim_marker.with_marker(image)
                self.assertEqual(sent[:size], image)
                self.assertEqual(sent[size:at], b"\xff" * (at - size))   # as erased flash
                self.assertEqual(len(sent), at + interim_marker.SECTOR)
                self.assertTrue(interim_marker.valid_at(sent, at))
                self.assertEqual(interim_marker.find(sent), at)

    def test_the_sector_is_its_fields_their_crc_then_ff(self):
        image = an_image(508736)
        header = rbl.header(image)
        sector = interim_marker.sector(image)
        self.assertEqual(len(sector), interim_marker.SECTOR)
        self.assertEqual(sector[:8], b"AFINTERM")
        self.assertEqual(struct.unpack_from("<IIII", sector, 8), (2, 508736, len(APP), rbl.fnv1a32(APP)))
        self.assertEqual((header.size_raw, header.raw_hash), (len(APP), rbl.fnv1a32(APP)))
        self.assertEqual(struct.unpack_from("<I", sector, 24)[0], zlib.crc32(sector[:24]))
        self.assertEqual(sector[28:], b"\xff" * (interim_marker.SECTOR - 28))
        self.assertEqual(interim_marker.read(sector.rjust(0x7D000 + interim_marker.SECTOR, b"\xff"), 0x7D000),
                         interim_marker.Marker(508736, len(APP), rbl.fnv1a32(APP)))

    def test_only_an_rt_ota_image_has_one(self):
        # The app it names comes from the image's own header.
        with self.assertRaises(rbl.RblError):
            interim_marker.with_marker(b"RBL\0" + bytes(5000))

    def test_it_is_for_the_app_its_image_installs_and_no_other(self):
        image = an_image(5000)
        marker = interim_marker.read(interim_marker.with_marker(image), 0x2000)
        self.assertTrue(interim_marker.for_app(marker, APP))
        # The app partition holds more than the app: erased flash, or an older app's tail.
        self.assertTrue(interim_marker.for_app(marker, APP + b"\xff" * 0x1000))
        other = APP[:-1] + b"\x00"
        self.assertFalse(interim_marker.for_app(marker, other))
        self.assertFalse(interim_marker.for_app(marker, APP[:-1]))
        # A marker sent with another image, valid in itself, is not this app's.
        stale = interim_marker.read(interim_marker.with_marker(an_image(5000, other)), 0x2000)
        self.assertIsNotNone(stale)
        self.assertFalse(interim_marker.for_app(stale, APP))
        self.assertTrue(interim_marker.for_app(stale, other))

    def test_a_marker_changed_anywhere_is_refused(self):
        sent = interim_marker.with_marker(an_image(5004))
        at = interim_marker.offset_after(5004)
        for index in range(interim_marker.SIZE):
            with self.subTest(byte=index):
                broken = bytearray(sent)
                broken[at + index] ^= 0x01
                self.assertFalse(interim_marker.valid_at(bytes(broken), at))
                self.assertIsNone(interim_marker.find(bytes(broken)))

    def test_one_of_the_first_version_is_not_one(self):
        fields = struct.pack("<8sII", b"AFINTERM", 1, 0x1000)
        slot = b"\xff" * 0x1000 + (fields + struct.pack("<I", zlib.crc32(fields))).ljust(0x1000, b"\xff")
        self.assertIsNone(interim_marker.find(slot))

    def test_one_elsewhere_than_after_the_image_it_names_is_refused(self):
        # A copy of a marker in other data - a file in OpenBeken's own
        # filesystem at the slot's top, say - is not after an image of its size.
        slot = bytearray(b"\xff" * 0x10000)
        slot[0x3000:0x4000] = interim_marker.sector(an_image(0x1000))
        self.assertFalse(interim_marker.valid_at(bytes(slot), 0x3000))
        self.assertIsNone(interim_marker.find(bytes(slot)))
        self.assertFalse(interim_marker.valid_at(bytes(slot), 0x3001))   # off a sector's start

    def test_an_erased_slot_or_one_without_a_marker_has_none(self):
        self.assertIsNone(interim_marker.find(b"\xff" * interim_marker.SLOT_SIZE))
        self.assertIsNone(interim_marker.find(b"RBL\0" + bytes(0x20000)))

    def test_the_slots_first_sector_is_never_a_marker(self):
        # The image's header is there - erased by the bootloader once applied.
        self.assertIsNone(interim_marker.find(interim_marker.sector(an_image(0x1000))))

    def test_it_must_fit_the_download_slot(self):
        largest = interim_marker.SLOT_SIZE - interim_marker.SECTOR
        self.assertEqual(len(interim_marker.with_marker(an_image(largest))), interim_marker.SLOT_SIZE)
        with self.assertRaisesRegex(ValueError, "no room for the marker"):
            interim_marker.with_marker(an_image(largest + 1))
        self.assertEqual(interim_marker.SLOT_SIZE, uf2.DOWNLOAD_SIZE)

    def test_the_openbeken_driver_reads_the_same_format(self):
        # tools/beken/openbeken/drv_autoflash.c, which a build of OpenBeken
        # carries: its magic, version and size are these, its fields where
        # these are, and it hashes the app as the RBL header does.
        driver = (ROOT / "tools/beken/openbeken/drv_autoflash.c").read_text(encoding="utf-8")
        defined = dict(re.findall(r"^#define (AUTOFLASH_\w+)\s+(\S+)", driver, re.MULTILINE))
        self.assertEqual(defined["AUTOFLASH_MARKER_MAGIC"], f'"{interim_marker.MAGIC.decode()}"')
        self.assertEqual(int(defined["AUTOFLASH_MARKER_VERSION"]), interim_marker.VERSION)
        self.assertEqual(int(defined["AUTOFLASH_MARKER_SIZE"]), interim_marker.SIZE)
        self.assertEqual(int(defined["AUTOFLASH_SECTOR"], 16), interim_marker.SECTOR)
        self.assertEqual(int(defined["AUTOFLASH_STRIPE"]), 34)   # as stock_app.unstripe takes the app partition
        marker_at = driver[driver.index("static int autoflash_marker_at("):
                           driver.index("static int autoflash_marker_for_this_app(")]
        offsets = {name: int(at) for name, at in re.findall(r"\*(\w+) = autoflash_u32\(head \+ (\d+)\)", marker_at)}
        self.assertEqual(offsets, {"appSize": 16, "appHash": 20})
        self.assertIn("autoflash_u32(head + 12)", marker_at)   # the image size
        self.assertIn(f"autoflash_u32(head + {interim_marker.FIELDS.size}) != "
                      f"ef40_crc32(head, {interim_marker.FIELDS.size})", marker_at)
        self.assertIn("0x811c9dc5", driver)
        self.assertIn("* 0x01000193", driver)
        self.assertEqual(rbl.fnv1a32(b"a"), 0xE40C292C)   # FNV-1a's own vector


if __name__ == "__main__":
    unittest.main()
