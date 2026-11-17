"""Tasmota's steps (tools/autoflash/routes/espressif/tasmota.py): the commit and
what its evidence says when it is cut off, official Tasmota installed, seen
and configured, the restore, Tasmota put back on its own safeboot (T1), and
an earlier release updated from its safeboot (U5, U6)."""
import contextlib
import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.routes.espressif.test_espressif import SUPLA, RouteTestCase
from tools.autoflash import detect, device, errors, hostsys, join, kinds, names, output, prompts, routes, runs
from tools.autoflash import safety_locks, wifi
from tools.autoflash.phases import PhaseError
from tools.autoflash.routes import espressif, update
from tools.autoflash.routes.espressif import evidence, hub, source as espressif_source, tasmota
from tools.autoflash.state import ATTEMPTED, FAILED, RunState
from tools.espressif.safeboot import prepare_imprinted_bundle, safeboot_migration
from tools.espressif.safeboot.safeboot_migration import INTERIM_TABLE_SHA256
from tools.espressif.safeboot.safeboot_migration import OFFICIAL_TASMOTA_VERSION


class PrepareUpdateTests(RouteTestCase):
    """Before a device converted on an earlier release is updated in the same
    run, its bundle is built again of this release (prepare_update)."""

    def prepare(self, *, own_bundle=False, fetched=True):
        ctx = self.ctx(run=f"prepare-{own_bundle}-{fetched}", device_kind="sonoff-s60")
        ctx.manifest_path = Path(tempfile.mkdtemp()) / "manifest.json"
        self.addCleanup(shutil.rmtree, ctx.manifest_path.parent, ignore_errors=True)
        default = ctx.manifest_path if not own_bundle else Path("/elsewhere/manifest.json")
        with mock.patch.object(espressif.bundle, "default_manifest", return_value=default), \
                mock.patch.object(espressif.bundle, "build_bundle",
                                  side_effect=None if fetched else prepare_imprinted_bundle.BundleError("offline")) \
                as built, mock.patch.object(output, "say"):
            return tasmota.prepare_update(ctx), built

    def test_the_bundle_is_built_again_of_this_release(self):
        prepared, built = self.prepare()
        self.assertTrue(prepared)
        self.assertIsNone(built.call_args.kwargs["release"])   # this release's, not the device's

    def test_not_a_bundle_of_the_persons_own(self):
        prepared, built = self.prepare(own_bundle=True)
        self.assertFalse(prepared)
        built.assert_not_called()

    def test_this_release_not_fetched_leaves_the_device_on_its_own(self):
        self.assertFalse(self.prepare(fetched=False)[0])


class TasmotaUpdateTests(RouteTestCase):
    """Official Tasmota of an earlier release than the one this tool installs
    is offered that one, uploaded to its safeboot as F2 uploads it; any
    other Tasmota is not."""

    def settle(self, release, *, given=None, answer=0, run="run"):
        ctx = self.ctx(given=given, run=run, device_kind="sonoff-s60")
        with mock.patch.object(routes.source, "find_current_firmware", return_value="tasmota"), \
                mock.patch.dict(update.RELEASE, tasmota=lambda ctx: release), \
                mock.patch.object(routes, "sonoff_backup_exists", return_value=True), \
                mock.patch.object(espressif_source, "leaves_tasmota_direct", return_value=False), \
                mock.patch.object(prompts, "ask_choice", return_value=answer) as asked:
            route = routes.settle_route(ctx)
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        return ctx, route, (asked.call_args.args[1] if asked.called else None)

    def test_an_earlier_release_is_offered_the_one_this_tool_installs(self):
        with mock.patch.object(prompts, "section") as shown:
            ctx, route, offered = self.settle("15.6.0")
        # Said on its option alone: no line above the options on it, nor a heading.
        shown.assert_not_called()
        self.assertEqual(offered, [f"Tasmota, update to {OFFICIAL_TASMOTA_VERSION}", "ESPHome", SUPLA,
                                   "Sonoff (eWeLink)"])
        self.assertEqual(route, ("tasmota", "outdated"))
        self.assertEqual([step for step, _, _ in routes.steps_of(ctx.state)], ["A0", "A3", "A5", "U5", "U6"])
        self.assertEqual(update.versions(ctx), ("15.6.0", OFFICIAL_TASMOTA_VERSION))
        self.assertTrue(runs.device_runs_tasmota(ctx.state))

    def test_asked_for_on_the_command_line_it_is_updated_unasked(self):
        _, route, offered = self.settle("15.6.0", given="tasmota")
        self.assertEqual((route, offered), (("tasmota", "outdated"), None))

    def test_leaving_it_is_leaving_any_tasmota(self):
        _, route, _ = self.settle("15.6.0", answer=1)
        self.assertEqual(route, ("esphome", "tasmota"))

    def test_this_release_or_anyones_own_build_is_kept_as_it_is(self):
        # Its meter checked instead (rerun_on_tasmota), as ever.
        for why, release in (("this release", OFFICIAL_TASMOTA_VERSION), ("not official", None)):
            with self.subTest(why):
                with self.assertRaisesRegex(routes.NothingToDo, "already runs Tasmota"):
                    self.settle(release, given="tasmota", run=why)

    def test_its_release_is_read_from_an_official_builds_version_alone(self):
        for version, release in (("15.6.0(release-tasmota32)", "15.6.0"), ("16.0.0 (release-tasmota32)", "16.0.0"),
                                 ("15.6.0.1(f5b34a2-bluetooth)", None), ("16.0.0(release-safeboot)", None),
                                 ("14.0.0(tasmota32c3-mine)", None), ("", None)):
            ctx = SimpleNamespace(device=SimpleNamespace(firmware_version=lambda version=version: version))
            self.assertEqual(update.tasmota_release(ctx), release, version)

    def test_it_waits_for_the_safeboot_and_gives_up_with_nothing_changed(self):
        clock = [0.0]
        with mock.patch.object(tasmota.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(tasmota.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)):
            ctx, _ = self.outdated_tasmota(safeboot_after=10)
            with mock.patch.object(tasmota, "install_official", return_value=("uploaded", "rejoined the network")):
                tasmota.step_tasmota_update(ctx)   # ten looks, 3 s apart: within the wait
            ctx, _ = self.outdated_tasmota(safeboot_after=1000)
            with mock.patch.object(tasmota, "install_official") as installed, \
                    self.assertRaisesRegex(errors.Abort, "did not switch to its safeboot. Nothing was changed"):
                tasmota.step_tasmota_update(ctx)
            installed.assert_not_called()

    def outdated_tasmota(self, safeboot_after=1):
        """A Tasmota that goes to its safeboot on Restart 3, after `safeboot_after` asks."""
        commands = []
        asks = iter([False] * safeboot_after + [True] * 100)
        device = SimpleNamespace(is_safeboot=lambda: next(asks),
                                 command=lambda text, timeout=None: commands.append(text) or {})
        ctx = self.ctx(source="outdated", firmware="tasmota", update_from="15.6.0",
                       update_to=OFFICIAL_TASMOTA_VERSION)
        ctx.device = device
        return ctx, commands

    def test_it_is_switched_to_its_safeboot_and_given_this_release_there(self):
        ctx, commands = self.outdated_tasmota()
        with mock.patch.object(tasmota, "install_official", return_value=("uploaded", "rejoined the network")) \
                as installed, mock.patch.object(tasmota.time, "sleep"):
            result = tasmota.step_tasmota_update(ctx)
        self.assertEqual(commands, ["Restart 3"])
        self.assertEqual(installed.call_args.args[1].name, "tasmota32c3.bin")
        self.assertEqual(result["outcome"], "rejoined the network")

    def test_left_on_its_safeboot_it_is_given_it_from_there(self):
        ctx, commands = self.outdated_tasmota(safeboot_after=0)
        with mock.patch.object(tasmota, "install_official", return_value=("uploaded", "rejoined the network")):
            tasmota.step_tasmota_update(ctx)
        self.assertEqual(commands, [])

    def test_one_that_lost_its_settings_is_left_to_the_person(self):
        ctx, _ = self.outdated_tasmota()
        with mock.patch.object(tasmota, "install_official", return_value=("uploaded", "raised its own network")), \
                mock.patch.object(tasmota.time, "sleep"), self.assertRaisesRegex(errors.Abort, "lost its settings"):
            tasmota.step_tasmota_update(ctx)

    def test_the_check_waits_for_the_new_release_then_checks_the_meter(self):
        ctx, _ = self.outdated_tasmota()
        answers = iter(["15.6.0", None, OFFICIAL_TASMOTA_VERSION])
        with mock.patch.dict(update.RELEASE, tasmota=lambda ctx: next(answers)), \
                mock.patch.object(tasmota, "calibrate_tasmota_meter", return_value={"changed": False}) as meter, \
                mock.patch.object(time, "sleep"):
            result = tasmota.step_tasmota_updated(ctx)
        self.assertEqual((result["to"], result["meter"]), (OFFICIAL_TASMOTA_VERSION, {"changed": False}))
        meter.assert_called_once()

    def test_a_try_cut_off_is_judged_by_the_release_it_runs(self):
        ctx, _ = self.outdated_tasmota()
        for release, finished in ((OFFICIAL_TASMOTA_VERSION, True), ("15.6.0", False), (None, None)):
            with mock.patch.dict(update.RELEASE, tasmota=lambda ctx, release=release: release):
                self.assertIs(evidence.ATTEMPTED_EVIDENCE["U5"](ctx)[0], finished, release)
                self.assertIs(espressif.STEP_VERIFIERS["U5"](ctx)[0], finished is not False, release)


