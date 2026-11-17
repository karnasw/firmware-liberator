"""The RBL image the stock bootloader applies (tools/beken/rbl.py): packed and
unpacked, its header, its checks."""
import lzma
import struct
import unittest
import zlib

from tools.beken import aes, rbl


def rbl_header(name=b"app", version=b"1.0.2", raw_hash=0, size_raw=0):
    """A 96-byte rt_ota RBL header as stock_app and fota_wrap read it."""
    h = bytearray(96)
    h[0:4] = b"RBL\x00"
    h[12:12 + len(name)] = name
    h[28:28 + len(version)] = version
    struct.pack_into("<II", h, 80, raw_hash, size_raw)
    struct.pack_into("<I", h, 92, zlib.crc32(bytes(h[:92])) & 0xFFFFFFFF)
    return bytes(h)


class RblTests(unittest.TestCase):
    def test_pack_and_unpack_round_trip(self):
        firmware = bytes(range(256)) * 40 + b"tail"
        packed = rbl.pack(firmware, version="1.2.3", timestamp=1234, sn=b"serial")
        header = rbl.header(packed)
        self.assertEqual((header.name, header.version, header.timestamp, header.algo),
                         ("app", "1.2.3", 1234, rbl.ALGO_AES_GZIP))
        self.assertEqual(header.sn, b"serial".ljust(24, b"\0"))
        self.assertEqual((header.size_raw, header.raw_hash), (len(firmware), rbl.fnv1a32(firmware)))
        self.assertEqual(header.size_package, len(packed) - rbl.HEADER)
        self.assertEqual(zlib.crc32(packed[rbl.HEADER:]) & 0xFFFFFFFF, header.body_crc32)
        self.assertEqual(rbl.unpack(packed), firmware)
        # The body is gzip under AES-CBC with the public key, as the SDK's packer makes it.
        plain = aes.cbc_decrypt(rbl.KEY, rbl.IV, packed[rbl.HEADER:])
        self.assertEqual(plain[:10], b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\x03")

    def test_fnv1a32_is_the_standard_one(self):
        self.assertEqual(rbl.fnv1a32(b""), 0x811C9DC5)
        self.assertEqual(rbl.fnv1a32(b"a"), 0xE40C292C)

    def test_a_damaged_rbl_is_refused(self):
        packed = bytearray(rbl.pack(b"firmware", version="1"))
        for where, message in ((0, "magic"), (20, "header's CRC"), (rbl.HEADER + 3, "body's CRC")):
            damaged = bytearray(packed)
            damaged[where] ^= 0xFF
            with self.assertRaisesRegex(rbl.RblError, message):
                rbl.unpack(bytes(damaged))
        with self.assertRaisesRegex(rbl.RblError, "shorter"):
            rbl.unpack(bytes(packed[:-1]))
        other = rbl.Header(algo=0x302, timestamp=0, name="app", version="1", sn=b"", body_crc32=0,
                           raw_hash=0, size_raw=0, size_package=0).pack()
        with self.assertRaisesRegex(rbl.RblError, "0x302"):
            rbl.unpack(other)

    def lzma_rbl(self, firmware, body=None):
        """An RBL as Sonoff's official images are: an LZMA-alone stream under the same AES."""
        plain = lzma.compress(firmware, format=lzma.FORMAT_ALONE) if body is None else body
        cipher = aes.cbc_encrypt(rbl.KEY, rbl.IV, plain)
        return rbl.Header(algo=rbl.ALGO_AES_LZMA, timestamp=1, name="app", version="1.1.0", sn=b"",
                          body_crc32=zlib.crc32(cipher) & 0xFFFFFFFF, raw_hash=rbl.fnv1a32(firmware),
                          size_raw=len(firmware), size_package=len(cipher)).pack() + cipher

    def test_an_official_images_lzma_is_unpacked_and_checked(self):
        firmware = b"official firmware" * 500
        self.assertEqual(rbl.unpack(self.lzma_rbl(firmware)), firmware)
        with self.assertRaisesRegex(rbl.RblError, "does not match"):
            rbl.unpack(self.lzma_rbl(firmware, lzma.compress(b"another", format=lzma.FORMAT_ALONE)))
        with self.assertRaisesRegex(rbl.RblError, "LZMA"):
            rbl.unpack(self.lzma_rbl(firmware, b"not an LZMA stream"))


if __name__ == "__main__":
    unittest.main()
