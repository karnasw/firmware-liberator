"""A readout's flash leaves the computer with no Wi-Fi password in it (the
current one, one replaced before, Sonoff's copy, its PMK, wherever they are)
and no eWeLink identity, and still with every settings checksum good."""
import hashlib
import struct
import unittest
import zlib

from tests.espressif.test_inspect_stock_nvs import entry, page, variable
from tools.espressif import ais_records, scrub

SSID = b"ExampleNet"
OLD_SSID = b"OldNetwork-2"        # a network the device was on before: its entry replaced since
AP_SSID = b"ITEAD-1000abcdef"     # the device's own setup network, not the owner's
NOW, BEFORE = b"correct-horse-9", b"older-secret-1"
PMK = hashlib.pbkdf2_hmac("sha1", NOW, SSID, 4096, 32)
APIKEY = b"12345678-1234-4abc-8def-123456789abc"
FRAME = (b'{"ssid":"ExampleNet","passwd":"' + NOW + b'","deviceid":"1000abcdef","apikey":"' + APIKEY
         + b'","encrypt_key":"' + APIKEY + b'","model":"SN-ESP32C3-NEW-01"}')


def ssid_blob(name: bytes) -> bytes:
    return struct.pack("<I", len(name)) + name.ljust(32, b"\0")


def flash() -> bytes:
    nvs = page([
        entry(0, 0x01, "nvs.net80211", data=b"\x01"),
        entry(0, 0x01, "frame_params", data=b"\x02"),
        *variable(1, 0x41, "sta.ssid", ssid_blob(OLD_SSID)),   # replaced since: its bytes stay
        *variable(1, 0x41, "sta.ssid", ssid_blob(SSID)),
        *variable(1, 0x41, "ap.ssid", ssid_blob(AP_SSID)),
        *variable(1, 0x21, "sta.pswd", BEFORE + b"\0"),  # replaced since: its bytes stay
        *variable(1, 0x21, "sta.pswd", NOW + b"\0"),
        *variable(1, 0x41, "sta.pmk", PMK),
        *variable(2, 0x21, "content", FRAME + b"\0"),
    ])
    elsewhere = (b"\xff" * 0x100 + b"cfg:" + NOW + b";" + PMK + b";" + APIKEY + b";net=" + SSID
                 + b"\xff" * 0x100)
    image = b"\xff" * scrub.NVS_OFFSET + nvs
    return image.ljust(scrub.NVS_OFFSET + scrub.NVS_SIZE, b"\xff") + elsewhere


class ScrubTests(unittest.TestCase):
    def test_no_password_or_its_key_is_left(self):
        out, count = scrub.wifi(flash())
        for secret in (NOW, BEFORE, PMK):
            self.assertNotIn(secret, out)
        self.assertGreaterEqual(count, 5)

    def test_the_rest_is_kept_byte_for_byte_in_place(self):
        before = flash()
        out, _ = scrub.wifi(before)
        self.assertEqual(len(out), len(before))
        self.assertIn(b'"model":"SN-ESP32C3-NEW-01"', out)
        self.assertIn(b'"passwd":"' + b"*" * len(NOW) + b'"', out)


    def test_no_name_of_the_owners_network_is_left(self):
        out, _ = scrub.wifi(flash())
        for name in (SSID, OLD_SSID):
            self.assertNotIn(name, out, name)
        self.assertIn(b'"ssid":"' + b"*" * len(SSID) + b'"', out)
        self.assertIn(ssid_blob(b"*" * len(SSID)), out)   # the entry keeps its length, blanked

    def test_the_devices_own_setup_network_name_is_kept(self):
        # Not the owner's: identity() takes the device id out of it.
        self.assertIn(AP_SSID, scrub.wifi(flash())[0])

    def test_a_short_name_is_taken_out_of_the_settings_too(self):
        nvs = page([entry(0, 0x01, "nvs.net80211", data=b"\x01"),
                    *variable(1, 0x41, "sta.ssid", ssid_blob(b"Hm"))])
        image = (b"\xff" * scrub.NVS_OFFSET + nvs).ljust(0x40000, b"\xff")
        out, _ = scrub.wifi(image)
        self.assertIn(ssid_blob(b"**"), out)
        self.assertNotIn(ssid_blob(b"Hm"), out)

    def test_a_flash_with_no_settings_is_returned_as_it_was(self):
        blank = b"\xff" * 0x20000
        self.assertEqual(scrub.wifi(blank), (blank, 0))

    def test_a_slot_never_given_a_password_takes_nothing_else_with_it(self):
        # Seen on a real S60: password entries of erased flash, 0xFF, which is
        # in every empty part of the flash too.
        nvs = page([entry(0, 0x01, "nvs.net80211", data=b"\x01"), *variable(1, 0x21, "ap.passwd", b"\xff" * 65)])
        image = (b"\xff" * scrub.NVS_OFFSET + nvs).ljust(0x40000, b"\xff")
        self.assertEqual(scrub.wifi(image), (image, 0))