class ConversionWifiTests(unittest.TestCase):
    """G1 needs the Wi-Fi the device was converted with, proven against the
    commit's record of the startup firmware - never the bundle's say-so."""

    def setUp(self):
        import hashlib
        import json
        from types import SimpleNamespace
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        evidence = Path(directory.name)
        image = lambda ssid, password, chip=None: f"{ssid}/{password}".encode()  # noqa: E731
        (evidence / "commit-report.json").write_text(json.dumps({
            "phase": "commit", "status": "PASS",
            "safeboot_sha256": hashlib.sha256(image("Home", "right")).hexdigest(),
        }))
        self.ctx = SimpleNamespace(evidence=evidence, manifest_path=Path("unused"),
                                   state=SimpleNamespace(input=lambda name: None),
                                   args=SimpleNamespace(wifi=False), held_wifi=lambda: None,
                                   remember_wifi=mock.Mock())
        self.pinned = ("Home", "right")
        self.pin = mock.Mock(side_effect=lambda _, ssid, pw: setattr(self, "pinned", (ssid, pw)))

        def recovery(_, ssid, password):
            if (ssid, password) != self.pinned:
                raise prepare_imprinted_bundle.BundleError("not the pinned one")
            return image(ssid, password)

        for patch in (
            mock.patch.object(tasmota, "startup_image", side_effect=image),
            mock.patch.object(tasmota, "recovery_image", side_effect=recovery),
            mock.patch.object(tasmota, "pin_wifi", self.pin),
            mock.patch.object(prompts, "section"),
            mock.patch.object(output, "say"),
            mock.patch.object(output, "tell"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_with(self, on_device, typed=()):
        from unittest import mock
        answers = iter(typed)
        with mock.patch.object(wifi, "read_wifi_from_device", return_value=on_device), \
             mock.patch.object(prompts, "ask_text", side_effect=lambda *_, **__: next(answers)), \
             mock.patch.object(prompts, "ask_secret", side_effect=lambda *_: next(answers)) as asked:
            tasmota.conversion_wifi(self.ctx)
        return asked

    def test_the_devices_own_details_are_used_when_they_match(self):
        asked = self.run_with(("Home", "right"))
        asked.assert_not_called()
        self.pin.assert_not_called()
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_changed_wifi_on_the_device_asks_for_the_conversions(self):
        asked = self.run_with(("NewNet", "new"), typed=["Home", "wrong", "Home", "right"])
        self.assertEqual(asked.call_count, 2)  # a wrong password is asked again
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")
        self.pin.assert_not_called()  # the bundle was never moved off them

    def test_a_bundle_set_up_for_other_wifi_is_put_back(self):
        self.pinned = ("NewNet", "new")
        self.run_with(("Home", "right"))
        self.pin.assert_called_once_with(Path("unused"), "Home", "right")

    def test_the_devices_new_details_are_never_pinned(self):
        self.run_with(("NewNet", "new"), typed=["Home", "right"])
        for call in self.pin.call_args_list:
            self.assertNotEqual(call.args[1:], ("NewNet", "new"))

    def test_details_typed_earlier_in_the_run_are_used_unasked(self):
        # Seen in the emulator: typed at D5 (--wifi), and asked for again at G1.
        self.ctx.args.wifi = True
        self.ctx.held_wifi = lambda: ("Home", "right")
        with mock.patch.object(wifi, "read_wifi_from_device") as read:
            asked = self.run_with(None)
        asked.assert_not_called()
        read.assert_not_called()
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_details_held_that_match_are_used_and_the_device_is_not_asked(self):
        self.ctx.held_wifi = lambda: ("Home", "right")
        with mock.patch.object(wifi, "read_wifi_from_device") as read:
            asked = self.run_with(None)
        read.assert_not_called()
        asked.assert_not_called()
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_each_password_typed_is_kept_out_of_the_log(self):
        with mock.patch.object(output, "private") as private:
            self.run_with(("NewNet", "new"), typed=["Home", "wrong", "Home", "right"])
        self.assertEqual(private.call_args_list, [mock.call("wrong", "wifi-password"),
                                                  mock.call("right", "wifi-password")])

    def test_details_held_that_do_not_match_go_on_to_the_devices(self):
        self.ctx.held_wifi = lambda: ("NewNet", "new")
        asked = self.run_with(("Home", "right"))
        asked.assert_not_called()
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_with_wifi_typed_and_none_held_they_are_asked(self):
        self.ctx.args.wifi = True
        with mock.patch.object(wifi, "read_wifi_from_device") as read:
            asked = self.run_with(None, typed=["Home", "right"])
        read.assert_not_called()
        self.assertEqual(asked.call_count, 1)

    def test_a_lost_commit_report_falls_back_to_the_staging_record(self):
        import hashlib
        import json
        (self.ctx.evidence / "commit-report.json").unlink()
        (self.ctx.evidence / "stage-report.json").write_text(json.dumps({
            "phase": "stage", "status": "PASS",
            "staged_sha256": hashlib.sha256(b"Home/right").hexdigest()}))
        self.run_with(("Home", "right"))
        self.ctx.remember_wifi.assert_called_once_with("Home", "right", b"Home/right")

    def test_no_commit_record_stops_before_asking(self):
        (self.ctx.evidence / "commit-report.json").unlink()
        with self.assertRaisesRegex(errors.Abort, "no record of the startup firmware"):
            self.run_with(("Home", "right"))


def no_lock_records(root, name):
    """A lock lifted with nothing written where the runs that lift locks say
    so: this checkout's captures - the main checkout's, through a worktree's
    link - where a test's record would count as a real run's."""
    return contextlib.nullcontext()


class CommitEvidenceTests(unittest.TestCase):
    """E3 interrupted after the device reported is finished, not a question."""

    def ctx(self, report):
        import json
        from types import SimpleNamespace

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        evidence = Path(directory.name)
        if report is not None:
            (evidence / "commit-report.json").write_text(json.dumps(report))
        return SimpleNamespace(evidence=evidence)

    def test_a_pass_report_means_finished(self):
        finished, _ = evidence.evidence_commit_finished(
            self.ctx({"phase": "commit", "status": "PASS"}))
        self.assertTrue(finished)

    def test_a_fail_report_means_not_finished(self):
        finished, _ = evidence.evidence_commit_finished(
            self.ctx({"phase": "commit", "status": "FAIL"}))
        self.assertIs(finished, False)

    def no_report(self, *, fetched, safeboot=False, slot=None):
        # The emulated device's PASS report lost on its way (the commit with the
        # network cut as it finished): it then restarts into the startup firmware.
        from unittest import mock
        ctx = self.ctx(None)
        if fetched:
            (ctx.evidence / "commit-server.log").write_text(
                '  [10.0.0.63] "GET /berry/commit.be HTTP/1.1" 200 -\n'
                '  [10.0.0.63] "GET /target-table HTTP/1.1" 200 -\n')
        ctx.device = mock.Mock(is_safeboot=mock.Mock(return_value=safeboot),
                               current_ota=mock.Mock(return_value=slot))
        return evidence.evidence_commit_finished(ctx)

    def test_no_report_and_no_table_fetched_means_nothing_written(self):
        finished, _ = self.no_report(fetched=False)
        self.assertIs(finished, False)

    def test_no_report_but_on_the_startup_firmware_means_finished(self):
        finished, reason = self.no_report(fetched=True, safeboot=True)
        self.assertTrue(finished)
        self.assertIn("temporary startup", reason)

    def test_no_report_and_still_on_the_old_layout_means_not_finished(self):
        finished, _ = self.no_report(fetched=True, slot=1)
        self.assertIs(finished, False)

    def test_no_report_and_no_telling_is_left_to_the_operator(self):
        finished, _ = self.no_report(fetched=True, slot=None)
        self.assertIsNone(finished)

    def test_another_phase_report_is_not_taken(self):
        finished, _ = evidence.evidence_commit_finished(
            self.ctx({"phase": "stage", "status": "PASS"}))
        self.assertIsNot(finished, True)  # never taken as the commit's

    def test_a_passed_commit_supersedes_the_high_slot_check(self):
        # After the commit the device runs from slot 0 of the new layout: asking
        # to redo D4 then would put the interim Tasmota back over it.
        from types import SimpleNamespace
        from unittest import mock

        ctx = self.ctx({"phase": "commit", "status": "PASS"})
        ctx.state = SimpleNamespace(is_done=lambda step: False)
        ctx.device = mock.Mock()
        self.assertEqual(tasmota.verify_high_slot(ctx), (True, ""))
        ctx.device.current_ota.assert_not_called()

    def test_without_a_report_the_high_slot_is_still_checked(self):
        from types import SimpleNamespace
        from unittest import mock

        ctx = self.ctx(None)
        ctx.state = SimpleNamespace(is_done=lambda step: False)
        ctx.device = mock.Mock(current_ota=mock.Mock(return_value=0))
        ok, message = tasmota.verify_high_slot(ctx)
        self.assertFalse(ok)
        self.assertIn("high slot", message)

    def test_on_the_high_slot_the_check_passes(self):
        from types import SimpleNamespace
        ctx = self.ctx(None)
        ctx.state = SimpleNamespace(is_done=lambda step: False)
        ctx.device = mock.Mock(current_ota=mock.Mock(return_value=1))
        self.assertEqual(tasmota.verify_high_slot(ctx), (True, ""))

    def test_redoing_a_passed_commit_touches_nothing(self):
        # The startup firmware cannot answer the network check: running it
        # would fail and send the run back to E1 with no way forward.
        from unittest import mock

        ctx = self.ctx({"phase": "commit", "status": "PASS"})
        with mock.patch.object(join, "check_device_network") as network, \
             mock.patch.object(output, "say"):
            result = tasmota.step_commit(ctx)
        network.assert_not_called()
        self.assertTrue(result["already_written"])

    def test_a_commit_refused_as_changed_sends_the_run_back_to_its_checks(self):
        # After a power cut mid-commit the device's settings and staged copy have
        # changed: the commit, run again as it was, is refused every time.
        import json
        from unittest import mock

        ctx = self.ctx(None)
        manifest = ctx.evidence / "manifest.json"
        manifest.write_text("{}")
        (ctx.evidence / "stage-report.json").write_text(json.dumps(
            {"manifest_sha256": safeboot_migration.sha256_bytes(manifest.read_bytes())}))
        ctx.manifest_path = manifest
        ctx.state = RunState.open(ctx.evidence)
        for step in ("E1", "E2"):
            ctx.state.mark(step, "done")
        refused = 'THE DEVICE SAYS: migration_refused: NVS changed since preflight'
        ctx.phase_runner = lambda: mock.Mock(run=mock.Mock(return_value=(1, refused)))
        with mock.patch.object(join, "check_device_network"), \
             mock.patch.object(hub, "device_back_on_wifi"), \
             mock.patch.object(hub, "sonoff_layout_back"), \
             mock.patch.object(hostsys, "replace_file"), \
             mock.patch.object(safety_locks, "lifted", no_lock_records), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            with self.assertRaises(errors.Abort) as caught:
                tasmota.commit_once(ctx)
        self.assertIn("run the same command", str(caught.exception))
        self.assertEqual([ctx.state.status(s) for s in ("E1", "E2")], [FAILED, FAILED])

    def test_a_bundle_changed_since_staging_sends_the_run_back_and_writes_nothing(self):
        import json
        ctx = self.ctx(None)
        manifest = ctx.evidence / "manifest.json"
        manifest.write_text("{}")
        (ctx.evidence / "stage-report.json").write_text(json.dumps(
            {"manifest_sha256": safeboot_migration.sha256_bytes(b'{"rebuilt": true}')}))
        ctx.manifest_path = manifest
        ctx.state = RunState.open(ctx.evidence)
        for step in ("E1", "E2"):
            ctx.state.mark(step, "done")
        runner = mock.Mock(run=mock.Mock(return_value=(0, "")))
        ctx.phase_runner = lambda: runner
        order = []
        with mock.patch.object(join, "check_device_network", side_effect=lambda c: order.append("network")), \
             mock.patch.object(hub, "device_back_on_wifi", side_effect=lambda c: order.append("found")), \
             mock.patch.object(hub, "sonoff_layout_back"), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            with self.assertRaisesRegex(errors.Abort, "the firmware files changed.*Nothing was written"):
                tasmota.commit_once(ctx)
        self.assertEqual(order, ["found", "network"])   # the device found before it is asked anything
        runner.run.assert_not_called()
        self.assertEqual([ctx.state.status(s) for s in ("E1", "E2")], [FAILED, FAILED])

    def staged_after(self, preflight_manifest):
        """step_stage, E1's report made with `preflight_manifest`: which steps
        ran, the run's bundle being {}."""
        import json
        from unittest import mock

        ctx = self.ctx(None)
        manifest = ctx.evidence / "manifest.json"
        manifest.write_text("{}")
        (ctx.evidence / "preflight-report.json").write_text(json.dumps(
            {"manifest_sha256": safeboot_migration.sha256_bytes(preflight_manifest)}))
        ctx.manifest_path = manifest
        ctx.state = RunState.open(ctx.evidence)
        ctx.state.mark("E1", "done")
        ran = []
        ctx.phase_runner = lambda: mock.Mock(run=lambda phase: ran.append(phase) or (0, ""))
        with mock.patch.object(tasmota, "step_preflight", side_effect=lambda c: ran.append("E1") or {}), \
             mock.patch.object(output, "say"):
            self.assertEqual(tasmota.step_stage(ctx), {"exit": 0})
        return ran

    def test_staging_after_checks_with_this_bundle(self):
        self.assertEqual(self.staged_after(b"{}"), ["stage"])

    def test_staging_after_checks_with_another_bundle_checks_again_first(self):
        # A run of 1.2.0 stopped after E1, carried on by this version, which
        # rebuilt the bundle at A5: the stage server refused E1's report.
        self.assertEqual(self.staged_after(b'{"version": "1.2.0"}'), ["E1", "stage"])

    def commit_tries(self, outcomes, recovered=False):
        """step_commit with each try's outcome given: an exception, or a result."""
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ctx = SimpleNamespace(evidence=Path(directory.name), state=RunState.open(Path(directory.name)))
        tries = iter(outcomes)

        def once(_):
            outcome = next(tries)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(tasmota, "commit_once", side_effect=once) as commit, \
             mock.patch.object(tasmota, "commit_recovered", return_value=recovered) as recover, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            try:
                result = tasmota.step_commit(ctx)
            except Exception as exc:  # noqa: BLE001 - what the test looks at
                result = exc
        return result, commit.call_count, recover.call_count, ctx.state

    def test_a_power_cut_in_the_commit_is_recovered_from_and_tried_again(self):
        result, tries, recoveries, state = self.commit_tries(
            [PhaseError("the device stopped the commit without reaching this computer"), {"exit": 0}])
        self.assertEqual((result, tries, recoveries), ({"exit": 0}, 2, 1))
        self.assertEqual(state.data["captures"]["E3.attempts"], 2)
        self.assertEqual(tasmota.COMMIT_ATTEMPTS, 3)

    def test_a_read_back_that_differed_is_tried_again_and_so_is_a_refusal_as_changed(self):
        result, tries, _, _ = self.commit_tries([
            tasmota.CommitFailed("commit did not pass: the device says: migration_refused: "
                                   "canonical Safeboot read-back differs at 0x31000",
                                   "AUTOFLASH ROLLBACK PASS: interim layout kept"),
            tasmota.CommitFailed("commit refused before writing anything - the device says: "
                                   "migration_refused: staged Safeboot SHA-256 differs", ""),
            {"exit": 0}])
        self.assertEqual((result, tries), ({"exit": 0}, 3))

    def test_the_device_refusing_connections_while_it_comes_back_is_another_try(self):
        import urllib.error
        result, tries, _, _ = self.commit_tries(
            [urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), {"exit": 0}])
        self.assertEqual((result, tries), ({"exit": 0}, 2))

    def test_three_tries_at_most_then_it_stops(self):
        result, tries, recoveries, state = self.commit_tries([PhaseError("timed out")] * 5)
        self.assertIsInstance(result, PhaseError)
        self.assertEqual((tries, recoveries, state.data["captures"]["E3.attempts"]), (3, 2, 3))

    def test_a_rollback_that_failed_or_another_refusal_is_never_tried_again(self):
        for failure in (
                tasmota.CommitFailed("commit did not pass: the device says: migration_refused: "
                                       "canonical Safeboot read-back differs at 0x31000",
                                       "AUTOFLASH ROLLBACK FAILED; DO NOT REBOOT OR REMOVE POWER"),
                tasmota.CommitFailed("commit did not pass: the device says: migration_refused: "
                                       "commit requires old ota_1 to be running", "")):
            result, tries, recoveries, _ = self.commit_tries([failure, {"exit": 0}])
            self.assertIs(result, failure)
            self.assertEqual((tries, recoveries), (1, 0))

    def test_a_commit_the_device_finished_after_its_window_closed_is_not_tried(self):
        # Seen in the fault suite: the window closed as the commit ran; the device
        # finished it, and the run carried on at E3 ran its network check on
        # the startup firmware, which cannot say its network - and went back
        # to E1 with no way forward.
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        evidence = Path(directory.name)
        (evidence / "commit-server.log").write_text('[192.0.2.51] "GET /target-table HTTP/1.1" 200 -\n')
        ctx = SimpleNamespace(evidence=evidence, state=RunState.open(evidence),
                              device=mock.Mock(is_safeboot=mock.Mock(return_value=True)))
        with mock.patch.object(tasmota, "commit_once") as commit, \
             mock.patch.object(hub, "device_back_on_wifi"), \
             mock.patch.object(output, "say"):
            result = tasmota.step_commit(ctx)
        commit.assert_not_called()
        self.assertEqual(result, {"exit": 0, "finished_unseen": True})
        # Still on Tasmota, the table fetched but the commit not finished:
        # tried as ever.
        ctx.device.is_safeboot.return_value = False
        with mock.patch.object(tasmota, "commit_once", return_value={"exit": 0}) as commit, \
             mock.patch.object(hub, "device_back_on_wifi"), \
             mock.patch.object(output, "say"):
            self.assertEqual(tasmota.step_commit(ctx), {"exit": 0})
        commit.assert_called_once()

    def test_a_commit_that_finished_unseen_is_not_done_again(self):
        # The cut came after the final table: the device runs the startup firmware.
        result, tries, recoveries, _ = self.commit_tries([PhaseError("timed out")], recovered=True)
        self.assertEqual((result, tries, recoveries), ({"exit": 0, "finished_unseen": True}, 1, 1))

    def test_a_temporary_table_gives_way_to_the_devices_own_before_anything_else(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.autoflash import plans as plan_module
        from tools.espressif import layouts

        layout = layouts.SONOFF_TH_ELITE
        for table, planned in ((layout.temporary_sha256, True), (layout.table_sha256, False)):
            device = mock.Mock(sha_region=mock.Mock(side_effect=[table]))
            ctx = SimpleNamespace(dry_run=False, device=device,
                                  state=SimpleNamespace(input={"device_kind": "sonoff-th-elite"}.get))
            with mock.patch.object(hub, "run_plan") as run_plan, \
                 mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
                hub.sonoff_layout_back(ctx)
            self.assertEqual(run_plan.called, planned, table)
            self.assertEqual(device.wait_for_reboot.called, planned, table)
            if planned:
                plan = run_plan.call_args.args[1]
                self.assertEqual(plan.name, "own_table")
                self.assertEqual(plan.final_table_sha256(), layout.table_sha256)
                self.assertIsInstance(plan.steps[0], plan_module.Write)

    def test_the_interim_layout_is_put_back_before_anything_else(self):
        # A commit stopped part way leaves the settings moved aside, which
        # every check and plan after it does not expect.
        from types import SimpleNamespace
        from unittest import mock
        from tools.autoflash import plans as plan_module
        from tools.autoflash.device import DeviceError

        for table, planned in ((INTERIM_TABLE_SHA256, True),
                            (plan_module.SONOFF_TABLE_SHA256, False),
                            (DeviceError("no Berry"), False)):
            device = mock.Mock(sha_region=mock.Mock(side_effect=[table]))
            ctx = SimpleNamespace(dry_run=False, device=device, state=SimpleNamespace(input=lambda name: None))
            with mock.patch.object(hub, "run_plan") as run_plan, \
                 mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
                hub.sonoff_layout_back(ctx)
            self.assertEqual(run_plan.called, planned, table)
            self.assertEqual(device.wait_for_reboot.called, planned, table)
            if planned:
                self.assertEqual(run_plan.call_args.args[1].name, "sonoff_layout")

    def test_a_device_asking_to_be_set_up_is_set_up_before_the_commit(self):
        # After a power cut mid-commit, Tasmota may have lost its settings and be
        # waiting on its own network: the run used to stop on "No route to host".
        from types import SimpleNamespace
        from unittest import mock

        for on_wifi, setup, expected in ((True, None, None), (False, "tasmota-ABCDEF-1234", "configure"),
                                         (False, None, "locate")):
            ctx = SimpleNamespace(finder=mock.Mock(poll=mock.Mock(return_value="192.0.2.5" if on_wifi else None)),
                                  device=mock.Mock())
            with mock.patch.object(prompts, "find_ssid", return_value=setup), \
                 mock.patch.object(names, "setup_networks", return_value=("tasmota-ABCDEF-",)), \
                 mock.patch.object(join, "configure_device_wifi") as configure, \
                 mock.patch.object(join, "locate_device") as locate, \
                 mock.patch.object(output, "tell"):
                hub.device_back_on_wifi(ctx)
            self.assertEqual(configure.called, expected == "configure", (on_wifi, setup))
            self.assertEqual(locate.called, expected == "locate", (on_wifi, setup))
            if expected == "configure":
                configure.assert_called_once_with(ctx, setup, join.SETTINGS_LOST)
            # On the network is not answering yet: switched back on, the device
            # answers ping before its web server is up.
            ctx.device.wait_reachable.assert_called_once()


class TasmotaBackTests(unittest.TestCase):
    """T1: a device on Tasmota's own safeboot, its Tasmota missing, given
    official Tasmota back - its settings kept - before going on."""

    def back(self, *, safeboot=True, starts=False, outcome="rejoined the network",
             found=("tasmota", "tasmota-abcdef", "s60"), wants_password=False):
        from types import SimpleNamespace
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ctx = SimpleNamespace(state=RunState.open(Path(directory.name)), dry_run=False, web_password=None,
                              finder=mock.Mock(poll=mock.Mock(return_value=True)),
                              device=mock.Mock(host="192.0.2.50", is_safeboot=mock.Mock(return_value=safeboot),
                                               wants_password=mock.Mock(return_value=wants_password)))
        with mock.patch.object(tasmota, "install_official", return_value=("http:9s", outcome)) as install, \
             mock.patch.object(tasmota, "tasmota_started_again", return_value=starts), \
             mock.patch.object(detect, "tasmota", **({"side_effect": found} if isinstance(found, list)
                                                    else {"return_value": found})), \
             mock.patch.object(tasmota.unlock, "ask_web_password") as self.asked_password, \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            result = tasmota.step_tasmota_back(ctx)
        return result, install.called, ctx.state

    def test_its_web_password_is_asked_for_only_if_it_wants_one(self):
        self.back(wants_password=True)
        self.asked_password.assert_called_once()
        self.back(wants_password=None)   # not answering: nothing to ask yet
        self.asked_password.assert_not_called()

    def test_back_but_too_busy_ever_to_say_its_template(self):
        with self.assertRaisesRegex(errors.Abort, "not set up as a device this tool converts"):
            self.back(found=[detect.Unreadable("192.0.2.50")] * 4)

    def test_put_back_and_found_as_before(self):
        result, installed, state = self.back()
        self.assertTrue(installed)
        self.assertEqual(result["name"], "tasmota-abcdef")
        self.assertEqual(state.input("device_kind"), "s60")

    def started(self, versions, refused=False):
        """tasmota_started_again, the device's version each look in turn, 3 s apart."""
        from types import SimpleNamespace
        from unittest import mock
        clock = [1000.0]
        answers = iter(versions)
        device = mock.Mock(firmware_version=lambda: next(answers, versions[-1]))
        if refused:
            device.start_app.side_effect = device.DeviceError("refused")
        with mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
             mock.patch.object(output, "say"):
            started = tasmota.tasmota_started_again(SimpleNamespace(device=device))
        self.started_app = device.start_app.called
        return started

    def test_its_own_starts(self):
        self.assertTrue(self.started(["15.6.0(release-safeboot)", "", "16.0.0(release-tasmota32)"]))
        self.assertTrue(self.started_app)   # asked to, as the safeboot's page switch does

    def test_the_safeboot_answering_as_it_restarts_is_not_the_end(self):
        self.assertTrue(self.started(["15.6.0(release-safeboot)"] * 3 + ["16.0.0(release-tasmota32)"]))

    def test_silence_while_it_starts_is_not_the_end(self):
        self.assertTrue(self.started([""] * 15 + ["16.0.0(release-tasmota32)"]))

    def test_none_starts_it_is_back_on_its_safeboot(self):
        self.assertFalse(self.started(["15.6.0(release-safeboot)"]))

    def test_the_request_refused(self):
        self.assertFalse(self.started(["15.6.0(release-safeboot)"], refused=True))

    def test_its_own_started_again_is_not_replaced(self):
        # Switched to its safeboot by hand, its Tasmota unharmed: never an
        # upload over it - perhaps a newer Tasmota than the one pinned here.
        result, installed, _ = self.back(starts=True)
        self.assertFalse(installed)
        self.assertEqual(result["app_upload"], "started again")

    def test_back_already_is_not_sent_again(self):
        _, installed, _ = self.back(safeboot=False)
        self.assertFalse(installed)

    def test_back_but_not_a_device_this_tool_converts(self):
        with self.assertRaisesRegex(errors.Abort, "not set up as a device this tool converts"):
            self.back(found=None)

    def test_back_but_its_settings_gone(self):
        with self.assertRaisesRegex(errors.Abort, "lost its settings"):
            self.back(outcome="raised its setup network tasmota-ABCDEF-1234")


class BegunByAnEarlierVersionTests(unittest.TestCase):
    """A run of 1.2.0 carried on by this version: it had no D5 (ESPHome's
    bootloader), which needs the device as it is before the commit."""

    def ctx(self, done=(), e3=None):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        for step in done:
            state.mark(step, "done")
        if e3:
            state.mark("E3", e3)
        return SimpleNamespace(state=state)

    def bootloader(self, ctx):
        from unittest import mock
        with mock.patch.object(hub, "run_plan", return_value={"plan": "ran"}) as plan, \
             mock.patch.object(hub, "sonoff_layout_back"), mock.patch.object(output, "say"):
            result = hub.step_bootloader(ctx)
        return result, plan.called

    def test_d5_before_the_commit_writes_it(self):
        self.assertEqual(self.bootloader(self.ctx(["D4", "E1", "E2"])), ({"plan": "ran"}, True))

    def test_d5_once_the_commit_began_leaves_sonoffs(self):
        for e3 in (ATTEMPTED, "done"):
            ctx = self.ctx(["D4", "E1", "E2"], e3=e3)
            result, planned = self.bootloader(ctx)
            self.assertFalse(planned)
            self.assertIn("skipped", result)
            self.assertEqual(ctx.state.input("bootloader_kept"), "sonoff")
            self.assertTrue(ctx.state.input("startup_unproven"))

    def restored(self, unproven):
        """step_restore: whether the Wi-Fi was proven, the runner given the
        startup firmware, and the flags the restore server was started with."""
        from unittest import mock
        ctx = self.ctx()
        if unproven:
            ctx.state.set_input("startup_unproven", True)
        runner = mock.Mock(run=mock.Mock(return_value=(0, "")))
        given = []
        ctx.phase_runner = lambda with_recovery=True: given.append(with_recovery) or runner
        with mock.patch.object(tasmota, "conversion_wifi") as wifi, mock.patch.object(output, "say"):
            self.assertEqual(tasmota.step_restore(ctx), {"restore": 0})
        return wifi.called, given[0], runner.run.call_args.kwargs["extra_flags"]

    def test_g1_proves_the_startup_firmware_it_writes_over(self):
        self.assertEqual(self.restored(unproven=False), (True, True, ["--i-confirm-normal-app-is-stable"]))

    def test_g1_on_a_run_begun_by_1_2_0_goes_without(self):
        # 1.2.0's startup firmware is not one this checkout can rebuild: no
        # Wi-Fi details would ever prove it, and the run stopped there for good.
        self.assertEqual(self.restored(unproven=True),
                         (False, False, ["--i-confirm-normal-app-is-stable", "--recovery-unproven"]))

    def gate(self, checks):
        """G1's check that the device runs official Tasmota, given what each ask of
        the device gives: (whether it passed, and how many times it was asked)."""
        from unittest import mock
        ctx = self.ctx()
        ctx.device = mock.Mock()
        runner = mock.Mock(run=mock.Mock(return_value=(0, "")))
        ctx.phase_runner = lambda with_recovery=True: runner
        with mock.patch.object(tasmota, "conversion_wifi"), mock.patch.object(output, "say"):
            tasmota.step_restore(ctx)
        gate = runner.run.call_args.kwargs["confirm"]
        with mock.patch.object(tasmota, "verify_app", side_effect=checks) as asked, \
             mock.patch.object(output, "say"), mock.patch.object(output, "tell"):
            return gate("restore"), asked.call_count

    def test_g1s_check_dropped_by_the_network_is_asked_again(self):
        # Seen in the fault suite: a blip as G1 asked stopped the step, saying
        # swapping the startup firmware was only safe under official Tasmota.
        reset = ConnectionResetError(104, "Connection reset by peer")
        self.assertEqual(self.gate([reset, (True, "")]), (True, 2))
        self.assertEqual(self.gate([reset] * 5), (False, 3))

    def test_g1s_check_refused_stops_at_once(self):
        self.assertEqual(self.gate([(False, "app0 on the device no longer matches the pinned image")]), (False, 1))
        self.assertEqual(self.gate([device.DeviceError("no Berry console")]), (False, 1))

    def verified(self, kept, template=True, table="aa"):
        from unittest import mock
        ctx = self.ctx()
        self.state = ctx.state
        if kept:
            ctx.state.set_input("bootloader_kept", "sonoff")
        artifacts = {"target_table": {"sha256": "aa"}, "safeboot": {"size": 1, "sha256": "bb"},
                     "app": {"size": 1, "sha256": "cc"}}
        hashes = {safeboot_migration.TABLE_OFFSET: table, safeboot_migration.SAFEBOOT_OFFSET: "bb", safeboot_migration.APP0_OFFSET: "cc",
                  0: "sonoffs-own"}
        ctx.manifest_path = Path("unused")
        ctx.device = mock.Mock(sha_region=lambda address, length: hashes[address])
        with mock.patch.object(tasmota, "load_manifest", return_value={"artifacts": artifacts}), \
             mock.patch.object(tasmota, "template_in_place",
                               side_effect=lambda *_, fallback=True: template is True
                               or (template == "fallback" and not fallback)), \
             mock.patch.object(tasmota, "give_template") as given, \
             mock.patch.object(output, "say"), mock.patch.object(output, "tell"):
            results = tasmota.step_verify(ctx)
        self.given = given.called
        self.sent = [c.args[0] for c in ctx.device.command.call_args_list]
        return results

    def test_h1_takes_the_kept_bootloader(self):
        self.assertEqual(sorted(self.verified(kept=True)), ["app0", "safeboot", "table"])

    def test_h1_sets_the_fallback_alone_on_a_device_converted_before(self):
        # Seen: a converted device restarted six times in a row went over to
        # Tasmota's fallback module, Generic, and switched nothing.
        self.verified(kept=True, template="fallback")
        self.assertFalse(self.given)  # no restart for it
        self.assertEqual(self.sent, ["Module2 0"])

    def test_h1_puts_the_devices_template_in_again_without_it(self):
        # Seen: a run completed on a device with none, which switched nothing.
        self.verified(kept=True)
        self.assertFalse(self.given)
        self.verified(kept=True, template=False)
        self.assertTrue(self.given)
        self.assertIs(self.state.data["captures"].get("H1.template_again"), True)   # for the report

    def test_h1_takes_no_table_but_this_runs_or_tasmotas_own(self):
        # Tasmota's own: committed by a version before the identity was kept.
        self.assertTrue(self.verified(kept=True, table=tasmota.OFFICIAL_TABLE_SHA256)["table"]["match"])
        with self.assertRaisesRegex(errors.Abort, "does not match the official firmware files"):
            self.verified(kept=True, table="dd")

    def test_h1_otherwise_wants_esphomes(self):
        with self.assertRaises(errors.Abort):
            self.verified(kept=False)


class ConfigureStepTests(unittest.TestCase):
    """F4 rebuilds Tasmota's settings from its own defaults before anything else."""

    @staticmethod
    def console(sent, hostname=None, takes=True):
        """A Tasmota's console, recording what it is `sent`: set up as F4 sets
        it once the commands after the reset are in (if it `takes` them)."""
        import json
        settled = {"Template": json.loads(kinds.SONOFF_S60.tasmota_template()),
                   "Module": {"Module": {"0": "Sonoff-S60"}}, "SaveData": {"SaveData": "ON"},
                   "Hostname": {"Hostname": hostname}, "Module2": {"Module2": {"0": "Sonoff-S60"}}}

        def command(c, timeout=None):
            sent.append(c)
            return settled.get(c) if takes and any("Module 0" in s for s in sent) else None
        return command

    def test_reset_4_comes_first_and_keeps_the_wifi(self):
        from types import SimpleNamespace
        from unittest import mock
        sent = []
        # A Tasmota that does not take the name and template at the reset's save.
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=self.console(sent), is_safeboot=lambda: False,
                                 at_next_save=lambda commands, timeout=None: False)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device, state=state)
        with mock.patch.object(output, "say") as said:
            result = tasmota.step_configure(ctx)
        writes = [command for command in sent if " " in command]  # not the questions first
        self.assertEqual(writes[0], "Reset 4")
        self.assertEqual(writes[1:3], ["SaveData 1", f"Template {kinds.SONOFF_S60.tasmota_template()}"])
        self.assertIn("Sonoff-S60", " ".join(str(c.args[0]) for c in said.call_args_list))
        self.assertEqual(result["wifi_route"], "joined by itself")
        self.assertIn("joined your Wi-Fi by itself", " ".join(str(c.args[0]) for c in said.call_args_list))

    def test_it_is_named_as_every_converted_tasmota_with_one_restart(self):
        # tasmota-abcdef, whatever the device: its own network takes the name too.
        from types import SimpleNamespace
        from unittest import mock
        sent = []
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=self.console(sent, "tasmota-abcdef"), is_safeboot=lambda: False,
                                 at_next_save=lambda commands, timeout=None: False)
        inputs = {"product_model": "S60TPF", "device_mac": "12:34:56:ab:cd:ef"}
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device,
                              state=SimpleNamespace(input=inputs.get))
        with mock.patch.object(output, "say"):
            result = tasmota.step_configure(ctx)
        self.assertIn("Backlog Hostname tasmota-abcdef; DeviceName tasmota-abcdef; Module2 0; Module 0", sent)
        self.assertEqual(result["restarts"], 2)

    def test_a_tasmota_in_use_that_lost_its_template_keeps_its_settings(self):
        # Its MQTT, say, set up since it was converted: Reset 4 would erase it.
        from types import SimpleNamespace
        from unittest import mock
        sent = []
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=self.console(sent, "tasmota-abcdef"), is_safeboot=lambda: False,
                                 at_next_save=lambda commands, timeout=None: self.fail("hooked to a reset"))
        inputs = {"product_model": "S60TPF", "device_mac": "12:34:56:ab:cd:ef", "source": "untemplated"}
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device,
                              state=SimpleNamespace(input=inputs.get))
        with mock.patch.object(output, "say"):
            result = tasmota.step_configure(ctx)
        self.assertNotIn("Reset 4", sent)
        # Its name too: the one it was given since is kept.
        self.assertIn("Backlog Hostname tasmota-abcdef; Module2 0; Module 0", sent)
        self.assertEqual(result["template"], "applied")

    def test_tasmotas_fallback_module_counts_as_part_of_the_template(self):
        # Generic as the fallback: six quick restarts and the device switches nothing.
        import json
        from types import SimpleNamespace
        template = kinds.SONOFF_S60.tasmota_template()
        answers = {"Template": json.loads(template), "Module": {"Module": {"0": "Sonoff-S60"}},
                   "SaveData": {"SaveData": "ON"}, "Module2": {"Module2": {"1": "ESP32C3"}}}
        device = SimpleNamespace(command=lambda c, timeout=None: answers.get(c))
        self.assertFalse(tasmota.template_in_place(device, template))
        self.assertTrue(tasmota.template_in_place(device, template, fallback=False))
        answers["Module2"] = {"Module2": {"0": "Sonoff-S60"}}
        self.assertTrue(tasmota.template_in_place(device, template))

    def test_the_template_given_is_asked_for_only_once_the_device_is_back(self):
        import json
        from types import SimpleNamespace
        template = kinds.SONOFF_S60.tasmota_template()
        events = []
        answers = {"Template": json.loads(template), "Module": {"Module": {"0": "Sonoff-S60"}},
                   "SaveData": {"SaveData": "ON"}, "Module2": {"Module2": {"0": "Sonoff-S60"}}}
        device = SimpleNamespace(started_at=lambda: "t", uptime_seconds=lambda: 10,
                                 wait_for_reboot=lambda **kwargs: events.append(("restarted", kwargs)),
                                 wait_reachable=lambda **_: events.append("answers"),
                                 command=lambda c, timeout=None: events.append(c) or answers.get(c))
        with mock.patch.object(output, "say"):
            tasmota.give_template(device, template, None)
        backlog = events.index("Backlog Module2 0; Module 0")
        self.assertEqual(events[backlog + 1:backlog + 4],
                         [("restarted", {"timeout": 180, "started_at": "t", "before": 10}), "answers", "Template"])

    def test_a_template_not_kept_stops_the_step(self):
        # Seen: a run completed on a device left with no template, which
        # switched nothing - Home Assistant showed its restart button alone.
        from types import SimpleNamespace
        from unittest import mock
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=self.console([], "tasmota-abcdef", takes=False),
                                 is_safeboot=lambda: False, at_next_save=lambda commands, timeout=None: False)
        inputs = {"product_model": "S60TPF", "device_mac": "12:34:56:ab:cd:ef"}
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device,
                              state=SimpleNamespace(input=inputs.get))
        with mock.patch.object(output, "say"), self.assertRaises(errors.Abort):
            tasmota.step_configure(ctx)

    def test_named_and_templated_as_the_reset_saves_in_its_one_restart(self):
        # Reset 4 rebuilds Tasmota's defaults, named its own way; the name and
        # template go in as that restart saves them (a Berry driver for
        # save_before_restart), so it never comes back as tasmota-ABCDEF-1234
        # and needs no restart of its own for them.
        import json
        from types import SimpleNamespace
        from unittest import mock
        template = kinds.SONOFF_S60.tasmota_template()
        hooked, sent, reset = [], [], []
        settled = {"Template": json.loads(template), "Module": {"Module": {"0": "Sonoff-S60"}},
                   "SaveData": {"SaveData": "ON"}, "Hostname": {"Hostname": "tasmota-abcdef"},
                   "Module2": {"Module2": {"0": "Sonoff-S60"}}}

        def command(c, timeout=None):
            sent.append(c)
            if c == "Reset 4":
                reset.append(c)
            return settled.get(c) if reset else {"Hostname": {"Hostname": "autoflash-recovery-CDEF"}}.get(c)

        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=command, is_safeboot=lambda: False,
                                 at_next_save=lambda commands, timeout=None: hooked.append(commands) or True)
        inputs = {"product_model": "S60TPF", "device_mac": "12:34:56:ab:cd:ef"}
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device,
                              state=SimpleNamespace(input=inputs.get))
        with mock.patch.object(output, "say"), mock.patch.object(tasmota, "back_after_reset") as back, \
             mock.patch.object(tasmota, "calibrate_tasmota_meter", return_value={"changed": True}):
            result = tasmota.step_configure(ctx)
        self.assertEqual(result["meter"], {"changed": True})   # calibrated after the reset, for the report
        self.assertEqual(hooked, [["Hostname tasmota-abcdef", "DeviceName tasmota-abcdef",
                                   f"Template {template}", "Module2 0", "Module 0"]])
        self.assertEqual([c for c in sent if " " in c], ["Reset 4"])  # nothing after it
        back.assert_called_once()
        self.assertEqual(result["restarts"], 1)


    def test_tried_again_once_the_settings_are_in_place_it_resets_nothing(self):
        # A busy device took the template but answered too slowly: resetting
        # again would restart it again, every try.
        import json
        from types import SimpleNamespace
        from unittest import mock
        template = kinds.SONOFF_S60.tasmota_template()
        answers = {"Template": json.loads(template), "Module": {"Module": {"0": "Sonoff-S60"}},
                   "SaveData": {"SaveData": "ON"}, "Hostname": {"Hostname": "tasmota-abcdef"},
                   "Module2": {"Module2": {"0": "Sonoff-S60"}}}
        sent = []

        def command(c, timeout=None):
            sent.append(c)
            if c.startswith("Backlog Hostname "):
                answers["Hostname"] = {"Hostname": c.split()[2].rstrip(";")}
            return answers.get(c)

        device = SimpleNamespace(wait_reachable=lambda **_: None, command=command, is_safeboot=lambda: False,
                                 started_at=lambda: "t", uptime_seconds=lambda: 10,
                                 wait_for_reboot=lambda **_: 1.0, at_next_save=lambda commands, timeout=None: False)
        inputs = {"product_model": "S60TPF", "device_mac": "12:34:56:ab:cd:ef"}
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device,
                              state=SimpleNamespace(input=inputs.get))
        with mock.patch.object(output, "say"):
            result = tasmota.step_configure(ctx)
        self.assertNotIn("Reset 4", sent)
        self.assertEqual(result["template"], "applied")
        # One set up before it was named so (an earlier version) is done again.
        answers["Hostname"] = {"Hostname": "tasmota-ABCDEF-1234"}
        sent.clear()
        with mock.patch.object(output, "say"), mock.patch.object(tasmota, "back_after_reset"):
            tasmota.step_configure(ctx)
        self.assertIn("Reset 4", sent)


