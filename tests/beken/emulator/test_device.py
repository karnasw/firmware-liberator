"""device.py, the emulated device."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class DeviceTests(unittest.TestCase):
    """The emulated device's own decisions (tools/beken/emulator/device.py)."""

    def device(self, tmp):
        fakes = {"cloud_online": SimpleNamespace(SSID=b"net", PW=b"password"), "src": SimpleNamespace(),
                 "src.main": SimpleNamespace(build_emulator=None), "unicorn": SimpleNamespace(UC_HOOK_CODE=4),
                 "unicorn.arm_const": SimpleNamespace(UC_ARM_REG_LR=14, UC_ARM_REG_R0=0, UC_ARM_REG_R1=1,
                                                      UC_ARM_REG_R2=2, UC_ARM_REG_R3=3)}
        with mock.patch.dict(sys.modules, fakes):
            sys.modules.pop("device", None)
            import device
        self.addCleanup(sys.modules.pop, "device", None)
        self.stock_points = device.stock_points
        return device.Device(Path(tmp), "tap0")

    def test_sonoffs_app_is_the_one_the_points_are_found_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            with mock.patch.object(self.stock_points, "app_of_flash", return_value=b"sonoff"), \
                    mock.patch.object(self.stock_points, "find", return_value="its points") as find:
                self.assertEqual(device.stock_points(b"flash"), "its points")
            find.assert_called_once_with(b"sonoff")

    def test_any_other_app_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            with mock.patch.object(self.stock_points, "app_of_flash", return_value=bytes(0x1000)):
                self.assertIsNone(device.stock_points(b"flash"))
            self.assertIn("no help for this app", (Path(tmp) / "net.log").read_text(encoding="utf-8"))

    def test_the_router_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            self.assertEqual(device.router(), (True, "password"))   # no switch yet: on, as provisioned
            for written, read in (("off\n", (False, "password")), ("on\n", (True, "password")),
                                  ("on new-pass-1\n", (True, "new-pass-1"))):
                (Path(tmp) / "router").write_text(written, encoding="utf-8")
                self.assertEqual(device.router(), read)

    def test_the_access_point_follows_the_router_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            device.air.mkdir()
            switched = []
            device.home = SimpleNamespace(passphrase="password", set_on_air=switched.append)
            device.follow_router()
            self.assertEqual((switched, (device.air / "home").read_text(encoding="utf-8")), ([True], "net\n"))
            # A new password restarts it: every device on it joins again.
            (Path(tmp) / "router").write_text("on new-pass-1\n", encoding="utf-8")
            device.follow_router()
            self.assertEqual((switched[1:], device.home.passphrase), ([False, True], "new-pass-1"))
            (Path(tmp) / "router").write_text("off\n", encoding="utf-8")
            device.follow_router()
            self.assertEqual(switched[-1], False)
            self.assertFalse((device.air / "home").exists())

    def test_the_devices_own_network_is_on_the_air_while_the_station_hears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            device.air.mkdir()
            device.station = SimpleNamespace(bss={"ssid": "esphome-abcdef"}, set_joining=lambda on: None)
            device.follow_air()
            self.assertEqual((device.air / "network").read_text(encoding="utf-8"), "esphome-abcdef\n")
            device.station.bss = None
            device.follow_air()
            self.assertFalse((device.air / "network").exists())

    def test_the_station_is_on_the_devices_network_only_while_the_computer_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            device.air.mkdir()
            resets, joining = [], []
            device.station = SimpleNamespace(bss=None, device_reset=lambda: resets.append(1),
                                             set_joining=joining.append)
            (Path(tmp) / "joins").write_text("", encoding="utf-8")
            device.follow_air()
            device.follow_air()
            self.assertEqual((resets, joining), ([], [False]), "the computer at home: it only hears the network")
            (Path(tmp) / "joins").write_text("join 1.0\n", encoding="utf-8")
            device.follow_air()
            device.follow_air()
            self.assertEqual((resets, joining), ([1], [False, True]), "joined afresh, once a join")
            (Path(tmp) / "joins").write_text("join 2.0\n", encoding="utf-8")
            device.follow_air()
            self.assertEqual(resets, [1, 1])
            (Path(tmp) / "joins").write_text("home 3.0\n", encoding="utf-8")
            device.follow_air()
            self.assertEqual((resets, joining[-1]), ([1, 1], False), "home: it leaves")

    def test_the_power_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = self.device(tmp)
            self.assertTrue(device.powered())                   # no switch yet: on
            (Path(tmp) / "power").write_text("off\n", encoding="utf-8")
            self.assertFalse(device.powered())
            (Path(tmp) / "power").write_text("on\n", encoding="utf-8")
            self.assertTrue(device.powered())


if __name__ == "__main__":
    unittest.main()
