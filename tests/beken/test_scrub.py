"""A Beken readout's flash leaves the computer with no Wi-Fi network name or
password and no eWeLink identity in it - nodes replaced since included, and
OpenBeken's own copy - with its firmware untouched and every node's CRC good."""
import unittest

from tools.beken import ef40
from tools.beken import scrub

PASSWORD = b"correct-horse-9"
SSID = b"ExampleNet"
# The factory record as an S61 holds it: base64 of ciphertext (made up here).
FACTORY = b"cXVpdGUtbWFkZS11cC1mYWN0b3J5LXJlY29yZC1ub3QtYS1kZXZpY2Vz"
APIKEY = b"12345678-1234-4abc-8def-123456789abc"


def flash() -> bytes:
    image = bytearray(b"\xff" * 0x400000)
    store = b"".join([
        ef40.encode_node("wifi-ssid", b"OldNetwork-2"),   # replaced since: still there
        ef40.encode_node("wifi-ssid", SSID),
        ef40.encode_node("ssid", b"\x21\x42encodedname\x00"),
        ef40.encode_node("lan&cfg", b'{"ssid":"' + SSID + b'","mode":1}'),
        ef40.encode_node("wifi-pass", b"older-pass-1"),   # replaced since: still there
        ef40.encode_node("wifi-pass", PASSWORD),
        ef40.encode_node("pass", b"\x13\x37encoded\x00"),
        ef40.encode_node("apiKey", APIKEY),
        ef40.encode_node("user", b'{"account":"someone"}'),
        ef40.encode_node("factory", FACTORY),
        ef40.encode_node("lan&info", b'{"deviceid":"1000abcdef","port":8081}'),
        ef40.encode_node("f/1/k", b"\x01\x02\x03\x04matter-key"),
        ef40.encode_node("elecCalibFlag", b"\x01"),
    ])
    image[0x3E2000:0x3E2000 + len(store)] = store
    image[0x3E1000:0x3E1000 + 30] = b"obk:" + PASSWORD + b"\0" * 11    # OpenBeken's imported copy
    image[0x3E1040:0x3E1040 + len(SSID)] = SSID                         # and the network it joined
    image[0x20000:0x20000 + len(PASSWORD)] = PASSWORD                   # in the firmware: code, left alone
    return bytes(image)


def nodes(image: bytes) -> dict[str, bytes]:
    return ef40.parse_store(image, (scrub.STORES[0], scrub.STORES[1]))


class ScrubTests(unittest.TestCase):
    def test_no_password_or_identity_is_left_in_the_stores(self):
        out, changed = scrub.scrub(flash())
        stores = out[scrub.STORES[0]:]
        for secret in (PASSWORD, b"older-pass-1", APIKEY, b"someone", b"1000abcdef", b"matter-key", b"encoded",
                       SSID, b"OldNetwork-2", b"encodedname"):
            self.assertNotIn(secret, stores, secret)
        self.assertGreaterEqual(changed, 7)

    def test_every_node_still_checks_out_and_the_rest_is_kept(self):
        out, _ = scrub.scrub(flash())
        found = nodes(out)  # parse_store keeps only nodes whose CRC holds
        self.assertEqual(found["wifi-ssid"], b"*" * len(SSID))
        self.assertEqual(found["lan&cfg"], b'{"ssid":"' + b"*" * len(SSID) + b'","mode":1}')
        self.assertEqual(found["elecCalibFlag"], b"\x01")
        self.assertEqual(found["apiKey"], b"10000000-0000-0000-0000-000000000000")
        self.assertIn(b'"deviceid":"1000000000"', found["lan&info"])
        self.assertEqual(set(found), set(nodes(flash())))

    def test_the_firmware_is_left_byte_for_byte(self):
        before = flash()
        out, _ = scrub.scrub(before)
        self.assertEqual(out[:scrub.STORES[0]], before[:scrub.STORES[0]])
        self.assertEqual(len(out), len(before))

    def test_a_binary_identity_blob_is_zeroed_whole(self):
        blob = bytes([0x24, 0x8c, 0x00]) + bytes(range(0x30, 0x60))   # a DER-ish apiKey, not printable
        image = bytearray(b"\xff" * 0x400000)
        store = ef40.encode_node("apiKey", blob)
        image[0x3E2000:0x3E2000 + len(store)] = store
        out, _ = scrub.scrub(bytes(image))
        kept = nodes(out)["apiKey"]
        self.assertEqual(kept, bytes(len(blob)))
        self.assertNotIn(blob, out)

    def test_a_short_name_is_starred_where_a_setting_names_it(self):
        image = bytearray(b"\xff" * 0x400000)
        store = ef40.encode_node("wifi-ssid", b"Hm") + ef40.encode_node("lan&cfg", b'{"ssid":"Hm"}')
        image[0x3E2000:0x3E2000 + len(store)] = store
        found = nodes(scrub.scrub(bytes(image))[0])
        self.assertEqual((found["wifi-ssid"], found["lan&cfg"]), (b"**", b'{"ssid":"**"}'))

    def test_the_factory_record_is_kept_byte_for_byte(self):
        # The stock firmware does not start with it changed (seen in the emulator).
        self.assertEqual(nodes(scrub.scrub(flash())[0])["factory"], FACTORY)

    def test_a_flash_with_no_store_is_returned_as_it_was(self):
        blank = b"\xff" * 0x400000
        self.assertEqual(scrub.scrub(blank), (blank, 0))


if __name__ == "__main__":
    unittest.main()