class OfficialTasmotaSeenTests(unittest.TestCase):
    """F2 - official Tasmota is seen running only from what it says it is: a
    device restarting does not answer its page for a moment (seen in the fault
    suite taken for Tasmota, while it came back on the startup firmware)."""

    def wait(self, versions, setup=None):
        from types import SimpleNamespace
        from unittest import mock
        answers = iter(versions)
        clock = [1000.0]

        def sleep(seconds):
            clock[0] += seconds

        device = SimpleNamespace(host="192.168.60.50", firmware_version=lambda: next(answers))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.60.50"), device=device)
        with mock.patch.object(time, "sleep", side_effect=sleep), \
             mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(prompts, "find_ssid", return_value=setup), \
             mock.patch.object(output, "say"):
            return tasmota.wait_for_official(ctx)

    def test_silence_is_not_tasmota(self):
        outcome = self.wait(["", "", "15.6.0(release-tasmota32)"])
        self.assertEqual(outcome, "rejoined the network")

    def test_silence_over_a_minute_is_not_the_startup_firmware(self):
        self.assertEqual(self.wait([""] * 20 + ["15.6.0(release-tasmota32)"]), "rejoined the network")

    def test_the_startup_firmware_answering_before_it_restarts_is_not_the_end(self):
        self.assertEqual(self.wait(["15.6.0(safeboot)"] * 3 + ["15.6.0(release-tasmota32)"]),
                         "rejoined the network")

    def test_still_on_the_startup_firmware_a_minute_on_did_not_take(self):
        self.assertEqual(self.wait(["15.6.0(safeboot)"] * 20), "startup firmware")

    def test_one_that_did_not_take_is_sent_again(self):
        from unittest import mock
        with mock.patch.object(tasmota, "upload_to_startup_firmware", return_value="http:20s") as up, \
             mock.patch.object(tasmota, "wait_for_official",
                               side_effect=["startup firmware", "rejoined the network"]), \
             mock.patch.object(output, "tell") as told:
            how, outcome = tasmota.install_official(object(), Path("tasmota32c3.bin"))
        self.assertEqual((up.call_count, outcome), (2, "rejoined the network"))
        self.assertIn("Still on the startup firmware", " ".join(str(c.args[0]) for c in told.call_args_list))

    def test_three_that_do_not_take_stop_with_the_reason(self):
        from unittest import mock
        with mock.patch.object(tasmota, "upload_to_startup_firmware", return_value="http:20s"), \
             mock.patch.object(tasmota, "wait_for_official", return_value="startup firmware") as waited, \
             mock.patch.object(output, "tell"):
            with self.assertRaisesRegex(errors.Abort, "did not take"):
                tasmota.install_official(object(), Path("tasmota32c3.bin"))
        self.assertEqual(waited.call_count, hub.UPLOAD_ATTEMPTS)

    def test_f4_finding_the_startup_firmware_sends_official_tasmota_again(self):
        from types import SimpleNamespace
        from unittest import mock
        on_startup = [True]
        device = SimpleNamespace(wait_reachable=lambda **_: None, is_safeboot=lambda: on_startup[0],
                                 command=lambda c, timeout=None: None)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.168.60.50"), device=device, state=state)

        def installed(ctx_, app):
            on_startup[0] = False
            return "http:20s", "rejoined the network"

        with mock.patch.object(tasmota, "install_official", side_effect=installed) as install, \
             mock.patch.object(tasmota, "template_in_place", return_value=True), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            tasmota.step_configure(ctx)
        install.assert_called_once()


