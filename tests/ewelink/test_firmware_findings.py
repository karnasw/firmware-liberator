"""What Sonoff's firmware says of its hardware, found as it is in the firmware
of devices known to have it."""

from __future__ import annotations

import unittest

from tools.ewelink import firmware_findings


def found(app: bytes) -> dict[str, str]:
    return {finding.what: finding.detail for finding in firmware_findings.findings(app)}


class FindingTests(unittest.TestCase):
    def test_a_basicr4_like_app_shows_its_sdk_overheat_lock_and_analog_input(self):
        app = b"\xe9...ais_factory_data.c\0TEMP_EVENT\0TEMPERATURE_CHECK\0esp_adc_cal\0"
        self.assertEqual(set(found(app)), {"Sonoff's ais SDK", "overheat lock", "analog input"})

    def test_an_s60_like_app_shows_its_meter_chip(self):
        result = found(b"\xe9 ../components/esf/esf_stru.c CSE7759 ")
        self.assertIn("CSE7759", result["power meter"])
        self.assertIn("Sonoff's esf SDK", result)

    def test_a_th_elite_like_app_shows_its_sensors(self):
        self.assertIn("DS18B20, AM2301", found(b"DS18B20 AM2301 MS01")["temperature sensor"])

    def test_only_both_names_of_the_overheat_task_make_one(self):
        self.assertNotIn("overheat lock", found(b"TEMP_EVENT"))

    def test_an_app_with_none_of_it_finds_nothing(self):
        self.assertEqual(firmware_findings.findings(b"\xe9" + bytes(64)), [])


if __name__ == "__main__":
    unittest.main()
