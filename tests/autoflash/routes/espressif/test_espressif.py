"""The Espressif routes: each firmware's steps, from each place a device may be
found, and which are tried again."""
import contextlib
import io
import tempfile
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import cli, errors, esphome, esphome_ota, join, prompts, routes, runner
from tools.autoflash.state import DONE, RunState
from tools.autoflash.routes import espressif, update
from tools.autoflash.routes.espressif import evidence, identity, tasmota


# How the choice of firmware names Supla.
SUPLA = "Supla GUI-Generic (from a file you build at gui-generic-builder-new.supla.io)"


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        # No device's own network to be seen, nor joined, an ESPHome that
        # wants nothing for an update and says nothing of what it is:
        # nothing goes on the network.
        for owner, name, value in ((join, "on_a_device_network", False), (prompts, "find_ssid", None),
                                   (esphome_ota, "wants", esphome_ota.NOTHING),
                                   (esphome, "api_device_info", None)):
            patch = mock.patch.object(owner, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        # A Tasmota that says no release of official Tasmota: asked, it would be the device at device_ip.
        patch = mock.patch.dict(update.RELEASE, tasmota=lambda ctx: None)
        patch.start()
        self.addCleanup(patch.stop)

    def ctx(self, given=None, run="run", **inputs):
        state = RunState.open(Path(self.tmp.name) / run)
        for key, value in {"product_model": "S60TPF", "stock_version": "1.2.0",
                           "device_ip": "192.0.2.50", **inputs}.items():
            state.set_input(key, value)
        return SimpleNamespace(state=state, firmware_given=given, esphome_firmware=None, supla_firmware=None,
                               replace_own_build=False,
                               dry_run=False, finder=SimpleNamespace(poll=lambda: None),
                               esphome_password=None, esphome_key=None, web_password=None,
                               esphome_login=None)

    def setUp_found(self, running="sonoff"):
        # What the device runs now, as the network would show it.
        patch = mock.patch.object(routes.source, "find_current_firmware", return_value=running)
        patch.start()
        self.addCleanup(patch.stop)


class RouteTests(RouteTestCase):
    def test_each_firmware_has_its_own_steps(self):
        esphome = [step for step, _, _ in espressif.steps_for("esphome")]
        tasmota = [step for step, _, _ in espressif.steps_for("tasmota")]
        self.assertEqual(esphome[-5:], ["D4", "D5", "K1", "I1", "I2"])
        self.assertNotIn("E3", esphome)  # the partition table is left as Sonoff made it
        self.assertEqual(tasmota[-1], "H1")
        self.assertEqual(esphome[:len(espressif.COMMON_STEPS)], tasmota[:len(espressif.COMMON_STEPS)])

    def test_d4_is_named_for_what_it_does_on_each_route(self):
        self.addCleanup(routes.apply_target, "tasmota")
        routes.apply_target("esphome")
        self.assertEqual(routes.stage_name("D4"), "Making room for ESPHome")
        routes.apply_target("tasmota")
        self.assertEqual(routes.stage_name("D4"), "Preparing memory layout")

    def test_the_steps_that_only_wait_or_repeat_what_they_did_are_tried_again(self):
        # I2 waits for ESPHome and gives it the calibration again; T1 finds
        # Tasmota back already, or uploads to Tasmota's safeboot only.
        self.assertLessEqual({"I2", "T1"}, espressif.RETRY_STEPS)
        self.assertFalse({"C2", "E3"} & espressif.RETRY_STEPS)   # these happen once

    def test_an_install_cut_short_by_the_network_is_tried_again(self):
        self.assertIn("I1", espressif.RETRY_STEPS)
        said = ("esphome did not pass: the device says: migration_refused: "
                "POST /capture/written/21 returned HTTP -11 after 6 attempts")
        self.assertRegex(said, errors.TRANSIENT_RE)

    def test_a_phase_left_to_be_asked_about_is_still_tried_again(self):
        # I1 stays "attempted", with no error kept, when its phase ends with no
        # result; the reason it gave decides whether to try again in this run.
        ctx = self.ctx(firmware="esphome")
        ctx.last_failure = ("the device stopped the esphome without reaching this computer; "
                            "its console says: POST /capture/written/22 returned HTTP -1")
        tries = []

        def execute(ctx, step):
            tries.append(step)
            return 1 if len(tries) == 1 else 0

        with mock.patch.object(runner, "execute", side_effect=execute), \
                mock.patch.object(runner, "wait_for_device_back"), mock.patch.object(time, "sleep"):
            self.assertEqual(runner.execute_with_retries(ctx, "I1"), 0)
        self.assertEqual(tries, ["I1", "I1"])

    def test_a_run_is_complete_by_its_own_firmware(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.mark("I2", DONE)
        self.assertTrue(cli.run_is_complete(Path(self.tmp.name) / "run"))

    def test_d4_is_not_rechecked_once_esphome_runs(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.mark("I1", DONE)
        ctx.device = SimpleNamespace(current_ota=lambda: self.fail("asked the device"))
        with mock.patch.object(evidence, "commit_report", return_value=None):
            self.assertEqual(tasmota.verify_high_slot(ctx), (True, ""))


class FromAConvertedDeviceTests(RouteTestCase):
    """A device this tool converted, found on the network, offered the other two."""

    def settle(self, running, *, backup=True, given=None, answer=0, record=None):
        ctx = self.ctx(given=given, run=f"{running}-{given}-{backup}-{answer}-{bool(record)}")
        with mock.patch.object(routes.source, "find_current_firmware", return_value=running), \
                mock.patch.object(routes, "sonoff_backup_exists", return_value=backup), \
                mock.patch.object(identity, "kept_in_sector", return_value=None), \
                mock.patch.object(identity, "account_record", return_value=record), \
                mock.patch.object(prompts, "ask_choice", return_value=answer) as asked:
            route = routes.settle_route(ctx)
        return route, (asked.call_args.args[1] if asked.called else None)

    def test_from_tasmota_the_other_two_are_offered_and_staying(self):
        (firmware, source), offered = self.settle("tasmota", answer=3)
        self.assertEqual(offered, ["Tasmota, as it is (its meter calibrated, if it is not)", "ESPHome",
                                   SUPLA, "Sonoff (eWeLink)"])
        self.assertEqual((firmware, source), ("sonoff", "tasmota"))

    def test_staying_on_tasmota_converts_nothing(self):
        # Its meter is checked instead (rerun_on_tasmota), asked or given.
        with self.assertRaisesRegex(routes.NothingToDo, "already runs Tasmota"):
            self.settle("tasmota", answer=0)
        with self.assertRaisesRegex(routes.NothingToDo, "already runs Tasmota"):
            self.settle("tasmota", given="tasmota")

    def test_without_its_backup_sonoff_is_still_offered(self):
        # Its firmware is Sonoff's official one; its identity the device keeps,
        # or its eWeLink account has: found once it is chosen.
        for running, answer in (("esphome", 0), ("tasmota", 2)):
            with self.subTest(running=running):
                _, offered = self.settle(running, backup=False, answer=answer, record={"deviceid": "x"})
                self.assertIn("Sonoff (eWeLink)", offered)

    def test_chosen_without_a_backup_its_identity_is_found_before_anything_is_written(self):
        # From ESPHome, what the device keeps is counted on with no account record;
        # from Tasmota, with neither kept nor recorded, it is refused there and then.
        (firmware, source), _ = self.settle("esphome", backup=False, answer=2)
        self.assertEqual((firmware, source), ("sonoff", "esphome"))
        self.assertEqual(self.settle("tasmota", backup=False, answer=3, record={"deviceid": "x"})[0],
                         ("sonoff", "tasmota"))
        with self.assertRaisesRegex(errors.Abort, "keeps no Sonoff identity"):
            self.settle("tasmota", backup=False, answer=3)

    def test_the_firmware_it_already_runs_cannot_be_asked_for(self):
        with self.assertRaisesRegex(routes.NothingToDo, "already runs ESPHome"):
            self.settle("esphome", given="esphome")
        with self.assertRaisesRegex(errors.Abort, "no backup"):
            self.settle("tasmota", backup=False, given="sonoff")

    def test_a_device_left_part_way_can_go_on_to_any_of_them(self):
        _, offered = self.settle("midway")
        self.assertEqual(len(offered), 4)

    def test_every_route_ends_by_checking_its_firmware(self):
        last = {"tasmota": "H1", "esphome": "I2", "supla": "S2", "sonoff": "R2"}
        for running in espressif.SOURCES:
            for firmware in espressif.FIRMWARES:
                if firmware == running or (running == "sonoff" and firmware == "sonoff"):
                    continue
                if running == "outdated":
                    # Updated, as its firmware takes any update: the last step checks
                    # the new release arrived. Left, it is a run from the one it runs.
                    if firmware in ("supla", "sonoff"):
                        self.assertRaises(ValueError, espressif.steps_for, firmware, running)
                        continue
                    self.assertEqual([step for step, _, _ in espressif.steps_for(firmware, running)],
                                     {"esphome": ["A5", "U1", "U2"], "tasmota": ["A5", "U5", "U6"]}[firmware])
                    continue
                steps = [step for step, _, _ in espressif.steps_for(firmware, running)]
                if (running, firmware) == ("safeboot", "tasmota"):
                    # Its own Tasmota put back, not converted: T1 sees it running,
                    # set up as before; H1 would hold its safeboot and bootloader,
                    # whatever made them, to this version's.
                    self.assertEqual(steps, ["A5", "T1"])
                    continue
                if (running, firmware) == ("untemplated", "tasmota"):
                    # Given its template alone, likewise.
                    self.assertEqual(steps, ["A5", "F4"])
                    continue
                self.assertEqual(steps[-1], last[firmware], (running, firmware))
                self.assertEqual(len(steps), len(set(steps)), (running, firmware))
                # The bootloader is written on every route, before any firmware goes
                # in - past the commit, by the run that got there (D5 comes before E3);
                # Supla's own with it, at S1.
                self.assertTrue("D5" in steps or "L1" in steps or "S1" in steps or running == "committed",
                                (running, firmware))

    def test_past_the_commit_official_tasmota_goes_on_first(self):
        # Never the hub's steps: they need Sonoff's two slots, and Tasmota's
        # layout has one - the slot swap waits for a restart that never comes.
        for firmware in espressif.FIRMWARES:
            steps = [step for step, _, _ in espressif.steps_for(firmware, "committed")
                     if step != "S0"]  # Supla's build, checked first (test_supla.py)
            self.assertEqual(steps[:3], ["A5", "F2", "F4"], firmware)
            if firmware != "tasmota":
                self.assertIn("H1", steps)
                self.assertEqual(steps[steps.index("H1") + 1], "L1", firmware)  # then on from Tasmota
            self.assertFalse({"X2", "D4", "E3"} & set(steps[:steps.index("H1")]), firmware)


class FromTheInstallerTests(RouteTestCase):
    def test_on_from_the_installer_it_needs_no_ewelink(self):
        # Left part way from Sonoff's firmware: the backup and D1 need only the
        # installer; the eWeLink steps, which a device on it cannot answer, are not run.
        for firmware in espressif.FIRMWARES:
            steps = [step for step, _, _ in espressif.steps_for(firmware, "installer")
                     if step != "S0"]  # Supla's build, checked first (test_supla.py)
            self.assertEqual(steps[:3], ["A5", "C3", "D1"], firmware)
            self.assertFalse({"A1", "A2", "C2"} & set(steps), firmware)

    def test_an_upload_to_the_installer_is_tried_again(self):
        # The installer selects itself before an upload writes its peer slot.
        self.assertIn("D1", espressif.RETRY_STEPS)


if __name__ == "__main__":
    unittest.main()