class BackAfterResetTests(unittest.TestCase):
    """F4 - Tasmota keeps its Wi-Fi through Reset 4 only once it has saved its
    defaults: cut off in the second before, it comes back on its own setup
    network, and is set up through that rather than the run failing."""

    def back(self, reboot_ok, setup):
        from types import SimpleNamespace
        from unittest import mock
        from tools.autoflash.device import DeviceError

        def wait_for_reboot(**kwargs):
            if not reboot_ok:
                raise DeviceError("192.168.60.50 did not restart within 180s")
            return 5.0

        device = SimpleNamespace(wait_for_reboot=wait_for_reboot, wait_reachable=mock.Mock())
        ctx = SimpleNamespace(device=device, state=SimpleNamespace(input=lambda key: None))
        with mock.patch.object(prompts, "find_ssid", return_value=setup) as looked, \
             mock.patch.object(names, "setup_networks", return_value=("tasmota-ABCDEF-1234",)), \
             mock.patch.object(join, "configure_device_wifi") as configured, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            try:
                tasmota.back_after_reset(ctx, "boot", 12)
                outcome = "back"
            except errors.Abort as exc:
                outcome = str(exc)
        return outcome, looked.called, configured.call_args

    def test_back_as_usual_looks_no_further(self):
        outcome, looked, configured = self.back(True, None)
        self.assertEqual((outcome, looked, configured), ("back", False, None))

    def test_back_on_its_setup_network_is_set_up_through_it(self):
        outcome, looked, configured = self.back(False, "tasmota-ABCDEF-1234")
        self.assertEqual(outcome, "back")
        self.assertEqual(configured.args[1], "tasmota-ABCDEF-1234")

    def test_nowhere_to_be_seen_says_to_switch_it_off_and_on(self):
        outcome, _, configured = self.back(False, None)
        self.assertIn("switch it off and on", outcome)
        self.assertIsNone(configured)