def bad_checksums(flash: bytes) -> list[bytes]:
    """The keys of written settings entries whose header or data checksum fails."""
    start = scrub.NVS_OFFSET
    bad = []
    for at, key, payload, length in scrub._entries(flash, start, scrub.NVS_SIZE):
        page = at - (at - start) % scrub.PAGE
        index = (at - page - 64) // 32
        if (flash[page + 32 + index // 4] >> ((index % 4) * 2)) & 3 != 2:
            continue  # erased: ESP-IDF reads none of it
        header = zlib.crc32(flash[at:at + 4] + flash[at + 8:at + 32], 0xFFFFFFFF)
        data = zlib.crc32(flash[payload:payload + length], 0xFFFFFFFF)
        if header != struct.unpack_from("<I", flash, at + 4)[0] or data != struct.unpack_from("<I", flash, at + 28)[0]:
            bad.append(key)
    return bad


class IdentityTests(unittest.TestCase):
    def test_the_identity_is_replaced_by_a_made_up_one_of_its_shape(self):
        out, count = scrub.identity(flash())
        self.assertNotIn(b"1000abcdef", out)
        self.assertNotIn(APIKEY, out)
        self.assertIn(b'"deviceid":"1000000000"', out)
        self.assertIn(b'"apikey":"10000000-0000-0000-0000-000000000000"', out)
        self.assertEqual(len(out), len(flash()))
        self.assertGreaterEqual(count, 4)

    def test_a_flash_with_no_sonoff_settings_is_returned_as_it_was(self):
        blank = b"\xff" * 0x20000
        self.assertEqual(scrub.identity(blank), (blank, 0))


class ChecksumTests(unittest.TestCase):
    def test_the_settings_still_check_out_after_both(self):
        self.assertEqual(bad_checksums(flash()), [])
        out = scrub.identity(scrub.wifi(flash())[0])[0]
        self.assertEqual(bad_checksums(out), [])


# The ais SDK's (the BASICR4's): its Wi-Fi and identity in records of its own.
AIS_KEY = "00000000-1111-2222-3333-444444444444"
FACTORY = ais_records.factory_record({
    "chip_mac": "123456ABCDEF", "device_model": "SN-ESP32C3-BASICR4-01", "deviceid": "1000abcdee",
    "factory_apikey": AIS_KEY, "sta_mac": "12:34:56:AB:CD:EF", "sap_mac": "12:34:56:AB:CD:F0"}, 138)


def ais_flash() -> bytes:
    nvs = page([
        entry(0, 0x01, "factory", data=b"\x01"),
        entry(0, 0x01, "pair", data=b"\x02"),
        *variable(1, 0x41, "info", FACTORY),
        *variable(2, 0x41, "info", ais_records.pair_record("OldNetwork-2", BEFORE.decode(), "eu-disp.coolkit.cc", 443)),
        *variable(2, 0x41, "info", ais_records.pair_record(SSID.decode(), NOW.decode(), "eu-disp.coolkit.cc", 443)),
    ])
    elsewhere = b"\xff" * 0x100 + b"seen:" + NOW + b";" + SSID + b";" + AIS_KEY.encode() + b"\xff" * 0x100
    return (b"\xff" * scrub.NVS_OFFSET + nvs).ljust(scrub.NVS_OFFSET + scrub.NVS_SIZE, b"\xff") + elsewhere


class AisTests(unittest.TestCase):
    def test_no_password_or_network_name_of_either_pair_record_is_left(self):
        out, _ = scrub.wifi(ais_flash())
        for secret in (NOW, BEFORE, SSID, OLD_SSID):
            self.assertNotIn(secret, out, secret)
        self.assertIn(b"eu-disp.coolkit.cc", out)  # where its cloud is: eWeLink's, nobody's own

    def test_the_identity_in_the_factory_record_is_made_up_and_sealed_again(self):
        out, _ = scrub.identity(ais_flash())
        self.assertNotIn(b"1000abcdee", out)
        self.assertNotIn(AIS_KEY.encode(), out)
        at = out.index(b"SN-ESP32C3-BASICR4-01") - ais_records.FACTORY_FIELDS["device_model"][0]
        record = out[at:at + ais_records.FACTORY_SIZE]
        self.assertEqual(ais_records.text(record, ais_records.FACTORY_FIELDS, "deviceid"), b"1000000000")
        self.assertEqual(record, ais_records.resealed(record))

    def test_a_version_2_pair_record_loses_its_password_and_name_too(self):
        # The MINIR4's 1.2.0 writes one, its host given more room.
        record = ais_records.pair_with(b"\xaa\x55\x02\x00" + bytes(172), 443, host="eu-disp-v1.coolkit.cc",
                                       ssid=SSID.decode(), password=NOW.decode())
        nvs = page([entry(0, 0x01, "pair", data=b"\x01"), *variable(1, 0x41, "info", record)])
        out, _ = scrub.wifi((b"\xff" * scrub.NVS_OFFSET + nvs).ljust(0x40000, b"\xff"))
        self.assertNotIn(NOW, out)
        self.assertNotIn(SSID, out)

    def test_a_record_of_a_version_not_known_here_is_blanked_whole(self):
        # Version 3 of either record: nothing says where its secrets are.
        record = b"\xaa\x55\x03\x00" + b"\xbb\x01" + b"some-host.example" + NOW + SSID + AIS_KEY.encode()
        nvs = page([entry(0, 0x01, "pair", data=b"\x01"), entry(0, 0x01, "factory", data=b"\x02"),
                    *variable(1, 0x41, "info", record), *variable(2, 0x41, "info", record)])
        image = (b"\xff" * scrub.NVS_OFFSET + nvs).ljust(0x40000, b"\xff")
        out = scrub.identity(scrub.wifi(image)[0])[0]
        for secret in (NOW, SSID, AIS_KEY.encode()):
            self.assertNotIn(secret, out)
        self.assertEqual(out.count(b"\xaa\x55\x03\x00"), 2)  # their headers stay, to be seen
        self.assertEqual(bad_checksums(out), [])

    def test_its_settings_still_check_out(self):
        out = scrub.identity(scrub.wifi(ais_flash())[0])[0]
        self.assertEqual(bad_checksums(out), [])


class FirmwareTests(unittest.TestCase):
    def test_a_password_the_firmware_holds_is_left_in_the_firmware(self):
        # A real S60's setup-network password is in its app: it is Sonoff's, the
        # same on every device, and the app must stay byte for byte to start.
        table = (scrub.flashmap.ENTRY_MAGIC + bytes([0, 0x10]) + struct.pack("<II", 0x20000, 0x10000)
                 + b"ota_0".ljust(16, b"\0") + bytes(4))
        image = bytearray(flash().ljust(0x30000, b"\xff"))
        image[scrub.flashmap.TABLE_AT:scrub.flashmap.TABLE_AT + len(table)] = table
        image[0x20100:0x20100 + len(NOW)] = NOW
        image[0x20200:0x20200 + len(APIKEY)] = APIKEY
        out = scrub.identity(scrub.wifi(bytes(image))[0])[0]
        self.assertEqual(out[0x20000:0x30000], bytes(image[0x20000:0x30000]))
        self.assertNotIn(NOW, out[:0x20000])
        self.assertNotIn(APIKEY, out[:0x20000])


if __name__ == "__main__":
    unittest.main()
