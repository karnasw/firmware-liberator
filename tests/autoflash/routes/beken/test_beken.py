"""The Beken routes: every direction one move, the device's own store never
written, the backup after the first conversion, through OpenBeken."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import detect, report, routes, state
from tools.autoflash.routes import beken
from tools.autoflash.routes.beken import images, onward


MAC = "12:34:56:ab:cd:ef"


class RouteTests(unittest.TestCase):
    def test_every_direction_is_one_move_and_a_check(self):
        for firmware, source, expected in (
            ("openbeken", "sonoff", ["A0", "A1", "A3", "B1", "B2", "K1", "B4"]),
            # Through the interim firmware, the backup taken on it.
            ("esphome", "sonoff", ["A0", "A1", "A3", "B1", "B2", "J1", "B4", "J2", "K2"]),
            ("esphome", "openbeken", ["A0", "A3", "B4", "O1", "K2"]),
            ("sonoff", "openbeken", ["A0", "A3", "B4", "O2", "K3"]),
            ("openbeken", "esphome", ["A0", "A3", "E1", "K1", "B4"]),
            ("sonoff", "esphome", ["A0", "A3", "E2", "K3"]),
            # Left on the interim firmware by a run whose record is gone: on
            # as that run would have gone, kept as OpenBeken, or as from OpenBeken.
            ("esphome", "interim", ["A0", "A3", "B4", "J2", "K2"]),
            ("openbeken", "interim", ["A0", "A3", "J3", "K1", "B4"]),
            ("sonoff", "interim", ["A0", "A3", "B4", "J4", "K3"]),
            # This tool's ESPHome or OpenBeken, an earlier release, updated
            # (left for another firmware, it is a run from the one it runs).
            ("esphome", "outdated", ["A0", "A3", "U1", "U2"]),
            ("openbeken", "outdated", ["A0", "A3", "U3", "U4"]),
        ):
            with self.subTest(firmware=firmware, source=source):
                self.assertEqual([s for s, _, _ in routes.steps_for(firmware, source, beken)], expected)
        self.assertEqual(beken.steps_for("esphome", "esphome"), [])
        with self.assertRaises(ValueError):
            beken.steps_for("esphome", "midway")
        with self.assertRaises(ValueError):
            beken.steps_for("sonoff", "outdated")
        self.assertIn("interim", beken.SOURCES)

    def test_a_run_to_esphome_shows_these_stages(self):
        names = [routes._stage_name(step, title, "esphome", beken)
                 for step, title, _ in routes.steps_for("esphome", "sonoff", beken)][-5:]
        self.assertEqual(names, ["Sending interim firmware", "Checking interim firmware", "Backing up the device",
                                 "Sending ESPHome", "Checking ESPHome"])

    def test_the_interim_firmware_is_never_called_openbeken(self):
        # Shown as OpenBeken, the person would think they chose it by mistake.
        steps = beken.INTERIM_STEPS + beken.FROM_INTERIM["esphome"] + beken.FROM_INTERIM["sonoff"]
        for name, title, _ in steps:
            with self.subTest(step=name):
                for text in (title, beken.STAGE_NAMES[name], beken.PLAIN_DESCRIPTION[name]):
                    self.assertNotIn("openbeken", text.lower())
        for text in (images.INTERIM_NAME, detect.NAMES["interim"], report.CONVERTED_FROM["interim"],
                     onward.INTERIM_FIRMWARE):
            self.assertNotIn("openbeken", text.lower())
        # A run to OpenBeken from it names OpenBeken as what the device is to
        # have, and the interim firmware as what it had.
        (name, title, _), = beken.FROM_INTERIM["openbeken"]
        self.assertEqual(title, "OpenBeken, in place of the interim firmware")

    def test_the_point_of_no_return_is_the_push(self):
        self.assertEqual(beken.POINT_OF_NO_RETURN, "B2")
        self.assertIn("B2", beken.ATTEMPTED_EVIDENCE)
        for step in beken.DESTRUCTIVE_STEPS:
            self.assertIn(step, beken.ATTEMPTED_EVIDENCE, step)
        self.assertFalse(beken.DESTRUCTIVE_STEPS & beken.RETRY_STEPS)
        self.assertIn("J3", beken.RETRY_STEPS)   # the name set again, and a restart

    def test_an_s61_run_takes_these_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = state.RunState.open(Path(tmp) / "run")
            run.set_input("device_kind", "sonoff-s61")
            self.assertIs(routes.family_of(run), beken)
            run.set_input("firmware", "openbeken")
            self.assertEqual([s for s, _, _ in routes.steps_of(run)][-2:], ["K1", "B4"])


def a_context(tmp, **inputs):
    run = state.RunState.open(Path(tmp) / "run")
    for key, value in {"device_kind": "sonoff-s61", "device_ip": "127.0.0.1", "device_mac": MAC, **inputs}.items():
        run.set_input(key, value)
    return SimpleNamespace(state=run, dry_run=False, finder=SimpleNamespace(poll=lambda: None),
                           esphome_password=None, esphome_key=None, esphome_firmware=None,
                           args=SimpleNamespace(no_backup=False), consented=True,
                           listen_ip=lambda: "192.0.2.2", email=lambda: "owner@example.com",
                           ewelink_env=lambda: {"EWELINK_PASSWORD": "pw"}, ewelink=mock.sentinel.account, account_device=None,
                           account_devices=lambda: None,
                           # OpenBeken answering Tasmota's Status 11, its Wi-Fi signal in it.
                           device=SimpleNamespace(command=lambda cmd: {"StatusSTS": {"Wifi": {"Signal": -60}}}))


if __name__ == "__main__":
    unittest.main()