class StalledUploadTests(unittest.TestCase):
    """F2 - an upload to the startup firmware that stalls is tried again from
    a restart, three tries in all, before it is left to the person."""

    def upload(self, uploads, back=True, restart_answers=True):
        from types import SimpleNamespace
        from unittest import mock
        events = []
        tries = iter(uploads)

        def upload_firmware(path, timeout=None):
            events.append(("upload", timeout))
            outcome = next(tries)
            if isinstance(outcome, Exception):
                raise outcome

        def command(c, timeout=None):
            events.append(c)
            if not restart_answers:
                raise OSError("not answering")

        answering = iter([True, True, False] * 10)  # gone a few seconds after "Restart 1"

        def reachable(timeout=None):
            events.append("reachable")
            return next(answering)

        ctx = SimpleNamespace(device=SimpleNamespace(upload_firmware=upload_firmware, command=command,
                                                     reachable=reachable))
        with mock.patch.object(tasmota, "recovery_firmware_up", return_value=back), \
             mock.patch.object(hub, "upload_or_prompt_by_hand", return_value="manual") as by_hand, \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "tell") as told, mock.patch.object(output, "say"):
            try:
                result = tasmota.upload_to_startup_firmware(ctx, Path("tasmota32c3.bin"))
            except errors.Abort as exc:
                result = exc
        return result, events, by_hand.called, " ".join(str(c.args[0]) for c in told.call_args_list)

    def test_an_upload_that_goes_through_is_done_once(self):
        result, events, by_hand, _ = self.upload([None])
        self.assertTrue(result.startswith("http:"))
        self.assertEqual(events, [("upload", 120.0)])  # a stall noticed in two minutes, not ten
        self.assertFalse(by_hand)

    def test_a_stalled_upload_is_tried_again_after_a_restart(self):
        result, events, by_hand, told = self.upload([TimeoutError("stalled"), TimeoutError("stalled"), None])
        self.assertEqual([e for e in events if e == "Restart 1"], ["Restart 1"] * 2)
        self.assertEqual(len([e for e in events if isinstance(e, tuple)]), 3)
        # sent again only once the device was seen to go for its restart
        restart = events.index("Restart 1")
        self.assertEqual(events[restart + 1:restart + 4], ["reachable"] * 3)
        self.assertTrue(result.startswith("http:"))
        self.assertIn("attempt 3 of 3", told)
        self.assertFalse(by_hand)

    def test_three_stalls_leave_it_to_the_person(self):
        result, events, by_hand, _ = self.upload([TimeoutError("stalled")] * 5)
        self.assertEqual(len([e for e in events if isinstance(e, tuple)]), 3)
        self.assertTrue(by_hand)
        self.assertEqual(result, "manual")

    def test_a_device_too_stuck_to_restart_is_switched_off_and_on(self):
        _, _, _, told = self.upload([TimeoutError("stalled"), None], restart_answers=False)
        self.assertIn("switch it off and on", told)

    def test_a_refused_upload_says_so_and_is_tried_again(self):
        from tools.autoflash.device import UploadRefused
        result, events, by_hand, told = self.upload([UploadRefused("Not enough space"), None])
        self.assertTrue(result.startswith("http:"))
        self.assertIn("refused the upload (Not enough space)", told)
        self.assertFalse(by_hand)

    def test_one_that_never_comes_back_stops_with_a_reason(self):
        result, _, by_hand, _ = self.upload([TimeoutError("stalled")], back=False)
        self.assertIsInstance(result, errors.Abort)
        self.assertFalse(by_hand)


