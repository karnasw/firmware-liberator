"""A run's record (tools/autoflash/state.py): kept across a reload and an
interruption, every step's status as it was left, the file replaced whole."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import hostsys, routes
from tools.autoflash.routes import espressif
from tools.autoflash.routes.espressif import evidence
from tools.autoflash.state import DONE, FAILED, RunState


class RunStateTests(unittest.TestCase):
    def test_a_moment_of_windows_refusal_is_waited_out(self):
        real = hostsys.os.replace
        with tempfile.TemporaryDirectory() as directory:
            run = RunState.open(Path(directory))
            refusals = [PermissionError(5, "Access is denied")] * 2

            def flaky(source, target):
                if refusals:
                    raise refusals.pop()
                real(source, target)

            with mock.patch.object(hostsys.os, "replace", side_effect=flaky), \
                    mock.patch.object(hostsys, "IS_WINDOWS", True), \
                    mock.patch.object(hostsys.time, "sleep"):
                run.mark("E3", DONE)
            self.assertTrue(RunState.open(Path(directory)).is_done("E3"))
            # Elsewhere a refusal means what it says, at once.
            with mock.patch.object(hostsys.os, "replace", side_effect=PermissionError(13, "denied")), \
                    mock.patch.object(hostsys, "IS_WINDOWS", False), \
                    self.assertRaises(PermissionError):
                run.mark("E2", DONE)
            self.assertEqual([f.name for f in Path(directory).iterdir()], ["autoflash-state.json"],
                             "nor does a write that failed leave its temporary file")

    def test_the_record_says_when_each_step_was_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            run = RunState.open(Path(directory))
            run.mark("E3", DONE)
            again = RunState.open(Path(directory))
            self.assertRegex(again.data["steps"]["E3"]["at"], r"^\d{4}-\d\d-\d\dT")

    def test_a_run_begun_by_a_release_before_2_4_0_is_read_under_todays_names(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "autoflash-state.json"
            path.write_text(json.dumps({
                "schema": 1, "run_id": "0a1b2c", "created_utc": "", "updated_utc": "", "steps": {},
                "inputs": {"plug_ip": "192.0.2.50", "plug_mac": "12:34:56:ab:cd:ef", "plug_kind": "sonoff-s60",
                           "firmware": "tasmota",
                           "firewall_rules": [{"kind": "ufw", "plug_ip": "192.0.2.50", "ports": [8088]}]},
                "captures": {"E2.plug_busy_seconds": 41.5, "D4.signal_dbm": -60}}), encoding="utf-8")
            run = RunState.open(Path(directory))
            self.assertEqual((run.input("device_ip"), run.input("device_mac"), run.input("device_kind")),
                             ("192.0.2.50", "12:34:56:ab:cd:ef", "sonoff-s60"))
            # The rule it added, to be taken away again: seen in the fault suite's
            # legacy scenarios, each later run stopped on it (TypeError: plug_ip).
            self.assertEqual(run.input("firewall_rules"), [{"kind": "ufw", "device_ip": "192.0.2.50", "ports": [8088]}])
            hostsys.FirewallRule.from_json(run.input("firewall_rules")[0])
            self.assertEqual(run.data["captures"], {"E2.device_busy_seconds": 41.5, "D4.signal_dbm": -60})
            run.save()   # and kept so, for the next step
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(saved["inputs"]), ["device_ip", "device_kind", "device_mac", "firewall_rules", "firmware"])
            self.assertEqual(sorted(saved["captures"]), ["D4.signal_dbm", "E2.device_busy_seconds"])

    def test_state_survives_reload_and_records_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = RunState.open(root)
            state.set_input("device_ip", "192.0.2.99")
            state.mark("A1", DONE, note="paired")
            state.capture("D1.upload_seconds", 41)

            reopened = RunState.open(root)
            self.assertEqual(reopened.input("device_ip"), "192.0.2.99")
            self.assertTrue(reopened.is_done("A1"))
            self.assertEqual(reopened.data["captures"]["D1.upload_seconds"], 41)

    def test_require_reports_every_missing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("device_ip", "")
            with self.assertRaisesRegex(ValueError, "device_ip.*listen_ip|listen_ip"):
                state.require("device_ip", "listen_ip")

    def test_failed_step_is_not_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = RunState.open(root)
            state.mark("E1", FAILED, error="stage refused")
            self.assertFalse(RunState.open(root).is_done("E1"))


class ResumeTests(unittest.TestCase):
    """The checklist is only useful if it behaves under interruption."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_a_step_that_never_reported_is_re_run(self):
        state = RunState.open(self.root)
        state.mark("C2", "attempted")
        reopened = RunState.open(self.root)
        self.assertFalse(reopened.is_done("C2"))
        self.assertEqual(reopened.status("C2"), "attempted")

    def test_a_crash_before_any_mark_leaves_the_step_pending(self):
        state = RunState.open(self.root)
        state.mark("A1", DONE)
        # C2 was never reached at all
        self.assertEqual(RunState.open(self.root).status("C2"), "pending")

    def test_completed_and_failed_steps_are_distinguishable(self):
        state = RunState.open(self.root)
        state.mark("E1", DONE)
        state.mark("E2", FAILED, error="stage refused")
        reopened = RunState.open(self.root)
        self.assertTrue(reopened.is_done("E1"))
        self.assertFalse(reopened.is_done("E2"))
        self.assertEqual(reopened.data["steps"]["E2"]["error"], "stage refused")

    def test_migration_phases_resume_independently(self):

        ids = [step for step, _, _ in routes.STEPS]
        self.assertEqual(ids[ids.index("E1"):ids.index("E1") + 3], ["E1", "E2", "E3"])
        state = RunState.open(self.root)
        state.mark("E1", DONE)
        state.mark("E2", DONE)
        reopened = RunState.open(self.root)
        self.assertTrue(reopened.is_done("E2"))
        self.assertFalse(reopened.is_done("E3"))

    def test_every_destructive_step_records_an_attempt_first(self):

        # Every route's steps: STEPS holds only the route chosen for the run.
        # From an earlier release ("outdated") only to this one: left, it is a run from what it runs.
        known = {step for firmware in espressif.FIRMWARES for source in espressif.SOURCES
                 if firmware != source and (source != "outdated" or firmware in espressif.UPDATE_STEPS)
                 for step, _, _ in espressif.steps_for(firmware, source)}
        known |= {step for step, _, _ in espressif.steps_for("esphome", espressif.DIRECT_FROM_TASMOTA)}
        self.assertTrue(evidence.DESTRUCTIVE_STEPS <= known)
        self.assertIn("E3", evidence.DESTRUCTIVE_STEPS)
        self.assertIn("C2", evidence.DESTRUCTIVE_STEPS)

    def test_state_file_is_replaced_atomically(self):
        state = RunState.open(self.root)
        state.mark("A1", DONE)
        leftovers = [f for f in self.root.iterdir() if f.name.startswith(".")]
        self.assertEqual(leftovers, [], "temporary files must not survive a write")


if __name__ == "__main__":
    unittest.main()
