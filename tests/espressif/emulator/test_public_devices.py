"""public_devices.py, the public devices each final firmware is tried on, and their runs."""

import json
import tempfile
import unittest
from pathlib import Path

from tools.espressif.emulator import public_devices


class PublicDevicesTests(unittest.TestCase):
    def test_a_kept_device_counts_as_stopped_only_after_its_step(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory)
            run = saved / "captures/devices/12-34-56-ab-cd-ef/runs/1"
            run.mkdir(parents=True)
            (saved / "captures/.autoflash-current").write_text("devices/12-34-56-ab-cd-ef/runs/1\n")
            (saved / "flash.bin").write_bytes(b"")
            (run / "autoflash-state.json").write_text(json.dumps({"steps": {"E3": {"status": "attempted"}}}))
            self.assertFalse(public_devices.step_done(saved, "E3"))
            (run / "autoflash-state.json").write_text(json.dumps({"steps": {"E3": {"status": "done"}}}))
            self.assertTrue(public_devices.step_done(saved, "E3"))

    def test_each_final_firmware_is_installed_then_left(self):
        self.assertEqual({way[0][1] for way in public_devices.WAYS}, {"tasmota", "esphome"})
        for (_, first, kept), (source, second, _) in public_devices.WAYS:
            self.assertEqual(source, kept)   # from the device its own first run made
            self.assertNotEqual(second, first)

    def test_each_way_goes_on_without_waiting_for_the_other(self):
        import argparse
        import io
        import threading
        from contextlib import redirect_stdout
        from unittest import mock
        # The way to Tasmota's first run fails, and lasts until the way to
        # ESPHome's second run starts - which it does only if not waiting on it.
        events, lock = [], threading.Lock()
        second_started = threading.Event()

        def fake_run(instance, source, firmware, save, log):
            with lock:
                events.append(("start", source))
            if source == "public-on-esphome":
                second_started.set()
            if source == "public-pre-final-tasmota":
                second_started.wait(10)
            with lock:
                events.append(("end", source))
            ok = source != "public-pre-final-tasmota"
            return ok, "Converted" if ok else "exit 1"

        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory)
            for start in ("public-pre-final-tasmota", "public-pre-final-esphome"):
                (saved / start).mkdir()
                (saved / start / "flash-mode").write_text("public\n")
            out = io.StringIO()
            with mock.patch.object(public_devices, "SAVED", saved), \
                    mock.patch.object(public_devices, "LOGS", saved / "logs"), \
                    mock.patch.object(public_devices, "run", side_effect=fake_run), redirect_stdout(out):
                code = public_devices.command_test(argparse.Namespace(instances=[1, 2]))
        self.assertEqual(code, 1)
        self.assertLess(events.index(("start", "public-on-esphome")),
                        events.index(("end", "public-pre-final-tasmota")))
        self.assertNotIn(("start", "public-on-tasmota"), events)
        said = out.getvalue()
        self.assertIn("FAIL  public-pre-final-tasmota to tasmota: exit 1", said)
        self.assertIn("SKIP  public-on-tasmota to esphome: the run before it failed", said)
        self.assertIn("PASS  public-on-esphome to tasmota: Converted", said)
        self.assertIn("1 failed", said)


if __name__ == "__main__":
    unittest.main()
