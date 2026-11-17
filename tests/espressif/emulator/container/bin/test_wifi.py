import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WIFI = Path(__file__).resolve().parents[5] / "tools/espressif/emulator/container/bin/wifi"


class WifiUsageTests(unittest.TestCase):
    def test_usage_is_the_header_and_no_code(self):
        result = subprocess.run(["bash", str(WIFI), "no-such-command"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stdout.startswith("This computer's Wi-Fi, emulated."))
        self.assertTrue(result.stdout.endswith("wifi home            back to the home network\n"))



class WifiJoinTests(unittest.TestCase):
    """`wifi join` without a name joins the device's own network on the air,
    as the Beken emulator's does: an attended run's "wifi": "join" answer
    cannot know its name."""

    def run_wifi(self, *args, softap=""):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp, "run")
            run.mkdir()
            (run / "softap").write_text(softap + "\n" if softap else "", encoding="utf-8")
            (run / "gateway").write_text("192.168.4.1\n", encoding="utf-8")
            calls = Path(tmp, "calls")
            for name, text in {"nmcli": f"#!/bin/sh\necho PICSimLabWifi\n{'echo ' + softap if softap else ''}\n",
                               "sudo": f'#!/bin/sh\necho "$*" >> {calls}\n'}.items():
                Path(tmp, name).write_text(text, encoding="utf-8")
                Path(tmp, name).chmod(0o755)
            done = subprocess.run(["bash", str(WIFI), *args], capture_output=True, text=True, timeout=30,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "AUTOFLASH_EMU_RUN": str(run)})
            return done, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_without_a_name_the_devices_network_on_the_air_is_joined(self):
        done, calls = self.run_wifi("join", softap="esphome-abcdef")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls, "/opt/autoflash-emu/bin/wifi-root join esphome-abcdef\n")
        self.assertIn("Joined esphome-abcdef; the device is at 192.168.4.1.", done.stdout)

    def test_without_a_name_and_nothing_on_the_air_nothing_is_joined(self):
        done, calls = self.run_wifi("join")
        self.assertEqual(done.returncode, 1)
        self.assertIn("No network of the device's own is on the air", done.stderr)
        self.assertEqual(calls, "")


if __name__ == "__main__":
    unittest.main()
