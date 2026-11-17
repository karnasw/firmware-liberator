"""The AES the rt_ota packer uses (tools/beken/aes.py): FIPS-197's vectors, then
CBC around them."""
import unittest

from tools.beken import aes


class AesTests(unittest.TestCase):
    """FIPS-197's own vectors (appendix C), then CBC around them."""

    def test_the_fips_197_vectors(self):
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        for key_length, expected in ((16, "69c4e0d86a7b0430d8cdb78070b4c55a"),
                                     (24, "dda97ca4864cdfe06eaf70a0ec0d7191"),
                                     (32, "8ea2b7ca516745bfeafc49904b496089")):
            with self.subTest(bits=key_length * 8):
                keys = aes.expand_key(bytes(range(key_length)))
                cipher = aes.encrypt_block(keys, plain)
                self.assertEqual(cipher.hex(), expected)
                self.assertEqual(aes.decrypt_block(keys, cipher), plain)

    def test_cbc_round_trip_pads_as_pkcs7(self):
        key, iv = b"k" * 32, b"i" * 16
        for length in (0, 1, 15, 16, 17, 100):
            data = bytes(range(256))[:length] * (1 if length else 0)
            cipher = aes.cbc_encrypt(key, iv, data)
            self.assertEqual(len(cipher), (length // 16 + 1) * 16)  # always some padding
            self.assertEqual(aes.cbc_decrypt(key, iv, cipher), data)
        # The padding bytes say how many they are: a whole block for an aligned length.
        self.assertEqual(aes.pad(b"x" * 16)[-16:], bytes([16]) * 16)
        with self.assertRaises(ValueError):
            aes.unpad(b"x" * 15 + b"\x00")

    def test_a_wrong_key_or_iv_is_refused(self):
        with self.assertRaises(ValueError):
            aes.expand_key(b"short")
        with self.assertRaises(ValueError):
            aes.cbc_encrypt(b"k" * 32, b"iv", b"data")
        with self.assertRaises(ValueError):
            aes.cbc_decrypt(b"k" * 32, b"i" * 16, b"not a block")


if __name__ == "__main__":
    unittest.main()