class StartupSettingsReportTests(unittest.TestCase):
    """F2 keeps what the startup firmware said about saving the Wi-Fi."""

    def report(self, lines=None, error=None):
        from types import SimpleNamespace
        from unittest import mock

        def console_log(**_):
            if error:
                raise error
            return lines
        with mock.patch.object(output, "say") as said:
            note = tasmota.startup_settings_report(SimpleNamespace(console_log=console_log))
        return note, " ".join(str(c.args[0]) for c in said.call_args_list)

    def test_a_saved_record_is_reported(self):
        note, text = self.report(["00:00:00.255 Autoflash recovery Wi-Fi defaults active",
                                  "00:00:00.316 Autoflash recovery: Wi-Fi settings saved for the next firmware"])
        self.assertEqual(note, "Autoflash recovery: Wi-Fi settings saved for the next firmware")
        self.assertIn("should join your network by itself", text)

    def test_a_failure_is_explained_and_the_fallback_named(self):
        note, text = self.report(["00:00:00.300 Autoflash recovery: settings not saved, read-back failed (4354)"])
        self.assertIn("read-back failed", note)
        self.assertIn("its own network instead", text)
        self.assertIn("Tasmota: settings not saved, read-back failed (4354).", text)

    def test_the_startup_firmware_from_before_its_rename_is_read_too(self):
        # A device a run from an older checkout left on the startup firmware.
        note, text = self.report(["00:00:00.300 S60 recovery: settings not saved, NVS wipe failed (-1)"])
        self.assertEqual(note, "S60 recovery: settings not saved, NVS wipe failed (-1)")
        self.assertIn("Tasmota: settings not saved, NVS wipe failed (-1).", text)

    def test_an_unreadable_log_does_not_stop_the_run(self):
        from tools.autoflash.device import DeviceError
        self.assertIsNone(self.report(error=DeviceError("no answer"))[0])
        self.assertIsNone(self.report(lines=["00:00:01 WIF: Connected"])[0])


