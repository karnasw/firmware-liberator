"""device_identity.py, the device's identity the stub reads from its dump."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import kinds
from tools.ewelink.stub_cloud import device_identity

STUB = Path(__file__).resolve().parents[3] / "tools" / "ewelink" / "stub_cloud"


class RunIsolatedTests(unittest.TestCase):
    """Run as the emulator runs it: in isolated mode (-I), where only its own
    path puts the repository on sys.path."""

    def test_it_imports_the_repository(self):
        done = subprocess.run([sys.executable, "-I", str(STUB / "device_identity.py"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


class ProductModelTests(unittest.TestCase):
    FRAME = {"deviceid": "1000000001", "apikey": "00000000-1111-2222-3333-444444444444",
             "model": "SN-ESP32C3-BASICR4-01"}

    def test_a_kind_not_ready_yet_is_served_too(self):
        # The emulator runs one to work on it, and reads it out as a person would.
        self.assertFalse(kinds.SONOFF_MINIR4.ready)
        with mock.patch.object(device_identity, "frame_params", return_value=dict(self.FRAME)):
            device = device_identity.identity(b"", "apikey", product_model="MINIR4")
        self.assertEqual((device["productModel"], device["name"]), ("MINIR4", "MINIR4 (stub)"))

    def test_another_kinds_product_model_is_refused(self):
        with mock.patch.object(device_identity, "frame_params", return_value=dict(self.FRAME)):
            with self.assertRaises(SystemExit):
                device_identity.identity(b"", "apikey", product_model="NOT-A-DEVICE")


class AisIdentityTests(unittest.TestCase):
    def test_a_device_with_no_frame_params_is_known_by_its_factory_record(self):
        # A real BASICR4's or MINIR4's flash: its SDK keeps no frame_params.
        from tests.espressif.test_inspect_stock_nvs import entry, page, variable
        from tools.espressif import ais_records
        record = ais_records.factory_record({
            "chip_mac": "123456ABCDEF", "device_model": "SN-ESP32C3-BASICR4-01", "deviceid": "1000000001",
            "factory_apikey": "00000000-1111-2222-3333-444444444444", "sta_mac": "12:34:56:AB:CD:EF",
            "sap_mac": "12:34:56:AB:CD:F0"}, 138)
        nvs = page([entry(0, 0x01, "factory", data=b"\x01"), *variable(1, 0x41, "info", record)])
        with mock.patch.object(device_identity.flashmap, "nvs", return_value=mock.Mock(offset=0, size=len(nvs))):
            device = device_identity.identity(nvs, "apikey", product_model="BASICR4")
        self.assertEqual((device["deviceid"], device["model"], device["uiid"], device["devicekey"]),
                         ("1000000001", "SN-ESP32C3-BASICR4-01", 138, "00000000-1111-2222-3333-444444444444"))


if __name__ == "__main__":
    unittest.main()
