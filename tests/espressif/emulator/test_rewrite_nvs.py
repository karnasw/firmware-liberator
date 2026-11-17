"""A dump's settings pointed at the emulator's network, reset device or not."""

from __future__ import annotations

import json
import struct
import unittest

from tools.espressif import ais_records
from tools.espressif.emulator import rewrite_nvs


class FrameParams(unittest.TestCase):
    def test_a_field_a_paired_device_has_is_changed(self):
        out = rewrite_nvs.set_json_string(b'{"ssid":"old","a":1}\0\0', "ssid", "new")
        self.assertEqual(json.loads(out.split(b"\0")[0]), {"ssid": "new", "a": 1})

    def test_a_field_a_reset_device_has_not_is_added(self):
        out = rewrite_nvs.set_json_string(b'{"a":1}\0\0', "ssid", "new")
        out = rewrite_nvs.set_json_number(out, "dis_port", 443)
        self.assertEqual(json.loads(out.split(b"\0")[0]), {"a": 1, "ssid": "new", "dis_port": 443})

    def test_a_field_twice_is_refused(self):
        with self.assertRaises(SystemExit):
            rewrite_nvs.set_json_string(b'{"ssid":"a","ssid":"b"}', "ssid", "new")


class PairRecord(unittest.TestCase):
    """The ais SDK's (the BASICR4's): pointed at the emulator's network and cloud."""

    def fields(self, record: bytes) -> tuple:
        text = lambda name: ais_records.text(record, ais_records.PAIR_FIELDS, name).decode()  # noqa: E731
        return struct.unpack_from("<H", record, ais_records.PAIR_PORT)[0], text("host"), text("ssid"), text("password")

    def test_it_joins_the_emulators_network_and_keeps_its_cloud(self):
        record = ais_records.pair_record("home", "secret", "eu-disp.coolkit.cc", 443)
        self.assertEqual(self.fields(rewrite_nvs.pair_rewritten(record, "PICSimLabWifi", None, None)),
                         (443, "eu-disp.coolkit.cc", "PICSimLabWifi", ""))

    def test_the_stubs_cloud_is_put_in_when_asked_for(self):
        record = ais_records.pair_record("home", "secret", "eu-disp.coolkit.cc", 443)
        self.assertEqual(self.fields(rewrite_nvs.pair_rewritten(record, "x", "192.168.60.1", 8443))[:2],
                         (8443, "192.168.60.1"))

    def test_a_network_name_too_long_for_its_place_is_refused(self):
        record = ais_records.pair_record("home", "secret", "eu-disp.coolkit.cc", 443)
        with self.assertRaises(SystemExit):
            rewrite_nvs.pair_rewritten(record, "x" * 33, None, None)


if __name__ == "__main__":
    unittest.main()
