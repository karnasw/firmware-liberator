"""OpenBeken's config as tools/beken/obk_config.py builds it: its CRC, and the
Wi-Fi it carries."""
import struct
import unittest

from tools.beken import obk_config


class TinyCrc8Tests(unittest.TestCase):
    """OpenBeken's Tiny_CRC8 as the BK7238 build runs it: a signed `char crc`, so
    the shift sign-extends. A plain CRC-8/MAXIM gives other values."""

    def test_the_values_worked_by_hand_from_the_c(self):
        # One byte 0x01: bit 0 sets 0x8c; from then on bit 7 is set, so each
        # shift brings in a 1 - 0xc6, 0xe3, 0x7d, 0xb2, 0xd9, 0x60, 0x30.
        self.assertEqual(obk_config.tiny_crc8(b"\x01"), 0x30)
        # An unsigned CRC-8/MAXIM would give 0x5e here and 0xa1 for the check string.
        self.assertNotEqual(obk_config.tiny_crc8(b"\x01"), 0x5E)
        self.assertEqual(obk_config.tiny_crc8(b"123456789"), 0x63)

    def test_zeros_leave_it_at_zero(self):
        self.assertEqual(obk_config.tiny_crc8(b""), 0)
        self.assertEqual(obk_config.tiny_crc8(bytes(100)), 0)

    def test_every_byte_counts(self):
        data = bytes(range(256))
        crc = obk_config.tiny_crc8(data)
        changed = bytearray(data)
        changed[200] ^= 0x01
        self.assertNotEqual(obk_config.tiny_crc8(bytes(changed)), crc)
        self.assertNotEqual(obk_config.tiny_crc8(data[:-1]), crc)


class BuildConfigTests(unittest.TestCase):
    def test_the_layout_openbeken_reads(self):
        blob = obk_config.build_config("HomeNet", "secret-pass")
        self.assertEqual(len(blob), 3584)
        self.assertEqual(blob[0:3], b"CFG")
        self.assertEqual(struct.unpack_from("<i", blob, 4)[0], 5)
        self.assertEqual(struct.unpack_from("<H", blob, 0x10)[0], 1)   # changeCounter: written once
        self.assertEqual(blob[0x14:0x14 + 64], b"HomeNet".ljust(64, b"\x00"))
        self.assertEqual(blob[0x54:0x54 + 64], b"secret-pass".ljust(64, b"\x00"))
        self.assertEqual(blob[3], obk_config.tiny_crc8(blob[4:]))
        self.assertEqual(blob[0x94:], bytes(3584 - 0x94))   # nothing else set

    def test_bytes_and_str_credentials_are_the_same_blob(self):
        self.assertEqual(obk_config.build_config("HomeNet", "secret-pass"),
                         obk_config.build_config(b"HomeNet", b"secret-pass"))

    def test_a_credential_needs_its_terminator(self):
        obk_config.build_config("s" * 63, "p" * 63)
        with self.assertRaises(ValueError):
            obk_config.build_config("s" * 64, "p")
        with self.assertRaises(ValueError):
            obk_config.build_config("s", "p" * 64)

    def test_a_template_keeps_every_other_field(self):
        template = bytes(range(256)) * 14   # 3584 bytes of anything
        blob = obk_config.build_config("HomeNet", "pw", template=template)
        self.assertEqual(len(blob), 3584)
        self.assertEqual(blob[0:3], b"CFG")
        self.assertEqual(struct.unpack_from("<i", blob, 4)[0], 5)
        self.assertEqual(blob[8:0x14], template[8:0x14])       # flags and counters as they were
        self.assertEqual(blob[0x94:], template[0x94:])
        self.assertEqual(blob[0x14:0x14 + 7], b"HomeNet")
        self.assertEqual(blob[3], obk_config.tiny_crc8(blob[4:]))

    def test_a_short_template_is_padded_to_the_size(self):
        blob = obk_config.build_config("HomeNet", "pw", template=b"CFG" + bytes(100))
        self.assertEqual(len(blob), 3584)
        self.assertEqual(blob[3], obk_config.tiny_crc8(blob[4:]))


if __name__ == "__main__":
    unittest.main()
