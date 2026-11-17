"""The ais SDK's records, laid out where its firmware (the BASICR4's) reads them."""

from __future__ import annotations

import hashlib
import struct
import unittest

from tools.espressif import ais_records

FIELDS = {"chip_mac": "123456ABCDEF", "device_model": "SN-ESP32C3-BASICR4-01", "deviceid": "1000000001",
          "factory_apikey": "00000000-1111-2222-3333-444444444444", "sta_mac": "12:34:56:AB:CD:EF",
          "sap_mac": "12:34:56:AB:CD:F0"}


V2 = b"\xaa\x55\x02\x00" + bytes(172)


class Factory(unittest.TestCase):
    def test_it_is_laid_out_as_aisFactoryLoadInfo_reads_it(self):
        record = ais_records.factory_record(FIELDS, 138)
        self.assertEqual(len(record), 178)
        self.assertEqual(struct.unpack_from("<HH", record), (0x55AA, 1))
        self.assertEqual(record[52:62], b"1000000001")
        self.assertEqual(record[68:104], FIELDS["factory_apikey"].encode())
        self.assertEqual(struct.unpack_from("<H", record, 144)[0], 138)
        self.assertTrue(ais_records.is_factory(record))

    def test_its_digest_is_the_fields_run_together_then_the_chip(self):
        record = ais_records.factory_record(FIELDS, 138)
        signed = "".join(FIELDS[name] for name in ("factory_apikey", "sta_mac", "sap_mac", "device_model")) \
            + "138" + FIELDS["deviceid"] + FIELDS["chip_mac"]
        self.assertEqual(record[146:], hashlib.sha256(signed.encode()).digest())

    def test_a_changed_field_is_sealed_again(self):
        record = bytearray(ais_records.factory_record(FIELDS, 138))
        record[52:62] = b"1000000000"
        self.assertEqual(ais_records.resealed(bytes(record)),
                         ais_records.factory_record({**FIELDS, "deviceid": "1000000000"}, 138))


class Pair(unittest.TestCase):
    def test_it_is_laid_out_as_aisPairLoadInfo_reads_it(self):
        record = ais_records.pair_record("home", "secret", "eu-disp.coolkit.cc", 443)
        self.assertEqual(len(record), 142)
        self.assertEqual(struct.unpack_from("<HHH", record), (0x55AA, 1, 443))
        self.assertEqual((record[6:24], record[38:42], record[71:77]), (b"eu-disp.coolkit.cc", b"home", b"secret"))
        self.assertTrue(ais_records.is_pair(record) and not ais_records.is_factory(record))

    def test_version_2_is_read_where_the_minir4_puts_it(self):
        # Seen in an emulated MINIR4 1.2.0, which writes version 1's again so.
        record = ais_records.pair_with(V2, 443, host="eu-disp-v1.coolkit.cc", ssid="home", password="secret")
        self.assertEqual((len(record), record[6:27], record[71:75], record[104:110]),
                         (176, b"eu-disp-v1.coolkit.cc", b"home", b"secret"))
        self.assertTrue(ais_records.is_pair(record))

    def test_a_record_is_changed_in_its_own_version(self):
        record = ais_records.pair_with(V2, 443, ssid="home", password="secret", host="h")
        changed = ais_records.pair_with(record, ssid="other")
        self.assertEqual(changed[2:4], b"\x02\x00")
        self.assertEqual(ais_records.text(changed, ais_records.pair_fields(changed), "password"), b"secret")

    def test_another_version_or_size_is_not_one(self):
        self.assertFalse(ais_records.is_pair(V2[:-1]))
        self.assertFalse(ais_records.is_pair(b"\xaa\x55\x03\x00" + bytes(172)))

    def test_a_value_too_long_for_its_place_is_refused(self):
        with self.assertRaises(ValueError):
            ais_records.pair_record("x" * 33, "", "h", 1)


if __name__ == "__main__":
    unittest.main()
