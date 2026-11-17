"""The UF2 a LibreTiny ESPHome takes as an update (tools/beken/uf2.py)."""
import struct
import unittest

from tools.beken import uf2


class Uf2Tests(unittest.TestCase):
    def test_the_image_goes_into_the_download_partition_in_order(self):
        image = bytes(range(256)) * 3 + b"end"
        data = uf2.write(image, name="openbeken-s61", version="1.0.0", build_date=1700000000)
        self.assertEqual(len(data) % uf2.BLOCK, 0)
        blocks = [data[i:i + uf2.BLOCK] for i in range(0, len(data), uf2.BLOCK)]
        self.assertEqual(len(blocks), 1 + 4)  # the tags, then 256-byte pieces
        for number, block in enumerate(blocks):
            start0, start1, flags, address, size, index, count, family = struct.unpack_from("<8I", block, 0)
            self.assertEqual((start0, start1, index, count, family),
                             (uf2.MAGIC_START0, uf2.MAGIC_START1, number, 5, uf2.FAMILY_BK7238))
            self.assertEqual(struct.unpack_from("<I", block, 508)[0], uf2.MAGIC_END)
            if number:
                self.assertEqual(address, (number - 1) * 256)
                self.assertTrue(flags & uf2.FLAG_FAMILY_ID)
        self.assertEqual(struct.unpack_from("<I", blocks[-1], 16)[0], 3)  # the last piece's size
        self.assertEqual(uf2.image_in(data), ("download", image))
        # The first block says what the file is, for the device to check.
        self.assertIn(b"generic-bk7238", blocks[0])
        self.assertIn(b"openbeken-s61", blocks[0])
        self.assertIn(b"1.0.0", blocks[0])
        self.assertIn(uf2.NOTE, blocks[0])
        self.assertIn(b"01PEdownload", blocks[0])

    def test_the_partition_entry_counts_in_256s(self):
        entry = uf2.partition_entry()
        self.assertEqual(len(entry), 48)
        self.assertEqual(struct.unpack_from("<II", entry, 37), (0x286000 // 256, 0x15A000 // 256))

    def test_an_image_too_big_for_the_slot_is_refused(self):
        with self.assertRaises(ValueError):
            uf2.write(b"x" * (uf2.DOWNLOAD_SIZE + 1), name="n", version="v")
        with self.assertRaises(ValueError):
            uf2.write(b"", name="n", version="v")

    def test_something_else_is_not_read_as_a_uf2(self):
        with self.assertRaises(ValueError):
            uf2.image_in(b"\0" * 1024)
        with self.assertRaises(ValueError):
            uf2.image_in(b"short")


if __name__ == "__main__":
    unittest.main()
