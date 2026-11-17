"""firmware.py, the firmware the stub offers as eWeLink's latest."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

STUB = Path(__file__).resolve().parents[3] / "tools" / "ewelink" / "stub_cloud"


class RunIsolatedTests(unittest.TestCase):
    """Run as emu.sh runs it: in isolated mode (-I), where only its own path
    puts the repository on sys.path."""

    def test_it_imports_the_repository(self):
        done = subprocess.run([sys.executable, "-I", str(STUB / "firmware.py"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


class ReadTests(unittest.TestCase):
    def test_the_file_emu_sh_made_is_read_with_its_version(self):
        from tools.autoflash import kinds
        from tools.espressif import build_vendor_ota
        from tools.ewelink.stub_cloud import firmware
        from tests.espressif.test_dump_stock_flash import esp_image
        wrapped = build_vendor_ota.build_wrapper(esp_image("1.2.0", "esp32_fw"), version="1.2.0",
                                                 model=kinds.SONOFF_S60.wrapper_model)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stub-firmware.bin"
            self.assertIsNone(firmware.read(path))
            path.write_bytes(wrapped)
            self.assertEqual(firmware.read(path), firmware.Update("1.2.0", wrapped))


class KindTests(unittest.TestCase):
    def test_by_its_firmwares_own_model(self):
        from tools.autoflash import kinds
        from tools.ewelink.stub_cloud import firmware
        self.assertIs(firmware.kind_of("SN-ESP32C3-S60-01", ""), kinds.SONOFF_S60)

    def test_by_its_product_model_where_its_firmware_does_not_say(self):
        # The DualR3's firmware calls itself E32-2SW-P0, as other devices' do:
        # with no product model no file was made, and a run back to Sonoff's
        # firmware without a backup found none offered.
        from tools.autoflash import kinds
        from tools.ewelink.stub_cloud import firmware
        self.assertIs(firmware.kind_of("E32-2SW-P0", "DUALR3"), kinds.SONOFF_DUALR3)
        self.assertIsNone(firmware.kind_of("E32-2SW-P0", ""))


if __name__ == "__main__":
    unittest.main()