class RecoveryWaitTests(unittest.TestCase):
    def ctx(self, versions):
        device = types.SimpleNamespace(host="192.0.2.50", firmware_version=lambda: next(versions),
                                       is_safeboot=lambda: False)
        return types.SimpleNamespace(finder=types.SimpleNamespace(poll=lambda: "192.0.2.50"), device=device)

    def test_it_is_seen_once_the_version_says_safeboot(self):
        ctx = self.ctx(iter(["15.6.0(tasmota)", "15.6.0(safeboot)"]))
        with mock.patch.object(time, "sleep"), mock.patch.object(output, "say"):
            self.assertTrue(tasmota.recovery_firmware_up(ctx, 0.0, within=300))

    def test_one_that_gives_no_version_is_seen_by_its_page(self):
        ctx = self.ctx(iter(lambda: "", None))
        ctx.device.is_safeboot = lambda: True
        with mock.patch.object(time, "sleep"), mock.patch.object(output, "say"):
            self.assertTrue(tasmota.recovery_firmware_up(ctx, 0.0, within=300))

    def test_it_gives_up_on_this_window_without_stopping_the_run(self):
        clock = iter(range(0, 1000, 100))
        ctx = self.ctx(iter(lambda: "15.6.0(tasmota)", None))
        with mock.patch.object(time, "sleep"), mock.patch.object(time, "monotonic", lambda: next(clock)):
            self.assertFalse(tasmota.recovery_firmware_up(ctx, 0.0, within=300))


