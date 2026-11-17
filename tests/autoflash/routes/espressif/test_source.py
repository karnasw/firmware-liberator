"""Where an Espressif device was left, when a run starts
(tools/autoflash/routes/espressif/source.py): past the commit or not,
ESPHome in safe mode, and a device on Tasmota with ESPHome's bootloader
already leaving it for ESPHome without the hub."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.autoflash.routes.espressif.test_espressif import RouteTestCase
from tools.autoflash import detect, errors, handover, hostsys, join, output, records, routes, runs, unlock
from tools.autoflash.device import DeviceError
from tools.autoflash.routes import espressif, source, update
from tools.autoflash.routes.espressif import images, mover, source as espressif_source
from tools.autoflash.state import ATTEMPTED, DONE, RunState
from tools.espressif import chips


class LeavingTasmotaForESPHomeTests(RouteTestCase):
    """Every device ends with ESPHome's bootloader: only a device that has it skips
    the hub, which writes it, and the choice is kept for the rest of the run."""

    def settle(self, *, direct):
        ctx = self.ctx(given="esphome", run=str(direct), device_kind="sonoff-pow-elite")
        ctx.device = mock.Mock(sha_region=mock.Mock(return_value=self.ON_DEVICE[direct]))
        with mock.patch.object(routes.source, "find_current_firmware", return_value="tasmota"), \
                mock.patch.object(update, "outdated", return_value=False), \
                mock.patch.object(routes, "sonoff_backup_exists", return_value=True):
            routes.settle_route(ctx)
        return ctx

    ON_DEVICE = {True: hashlib.sha256(images.published_bootloader(chips.ESP32)).hexdigest(), False: "0" * 64}

    def names(self, state):
        return [step for step, _, _ in routes.steps_of(state)]

    def test_a_device_with_esphomes_bootloader_goes_straight_to_esphome(self):
        ctx = self.settle(direct=True)
        self.assertEqual(self.names(ctx.state), ["A0", "A3", "A5", "L1", "M1", "I2"])

    def test_it_is_read_where_the_chip_keeps_it(self):
        ctx = self.settle(direct=True)
        ctx.device.sha_region.assert_called_once_with(chips.ESP32.bootloader_at,
                                                      len(images.published_bootloader(chips.ESP32)), timeout=30)

    def test_any_other_goes_through_the_hub_which_writes_it(self):
        steps = self.names(self.settle(direct=False).state)
        self.assertEqual(steps, ["A0", "A3", "A5", "L1", "X1", "X2", "D4", "D5", "K1", "I1", "I2"])

    def test_a_device_that_will_not_say_goes_through_the_hub(self):
        ctx = self.ctx(device_kind="sonoff-pow-elite")
        for failure in (DeviceError("no Berry"), OSError("gone")):
            ctx.device = mock.Mock(sha_region=mock.Mock(side_effect=failure))
            self.assertFalse(espressif_source.leaves_tasmota_direct(ctx))

    def test_a_layout_on_a_temporary_table_needs_the_hub_to_leave_it(self):
        # M1 would leave it there for good, whatever bootloader it has.
        ctx = self.ctx(device_kind="sonoff-th-elite")
        ctx.device = mock.Mock(sha_region=mock.Mock(return_value=self.ON_DEVICE[True]))
        self.assertFalse(espressif_source.leaves_tasmota_direct(ctx))

    def test_a_rerun_takes_the_way_it_began_and_does_not_ask_the_device_again(self):
        for direct, next_step in ((True, "M1"), (False, "X1")):
            ctx = self.settle(direct=direct)
            ctx.device.sha_region.reset_mock()
            ctx.device.sha_region.return_value = self.ON_DEVICE[not direct]  # by now the device says otherwise
            self.assertEqual(routes.settle_route(ctx), ("esphome", "tasmota"))
            ctx.device.sha_region.assert_not_called()
            steps = self.names(ctx.state)
            self.assertEqual(steps[steps.index("L1") + 1], next_step)

    def test_a_run_begun_before_the_choice_keeps_to_the_way_it_went(self):
        ctx = self.ctx(source="tasmota", firmware="esphome")
        self.assertIn("D5", self.names(ctx.state))  # wrote nothing yet: as any new run
        ctx.state.mark("L1", DONE)     # that version's L1 wrote the bootloader
        self.assertEqual(self.names(ctx.state), ["A0", "A3", "A5", "L1", "M1", "I2"])

    def test_the_other_ways_from_tasmota_all_pass_through_the_hub(self):
        for running in ("safeboot", "committed", "untemplated"):
            self.assertIn("D5", [step for step, _, _ in espressif.steps_for("esphome", running)], running)
        self.assertIn("D5", [step for step, _, _ in espressif.steps_for("sonoff", "tasmota")])


class EsphomeSafeModeTests(unittest.TestCase):
    """A device answering as the mover that a run left on ESPHome is ESPHome in
    safe mode: waited out, never sent the startup firmware."""

    def running(self, earlier, answers=("mover", "esphome"), by_address=False, other_device=()):
        from types import SimpleNamespace

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        kept = []
        for index, (firmware, done) in enumerate(earlier):
            run = root / f"run{index}"
            run.mkdir()
            state = RunState.open(run)
            state.set_input("firmware", firmware)
            for step in done:
                state.mark(step, "done")
            kept.append(run)
        now = root / "now"
        now.mkdir()
        inputs = {"device_ip": "192.0.2.137", "device_mac": "12:34:56:ab:cd:ef"}
        ctx = SimpleNamespace(dir=now, state=SimpleNamespace(input=inputs.get),
                              web_password=None, esphome_login=None)
        answers = iter(answers)
        clock = iter(range(80000, 90000, 5))  # as time.monotonic() is: seconds since some moment
        macs = {run: "12:34:56:ab:cd:01" if index in other_device else "12:34:56:ab:cd:ef"
                for index, run in enumerate(kept)}
        self.slept = []
        with mock.patch.object(runs, "run_directories", return_value=kept), \
                mock.patch.object(records, "mac_of_run", side_effect=macs.get), \
                mock.patch.object(detect, "identify", side_effect=lambda *a, **k: next(answers)), \
                mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(time, "sleep", side_effect=self.slept.append), \
                mock.patch.object(output, "say"):
            if by_address:
                with mock.patch.object(hostsys, "mac_of", return_value="12:34:56:AB:CD:EF"), \
                        mock.patch.object(source, "adopt_device"), \
                        mock.patch.object(espressif_source, "past_the_commit", side_effect=lambda ctx, running: running):
                    return source.find_current_firmware(ctx)
            return espressif_source.esphome_safe_mode(ctx, "midway")

    def test_left_on_esphome_it_is_waited_out(self):
        converted = [("esphome", ["D5", "I1", "I2"])]
        self.assertEqual(self.running(converted, answers=("mover", "mover", "esphome")), "esphome")
        self.assertEqual(self.slept, [5, 5], "a pause between asks")
        # Run again at its address, as the network search finds it.
        self.assertEqual(self.running(converted, answers=("mover", "esphome"), by_address=True), "esphome")

    def test_otherwise_it_is_the_mover(self):
        self.assertEqual(self.running([]), "midway")
        # Stopped before ESPHome had confirmed itself: back on the mover, part way.
        self.assertEqual(self.running([("esphome", ["D5", "I1"])]), "midway")
        # Its last run took it on from ESPHome.
        self.assertEqual(self.running([("esphome", ["D5", "I1", "I2"]), ("tasmota", ["X1", "X2"])]), "midway")

    def test_a_run_that_wrote_nothing_does_not_hide_it(self):
        self.assertEqual(self.running([("esphome", ["D5", "I1", "I2"]), ("tasmota", [])]), "esphome")

    def test_only_this_devices_runs_count(self):
        self.assertEqual(self.running([("esphome", ["D5", "I1", "I2"])], other_device=(0,)), "midway")

    def test_one_that_never_comes_back_stops_the_run(self):
        with self.assertRaises(errors.Abort):
            self.running([("esphome", ["D5", "I1", "I2"])], answers=["mover"] * 1000)


class PastTheCommitTests(unittest.TestCase):
    """A device left on the startup firmware is on Tasmota's layout if this
    computer's run of it got past the commit - Sonoff's otherwise."""

    def source(self, earlier, safeboot=False, version="15.6.0(safeboot)", running="midway", other_device=(),
               damaged=(), evidence_made=False):
        import json
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        kept = []
        for index, (done, outcome, *stopped) in enumerate(earlier):
            run = root / f"run{index}"
            (run / "live").mkdir(parents=True)
            state = RunState.open(run)
            for step in done:
                # "X1!": stopped before it wrote anything; "X1?": stopped, maybe after.
                if step.endswith(("!", "?")):
                    state.mark(step[:-1], "failed", **({"nothing_written": True} if step[-1] == "!" else {}))
                else:
                    state.mark(step, "done")
            if stopped:  # stopped as E3 ran: what its server saw of the device
                state.mark("E3", ATTEMPTED)
                (run / "live" / "commit-server.log").write_text(stopped[0])
            if outcome:
                (run / "live" / "commit-report.json").write_text(json.dumps({"phase": "commit", "status": outcome}))
            if index in damaged:
                (run / "autoflash-state.json").write_text("{not json", encoding="utf-8")
            kept.append(run)
        now = root / "now"
        now.mkdir()
        ctx = SimpleNamespace(dir=now, state=RunState.open(now), evidence=now / "live",
                              device=SimpleNamespace(is_safeboot=lambda: safeboot, firmware_version=lambda: version))
        self.evidence, self.state = ctx.evidence, ctx.state
        if evidence_made:
            ctx.evidence.mkdir()
        ctx.state.set_input("device_mac", "12:34:56:ab:cd:ef")
        macs = {run: "12:34:56:ab:cd:01" if index in other_device else "12:34:56:ab:cd:ef"
                for index, run in enumerate(kept)}
        with mock.patch.object(runs, "run_directories", return_value=kept), \
             mock.patch.object(records, "mac_of_run", side_effect=macs.get), \
             mock.patch.object(output, "say"):
            return espressif_source.past_the_commit(ctx, running)

    def test_a_run_that_committed_and_did_not_finish(self):
        self.assertEqual(self.source([(["E1", "E2", "E3"], None)]), "committed")
        # Stopped as the device restarted: the report says so, the record not yet.
        self.assertEqual(self.source([(["E1", "E2"], "PASS")]), "committed")
        # And G1 has that run's record of the startup firmware it put on.
        self.assertTrue((self.evidence / "commit-report.json").is_file())

    def test_tasmotas_own_safeboot_is_on_tasmotas_layout(self):
        # Seen asked on Reddit: an ESPHome factory image uploaded through
        # Tasmota's page wrote over its Tasmota: Tasmota's safeboot from then
        # on. Taken for part way, it went down the hub's steps, stopping at D4.
        tasmotas = "15.6.0(release-safeboot)"
        self.assertEqual(self.source([], safeboot=True, version=tasmotas), "safeboot")
        self.assertEqual(self.source([], safeboot=True, version="15.6.0 (development-safeboot)"), "safeboot")
        # Converted here, and finished: Tasmota's own safeboot all the same.
        self.assertEqual(self.source([(["E3", "F2", "F4", "G1", "H1"], None)], safeboot=True,
                                     version=tasmotas), "safeboot")
        # This tool's startup firmware, built here: part way, as before.
        self.assertEqual(self.source([], safeboot=True, version="15.6.0(safeboot)"), "midway")
        self.assertEqual(self.source([], safeboot=True, version=""), "midway")
        # Past the commit on this computer's record: the commit's own way on.
        self.assertEqual(self.source([(["E1", "E2", "E3"], None)], safeboot=True, version=tasmotas),
                         "committed")

    def test_a_run_begun_by_1_2_0_keeps_its_bootloader_through_the_next(self):
        # It never put ESPHome's bootloader on (no D5 then): H1 is not to look
        # for it - nor G1 to prove the startup firmware that version put on.
        self.assertEqual(self.source([(["D4", "E1", "E2", "E3"], None)]), "committed")
        self.assertEqual(self.state.input("bootloader_kept"), "sonoff")
        self.assertTrue(self.state.input("startup_unproven"))
        self.assertEqual(self.source([(["D4", "D5", "E1", "E2", "E3"], None)]), "committed")
        self.assertIsNone(self.state.input("bootloader_kept"))
        self.assertIsNone(self.state.input("startup_unproven"))

    def test_otherwise_part_way_as_before(self):
        self.assertEqual(self.source([(["X1", "X2"], None)]), "midway")       # the ESPHome route
        self.assertEqual(self.source([(["E3", "F2", "F4", "G1", "H1"], None)]), "midway")  # finished
        self.assertEqual(self.source([]), "midway")                           # no record of it
        # The latest run decides: one past the commit, then one that started over.
        self.assertEqual(self.source([(["E3"], None), (["X1"], None)]), "midway")

    def test_only_this_devices_runs_count(self):
        self.assertEqual(self.source([(["E1", "E2", "E3"], None)], other_device=(0,)), "midway")
        self.assertEqual(self.source([(["E1", "E2", "E3"], None), (["X1"], None)], other_device=(1,)),
                         "committed")

    def test_a_record_that_cannot_be_read_is_passed_over(self):
        self.assertEqual(self.source([(["E1", "E2", "E3"], None), (["X1"], None)], damaged=(1,)), "committed")
        self.assertEqual(self.source([(["X1"], None)], damaged=(0,)), "midway")

    def test_the_commits_record_is_kept_where_this_run_already_keeps_its_own(self):
        self.assertEqual(self.source([(["E1", "E2", "E3"], "PASS")], evidence_made=True), "committed")
        self.assertTrue((self.evidence / "commit-report.json").is_file())

    def test_a_run_that_wrote_nothing_does_not_hide_the_one_before(self):
        # Seen: runs stopped at X1, sent to a Tasmota with no update service,
        # stood between a device and the record of the run that knew it.
        self.assertEqual(self.source([(["E1", "E2", "E3"], None), (["X1!"], None)]), "committed")
        self.assertEqual(self.source([(["E1", "E2", "E3"], None), ([], None)]), "committed")
        # One that may have written something decides, as before.
        self.assertEqual(self.source([(["E1", "E2", "E3"], None), (["X1?"], None)]), "midway")

    def test_x1_finding_no_update_service_says_nothing_was_written(self):
        from types import SimpleNamespace
        inputs = {"device_ip": "192.0.2.137", "source": "tasmota"}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get), dry_run=False,
                              wifi=lambda why=None: ("wifi", "password"))
        with mock.patch.object(unlock, "identify", return_value="tasmota"), \
                mock.patch.object(mover, "startup_image", return_value=b"image"), \
                mock.patch.object(join, "wait_for", side_effect=errors.Abort("did not answer")), \
                mock.patch.object(handover, "send_as_esphome_update") as sent, \
                self.assertRaises(errors.NothingWritten):
            mover.step_startup_from_esphome(ctx)
        sent.assert_not_called()

    def test_a_tasmota_without_its_template_is_never_sent_the_hubs_steps(self):
        # Seen: a converted device that had lost its template, taken for part
        # way, was sent X1 - an ESPHome update - and waited 4 minutes for an
        # update service no Tasmota has (firmware-liberator discussion #8).
        self.assertEqual(self.source([(["E3", "F2", "F4", "G1", "H1"], None)], running="untemplated"),
                         "untemplated")
        self.assertEqual(self.source([], running="untemplated"), "untemplated")
        # Left past the commit by a run: official Tasmota's steps, as before.
        self.assertEqual(self.source([(["E1", "E2", "E3"], None)], running="untemplated"), "committed")
        for firmware in ("tasmota", "esphome", "sonoff"):
            steps = [step for step, _, _ in espressif.steps_for(firmware, "untemplated")]
            self.assertIn(steps[1], ("L1", "F4"))  # leaving Tasmota, or given its template
        self.assertEqual([step for step, _, _ in espressif.steps_for("tasmota", "untemplated")],
                         ["A5", "F4"])
        self.assertEqual(espressif.steps_for("sonoff", "untemplated"), espressif.steps_for("sonoff", "tasmota"))


    def test_a_commit_the_device_finished_after_its_run_was_stopped(self):
        # Seen in the fault suite: Ctrl-C a second after the device fetched the
        # new table; it finished alone, and a --new run took it for the
        # ESPHome route's, whose steps need Sonoff's layout.
        fetched = "GET /target-table HTTP/1.1 200\n"
        self.assertEqual(self.source([(["E1", "E2"], None, fetched)], safeboot=True), "committed")
        self.assertEqual(self.source([(["E1", "E2"], None, "")], safeboot=True), "midway")      # never fetched
        self.assertEqual(self.source([(["E1", "E2"], None, fetched)], safeboot=False), "midway")  # not finished


if __name__ == "__main__":
    unittest.main()