class StepCanonicalWaitTests(unittest.TestCase):
    """F2 asks what now once it has waited five minutes: ten with nothing to
    see would be too long."""

    def wait(self, seen):
        ctx = types.SimpleNamespace(state=types.SimpleNamespace(data={}, input=lambda key, default=None: None),
                                    device=types.SimpleNamespace())
        with mock.patch.object(tasmota, "recovery_firmware_up", side_effect=seen), \
                mock.patch.object(tasmota.runs, "run_source", return_value="sonoff"), \
                mock.patch.object(tasmota, "startup_settings_report", return_value=None), \
                mock.patch.object(tasmota, "install_official", return_value=("uploaded", "rejoined")), \
                mock.patch.object(tasmota.prompts, "section"), mock.patch.object(time, "sleep"), \
                mock.patch.object(tasmota.prompts, "ask_choice", return_value=0) as asked, \
                mock.patch.object(output, "say"), mock.patch.object(output, "transient_begin"), \
                mock.patch.object(output, "transient_end"):
            tasmota.step_canonical(ctx)
        return asked.call_count

    def test_the_person_is_asked_after_the_first_wait(self):
        self.assertEqual(self.wait([True]), 0)
        self.assertEqual(self.wait([False, True]), 1)


if __name__ == "__main__":
    unittest.main()
