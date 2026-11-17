"""The fault suite's own logic: the names a device may go by, the saved devices
each device has, and the scenarios it lists."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import kinds, output, records, readout
from tools.autoflash.routes import espressif
from tools.espressif.emulator import fault_suite as fs
from tools.ewelink.stub_cloud import stub_server

MAC = "12:34:56:ab:cd:ef"


class NameProblemTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(fs, "MAC", MAC)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_conversion_under_the_names_it_should_have(self):
        names = ["espressif", "autoflash-recovery-CDEF", "tasmota-ABCDEF-1234", "tasmota-abcdef"]
        self.assertEqual(fs.name_problems(names, ["autoflash-recovery-CDEF", "ESP_ABCDF0"], "tasmota"), ([], None))
        self.assertEqual(fs.name_problems(["tasmota-abcdef", "autoflash-recovery-CDEF", "esphome-abcdef"],
                                          [], "esphome"), ([], None))
        self.assertEqual(fs.name_problems(["esphome-abcdef", "autoflash-recovery-CDEF", "espressif"],
                                          [], "sonoff"), ([], None))

    def test_esp_idf_default_network_name_follows_the_chip(self):
        # An ESP32-C3's is from its access-point MAC (the Wi-Fi MAC plus one);
        # a classic ESP32's, from its Wi-Fi MAC.
        for kind, ok, wrong in ((kinds.SONOFF_S60, "ESP_ABCDF0", "ESP_ABCDEF"),
                                (kinds.SONOFF_POWCT, "ESP_ABCDEF", "ESP_ABCDF0")):
            with self.subTest(kind=kind.key), mock.patch.object(fs, "DEVICE", kind):
                self.assertEqual(fs.name_problems(["tasmota-abcdef"], [ok], "tasmota"), ([], None))
                self.assertEqual(fs.name_problems(["tasmota-abcdef"], [wrong], "tasmota"), ([wrong], None))

    def test_a_name_this_device_should_never_have(self):
        unknown, _ = fs.name_problems(["autoflash-recovery-1234", "tasmota-abcdef"], ["tasmota"], "tasmota")
        self.assertEqual(unknown, ["autoflash-recovery-1234", "tasmota"])

    def test_not_ending_under_its_converted_name(self):
        _, final = fs.name_problems(["autoflash-recovery-CDEF", "tasmota-ABCDEF-1234"], [], "tasmota")
        self.assertEqual(final, "tasmota-ABCDEF-1234")

    def test_names_of_earlier_releases_are_allowed(self):
        names = ["sonoff-s60-abcdef", "s60-ota-bridge", "s60-recovery-0042", "x0042", "sonoff-s60-mover-abcdef",
                 "tasmota-abcdef"]
        self.assertEqual(fs.name_problems(names, ["Sonoff-Setup-CDEF", "S60-OTA-Bridge-CDEF"], "tasmota"),
                         ([], None))


class TunerTests(unittest.TestCase):
    PART_WAY = "FAILED: the installer was not fully delivered\n"
    NEVER_ASKED = "Device did not connect; no firmware bytes were transferred.\n"

    def finish(self, tuner, verdict, text=""):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            log.write_text(text, encoding="utf-8")
            with mock.patch.object(tuner, "note"):
                tuner.finished({"id": "c2", "route": "sonoff>tasmota", "step": "C2", "fault": "outage",
                                "verdict": verdict}, log, tuner.changed + 1)

    def tuner_with_no_pause(self):
        with mock.patch.dict(fs.os.environ, {"AUTOFLASH_EMU_CLOUD_COOL_DOWN": "0", "AUTOFLASH_EMU_CLOUD_WORKERS": "6"}):
            return fs.Tuner()

    def test_sends_the_device_never_asked_for_put_the_pause_back(self):
        # Seen against the real cloud, with no pause left: two scenarios failing
        # each other's sends, each after a download given up on, and no trouble noted.
        tuner = self.tuner_with_no_pause()
        for _ in range(2):
            self.finish(tuner, "FAIL", self.PART_WAY + self.NEVER_ASKED)
        self.assertEqual(tuner.cool_down, 5)

    def test_a_download_given_up_on_alone_is_not_the_cloud(self):
        tuner = self.tuner_with_no_pause()
        for _ in range(2):
            self.finish(tuner, "FAIL", self.PART_WAY + "offline in eWeLink\n")
        self.assertEqual(tuner.cool_down, 0)

    def test_trouble_after_the_last_of_the_pause_is_still_noticed(self):
        tuner = self.tuner_with_no_pause()
        for _ in range(fs.Tuner.WINDOW):
            self.finish(tuner, "PASS")
        for _ in range(2):
            tuner.changed = 0
            self.finish(tuner, "FAIL", self.NEVER_ASKED)
        self.assertEqual(tuner.cool_down, 5)


class CloudTurnTests(unittest.TestCase):
    def turns_from_the_start(self, sc):
        monitor = fs.DeviceMonitor(mock.Mock(n=24, state=Path("/nonexistent"), sc=sc, say=lambda _: None))
        monitor.preheld = True
        with mock.patch.object(monitor, "live_run", return_value=None), \
                mock.patch.object(monitor, "hold") as hold, \
                mock.patch.object(fs, "device_internet"), mock.patch.object(fs, "NO_CLOUD", False), \
                mock.patch.object(monitor.stop, "wait", side_effect=lambda _: monitor.stop.set()):
            monitor.watching()
        return [call.args[0] for call in hold.call_args_list]

    def test_a_legacy_run_s_device_is_online_from_the_start_whatever_step_it_stops_at(self):
        # Seen against the real cloud: its turn held and its way out shut, so
        # eWeLink showed the device offline and the old release stopped to ask,
        # long before the step under test.
        self.assertEqual(self.turns_from_the_start({"step": "C3", "fault": "legacy"}), ["from the start"])
        self.assertEqual(self.turns_from_the_start({"step": "start", "fault": "legacy"}), ["from the start"])
        self.assertEqual(self.turns_from_the_start({"step": "C2", "fault": "crash"}), ["from the start"])
        self.assertEqual(self.turns_from_the_start({"step": "C3", "fault": "crash"}), [])

    def hold_while_never_back(self, **hold):
        """A turn whose device never reaches eWeLink; what was said and done."""
        with tempfile.TemporaryDirectory() as state:
            (Path(state) / "logs").mkdir()
            (Path(state) / "logs/uart.log").write_text("", encoding="utf-8")
            said = []
            sc = mock.Mock(n=24, state=Path(state), sc={"step": "C2"}, say=said.append, notes={})
            monitor = fs.DeviceMonitor(sc)
            clock = [1000.0]

            def wait(_):
                clock[0] += 5
            with mock.patch.object(monitor, "live_run", return_value=None), \
                    mock.patch.object(monitor, "run_text", return_value=""), \
                    mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "release"), \
                    mock.patch.object(fs, "cloud_connections", return_value=set()), \
                    mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(monitor.stop, "wait", side_effect=wait):
                monitor.hold("from the start", **hold)
        return said, sc

    def test_a_fresh_device_never_back_at_ewelink_is_switched_off_and_on_once(self):
        said, sc = self.hold_while_never_back(restart_once=True)
        self.assertEqual(sc.ex.call_args_list, [mock.call(fs.DEVICE_CMD, "restart")])
        self.assertTrue(any("switched off and on" in line for line in said))
        self.assertTrue(any("the run goes on" in line for line in said))   # not back after it either
        self.assertTrue(sc.notes["fresh_device_restarted"])

    def test_other_turns_never_switch_the_device(self):
        said, sc = self.hold_while_never_back()
        sc.ex.assert_not_called()
        self.assertTrue(any("the run goes on" in line for line in said))

    def test_the_turn_from_the_start_may_switch_its_device(self):
        monitor = fs.DeviceMonitor(mock.Mock(n=24, state=Path("/nonexistent"), sc={"step": "C2", "fault": "crash"},
                                           say=lambda _: None))
        monitor.preheld = True
        with mock.patch.object(monitor, "live_run", return_value=None), \
                mock.patch.object(monitor, "hold") as hold, \
                mock.patch.object(fs, "device_internet"), mock.patch.object(fs, "NO_CLOUD", False), \
                mock.patch.object(monitor.stop, "wait", side_effect=lambda _: monitor.stop.set()):
            monitor.watching()
        self.assertEqual(hold.call_args_list, [mock.call("from the start", restart_once=True)])

    def test_the_installer_sent_again_while_waiting_for_a_turn_is_answered_by_it(self):
        said = []
        monitor = fs.DeviceMonitor(mock.Mock(n=24, state=Path("/nonexistent"), sc={"step": "C3"}, say=said.append))
        text = ["=== run started\n"]
        intent = "[11:31:39] Sending the installer again (attempt %d of 3)\n"
        calls = iter(range(1000))
        waits = iter([False] * 20 + [True] * 1000)   # a bounded wait, whatever the turns

        def live_run():
            if next(calls) == 0:
                return ("run", "=== run started\n")
            return ("run", "".join(text))
        turns = []

        def acquire(*_):
            if not turns:
                text.append(intent % 2)   # sent again while it waits
            return True

        def hold(why, run=None):
            turns.append(why)
            return len(fs.INTENT.findall("".join(text)))
        text.append(intent % 1)
        with mock.patch.object(monitor, "live_run", side_effect=live_run), \
                mock.patch.object(monitor, "hold", side_effect=hold), \
                mock.patch.object(fs, "device_internet"), mock.patch.object(fs, "NO_CLOUD", False), \
                mock.patch.object(fs.CLOUD, "acquire", side_effect=acquire), \
                mock.patch.object(monitor.stop, "wait", side_effect=lambda t: next(waits) and monitor.stop.set()):
            monitor.watching()
        self.assertEqual(turns, ["fallen back"])

    def test_a_turn_starts_again_when_the_installer_is_sent_again_part_way(self):
        # Seen against the real cloud: sent again with 70 s of a turn left, and
        # the device shut off part way through that attempt.
        with tempfile.TemporaryDirectory() as state:
            (Path(state) / "logs").mkdir()
            (Path(state) / "logs/uart.log").write_text("", encoding="utf-8")
            said = []
            monitor = fs.DeviceMonitor(mock.Mock(n=24, state=Path(state), sc={"step": "C3"}, say=said.append))
            text = ["=== run started\n", "Sending the installer again (attempt 1 of 3)\n"]
            clock = [1000.0]

            def wait(_):
                clock[0] += 100
                if clock[0] == 1200:
                    text.append("Sending the installer again (attempt 2 of 3)\n")
            with mock.patch.object(monitor, "live_run", side_effect=lambda: ("run", "".join(text))), \
                    mock.patch.object(monitor, "run_text", return_value=""), \
                    mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "release"), \
                    mock.patch.object(fs, "cloud_connections", return_value=set()), \
                    mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(monitor.stop, "wait", side_effect=wait):
                seen = monitor.hold("fallen back", run="run")
        self.assertEqual(seen, 2)
        self.assertIn("CLOUD: turn renewed (asked again)", said)
        # Held a whole turn past the attempt sent at 1200.
        self.assertGreaterEqual(clock[0], 1200 + fs.HOLD_AT_MOST)


class SavedDeviceTests(unittest.TestCase):
    def stale(self, made, todo):
        # A saved device's name from its route; its firmware as prep recorded it.
        with tempfile.TemporaryDirectory() as emu:
            start = Path(emu) / "saved" / fs.saved("suite-esphome")
            start.mkdir(parents=True)
            if made is not None:
                (start / "firmware.json").write_text(made, encoding="utf-8")
            with mock.patch.object(fs, "EMU", Path(emu)), \
                    mock.patch.object(fs, "installs", return_value={"version": "2"}):
                return fs.stale_starts(todo)

    def test_a_batch_is_told_when_a_saved_device_is_not_on_this_checkout_s_firmware(self):
        # Seen: ESPHome moved on and every esphome> scenario still started from
        # the release before, passing, until the tool began offering it an update.
        todo = [{"route": "esphome>tasmota", "fault": "crash"}]
        name = fs.saved("suite-esphome")
        self.assertEqual(self.stale('{"version": "1"}', todo), [name])
        self.assertEqual(self.stale(None, todo), [name])
        self.assertEqual(self.stale("not json", todo), [name])
        self.assertEqual(self.stale('{"version": "2"}', todo), [])

    def test_a_batch_refuses_to_start_from_one(self):
        with mock.patch.object(fs, "scenarios", return_value=[{"id": "a", "route": "esphome>tasmota", "fault": "crash"}]), \
                mock.patch.object(fs, "latest", return_value={}), mock.patch.object(fs, "LOGS"), \
                mock.patch.object(fs, "stale_starts", return_value=["suite-esphome"]), \
                mock.patch.object(fs, "worker", side_effect=AssertionError("a scenario was run")):
            with self.assertRaisesRegex(SystemExit, "suite-esphome.*Run prep"):
                fs.batch()

    def untrusting(self, saved: dict[str, dict[str, bytes]], rsa: bool, cas: dict[str, bytes]):
        """untrusting_starts over saved devices {name: {path: flash}}, of a device whose
        firmware trusts eWeLink's RSA CA or not, and the stub's CAs {dir: pem}.
        A flash is Sonoff's when it holds b"sonoff", trusting the CAs it holds."""
        with tempfile.TemporaryDirectory() as root:
            emu, repo = Path(root) / "emulation", Path(root) / "repo"
            source = repo / "captures/devices/12-34-56-ab-cd-ef/sonoff-backup/flash.bin"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"rsa" if rsa else b"ec")
            for name, pem in cas.items():
                (emu / name).mkdir(parents=True)
                (emu / name / "ca.crt").write_bytes(pem)
            for name, files in saved.items():
                for path, flash in files.items():
                    (emu / "saved" / name / path).parent.mkdir(parents=True, exist_ok=True)
                    (emu / "saved" / name / path).write_bytes(flash)
            with mock.patch.object(fs, "EMU", emu), mock.patch.object(fs, "REPO", repo), \
                    mock.patch.object(fs, "MAC", "12:34:56:ab:cd:ef"), mock.patch.object(fs, "STUB_CLOUD", True), \
                    mock.patch.object(fs, "holds_rsa_ca", side_effect=lambda flash: flash == b"rsa"), \
                    mock.patch.object(fs, "trusts", side_effect=lambda flash, pem:
                                      None if b"sonoff" not in flash else pem is not None and pem in flash):
                return fs.untrusting_starts()

    def test_a_saved_device_trusting_a_stub_ca_made_before_the_one_there_now_is_found(self):
        # Seen: the stub's RSA CA made again the day after the S60's saved devices,
        # whose Sonoff backups turned the stub down (unknown_ca), so a run back
        # to Sonoff's failed its checks.
        backup = "captures/devices/12-34-56-ab-cd-ef/sonoff-backup/flash.bin"
        saved = {"stub-suite-tasmota": {"flash.bin": b"tasmota", backup: b"sonoff OLD"},
                 "stub-suite-stockC2-tasmota": {"flash.bin": b"sonoff NEW"},
                 "stub-suite-esphome": {"flash.bin": b"esphome", backup: b"sonoff NEW"},
                 "stub-suite-tasmota-nobackup": {"flash.bin": b"tasmota"}}
        self.assertEqual(self.untrusting(saved, True, {"stub-cloud-rsa": b"NEW", "stub-cloud": b"EC"}),
                         ["stub-suite-tasmota"])

    def test_one_trusting_the_other_kind_of_ca_is_found_too(self):
        # The S60's update devices, made before emu.sh gave its firmware an RSA CA.
        saved = {"stub-suite-tasmota-earlier": {"captures/devices/12-34-56-ab-cd-ef/sonoff-backup/flash.bin":
                                                b"sonoff EC"}}
        self.assertEqual(self.untrusting(saved, True, {"stub-cloud-rsa": b"RSA", "stub-cloud": b"EC"}),
                         ["stub-suite-tasmota-earlier"])
        self.assertEqual(self.untrusting(saved, False, {"stub-cloud-rsa": b"RSA", "stub-cloud": b"EC"}), [])

    def test_with_no_stub_ca_yet_every_saved_sonoff_firmware_is_found(self):
        # emu.sh makes one afresh, which none of them trusts.
        self.assertEqual(self.untrusting({"stub-suite-stockC2-tasmota": {"flash.bin": b"sonoff EC"}}, False, {}),
                         ["stub-suite-stockC2-tasmota"])

    def test_another_devices_and_the_real_clouds_saved_devices_are_left_out(self):
        backup = "captures/devices/{}/sonoff-backup/flash.bin"
        saved = {"stub-suite-tasmota": {backup.format("12-34-56-ab-cd-ef"): b"sonoff EC"},
                 "stub-suite-tasmota-2d01": {backup.format("12-34-56-e3-2d-01"): b"sonoff OLD"},
                 "suite-tasmota": {backup.format("12-34-56-ab-cd-ef"): b"sonoff COOLKIT"}}
        self.assertEqual(self.untrusting(saved, False, {"stub-cloud": b"EC"}), [])

    def test_a_stub_batch_refuses_to_start_from_one(self):
        with mock.patch.object(fs, "scenarios", return_value=[{"id": "a", "route": "tasmota>sonoff", "fault": "crash"}]), \
                mock.patch.object(fs, "latest", return_value={}), mock.patch.object(fs, "LOGS"), \
                mock.patch.object(fs, "stale_starts", return_value=[]), mock.patch.object(fs, "STUB_CLOUD", True), \
                mock.patch.object(fs, "untrusting_starts", return_value=["stub-suite-tasmota"]), \
                mock.patch.object(fs, "worker", side_effect=AssertionError("a scenario was run")):
            with self.assertRaisesRegex(SystemExit, "stub-suite-tasmota.*Run prep"):
                fs.batch()

    def test_the_connection_log_says_each_connection_once_up_and_once_gone(self):
        session = "ipv4 2 tcp 6 300 ESTABLISHED src=192.168.60.50 dst=192.0.2.81 sport=50280 dport=443 "
        closing = "ipv4 2 tcp 6 10 CLOSE src=192.168.60.50 dst=192.0.2.81 sport=50280 dport=443 "
        fetched = "ipv4 2 tcp 6 120 TIME_WAIT src=192.168.60.50 dst=192.168.60.1 sport=1 dport=8089 "
        seen, said = {}, []
        for at, conntrack in (("1", session), ("2", session + "\n" + fetched), ("3", closing), ("4", closing),
                              ("5", "")):
            said += fs.connection_changes(seen, "autoflash-emu-1", conntrack, at)
        self.assertEqual(said, ["1 autoflash-emu-1 ESTABLISHED 192.0.2.81:443", "3 autoflash-emu-1 CLOSE 192.0.2.81:443"])
        self.assertEqual(seen, {})

    def test_the_real_cloud_keeps_where_ewelink_is_under_test(self):
        def kind(route, step, fault, arg):
            return fs.kind_of({"route": route, "step": step, "fault": fault, "arg": arg})
        self.assertEqual(kind("sonoff>tasmota", "C2", "outage", 120), "cloud")    # every length at C2
        self.assertEqual(kind("sonoff>esphome", "C2", "token", "login"), "cloud")
        self.assertEqual(kind("sonoff>tasmota", "C3fb", "deviceoff", 180), "cloud")
        self.assertEqual(kind("sonoff>tasmota", "C3fb", "outage", 30), "stub")
        self.assertEqual(kind("sonoff>tasmota", "C3", "deviceoff", 1), "cloud")     # sent again through it
        self.assertEqual(kind("sonoff>esphome", "D1", "crash", 60), "stub")
        self.assertEqual(kind("sonoff>tasmota", "C2", "legacy", "v1.2.0"), "cloud")
        self.assertEqual(kind("sonoff>tasmota", "G1", "legacy", "v1.2.0"), "stub")
        self.assertEqual(kind("tasmota>esphome", "start", "legacy", "v2.0.2"), "stub")
        self.assertEqual(kind("tasmota>esphome", "F4", "crash", 1), "any")

    def test_a_real_cloud_batch_leaves_the_stub_only_scenarios_out(self):
        rows = [{"id": "c", "route": "sonoff>tasmota", "step": "C2", "fault": "crash", "arg": 1},
                {"id": "s", "route": "sonoff>tasmota", "step": "D1", "fault": "crash", "arg": 60}]
        ran, said = [], []

        def worker(n, todo, lock, cloud, *rest):
            while not todo.empty():
                ran.append(todo.get()["id"])
        with mock.patch.object(fs, "scenarios", return_value=rows), mock.patch.object(fs, "latest", return_value={}), \
                mock.patch.object(fs, "LOGS"), mock.patch.object(fs, "stale_starts", return_value=[]), \
                mock.patch.object(fs, "STUB_CLOUD", False), mock.patch.object(fs, "NO_CLOUD", False), \
                mock.patch.dict(fs.os.environ, {"AUTOFLASH_EMU_CONNECTION_LOG": "0"}), \
                mock.patch.object(fs, "cloud_workers", return_value=1), mock.patch.object(fs, "worker", worker), \
                mock.patch.object(fs.time, "sleep"), mock.patch.object(fs, "report"), \
                mock.patch.object(fs.scenario_times, "times", return_value={}), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: said.append(" ".join(map(str, a)))):
            fs.batch()
        self.assertEqual(ran, ["c"])
        self.assertTrue(any("1 left to the stub" in line for line in said))

    def test_share_writes_each_machines_share_from_the_times_kept_here_and_given(self):
        with tempfile.TemporaryDirectory() as directory:
            ids = Path(directory) / "all.txt"
            ids.write_text("a any\nb any\n", encoding="utf-8")
            rows = [{"id": "a"}, {"id": "b"}]
            emulation = Path(directory, "emulation")
            Path(emulation, "kept", "second").mkdir(parents=True)
            with mock.patch.object(fs, "scenarios", return_value=rows), mock.patch.object(fs, "EMU", emulation), \
                    mock.patch.object(fs.scenario_times, "times", return_value={"a": 60, "b": 60}) as took, \
                    mock.patch.object(fs.sys, "argv", ["fault_suite.py", "share", str(ids), "1", "1",
                                                       "--times-from", "elsewhere"]), \
                    mock.patch("builtins.print"):
                fs.main()
            # Its own folders, those kept from another machine, and those given.
            self.assertEqual(took.call_args.args, (emulation, emulation / "kept" / "second", Path("elsewhere")))
            self.assertEqual([Path(f"{ids}.{n}").read_text(encoding="utf-8") for n in (1, 2)], ["a\n", "b\n"])

    def test_a_scenario_waits_for_room_before_it_starts(self):
        import queue
        import threading
        todo = queue.Queue()
        todo.put({"id": "a", "route": "sonoff>tasmota", "step": "D1", "fault": "crash", "arg": 60})
        events = []

        class Started:
            def __init__(self, *args):
                events.append("run")

            def go(self):
                return {"verdict": "PASS"}
        with tempfile.TemporaryDirectory() as out, \
                mock.patch.object(fs, "OUT", Path(out)), mock.patch.object(fs, "LOGS", Path(out)), \
                mock.patch.object(fs, "memory_now", return_value=(fs.LOW_MEMORY_KB * 2, None)), \
                mock.patch.object(fs.cpu_load, "wait_for_room", side_effect=lambda *a: events.append("wait")), \
                mock.patch.object(fs, "Scenario", Started), mock.patch.object(fs, "TUNER"), \
                mock.patch("builtins.print"):
            fs.worker(1, todo, threading.Lock())
        self.assertEqual(events, ["wait", "run"])

    def test_a_batch_starts_the_longest_first(self):
        rows = [{"id": i, "route": "sonoff>tasmota", "step": "D1", "fault": "crash", "arg": 60} for i in "abc"]
        ran = []

        def worker(n, todo, lock, cloud, *rest):
            while not todo.empty():
                ran.append(todo.get()["id"])
        with mock.patch.object(fs, "scenarios", return_value=rows), mock.patch.object(fs, "latest", return_value={}), \
                mock.patch.object(fs, "LOGS"), mock.patch.object(fs, "stale_starts", return_value=[]), \
                mock.patch.object(fs, "untrusting_starts", return_value=[]), \
                mock.patch.object(fs, "STUB_CLOUD", True), mock.patch.object(fs, "worker", worker), \
                mock.patch.dict(fs.os.environ, {"AUTOFLASH_EMU_STUB_WORKERS": "1"}), \
                mock.patch.object(fs.time, "sleep"), mock.patch.object(fs, "report"), \
                mock.patch.object(fs.scenario_times, "times", return_value={"a": 60, "b": 1800, "c": 300}) as took, \
                mock.patch.object(fs, "suite_roots", return_value=[Path("here"), Path("kept")]), \
                mock.patch("builtins.print"):
            fs.batch()
        self.assertEqual(ran, ["b", "c", "a"])
        self.assertEqual(took.call_args.args, (Path("here"), Path("kept")))

    def test_runs_that_start_from_no_converted_device_ask_nothing_of_it(self):
        self.assertEqual(self.stale('{"version": "1"}', [{"route": "sonoff>esphome", "fault": "crash"},
                                                         {"route": "esphome>sonoff", "fault": "legacy"}]), [])

    def test_a_starting_device_is_kept_only_when_its_run_got_there(self):
        # All six once listed as saved, each run stopped at "Discovering the device".
        stopped_early = "Stage  2 - Discovering the device - Stopped\nExit code 130"
        self.assertFalse(fs.prepared(stopped_early, None, False))
        self.assertFalse(fs.prepared(stopped_early, "C2", False))
        self.assertTrue(fs.prepared("     Result:               Converted\nExit code 0", None, False))
        self.assertTrue(fs.prepared("Stage  7 - Backing up Sonoff firmware - Stopped", "C2", True))

    def saved_as(self, owner, mac):
        with tempfile.TemporaryDirectory() as emu:
            if owner:
                (Path(emu) / "saved/stub-suite-tasmota/captures/devices" / owner).mkdir(parents=True)
            with mock.patch.object(fs, "EMU", Path(emu)), mock.patch.object(fs, "STUB_CLOUD", True), \
                 mock.patch.object(fs, "MAC", mac):
                return fs.saved("suite-tasmota")

    def test_the_device_that_saved_it_has_it(self):
        self.assertEqual(self.saved_as("12-34-56-ab-cd-ef", MAC), "stub-suite-tasmota")

    def test_another_device_has_its_own_beside_it(self):
        self.assertEqual(self.saved_as("12-34-56-ab-cd-ef", "12:34:56:00:11:22"), "stub-suite-tasmota-1122")

    def test_one_not_saved_yet_is_the_first_devices(self):
        self.assertEqual(self.saved_as(None, MAC), "stub-suite-tasmota")


class RunWordsTests(unittest.TestCase):
    """The lines of a run the suite times its faults by, as the tool says them
    now: reworded, a fault would never fire there (NOT-FIRED)."""

    def test_c3_starts_at_the_line_the_backup_begins_with(self):
        import re
        source = (Path(__file__).resolve().parents[3] / "tools/autoflash/routes/espressif/installer.py")
        self.assertRegex(source.read_text(encoding="utf-8"), re.compile(fs.STARTS["C3"][0]))
        self.assertRegex("Backing up the device's Sonoff firmware - about a minute...", fs.STARTS["C3"][0])  # legacy

    def test_each_line_a_fault_waits_for_is_in_the_tools_code(self):
        # ownnet-dropsetup went NOT-FIRED in a whole run once its line was
        # shortened ("Sending the Wi-Fi details" no longer said).
        code = "\n".join(path.read_text(encoding="utf-8") for path in
                         (Path(__file__).resolve().parents[3] / "tools/autoflash").rglob("*.py"))
        for name, words in fs.SAID.items():
            with self.subTest(name):
                self.assertIn(words, code)

    def test_the_installer_sent_again_asks_for_a_turn_as_said_now_and_before_3_0_0(self):
        # Against the real cloud, once the line began "sending" in lower case,
        # no turn came for it: the device stayed shut off eWeLink, the send was
        # refused as offline, and prep and every C2 scenario after it stopped.
        for said in ("[14:47:27]     Back on its Sonoff firmware: sending the installer again (attempt 1 of 3).",
                     "[11:31:39] Sending the installer again (attempt 1 of 3)"):
            with self.subTest(said):
                self.assertTrue(fs.INTENT.search(said))
                self.assertTrue(fs.SENT.search(said))

    def test_the_person_switches_the_device_off_and_on_when_told_as_said_now_and_before_3_0_0(self):
        for said in ("[10:00:00]     No answer from the device: switch it off and on - it is safe now.",
                     "[10:00:00]     after a Wi-Fi outage, Sonoff's firmware can stay off it - switch it off and on:\n"
                     "[10:00:00]     at this stage that is safe. This carries on by itself."):
            with self.subTest(said):
                self.assertTrue(fs.SWITCH_NOW.search(said))

    def test_f4_without_the_driver_is_told_by_what_its_reset_says(self):
        from types import SimpleNamespace
        from tools.autoflash import output
        from tools.autoflash.routes.espressif import tasmota
        from tests.autoflash.routes.espressif import test_tasmota
        # A Tasmota that does not take the name and template at the reset's save.
        device = SimpleNamespace(wait_reachable=lambda **_: None, started_at=lambda: "t",
                                 uptime_seconds=lambda: 10, wait_for_reboot=lambda **_: 1.0,
                                 command=test_tasmota.ConfigureStepTests.console([]), is_safeboot=lambda: False,
                                 at_next_save=lambda commands, timeout=None: False)
        state = SimpleNamespace(input=lambda key: {"product_model": "S60TPF"}.get(key))
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"), device=device, state=state)
        with mock.patch.object(output, "say") as said:
            tasmota.step_configure(ctx)
        self.assertRegex(" ".join(str(c.args[0]) for c in said.call_args_list), fs.NOHOOK_RESET)


class EventFollowingTests(unittest.TestCase):
    """A run of this checkout's tool followed by its events (output.EVENTS), not
    its words; an earlier release's, which writes none, by its words."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name, "runs/1/run.log")
        self.log.parent.mkdir(parents=True)
        self.sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "C3", "fault": "outage",
                                   "arg": 30, "at": 40}, "/dev/null")

    def write(self, *items):
        (self.log.parent / output.EVENTS).write_text(
            "".join(json.dumps({"at": 0, "event": name, **fields}) + "\n" for name, fields in items), encoding="utf-8")

    def logs(self, text):
        return mock.patch.object(fs, "run_logs", return_value=[(1.0, self.log, text)])

    def test_a_step_or_point_is_reached_by_the_runs_events(self):
        self.write(("run", {}), ("began", {"step": "C3"}), ("phase", {"phase": "backup"}))
        with self.logs("=== run started\n[1] words the tool no longer says\n"):
            self.assertTrue(self.sc.reached(0, None, ("began", "C3"), r"\] C3  "))
            self.assertTrue(self.sc.reached(0, None, fs.STARTS_AT["C3"], fs.STARTS["C3"][0]))
            self.assertFalse(self.sc.reached(0, None, ("done", "C3"), r"words"))   # its words do not count

    def test_only_the_runs_own_events_count_once_another_has_begun(self):
        self.write(("run", {}), ("began", {"step": "C3"}), ("done", {"step": "C3"}), ("ended", {}),
                   ("run", {}), ("began", {"step": "C3"}))
        with self.logs("=== run started\n=== run started\n"):
            self.assertFalse(self.sc.reached(0, None, ("done", "C3"), r"\] C3 done"))

    def test_an_earlier_releases_run_is_reached_by_its_words(self):
        with self.logs("=== run started\n[1] C3  Backing up\n"):
            self.assertTrue(self.sc.reached(0, None, ("began", "C3"), r"\] C3  "))
            self.assertFalse(self.sc.reached(0, None, ("done", "C3"), r"\] C3 done"))

    def test_what_a_run_asks_of_the_person(self):
        self.write(("run", {}), ("join", {"network": "tasmota-ABCDEF-1234"}),
                   ("switch", {"now": True}), ("switch", {"now": True, "key": True}), ("home", {}), ("ended", {}))
        self.assertEqual(fs.run_asks(self.log, "words"),
                         {"from": "events", "joins": ["tasmota-ABCDEF-1234"], "backs": 1, "switch_now": 1,
                          "readout_switch": 1, "ended": True})
        (self.log.parent / output.EVENTS).unlink()
        said = ("[1] In the menu, join:\n[1]     tasmota-ABCDEF-1234      (open - no password)\n"
                "[1] -- Back to your usual Wi-Fi\n[1] === run ended ===\n")
        self.assertEqual(fs.run_asks(self.log, said),
                         {"from": "words", "joins": ["tasmota-ABCDEF-1234"], "backs": 1, "switch_now": 0,
                          "readout_switch": 0, "ended": True})

    def test_advice_to_switch_the_device_off_and_on_by_the_runs_events_or_its_words(self):
        for events, advised in (([("run", {}), ("began", {"step": "D1"}), ("switch", {})], True),
                                ([("run", {}), ("switch", {}), ("began", {"step": "D1"})], False),
                                ([("run", {}), ("began", {"step": "D1"}), ("switch", {"now": True})], False)):
            with self.subTest(events=events):
                self.write(*events)
                with self.logs("switch the device off and on"), \
                        mock.patch.object(self.sc, "last_said", return_value="switch the device off and on"):
                    self.assertEqual(self.sc.advised_to_switch(), advised)
        (self.log.parent / output.EVENTS).unlink()
        with self.logs(""), mock.patch.object(self.sc, "last_said", return_value="... switch it off and on, then"):
            self.assertTrue(self.sc.advised_to_switch())


class ScaledBatchTests(unittest.TestCase):
    def allowed_with(self, text):
        with tempfile.TemporaryDirectory() as out:
            if text is not None:
                (Path(out) / "workers").write_text(text)
            with mock.patch.object(fs, "OUT", Path(out)):
                return fs.allowed(4)

    def test_the_number_in_the_file_while_it_is_there(self):
        self.assertEqual(self.allowed_with("6\n"), 6)
        self.assertEqual(self.allowed_with("0"), 0)

    def test_how_many_it_began_with_otherwise(self):
        self.assertEqual(self.allowed_with(None), 4)
        self.assertEqual(self.allowed_with("lots"), 4)

    def test_a_worker_held_back_stops_once_nothing_is_left(self):
        todo = fs.queue.Queue()
        with mock.patch.object(fs.time, "sleep", side_effect=AssertionError("waited")):
            fs.worker(5, todo, None, room=lambda n: False)


class ScenarioTests(unittest.TestCase):
    def test_batch_takes_the_file_list_writes(self):
        # `list > all.txt` then `batch all.txt`, as docs/testing.md runs them:
        # each line an id and whether it needs the cloud.
        with tempfile.TemporaryDirectory() as tmp:
            listed = Path(tmp, "all.txt")
            listed.write_text("sonoff>tasmota:C3:outage-5 cloud\ntasmota>esphome:L1:outage-5 any\n\n",
                              encoding="utf-8")
            self.assertEqual(fs.read_ids(listed), ["sonoff>tasmota:C3:outage-5", "tasmota>esphome:L1:outage-5"])

    def test_every_scenario_has_its_own_id(self):
        ids = [sc["id"] for sc in fs.scenarios()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_tasmota_update_has_its_own_faults(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for wanted in ("update>tasmota:U5:deviceoff-1", "update>tasmota:U5@90:deviceoff-1", "update>tasmota:U5:double-deviceoff-1",
                       "update>tasmota:U5:cancel-switch", "update>tasmota:U6:outage-30", "update>tasmota:U6:crash-1"):
            self.assertIn(wanted, ids)

    def test_the_esphome_update_has_its_own_faults(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for wanted in ("update>esphome:U1:deviceoff-1", "update>esphome:U1:outage-30", "update>esphome:U2:crash-1"):
            self.assertIn(wanted, ids)

    def test_a_fault_this_computers_network_kept_from_happening_is_its_own_verdict(self):
        # Seen: outage-60 counted as NOT-FIRED twice, this computer's lookup of
        # eWeLink having failed before the fault, nothing of the device's.
        offline = ("no report (stopped at stage 2, Discovering the device: eWeLink is unreachable (eWeLink "
                   "connection failed: [Errno -2] Name or service not known): listing every device that answers.)")
        self.assertEqual(fs.no_internet("NOT-FIRED", ["no report", offline]), "NO-INTERNET")
        self.assertEqual(fs.no_internet("FAIL", [offline]), "FAIL")              # pcoff: cut off on purpose
        self.assertEqual(fs.no_internet("NOT-FIRED", ["no report (stopped at stage 3, Discovering the device: "
                                                      "eWeLink shows it offline)"]), "NOT-FIRED")

    def test_the_tuner_does_not_blame_the_cloud_for_this_computers_network(self):
        tuner = fs.Tuner()
        tuner.phase, tuner.changed = "pause-up", 0
        row = {"id": "x", "route": "sonoff>tasmota", "step": "C2", "fault": "outage", "verdict": "NO-INTERNET"}
        with mock.patch.object(fs, "STUB_CLOUD", False):
            tuner.finished(row, "/nonexistent", 1)
        self.assertEqual(list(tuner.recent), [])

    def no_report(self, screen):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            log.write_text("--- run\n" + screen, encoding="utf-8")
            return fs.Scenario.screen_result(mock.Mock(log=log))

    def test_a_run_with_no_report_says_where_it_stopped_and_why(self):
        # Seen: "no report" ten times on the real cloud, and why only on screen -
        # once this computer's own way to eWeLink failing, not the device.
        self.assertEqual(self.no_report(
            " Stage  2 - Discovering the device       - Underway\n"
            "     eWeLink is unreachable (eWeLink connection failed: [Errno -2] Name or service not known):\n"
            "     listing every device that answers.\n"
            " Stage  2 - Discovering the device       - Stopped\n"
            " Stopped as asked. Run this again to carry on.\n"
            "Exit code 130 - stopped at a question, or by Ctrl-C\n"),
            "no report (stopped at stage 2, Discovering the device: eWeLink is unreachable (eWeLink connection"
            " failed: [Errno -2] Name or service not known): listing every device that answers.; exit 130)")

    def test_a_question_an_unattended_run_cannot_answer_is_named(self):
        self.assertEqual(self.no_report(
            " Stage  3 of 15 - Discovering the device        - Underway 22s\n\n"
            " -- Your S60 did not answer on this network\n"
            "     - 'S60TPG': eWeLink shows it offline - check it is switched on at the wall.\n"
            "     1. Look again\n     4. Quit - stop here\n\n"
            " ! An unattended run cannot answer this, so it stops here:\n     What now\n"
            " Stage  3 of 15 - Discovering the device        - Stopped\n"),
            "no report (stopped at stage 3, Discovering the device: Your S60 did not answer on this network "
            "'S60TPG': eWeLink shows it offline - check it is switched on at the wall. What now)")

    def test_an_update_counts_as_having_got_there(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            log.write_text("--- run\n     Result:               Updated\n", encoding="utf-8")
            result = fs.Scenario.screen_result(mock.Mock(log=log))
        self.assertIn("Converted", result)

    def test_the_result_is_read_up_to_the_column_beside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            log.write_text("--- run\n    Result:        Converted                        Firmware: Tasmota\n",
                           encoding="utf-8")
            self.assertEqual(fs.Scenario.screen_result(mock.Mock(log=log, state=Path(directory))), "Converted")

    def test_the_result_is_the_one_the_harness_says_from_the_runs_log(self):
        # The screen says how it ended above the report, which has no Result;
        # unattended_run says the log's, in full.
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            log.write_text("--- run\n FAILED: it went quiet\n========\n"
                           "Result: Stopped at stage 6 of 15 (Installing Tasmota): it went quiet\n"
                           "Exit code 2 - failed\n", encoding="utf-8")
            self.assertEqual(fs.Scenario.screen_result(mock.Mock(log=log, state=Path(directory))),
                             "Stopped at stage 6 of 15 (Installing Tasmota): it went quiet")

    def test_a_run_over_before_its_fault_waits_for_all_of_it(self):
        self.assertGreater(fs.fault_wait(180), 180)
        self.assertEqual(fs.fault_wait("once"), 120)

    def test_an_updates_earlier_release_is_never_stale(self):
        with mock.patch.object(fs, "converted_start", side_effect=AssertionError("asked")):
            self.assertEqual(fs.stale_starts([{"route": "update>tasmota", "fault": "deviceoff"},
                                               {"route": "update>esphome", "fault": "deviceoff"}]), [])

    def test_the_composite_faults_are_listed(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for wanted in ("sonoff>tasmota:C3:ownnet-cut", "sonoff>esphome:D1:lostanswer-once",
                       "sonoff>tasmota:D1:seven-advised", "esphome>tasmota:G1:seven-times",
                       "sonoff>tasmota:F4:nohook-run", "esphome>tasmota:F4rs@90:deviceoff-1"):
            self.assertIn(wanted, ids)

    def test_a_routine_run_has_the_outages_only_at_their_shortest_and_longest(self):
        every = {sc["id"] for sc in fs.scenarios()}
        quick = {sc["id"] for sc in fs.scenarios(quick=True)}
        self.assertFalse([i for i in every if i.endswith(":outage-60")])
        self.assertEqual({i for i in every - quick},
                         {i for i in every if any(i.endswith(f":outage-{s}") for s in (15, 30, 120))
                          and "@" not in i and ">readout:" not in i})
        self.assertIn("tasmota>sonoff:D4:outage-5", quick)
        self.assertIn("sonoff>esphome:C2:outage-180", quick)
        # Not the step faults' outages: those at 10% and 90%, and the readout's.
        self.assertTrue([i for i in quick if "@" in i and i.endswith(":outage-30")])

    def test_found_says_what_each_fault_found_here_and_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            emulation = Path(directory)
            for folder, row in ((emulation / "suite-stub", {"id": "a>b:C3:outage-15", "verdict": "FAIL-CHECKS"}),
                                (emulation / "kept" / "second" / "suite-stub",
                                 {"id": "a>b:C3:outage-180", "verdict": "PASS"})):
                folder.mkdir(parents=True)
                (folder / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            with mock.patch.object(fs, "EMU", emulation), mock.patch.object(fs.sys, "argv", ["fault_suite.py", "found"]), \
                    mock.patch("builtins.print") as said:
                fs.main()
        self.assertEqual([call.args[0].split()[:4] for call in said.call_args_list],
                         [["outage-15", "1", "run", "1"], ["outage-180", "1", "run", "0"]])

    def test_each_length_only_where_it_leads_somewhere_of_its_own(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        self.assertFalse([i for i in ids if i.endswith(":outage-1")])
        self.assertIn("tasmota>sonoff:D4:outage-15", ids)
        # A rerun 60 s after a crash: only where a fallback network may come up.
        self.assertIn("tasmota>esphome:L1:crash-60", ids)
        self.assertIn("sonoff>tasmota:C3fb:crash-60", ids)
        self.assertIn("sonoff>esphome:C2:crash-60", ids)
        self.assertNotIn("tasmota>sonoff:D4:crash-60", ids)

    def test_a_fault_at_90_percent_of_r1_with_no_backup_comes_inside_it(self):
        # R1 putting Sonoff's firmware and the kept identity took 53 s in the
        # emulator; estimated at 60 s, its fault at 90% came a second after (MISTIMED).
        at = fs.ROUTES["nobackup>sonoff"][2]["R1"] * 90 / 100
        self.assertLess(at, 53 - 3)

    def test_a_fault_at_90_percent_of_u5_comes_inside_it(self):
        # U5 took 37 s in the emulator (2026-11-16); estimated at 44 s, its fault
        # at 90% came 3 s after it had ended (MISTIMED), on every computer.
        at = fs.ROUTES["update>tasmota"][2]["U5"] * 90 / 100
        self.assertLess(at, 37 - 2)

    def test_a_packet_is_changed_only_where_a_step_sends_one(self):
        # R2 only sees Sonoff's LAN port take a connection: no packet there
        # carries data, and corrupt-once was always NO-TRAFFIC.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route in ("tasmota>sonoff", "esphome>sonoff"):
            self.assertNotIn(f"{route}:R2:corrupt-once", ids)
            self.assertIn(f"{route}:R2:outage-5", ids)
            self.assertIn(f"{route}:R1:corrupt-once", ids)
        self.assertIn("sonoff>tasmota:H1:corrupt-once", ids)
        # Nor in an ESPHome update's wait while ESPHome confirms itself.
        self.assertNotIn("update>esphome:U2:corrupt-once", ids)
        self.assertIn("update>esphome:U1:corrupt-once", ids)
        self.assertIn("update>tasmota:U6:corrupt-once", ids)

    def test_the_tools_own_handling_at_the_riskiest_steps(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for fault in ("second-run", "ctrlc-countdown", "ctrlc-apart", "sleep-120"):
            self.assertIn(f"esphome>tasmota:F4:{fault}", ids)
            self.assertIn(f"sonoff>tasmota:C2:{fault}", ids)
            self.assertNotIn(f"esphome>tasmota:E1:{fault}", ids)
        self.assertIn("tasmota>esphome:M1:sleep-120", ids)
        self.assertNotIn("tasmota>esphome:M1:second-run", ids)
        self.assertIn("esphome>tasmota:E1:hangup-continue", ids)

    def test_runs_left_by_1_2_0_are_carried_on(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for step in ("C2", "C3", "D1", "D4", "E1", "E2", "E3", "F2", "F4", "G1", "H1"):
            self.assertIn(f"sonoff>tasmota:{step}:legacy-v1.2.0", ids)
        self.assertIn("tasmota>esphome:start:legacy-v1.2.0", ids)
        self.assertIn("tasmota>sonoff:start:legacy-v1.2.0", ids)
        # G1 over that release's startup firmware, unproven, cut off part way.
        self.assertIn("sonoff>tasmota:F4:legacy-v1.2.0+G1off", ids)
        self.assertTrue(fs.needs_cloud({"route": "tasmota>esphome", "step": "start", "fault": "legacy"}))

    def test_runs_left_by_the_release_before_a_firmware_update_are_carried_on(self):
        # Its bundle pinned to the firmware the tool installed then
        # (docs/updating-firmware.md): every step from Sonoff, both ways, and
        # each kind of device it converted.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route, steps in fs.LEGACY_STEPS["v2.0.2"].items():
            self.assertEqual(set(steps), {fs.STEP_OF.get(step, step) for step in fs.ROUTES[route][2]} - {"K1"} | {"C2"})
            for step in steps:
                self.assertIn(f"{route}:{step}:legacy-v2.0.2", ids)
        for route in ("tasmota>esphome", "tasmota>sonoff", "esphome>tasmota", "esphome>sonoff"):
            self.assertIn(f"{route}:start:legacy-v2.0.2", ids)
        self.assertNotIn("sonoff>tasmota:F4:legacy-v2.0.2+G1off", ids)

    def test_phases_must_ride_out_blips_by_themselves(self):
        # PR #5: one early blip used up a phase's 20 s of retrying, and a later
        # one failed on its first try - the step then tried again from the start.
        ids = {sc["id"] for sc in fs.scenarios()}
        for route, step in fs.BLIP_STEPS:
            self.assertIn(f"{route}:{step}@10:blips-3", ids)
            self.assertIn(f"{route}:{step}@10:lossy-phase", ids)
        # Two blips 80% of the step apart: more than a phase's 20 s of retrying
        # in the longer ones, which PR #5's window, counted from the first
        # failure, ran out of; E2, G1 and L1 are shorter than that from 2.0.2.
        apart = {step: 0.8 * fs.ROUTES[route][2][step] for route, step in fs.BLIP_STEPS}
        self.assertGreaterEqual(sum(gap > 20 for gap in apart.values()), 2, apart)

    def test_how_busy_each_phase_kept_the_device(self):
        import json
        import os
        import time
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for name, captures, age in (("new", {"E2.device_busy_seconds": 31.2, "G1.device_busy_seconds": 12,
                                                 "E2.seconds": 40}, 0),
                                        ("again", {"E2.device_busy_seconds": 44.0}, 0),
                                        ("old", {"E2.device_busy_seconds": 99.0}, 3600)):
                run = state / "work/captures/devices/12-34-56-ab-cd-ef/runs" / name
                run.mkdir(parents=True)
                path = run / "autoflash-state.json"
                path.write_text(json.dumps({"captures": captures}))
                os.utime(path, (time.time() - age, time.time() - age))
            self.assertEqual(fs.device_busy(state, time.time() - 60), {"E2": 44.0, "G1": 12})

    def test_an_esphome_factory_image_as_its_builder_makes_one(self):
        image = fs.esphome_factory_image()
        self.assertEqual(image[0], 0xE9)  # all Tasmota's page checks of it, with the size
        self.assertEqual(image[0x8000:0x8002], b"\xaa\x50")  # a partition table
        self.assertEqual(image[0x20000], 0xE9)  # the app, in the first slot
        self.assertIn("tasmota>esphome:start:lostapp-factory", {sc["id"] for sc in fs.scenarios()})

    def test_a_legacy_run_goes_without_firmware_and_the_next_with_it(self):
        with tempfile.TemporaryDirectory() as tree:
            (Path(tree) / "tools").mkdir()
            (Path(tree) / fs.LEGACY_TOOL).touch()
            sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "legacy",
                                  "arg": "v1.2.0", "at": 40}, "/dev/null")
            with mock.patch.object(fs.subprocess, "call", return_value=0) as call:
                sc.run_tool("tasmota", code=tree)
                sc.run_tool("tasmota", code=fs.REPO)
            old, new = (c.args[0] for c in call.call_args_list)
            self.assertIn(f"--code={tree}", old)
            self.assertNotIn("--firmware", old)
            self.assertEqual(new[-2:], ["--firmware", "tasmota"])

    def test_the_advice_beside_a_question_is_followed_though_the_screen_lost_it(self):
        # sonoff>esphome:C2:cancel-switch: eWeLink showed the device offline after
        # its update was cut off, the run said to switch it off and on beside
        # "What now", and stopped there - its last screen without the advice.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "scenario.log")
            log.write_text("--- --instance=4\n Stage 2 - Discovering the device - Stopped\n"
                           " Stopped as asked. Run this again to carry on.\n", encoding="utf-8")
            runs = Path(tmp, "state/work/captures/devices/unidentified/runs/20261103-041854")
            runs.mkdir(parents=True)
            (runs / "run.log").write_text(
                "=== run started ===\n[04:20:00] earlier: switch it off and on\n=== run ended ===\n"
                "=== run started ===\n[04:21:45]     - 'S60' (S60TPG): eWeLink shows it offline - check it is\n"
                "[04:21:45]     switched on. If it is, switch the device off and on: after an update cut off\n"
                "[04:21:45]     What now\n", encoding="utf-8")
            sc = fs.Scenario(24, {"id": "x", "route": "sonoff>esphome", "step": "C2", "fault": "cancel",
                                  "arg": "switch", "at": 40}, str(log))
            sc.state = Path(tmp, "state")
            said = sc.last_said()
        self.assertIn("switch the device off and on", said)
        self.assertNotIn("earlier", said)   # the last run's, not one before it

    def test_a_legacy_tree_is_run_by_its_own_driver(self):
        # unattended-test.sh --code: a release from before the reorganisation
        # keeps its driver, and the tool it starts, directly in tools/.
        import re
        import subprocess
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        choice = re.search(r"^driver=.*\n.*\|\| driver=.*\n", script, re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp, "work")
            (work / "tools/repo").mkdir(parents=True)
            (work / "tools/unattended_run.py").touch()
            for tree, driver in (("old", "tools/unattended_run.py"), ("new", "tools/repo/unattended_run.py")):
                if tree == "new":
                    (work / driver).touch()
                chosen = subprocess.run(["bash", "-c", choice + 'echo "$driver"'], env={"STATE": tmp},
                                        capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(chosen, driver, tree)

    def test_a_run_is_given_the_ewelink_account_and_nothing_else_from_env(self):
        # Seen: a token kept in .env for something else reached the run, which
        # refused the name, and every real-cloud scenario after was NOT-FIRED.
        import re
        import subprocess
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        given = re.search(r"^ *grep .*\"\$ROOT/\.env\"", script, re.M).group(0)
        with tempfile.TemporaryDirectory() as root:
            Path(root, ".env").write_text(
                "EWELINK_EMAIL=someone@example.com\nexport EWELINK_PASSWORD=x\nAUTOFLASH_WIFI_PASSWORD=y\n"
                "AUTOFLASH_BACKUP_KEY=z\nOTHER_TOKEN=t\n", encoding="utf-8")
            out = subprocess.run(["bash", "-c", given], env={"ROOT": root},
                                 capture_output=True, text=True).stdout
        self.assertEqual(out, "EWELINK_EMAIL=someone@example.com\nexport EWELINK_PASSWORD=x\n")

    def test_a_legacy_tree_goes_in_only_once_the_stub_has_started_from_this_checkout(self):
        # Swapped in as the emulator started, a release from before the
        # reorganisation took away what the stub reads the device with: no stub,
        # and every 2.0.2 run stopped at "Connection refused".
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        self.assertLess(script.index("tmux has-session"), script.index('"$code/" "$STATE/work/"'))

    @unittest.skipUnless(all(fs.subprocess.run(["git", "-C", str(fs.REPO), "rev-parse", "-q", "--verify",
                                                f"refs/tags/{tag}"], capture_output=True).returncode == 0
                             for tag in fs.LEGACY_STEPS), "no release tags in this checkout")
    def test_each_legacy_release_takes_its_patch(self):
        # The reorganisation moved 1.2.0's patch to tools/ewelink/, which 1.2.0
        # never had: every 1.2.0 scenario was a HARNESS-ERROR.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(fs, "LEGACY", Path(tmp)):
            for tag in fs.LEGACY_STEPS:
                with self.subTest(tag=tag):
                    fs.legacy_tree(tag)

    def test_the_person_carries_on_when_a_run_log_moves_as_it_looks(self):
        # Seen on a VM: the tool moved a run from unidentified/ under its
        # device's MAC between the person's listing and its look at the log,
        # and the person's thread died with FileNotFoundError.
        import threading
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "cancel",
                              "arg": "new", "at": 40}, "/dev/null")
        sc.started = 0
        stop = threading.Event()
        moved = Path(tempfile.gettempdir()) / "no-such-run" / "run.log"

        def logs(state):
            stop.set()  # one look, then stop
            return [(1.0, moved, "a run")]
        with mock.patch.object(sc, "ex", return_value=mock.Mock(stdout="", returncode=1)), \
                mock.patch.object(fs, "run_logs", side_effect=logs):
            sc.person(stop)

    def scenario(self, fault="outage", arg=60):
        return fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": fault,
                                "arg": arg, "at": 40}, "/dev/null")

    def test_a_router_switch_not_taken_is_noted_and_one_taken_is_not(self):
        sc = self.scenario()
        with mock.patch.object(sc, "ex", return_value=mock.Mock(returncode=0)) as ex:
            sc.router("off")
        ex.assert_called_once_with(fs.DEVICE_CMD, "router", "off", timeout=60)
        self.assertNotIn("not_applied", sc.notes)
        with mock.patch.object(sc, "ex", return_value=mock.Mock(returncode=1)):
            sc.router("off")
        self.assertEqual(sc.notes["not_applied"], [f"{fs.DEVICE_CMD} router off"])

    def test_each_fault_that_cannot_start_is_noted(self):
        for fault, arg in (("deviceoff", 0), ("crash", "1"), ("cancel", "switch"), ("ctrlc", "once"),
                           ("hangup", "1"), ("sleep", 0), ("pcoff", 0), ("weak", 0), ("lossy", 0),
                           ("diskfull", "1"), ("token", "login"), ("lostanswer", "once"), ("seven", "times")):
            with self.subTest(fault=fault):
                sc = self.scenario(fault, arg)
                sc.step_length = 0
                sc.current_run = mock.Mock(return_value="] D1 done")
                with mock.patch.object(sc, "ex", return_value=mock.Mock(returncode=1, stdout="", stderr="")), \
                        mock.patch.object(fs.time, "sleep"):
                    sc.fault(fault, arg, 0, None)
                self.assertTrue(sc.notes.get("not_applied"))

    def test_a_cancel_that_came_as_the_run_finished_leaves_its_firmware_the_one_to_end_on(self):
        # Seen at K1, a tenth of a second long: the run converted the device to
        # Tasmota as it was cancelled, no run followed for ESPHome, and the
        # device's Tasmota name was taken for a wrong one.
        sc = self.scenario("cancel", "switch")
        sc.target = "tasmota"
        with mock.patch.object(sc, "run_tool") as runs, mock.patch.object(sc, "finish"):
            sc.again([(0, "Converted")], "esphome", [])
        runs.assert_not_called()
        self.assertEqual(sc.target_final, "tasmota")
        with mock.patch.object(sc, "run_tool", return_value=0), mock.patch.object(sc, "finish"), \
                mock.patch.object(sc, "screen_result", return_value="Converted"), \
                mock.patch.object(sc, "last_said", return_value=""), mock.patch.object(fs.Path, "read_text", return_value=""):
            sc.again([(1, "Cancelled")], "esphome", [])
        self.assertEqual(sc.target_final, "esphome")

    def test_the_second_sign_in_goes_where_the_run_does(self):
        for stub_cloud, expected in ((True, True), (False, False)):
            with self.subTest(stub_cloud=stub_cloud):
                sc = self.scenario("token", "login")
                sc.step_length = 0
                sc.current_run = mock.Mock(return_value="] D1 done")
                with mock.patch.object(fs, "STUB_CLOUD", stub_cloud), \
                        mock.patch.object(sc, "ex", return_value=mock.Mock(returncode=0, stdout="logged in again", stderr="")) as ex, \
                        mock.patch.object(fs.time, "sleep"):
                    sc.fault("token", "login", 0, None)
                command = ex.call_args.args[-1]
                self.assertEqual("AUTOFLASH_EWELINK_API_BASE=https://apia.autoflash-stub.test" in command, expected)
                self.assertEqual("SSL_CERT_FILE=/run/autoflash-emu/stub-ca-bundle.pem" in command, expected)

    def monitor(self, tmp, text=""):
        sc = self.scenario()
        sc.state = Path(tmp)
        (Path(tmp) / "logs").mkdir()
        (Path(tmp) / "logs/uart.log").write_text("", encoding="utf-8")
        (Path(tmp) / "work/captures").mkdir(parents=True)
        monitor = fs.DeviceMonitor(sc)
        monitor.run_text = mock.Mock(return_value=text)
        return monitor

    def holding(self, monitor, connections=(), **kwargs):
        with mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "release"), \
                mock.patch.object(fs, "cloud_connections", return_value=set(connections)), \
                mock.patch.object(monitor.sc, "say"):
            return monitor.hold("test", **kwargs)

    def turn_file_after(self, seconds, connections=(), before=()):
        """Whether a waiting run's turn file is there `seconds` into a device's turn."""
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(tmp)
            monitor.turn_file().touch()
            clock = [1000.0]
            monitor.stop = mock.Mock(is_set=mock.Mock(side_effect=lambda: clock[0] > 1000 + seconds))
            monitor.stop.wait = mock.Mock(side_effect=lambda _: clock.__setitem__(0, clock[0] + 1))
            looks = [set(before)]

            def cloud_connections(_n):
                return looks.pop() if looks else {*before, *connections}
            with mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "release"), \
                    mock.patch.object(fs, "cloud_connections", side_effect=cloud_connections), \
                    mock.patch.object(monitor.sc, "say"):
                monitor.hold("test")
            return monitor.turn_file().exists()

    def test_a_run_signs_in_only_once_the_device_is_back_at_ewelink(self):
        # Seen against the real cloud: a run let in at the start of its turn sent
        # its command down the last device's connection, and its device never asked.
        session = ("192.0.2.80", "50280", "443")
        self.assertTrue(self.turn_file_after(5))
        self.assertTrue(self.turn_file_after(fs.SIGNED_ON_AFTER - 1, [session]))
        self.assertFalse(self.turn_file_after(fs.SIGNED_ON_AFTER + 1, [session]))

    def test_a_run_waits_out_a_session_that_may_drop_again(self):
        # Seen on the real cloud: sessions dropped 12 s after they opened, and a
        # run let in at 10 s found its device offline at eWeLink and stopped.
        self.assertTrue(self.turn_file_after(12, [("192.0.2.81", "2", "443")]))
        # Nor does the first connection, to find its server, which lasts 2 s.
        dispatch = ("192.0.2.80", "1", "443")
        self.assertNotIn("device back", " ".join(self.said_in_a_turn([{dispatch}, {dispatch}, set(), set(), set(), set()])))

    def test_a_connection_from_before_the_turn_is_not_the_device_back(self):
        old = ("192.0.2.80", "50280", "443")
        self.assertTrue(self.turn_file_after(fs.SIGNED_ON_AFTER + 1, before=[old]))

    def test_a_device_that_never_gets_back_does_not_keep_the_run_waiting(self):
        self.assertTrue(self.turn_file_after(fs.DEVICE_BACK_AT_MOST - 1))
        self.assertFalse(self.turn_file_after(fs.DEVICE_BACK_AT_MOST + 1))

    def said_waiting(self, waited, got=True):
        """What a run says when a turn takes `waited` seconds to come."""
        clock = [1000.0]

        def acquire(*_):
            clock[0] += waited
            return got
        sc = mock.Mock(n=1)
        with mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                mock.patch.object(fs.CLOUD, "acquire", side_effect=acquire):
            self.assertEqual(fs.wait_for_turn(sc, fs.CLOUD.FRESH, lambda: False), got)
        return [call.args[0] for call in sc.say.call_args_list]

    def test_a_run_that_waited_for_its_turn_says_how_long(self):
        # A run past its sign-in keeps its time limits while it waits, so a
        # NOT-FIRED could be the queue's doing with nothing to show it.
        self.assertEqual(self.said_waiting(95), ["CLOUD: waited 95s for a turn"])

    def test_a_turn_with_no_queue_or_never_given_says_nothing(self):
        self.assertEqual(self.said_waiting(0), [])
        self.assertEqual(self.said_waiting(95, got=False), [])

    def turn_length(self, texts, fetching=lambda second: False):
        """How long a device's turn lasts, connected from its start, its run's log
        at each second being `texts(second)`, and what it said."""
        session = ("192.0.2.81", "2", "443")
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(tmp)
            said = []
            monitor.sc.say = said.append
            clock = [1000.0]
            monitor.run_text = mock.Mock(side_effect=lambda: texts(clock[0] - 1000))
            monitor.stop = mock.Mock(is_set=mock.Mock(return_value=False))
            monitor.stop.wait = mock.Mock(side_effect=lambda _: clock.__setitem__(0, clock[0] + 0.5))
            first = iter([set()])
            with mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "release"), \
                    mock.patch.object(fs, "device_fetching", side_effect=lambda _n: fetching(clock[0] - 1000)), \
                    mock.patch.object(fs, "cloud_connections",
                                      side_effect=lambda _n: next(first, {session})):
                monitor.hold("test")
        return clock[0] - 1000, said

    def test_a_turn_ends_when_a_sent_command_brought_nothing(self):
        # Seen on the real cloud: 3 minutes of a turn, its device back on eWeLink
        # after a power cut, with its command lost and eWeLink not passing it on.
        length, said = self.turn_length(lambda _: "[12:00:00] C2  Stock to recovery bridge")
        self.assertEqual(length, fs.SIGNED_ON_AFTER + fs.TURN_IDLE)
        self.assertIn(f"CLOUD: nothing fetched for {fs.TURN_IDLE:.0f}s; the turn is free", said)

    def test_a_command_an_earlier_run_sent_does_not_end_a_later_runs_turn(self):
        # Seen: a run cancelled after its send, then one with --new whose turn to
        # find the device ended a minute in, its device cut off as it was looked up.
        earlier = "=== run started\n[12:00:00] C2  Stock to recovery bridge\n=== run ended ===\n"
        length, said = self.turn_length(lambda _: earlier + "=== run started\n[12:05:00] A1  Pair the device\n")
        self.assertEqual(length, fs.HOLD_AT_MOST)
        self.assertFalse([line for line in said if "nothing fetched" in line])

    def test_a_turn_stays_while_nothing_has_been_sent(self):
        length, said = self.turn_length(lambda _: "[12:00:00] A1  Pair the device in eWeLink")
        self.assertEqual(length, fs.HOLD_AT_MOST)
        self.assertFalse([line for line in said if "nothing fetched" in line])

    def test_a_turn_stays_while_the_device_fetches(self):
        length, _ = self.turn_length(lambda _: "[12:00:00] C2  Stock", fetching=lambda second: second < 100)
        self.assertAlmostEqual(length, 100 + fs.TURN_IDLE, delta=1.5)

    def test_sending_again_gives_the_device_as_long_again(self):
        def texts(second):
            return "[12:00:00] C2  Stock\n" + ("    Sending the installer again\n" if second >= 50 else "")
        length, _ = self.turn_length(texts)
        self.assertAlmostEqual(length, 50 + fs.TURN_IDLE, delta=1.5)

    def said_in_a_turn(self, looks, limit=fs.HOLD_AT_MOST):
        """What a turn says, the device's connections at each second being `looks`."""
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(tmp)
            said = []
            monitor.sc.say = said.append
            clock = [1000.0]
            monitor.stop = mock.Mock(is_set=mock.Mock(side_effect=lambda: clock[0] >= 1000 + len(looks)))
            monitor.stop.wait = mock.Mock(side_effect=lambda _: clock.__setitem__(0, clock[0] + 1))
            looked = iter([set(), *looks])  # the first, from before its way out opens
            with mock.patch.object(fs.time, "time", side_effect=lambda: clock[0]), \
                    mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "release"), \
                    mock.patch.object(fs, "cloud_connections", side_effect=lambda _n: next(looked)):
                monitor.hold("test", limit=limit)
        return [line.removeprefix("CLOUD: ") for line in said if "turn " not in line]

    def test_a_device_dropped_as_it_connects_can_be_told_from_one_that_never_tried(self):
        server, session = ("192.0.2.80", "1", "443"), ("192.0.2.81", "2", "443")
        looks = [set(), {server}, {server}, {session}, set(), set()]
        self.assertEqual(self.said_in_a_turn(looks), [
            "device connected to 192.0.2.80", "device connected to 192.0.2.81",
            "device's connection to 192.0.2.80 closed", "device's connection to 192.0.2.81 closed"])

    def test_a_turn_says_when_the_device_is_back_and_when_it_never_was(self):
        session = ("192.0.2.81", "2", "443")
        back = self.said_in_a_turn([set(), *[{session}] * (int(fs.SIGNED_ON_AFTER) + 1)])
        self.assertEqual(back, ["device connected to 192.0.2.81", f"device back after {int(fs.SIGNED_ON_AFTER) + 1}s"])
        never = self.said_in_a_turn([set()] * (int(fs.DEVICE_BACK_AT_MOST) + 2))
        self.assertEqual(never, [f"device not back after {int(fs.DEVICE_BACK_AT_MOST)}s; the run goes on"])

    def test_a_connection_after_the_device_is_back_is_still_said(self):
        session, later = ("192.0.2.81", "2", "443"), ("192.0.2.82", "3", "443")
        looks = [*[{session}] * (int(fs.SIGNED_ON_AFTER) + 1), {later}]
        said = self.said_in_a_turn(looks)
        self.assertIn("device connected to 192.0.2.82", said)
        self.assertIn("device's connection to 192.0.2.81 closed", said)

    def test_a_sign_in_no_device_needs_does_not_wait_for_the_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(tmp)
            monitor.turn_file().touch()
            monitor.stop = mock.Mock(is_set=mock.Mock(side_effect=[False, True]))
            self.holding(monitor, limit=fs.SIGN_IN_TURN)
            self.assertFalse(monitor.turn_file().exists())

    def test_only_the_devices_own_way_to_the_internet_counts_as_a_connection(self):
        table = "\n".join((
            "ipv4 2 tcp 6 431947 ESTABLISHED src=192.168.60.50 dst=192.0.2.80 sport=1 dport=443 "
            "src=192.0.2.80 dst=198.18.60.2 sport=443 dport=1 [ASSURED] mark=0 zone=0 use=2",
            "ipv4 2 tcp 6 9 CLOSE src=192.168.60.50 dst=203.0.113.5 sport=2 dport=443 "
            "src=203.0.113.5 dst=198.18.60.2 sport=443 dport=2 [ASSURED] mark=0 zone=0 use=2",
            "ipv4 2 tcp 6 431947 ESTABLISHED src=198.18.60.2 dst=203.0.113.6 sport=3 dport=443 "
            "src=203.0.113.6 dst=198.18.60.2 sport=443 dport=3 [ASSURED] mark=0 zone=0 use=2",
            "ipv4 2 tcp 6 431947 ESTABLISHED src=192.168.60.50 dst=192.168.60.1 sport=4 dport=8088 "
            "src=192.168.60.1 dst=192.168.60.50 sport=8088 dport=4 [ASSURED] mark=0 zone=0 use=2",
            "ipv4 2 udp 17 29 src=192.168.60.50 dst=192.0.2.81 sport=5 dport=53",
        ))
        with mock.patch.object(fs, "sh", return_value=mock.Mock(stdout=table)):
            connections = fs.cloud_connections(1)
        self.assertEqual(connections, {("192.0.2.80", "1", "443")})

    def test_a_turn_ends_once_a_step_after_c2_is_under_way(self):
        for step, ends in (("C3", True), ("C3fb", True), ("D1", True), ("A2", False), ("C2", False)):
            with self.subTest(step=step), tempfile.TemporaryDirectory() as tmp:
                monitor = self.monitor(tmp)
                later = f"[12:00:00] {step}  Something\n"
                monitor.run_text = mock.Mock(side_effect=["", "", later, later, later])
                monitor.stop = mock.Mock(is_set=mock.Mock(side_effect=[False, True]))
                monitor.intents = mock.Mock(return_value=None)
                self.holding(monitor)
                self.assertEqual(monitor.stop.wait.called, not ends)  # held on, waiting a moment

    def test_a_sign_in_with_no_step_asking_gets_a_short_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.monitor(tmp)
            monitor.turn_file().touch()
            monitor.live_run = mock.Mock(return_value=("=== run started 1", "=== run started 1"))
            monitor.stop.wait = mock.Mock(side_effect=lambda _s: monitor.stop.set())
            with mock.patch.object(fs, "device_internet"), mock.patch.object(fs.CLOUD, "acquire", return_value=True), \
                    mock.patch.object(monitor, "hold") as hold:
                monitor.watching()
            hold.assert_called_once_with("a sign-in", limit=fs.SIGN_IN_TURN)

    def test_cloud_scenarios_are_side_by_side_unless_told_otherwise(self):
        for env, cores, no_cloud, expected in (({}, 8, False, 6), ({}, 2, False, 2), ({}, None, False, 1),
                                               ({}, 8, True, 0), ({"AUTOFLASH_EMU_CLOUD_WORKERS": "1"}, 8, False, 1),
                                               ({"AUTOFLASH_EMU_CLOUD_WORKERS": "5"}, 2, True, 5)):
            with self.subTest(env=env, cores=cores, no_cloud=no_cloud), mock.patch.dict(fs.os.environ, env), \
                    mock.patch.object(fs.os, "cpu_count", return_value=cores), mock.patch.object(fs, "NO_CLOUD", no_cloud):
                if not env:
                    fs.os.environ.pop("AUTOFLASH_EMU_CLOUD_WORKERS", None)
                self.assertEqual(fs.cloud_workers(), expected)
                self.assertEqual(fs.Tuner().workers, expected)

    def test_only_a_real_cloud_run_is_told_to_wait_for_a_turn(self):
        for stub_cloud, no_cloud, expected in ((False, False, True), (True, False, False), (False, True, False)):
            with self.subTest(stub_cloud=stub_cloud, no_cloud=no_cloud), mock.patch.object(fs, "STUB_CLOUD", stub_cloud), \
                    mock.patch.object(fs, "NO_CLOUD", no_cloud), \
                    mock.patch.object(fs.subprocess, "call", return_value=0) as call:
                self.scenario().run_tool("tasmota")
                self.assertEqual("AUTOFLASH_EMULATOR_CLOUD_TURN" in call.call_args.kwargs["env"], expected)

    def test_the_wait_reaches_the_run_inside_the_emulator(self):
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        self.assertIn('-e AUTOFLASH_EMULATOR_CLOUD_TURN="${AUTOFLASH_EMULATOR_CLOUD_TURN:-}"', script)

    def test_a_full_disk_is_the_tools_own_soft_limit(self):
        # Root in the emulator may not change another user's limits, and a
        # lowered hard limit could not be raised again.
        sc = self.scenario("diskfull", "once")
        ran = []

        def sh(*args, **_):
            ran.append(args)
            return mock.Mock(returncode=0, stdout="42\n", stderr="")
        with mock.patch.object(fs, "sh", side_effect=sh), mock.patch.object(fs.time, "sleep"):
            sc.fault("diskfull", "once", 0, None)
        limits = [args for args in ran if "prlimit" in args]
        self.assertEqual([args[-1] for args in limits], ["--fsize=1000000:", "--fsize=unlimited:"])
        for args in limits:
            self.assertEqual(args[2:4], ("-u", f"{os.getuid()}:{os.getgid()}"))
        self.assertNotIn("not_applied", sc.notes)

    def test_a_packet_changed_in_a_short_step_is_not_taken_for_one_changed_after_it(self):
        # U6 lasts 2 s: its packet was counted at the next look, the step said done
        # by then, and corrupt-once went MISTIMED with its fault inside U6.
        sc = self.scenario("corrupt", "once")
        sc.sc["step"] = "U6"
        sc.step_length = 2
        said = iter(["[12:10:18] U6  Tasmota updated and on the Wi-Fi\n",
                     "[12:10:18] U6  Tasmota updated and on the Wi-Fi\n[12:10:19] U6 done\n"])
        counted = "action order 1: pedit\n Sent 1514 bytes 1 pkt"

        def ex(*args, **_):
            return mock.Mock(returncode=0, stdout=counted if "-s" in " ".join(args) else "")
        with mock.patch.object(sc, "ex", side_effect=ex), mock.patch.object(fs.time, "sleep"), \
                mock.patch.object(sc, "current_run", side_effect=lambda *a: next(said, "")):
            sc.fault("corrupt", "once", 0, None)
        self.assertEqual(sc.notes["corrupted_packets"], 1)
        self.assertNotIn("mistimed", sc.notes)

    def test_a_signal_reaching_any_of_the_run_is_enough(self):
        sc = self.scenario("crash", "1")
        with mock.patch.object(sc, "ex", side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=0)]):
            sc.signal("KILL", "a helper", "the tool")
        self.assertNotIn("not_applied", sc.notes)

    def test_the_second_ctrl_c_may_find_the_run_ended(self):
        sc = self.scenario("cancel", "switch")
        with mock.patch.object(sc, "ex", side_effect=[mock.Mock(returncode=0), mock.Mock(returncode=1)]), \
                mock.patch.object(fs.time, "sleep"):
            sc.fault("cancel", "switch", 0, None)
        self.assertNotIn("not_applied", sc.notes)

    def test_cleaning_up_after_a_restart_is_not_a_fault_not_applied(self):
        sc = self.scenario("weak", 0)
        results = iter([0, 0, 2, 2])
        with mock.patch.object(sc, "ex", side_effect=lambda *a, **k: mock.Mock(returncode=next(results))), \
                mock.patch.object(fs.time, "sleep"):
            sc.fault("weak", 0, 0, None)
        self.assertNotIn("not_applied", sc.notes)

    def test_joining_the_devices_network_and_failing_is_noted(self):
        sc = self.scenario("fallback", 1)
        def ex(*args, **_):
            if args[0] == "cat":
                return mock.Mock(returncode=0, stdout="device-network\n")
            return mock.Mock(returncode=1 if args[-2:] == ("join", "device-network") else 0, stdout="")
        now = iter([0, 0, 5])
        with mock.patch.object(sc, "ex", side_effect=ex), mock.patch.object(fs.time, "sleep"), \
                mock.patch.object(fs.time, "time", side_effect=lambda: next(now, 9)):
            sc.fault("fallback", 1, 0, None)
        self.assertEqual(sc.notes["not_applied"], ["joining device-network"])
        self.assertNotIn("joined_early", sc.notes)

    def test_a_conversion_without_its_fault_proves_nothing(self):
        outage = {"fault": "outage", "arg": 60}
        self.assertFalse(fs.not_fired(outage, {}, True))
        self.assertTrue(fs.not_fired(outage, {"not_applied": ["x"]}, True))
        cut = {"fault": "ownnet", "arg": "cut"}
        self.assertTrue(fs.not_fired(cut, {}, True))
        self.assertFalse(fs.not_fired(cut, {"cut_after_upload": True}, True))
        self.assertTrue(fs.not_fired({"fault": "seven", "arg": "advised"}, {}, True))

    def test_a_ctrl_c_the_run_never_logged_never_happened(self):
        for fault, arg in (("ctrlc", "once"), ("ctrlc", "apart"), ("cancel", "switch")):
            self.assertTrue(fs.not_fired({"fault": fault, "arg": arg}, {"ctrl_c_logged": False}, True))
            self.assertFalse(fs.not_fired({"fault": fault, "arg": arg}, {"ctrl_c_logged": True}, True))

    def test_a_ctrl_c_from_prep_in_a_saved_devices_log_is_not_this_faults(self):
        sc = self.scenario("ctrlc", "once")
        logs = [(1.0, "run.log", f"[01:00:00] {fs.CTRL_C_LOGGED}\n")]
        with mock.patch.object(fs, "run_logs", side_effect=lambda state: logs):
            sc.ctrl_c_before = sc.ctrl_c_lines()
            self.assertEqual(sc.ctrl_c_lines(), sc.ctrl_c_before)
            logs[0] = (2.0, "run.log", logs[0][2] + f"[02:00:00] {fs.CTRL_C_LOGGED}\n")
            self.assertGreater(sc.ctrl_c_lines(), sc.ctrl_c_before)

    def test_a_failure_stands_though_its_fault_never_happened(self):
        self.assertFalse(fs.not_fired({"fault": "outage", "arg": 60}, {"not_applied": ["x"]}, False))

    def test_a_saved_device_is_made_with_no_api_beside_it(self):
        # A conversion sends no readout: an API beside it would be a container for nothing.
        import inspect
        self.assertIn('"--no-api"', inspect.getsource(fs.make_start))

    def test_prep_refuses_an_instance_in_use_and_leaves_the_saved_devices(self):
        with mock.patch.object(fs, "sh", return_value=mock.Mock(stdout="abc123\n")), \
                mock.patch.object(fs.shutil, "rmtree") as removed, mock.patch.object(fs.subprocess, "call") as call:
            with self.assertRaises(SystemExit) as refused:
                fs.prep()
        self.assertIn("AUTOFLASH_EMU_PREP_INSTANCE", str(refused.exception))
        removed.assert_not_called()
        call.assert_not_called()

    def test_only_a_failure_is_labelled_starved(self):
        starved = {"notes": {"memory": {"starved": True}}}
        self.assertIn("memory-starved", fs.starved_label({**starved, "verdict": "FAIL"}))
        self.assertEqual(fs.starved_label({**starved, "verdict": "PASS"}), "")
        self.assertEqual(fs.starved_label({"verdict": "HARNESS-ERROR"}), "")

    def test_a_legacy_run_is_killed_by_its_own_tools_name(self):
        # 2.0.x's tool is tools/autoflash_cli.py, 1.2.0's tools/s60_autoflash.py.
        import re
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "D4", "fault": "legacy",
                              "arg": "v2.0.2", "at": 40}, "/dev/null")
        with mock.patch.object(sc, "ex") as ex:
            sc.fault("legacy", "v2.0.2", 0, None)
        patterns = [c.args[-1] for c in ex.call_args_list if c.args[0] == "pkill"]
        for tool in ("tools/autoflash_cli.py", "tools/s60_autoflash.py"):
            self.assertTrue(any(re.search(p, tool) for p in patterns), tool)

    def test_the_cut_in_the_g1_after_a_legacy_run_lands_in_this_checkouts_g1(self):
        # Timed by 1.2.0's G1 (33 s), it came after this one's (21 s from
        # 2.0.2) had finished: MISTIMED.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4", "fault": "legacy",
                              "arg": "v1.2.0+G1off", "at": 40}, "/dev/null")
        sc.fired = "legacy"
        with mock.patch.object(fs, "legacy_tree"), mock.patch.object(sc, "run_tool"), \
             mock.patch.object(sc, "screen_result", return_value="Stopped"), \
             mock.patch.object(fs.threading, "Thread"), mock.patch.object(sc, "again") as again:
            sc.go_legacy()
        self.assertEqual(again.call_args.kwargs["later"],
                         ("G1", "deviceoff", 1, 0.6 * fs.ROUTES["sonoff>tasmota"][2]["G1"]))

    def test_the_cut_in_the_update_after_a_legacy_conversion_is_timed_by_an_updates_u5(self):
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4", "fault": "legacy",
                              "arg": "v2.0.2+U5off", "at": 40}, "/dev/null")
        sc.fired = "legacy"
        with mock.patch.object(fs, "legacy_tree"), mock.patch.object(sc, "run_tool"), \
             mock.patch.object(sc, "screen_result", return_value="Stopped"), \
             mock.patch.object(fs.threading, "Thread"), mock.patch.object(sc, "again") as again:
            sc.go_legacy()
        self.assertEqual(again.call_args.kwargs["later"],
                         ("U5", "deviceoff", 1, 0.6 * fs.ROUTES["update>tasmota"][2]["U5"]))

    def test_an_earlier_releases_own_error_is_not_this_checkouts(self):
        # 1.2.0, killed as G1 began, had stopped on a TimeoutError of its own:
        # this checkout's run carried on in the same run log and converted.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "G1", "fault": "legacy",
                              "arg": "v1.2.0", "at": 40}, "/dev/null")
        old = f"=== run started, tool version 1.2.0 ===\nFAILED: {fs.fault_verdicts.UNEXPECTED}: TimeoutError\n"
        # Where that release kept it, and where this checkout's tool, renaming
        # its records' folder as it carries the run on, has it now.
        earlier = Path("work/captures/plugs/12-34-56-ab-cd-ef/runs/20261013-031500/run.log")
        log = Path("work/captures/devices/12-34-56-ab-cd-ef/runs/20261013-031500/run.log")
        sc.earlier_release_wrote = {fs.filed_as(earlier): len(old)}
        self.assertNotIn(fs.fault_verdicts.UNEXPECTED, sc.ours(log, old + "=== run started ===\nConverted\n"))
        self.assertIn(fs.fault_verdicts.UNEXPECTED, sc.ours(log, old + f"FAILED: {fs.fault_verdicts.UNEXPECTED}: KeyError\n"))
        self.assertIn(fs.fault_verdicts.UNEXPECTED, sc.ours(log.parents[1] / "20261013-040000/run.log", old))

    def test_an_earlier_releases_records_are_read_where_it_keeps_them(self):
        """captures/plugs/, as releases before 3.0.0 name it, until this
        checkout's tool has renamed it: a legacy run's log is read from there."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(fs.records_under(state / "work"), state / "work/captures/devices")
            run = state / "work/captures/plugs/12-34-56-ab-cd-ef/runs/20261013-031500"
            run.mkdir(parents=True)
            (run / "run.log").write_text("[C2] sent\n", encoding="utf-8")
            self.assertEqual([text for _, _, text in fs.run_logs(state)], ["[C2] sent\n"])
            (state / "work/captures/plugs").rename(state / "work/captures/devices")
            self.assertEqual([text for _, _, text in fs.run_logs(state)], ["[C2] sent\n"])

    def test_a_step_with_a_start_of_its_own_needs_no_length(self):
        # F4rs is in STARTS only: its length comes from there.
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>tasmota", "step": "F4rs", "fault": "deviceoff",
                              "arg": 1, "at": 50}, "/dev/null")
        with mock.patch.object(sc, "run_tool", side_effect=RuntimeError("stop here")), \
             mock.patch.object(fs.threading, "Thread"):
            with self.assertRaisesRegex(RuntimeError, "stop here"):
                sc.go_watched()
        self.assertEqual(sc.step_length, fs.STARTS["F4rs"][1])

    def test_every_step_has_a_length_or_a_start_of_its_own(self):
        for sc in fs.scenarios():
            if sc["fault"] == "legacy":
                steps = fs.LEGACY_STEPS[sc["arg"].partition("+")[0]].get(sc["route"], {})
                self.assertTrue(sc["step"] == "start" or sc["step"] in steps, sc["id"])
                continue
            if sc["fault"] in ("lostapp", "notemplate", "bootloop", "safemode"):
                self.assertEqual(sc["step"], "start", sc["id"])
                continue
            if fs.is_readout(sc["route"]):
                self.assertTrue(sc["step"] == "start" or sc["step"] in fs.STARTS, sc["id"])
                continue
            if sc["step"] == "C2":
                continue
            steps = fs.ROUTES[sc["route"]][2]
            self.assertTrue(sc["step"] in steps or sc["step"] in fs.STARTS, sc["id"])


class EndChecksTests(unittest.TestCase):
    """A converted device passes only if the report's end-of-run checks did."""

    def verdict(self, checks, relay="Switches (through eWeLink)", target="sonoff", route="esphome>sonoff",
                result="Converted", runs="--- a run\n", fault="deviceoff", arg=1, step="R1", release=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            run = tmp / "state/work/captures/devices" / records.folder_name(MAC) / "runs/1/run.log"
            run.parent.mkdir(parents=True)
            run.write_text("[10:00:00] === run started, tool version 2.3.0 ===\n"
                           f"[10:00:01]     Relay: {relay}\n[10:00:02]     Checks passed: {checks}\n"
                           "[10:00:03] === run ended ===\n", encoding="utf-8")
            log = tmp / "scenario.log"
            log.write_text(runs + "    Result:   Converted\n", encoding="utf-8")
            sc = fs.Scenario(24, {"id": f"{route}:{step}:{fault}-{arg}", "route": route, "step": step,
                                  "fault": fault, "arg": arg, "at": 40}, log)
            sc.state, sc.target, sc.started = tmp / "state", target, 0
            sc.memory = mock.Mock(note=mock.Mock(return_value={}))
            with mock.patch.object(fs, "RESULTS", tmp / "results.jsonl"), mock.patch.object(fs, "LOGS", tmp / "logs"), \
                    mock.patch.object(fs, "sh"), mock.patch.object(sc, "leftovers", return_value=[]), \
                    mock.patch.object(sc, "names_seen", return_value=([], [])), \
                    mock.patch.object(sc, "template_ok", return_value=True), \
                    mock.patch.object(sc, "release_of", return_value=release):
                return sc.finish([(0, result)], None)

    def test_a_run_whose_relay_switched_passes(self):
        self.assertEqual(self.verdict("True")["verdict"], "PASS")

    def test_a_run_whose_checks_failed_is_its_own_failure_saying_what_they_found(self):
        # Every run back to Sonoff's firmware before 83c517d: the relay asked
        # through eWeLink before the device was there, and judged PASS.
        row = self.verdict("False", relay="Could not be checked")
        self.assertEqual(row["verdict"], "FAIL-CHECKS")
        self.assertEqual(row["notes"]["checks"], "Checks passed: False; Relay: Could not be checked")

    def test_a_device_with_no_backup_must_be_reported_keeping_its_own_identity(self):
        self.assertEqual(self.verdict("True", route="nobackup>sonoff")["verdict"], "FAIL-IDENTITY")

    def test_a_last_run_that_found_nothing_to_convert_has_no_identity_to_report(self):
        # nobackup>sonoff:R2:cancel-new: cancelled as Sonoff's firmware was
        # checked, then started again afresh; that run makes no report.
        row = self.verdict("True", route="nobackup>sonoff", result="Converted (already: nothing to convert)")
        self.assertEqual(row["verdict"], "PASS")

    def test_a_stage_s_failed_try_left_on_the_screen_is_its_own_failure(self):
        rule = "=" * 72
        screen = (f"{rule}\nThe screen at the end of the run (100x40):\n{rule}\n"
                  "Stage 13 - Staging startup firmware - Stopped\n"
                  "Stage 13 - Staging startup firmware - Complete\n"
                  f"{rule}\nExit code 0 - finished\n")
        row = self.verdict("True", runs="--- run --firmware sonoff\n" + screen)
        self.assertEqual(row["verdict"], "FAIL-SCREEN")
        self.assertEqual(len(row["notes"]["screen"]), 1)
        # An earlier release's run is out already: not this checkout's to judge.
        self.assertEqual(self.verdict("True", runs="--- run --code=/tmp/v2.3.0 --firmware sonoff\n" + screen
                                      + "--- run --firmware sonoff\n")["verdict"], "PASS")
        # The run that carries a legacy run on puts this checkout back (--code=
        # its own path): it is this checkout's, and judged.
        self.assertEqual(self.verdict("True", runs=f"--- run --code={fs.REPO} --firmware sonoff\n" + screen)
                         ["verdict"], "FAIL-SCREEN")

    def test_a_conversion_an_earlier_release_began_must_end_on_this_ones_tasmota(self):
        legacy = dict(target="tasmota", route="sonoff>tasmota", fault="legacy", arg="v2.0.2", step="F4")
        self.assertEqual(self.verdict("True", release=fs.OFFICIAL_TASMOTA_VERSION, **legacy)["verdict"], "PASS")
        row = self.verdict("True", release="15.6.0", **legacy)
        self.assertEqual((row["verdict"], row["notes"]["release"]), ("FAIL-NOT-LATEST", "tasmota 15.6.0"))
        # And on ESPHome: its release, this project's own.
        to_esphome = dict(legacy, target="esphome", route="sonoff>esphome", step="I2")
        self.assertEqual(self.verdict("True", release=fs.LATEST["esphome"], **to_esphome)["verdict"], "PASS")
        self.assertEqual(self.verdict("True", release="2026.9.0-1", **to_esphome)["verdict"], "FAIL-NOT-LATEST")
        # Only a legacy run is asked: any other converts with this release from the start.
        self.assertEqual(self.verdict("True", release="15.6.0", target="tasmota", route="esphome>tasmota")
                         ["verdict"], "PASS")

    def test_esphomes_release_is_asked_of_its_api_from_the_emulated_computer(self):
        import json
        sc = fs.Scenario(24, {"id": "x", "route": "sonoff>esphome", "step": "I2", "fault": "legacy",
                              "arg": "v2.0.2", "at": 40}, "/dev/null")
        for answer, release in (({"project_name": fs.DEVICE.esphome_project, "project_version": "2026.10.0-1"},
                                 "2026.10.0-1"),
                                ({"project_name": "someone.else", "project_version": "1.0"}, None)):
            with mock.patch.object(sc, "ex", return_value=mock.Mock(stdout=json.dumps(answer) + "\n")) as ex:
                self.assertEqual(sc.esphome_release(), release)
            self.assertIn("api_device_info", ex.call_args.args[-1])
        with mock.patch.object(sc, "ex", return_value=mock.Mock(stdout="null\n")), mock.patch.object(fs.time, "sleep"):
            self.assertIsNone(sc.esphome_release())   # not answering, after a few tries

    def test_a_fault_in_the_update_after_a_legacy_conversion_is_its_own_scenario(self):
        ids = {sc["id"] for sc in fs.scenarios()}
        for tag in fs.LEGACY_STEPS:
            self.assertIn(f"sonoff>tasmota:F4:legacy-{tag}+U5off", ids)

    def test_checks_the_emulator_cannot_run_are_no_failure(self):
        self.assertEqual(self.verdict("Not run", target="sonoff")["verdict"], "PASS")
        self.assertEqual(self.verdict("Not run", target="tasmota")["verdict"], "FAIL-CHECKS")


class ReadoutScenarioTests(unittest.TestCase):
    """Readouts (cli.py readout): of a stock device, faulted where it matters
    most, and of a converted device, which must read its first stage alone."""

    def scenario(self, route="sonoff>readout", step="read", fault="crash", arg=1):
        sc = fs.Scenario(24, {"id": fs.sid(route, step, fault, arg), "route": route, "step": step,
                              "fault": fault, "arg": arg, "at": 40}, "/dev/null")
        sc.started = 0
        return sc

    def test_the_readouts_listed_each_on_the_stub(self):
        readouts = [sc for sc in fs.scenarios() if sc["route"].endswith(">readout")]
        self.assertEqual([sc["id"] for sc in readouts], [
            "sonoff>readout:start:none-unattended", "sonoff>readout:read:deviceoff-1", "sonoff>readout:read:outage-30",
            "sonoff>readout:read:crash-1", "sonoff>readout:back:deviceoff-1", "sonoff>readout:back:outage-30",
            "sonoff>readout:read:crash-1-then-sonoff", "sonoff>readout:read:crash-1-then-readout",
            "sonoff>readout:read:crash-1-then-sonoff-elsewhere",
            "tasmota>readout:start:none-first-stage"])
        self.assertEqual({fs.kind_of(sc) for sc in readouts}, {"stub"})   # signed in, and the bridge sent, there

    def test_a_readout_is_run_as_one_and_one_of_a_converted_device_given_the_devices_address(self):
        sc = self.scenario()
        with mock.patch.object(fs.subprocess, "call", return_value=0) as call:
            sc.run_tool("readout", fresh=True)
            sc.run_tool("readout", ["--device-ip", fs.DEVICE_IP], source="suite-tasmota")
        stock, converted = (c.args[0] for c in call.call_args_list)
        self.assertIn("--readout", stock)
        self.assertNotIn("--firmware", stock)
        self.assertEqual(stock[-1], "--")   # found as a person without its address finds it
        self.assertEqual(converted[-3:], ["--", "--device-ip", fs.DEVICE_IP])
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        self.assertIn("--readout) driver_args+=(--command readout)", script)

    def readout_says(self, restarts=True):
        """What readout_espressif says from the read to the device back on Sonoff's
        firmware: restarted by its bridge, or (`restarts` False) by the person."""
        from contextlib import ExitStack, nullcontext
        from tools.autoflash import output, prompts, readout_espressif
        said, self.events = [], []
        installer, dump = readout_espressif.installer, readout_espressif.dump_stock_flash
        with ExitStack() as stack:
            for owner, name, kwargs in (
                    (output, "say", {"side_effect": said.append}),
                    (output, "event", {"side_effect": lambda name, **fields: self.events.append((name, fields))}),
                    # On a terminal a wait is shown, not logged.
                    (output, "log_only", {"side_effect": said.append}),
                    (output, "working", {"side_effect": lambda text: nullcontext()}),
                    (output, "transient_begin", {}), (output, "transient_end", {}),
                    (prompts, "section", {"side_effect": lambda title, lines: said.extend([title, *lines])}),
                    (prompts, "wait_for_key", {}),
                    (prompts, "warning_box", {}), (prompts, "require_word", {"return_value": True}),
                    (installer, "back_on_sonoff", {"return_value": True}),
                    (installer, "bridge_answers", {"return_value": False}),
                    (installer, "restart_bridge", {"return_value": restarts}),
                    (installer.arrival, "sonoff_answers", {"return_value": True}),
                    (installer.time, "sleep", {}),
                    (dump, "read_dump", {"return_value": (b"", b"", {})}),
                    (dump, "summary", {"return_value": []}),
                    (dump, "read_info", {"return_value": b""}),
                    (readout_espressif, "find_bridge", {"return_value": fs.DEVICE_IP}),
                    (readout_espressif, "firewall", {}), (readout_espressif, "hostsys", {}),
                    (readout_espressif.safety_locks, "lifted", {}),
                    (readout_espressif.safety_locks, "say_lock_back", {}),
                    (readout_espressif.build_vendor_ota, "build_wrapper", {"return_value": b"ota"}),
                    (readout_espressif.build_vendor_ota, "app_version", {"return_value": "1"})):
                stack.enter_context(mock.patch.object(owner, name, **kwargs))
            tmp = stack.enter_context(tempfile.TemporaryDirectory())
            ctx = mock.Mock(dir=Path(tmp))
            inputs = {}
            ctx.state.set_input.side_effect = inputs.__setitem__
            ctx.state.input.side_effect = inputs.get
            chip = mock.Mock(bridge_app=mock.Mock(return_value=b"app"))
            readout_espressif.read_out(ctx, chip, "FWSW-E32S60-S60-ESP32C3FN4", "1.2.0", "1000abcdef")
            readout_espressif.back_on_sonoff(ctx)
        return "\n".join(f"[10:00:00] {line}" for line in said)

    def test_each_phase_is_timed_from_what_the_readout_says(self):
        import re
        for restarts in (True, False):
            with self.subTest(restarts=restarts):
                said = self.readout_says(restarts)
                for phase in ("read", "back"):
                    self.assertRegex(said, fs.STARTS[phase][0], phase)
                    self.assertRegex(said, fs.ENDS[phase], phase)
                    self.assertLess(re.search(fs.STARTS[phase][0], said).start(),
                                    re.search(fs.ENDS[phase], said).start())
                # Where the bridge will not restart it, the person is asked to, as the run waits.
                self.assertEqual(bool(fs.READOUT_SWITCH.search(said)), not restarts)

    def test_each_phase_is_timed_from_the_readouts_events(self):
        for restarts in (True, False):
            with self.subTest(restarts=restarts):
                self.readout_says(restarts)
                phases = [fields["phase"] for name, fields in self.events if name == "phase"]
                for phase in ("read", "back"):
                    self.assertLess(phases.index(fs.STARTS_AT[phase][1]), phases.index(fs.ENDS_AT[phase][1]), phase)

    def test_a_fault_putting_sonoff_back_lands_as_its_firmware_starts(self):
        # In the emulator the phase lasts 4 s: the bridge's restart 1.5 s in,
        # Sonoff's firmware answering by 4 s. At 18 s, 40% fell after its end (NOT-FIRED).
        at = fs.STARTS["back"][1] * 40 / 100
        self.assertGreater(at, 1.5)
        self.assertLess(at, 4)

    def test_the_person_switches_the_device_off_and_on_where_the_readout_asks_by_its_events(self):
        import threading
        sc = self.scenario()
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "run.log")
            events = Path(tmp, output.EVENTS)

            def write(*items):
                events.write_text("".join(json.dumps({"at": 0, "event": e, **f}) + "\n" for e, f in items),
                                  encoding="utf-8")
            started = [("run", {}), ("phase", {"phase": "read"})]
            looks = iter([lambda: write(*started),
                          lambda: write(*started, ("switch", {"now": True, "key": True}))])

            def logs(state):
                step = next(looks, None)
                if step is None:
                    stop.set()
                    return []
                step()
                return [(1.0, log, "words that say nothing")]
            with mock.patch.object(sc, "ex", return_value=mock.Mock(stdout="", returncode=0)) as ex, \
                    mock.patch.object(fs, "run_logs", side_effect=logs), mock.patch.object(stop, "wait"):
                sc.person(stop)
        self.assertIn(mock.call(fs.DEVICE_CMD, "restart"), ex.call_args_list)
        self.assertEqual(sc.notes["switched"], 1)

    def test_the_person_switches_the_device_off_and_on_where_the_readout_asks(self):
        import threading
        sc = self.scenario()
        stop = threading.Event()
        asked = "[10:00:00] Read. Now put it back on its Sonoff firmware\n" \
                "[10:00:00]     Switch it off for five seconds, then on (a wall switch at its breaker).\n"
        looks = iter([[(1.0, Path("run.log"), "=== run started ===\n")], [(1.0, Path("run.log"), asked)]])

        def logs(state):
            found = next(looks, None)
            if found is None:
                stop.set()
                return []
            return found
        with mock.patch.object(sc, "ex", return_value=mock.Mock(stdout="", returncode=0)) as ex, \
                mock.patch.object(fs, "run_logs", side_effect=logs), mock.patch.object(stop, "wait"):
            sc.person(stop)
        self.assertIn(mock.call(fs.DEVICE_CMD, "restart"), ex.call_args_list)
        self.assertEqual(sc.notes["switched"], 1)

    def test_after_a_crash_the_readout_is_run_again_as_the_run_advised(self):
        sc = self.scenario()
        sc.fired = 1.0   # the crash came while the read was under way
        # Killed, it said nothing; run again, it found the device on the bridge.
        advice = ["the device is on this tool's installer from an earlier run ... switch the device off and on", ""]
        with mock.patch.object(fs.threading, "Thread"), mock.patch.object(fs.time, "sleep"), \
                mock.patch.object(sc, "run_tool", side_effect=[137, 1, 0]) as runs, \
                mock.patch.object(sc, "readout_result", return_value="no readout"), \
                mock.patch.object(sc, "readout_done", side_effect=[False, False, False, True]), \
                mock.patch.object(sc, "last_said", side_effect=advice[::-1]), \
                mock.patch.object(sc, "ex") as ex, mock.patch.object(sc, "finish_readout") as finish:
            sc.go_readout()
        self.assertEqual([c.args[0] for c in runs.call_args_list], ["readout"] * 3)
        self.assertEqual(ex.call_args_list, [mock.call(fs.DEVICE_CMD, "restart")])
        self.assertEqual([code for code, _ in finish.call_args.args[0]], [137, 1, 0])

    def test_a_fault_that_never_came_proves_nothing(self):
        sc = self.scenario()
        with mock.patch.object(fs.threading, "Thread"), mock.patch.object(sc, "run_tool", return_value=0), \
                mock.patch.object(sc, "readout_result", return_value="Readout kept, not sent"), \
                mock.patch.object(sc, "finish_readout") as finish:
            sc.go_readout()
        self.assertEqual(finish.call_args.args[1], "NOT-FIRED")

    def verdict(self, route="sonoff>readout", kept=(), firmware="sonoff", left=(), **notes):
        sc = self.scenario(route=route)
        sc.target = "sonoff" if route == "sonoff>readout" else "tasmota"
        sc.memory = mock.Mock(note=mock.Mock(return_value={}))
        sc.notes.update(notes)
        with mock.patch.object(sc, "leftovers", return_value=list(left)), \
                mock.patch.object(sc, "names_seen", return_value=([], [])), \
                mock.patch.object(sc, "readout_kept", return_value=None if kept is None else list(kept)), \
                mock.patch.object(sc, "firmware_now", return_value=firmware), \
                mock.patch.object(sc, "bricked", return_value=False), mock.patch.object(fs, "run_logs", return_value=[]), \
                mock.patch.object(sc, "keep", side_effect=lambda results, verdict, left: verdict):
            return sc.finish_readout([(0, "Readout kept, not sent")], None)

    def test_a_whole_readout_kept_and_the_device_back_on_sonoff_passes(self):
        self.assertEqual(self.verdict(), "PASS")

    def test_a_readout_short_of_whole_or_none_fails(self):
        self.assertEqual(self.verdict(kept=["no efuse.bin.xz"]), "FAIL-READOUT")
        self.assertEqual(self.verdict(kept=None), "FAIL-READOUT")

    def test_a_device_not_back_on_sonoff_fails(self):
        self.assertEqual(self.verdict(firmware="installer"), "FAIL-NOT-SONOFF")

    def test_a_fault_landed_after_its_phase_is_mistimed(self):
        self.assertEqual(self.verdict(mistimed=True), "MISTIMED")
        self.assertEqual(self.verdict(left=["firewall rule"]), "LEFTOVER")

    def converted(self, code, sent=False, template=True, kept=()):
        sc = self.scenario(route="tasmota>readout", step="start", fault="none", arg="first-stage")
        logs = [(1.0, Path("run.log"), "$ python tools/espressif/serve_tasmota_ota.py" if sent else "FAILED: no")]
        with mock.patch.object(sc, "run_tool", return_value=code) as run, \
                mock.patch.object(sc, "readout_result", return_value="Not sent"), \
                mock.patch.object(sc, "first_stage_kept", return_value=None if kept is None else list(kept)), \
                mock.patch.object(fs, "run_logs", return_value=logs), \
                mock.patch.object(sc, "template_ok", return_value=template), \
                mock.patch.object(sc, "finish_readout", side_effect=lambda results, special: special) as finish:
            verdict = sc.go_readout_converted()
        self.assertEqual(run.call_args.args[:2], ("readout", ["--device-ip", fs.DEVICE_IP]))
        return verdict, finish

    def test_a_readout_of_a_converted_device_reads_its_first_stage_alone_leaving_it_as_it_was(self):
        self.assertEqual(self.converted(0)[0], None)   # judged as any: names, leftovers
        self.assertEqual(self.converted(1)[0], "FAIL-EXIT")
        self.assertEqual(self.converted(0, kept=None)[0], "FAIL-READOUT")   # none kept
        self.assertEqual(self.converted(0, kept=["flash.bin.xz in it"])[0], "FAIL-READOUT")
        self.assertEqual(self.converted(0, sent=True)[0], "FAIL-SENT")
        self.assertEqual(self.converted(0, template=False)[0], "FAIL-DISTURBED")

    def test_how_a_readout_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = self.scenario()
            sc.state = Path(tmp)
            run = Path(tmp, "work/captures/devices/unidentified/runs/1/run.log")
            run.parent.mkdir(parents=True)
            # Seen: every kept readout taken for none, once the screen no longer
            # said where it was saved.
            for text, said in (("[1]     Not sent from a test run: kept in /work/x/readout.tar.\n",
                                "Readout kept, not sent"),
                               ("[1] Readout saved in captures/x/readout.tar, not sent.\n", "Readout kept, not sent"),
                               ("[1] FAILED: the device's memory could not be read\n",
                                "Stopped: the device's memory could not be read"),
                               ("[1] Reading the device's memory\n", "no readout")):
                run.write_text(f"=== run started ===\n{text}", encoding="utf-8")
                self.assertEqual(sc.readout_result(), said)


class ReadoutAllScenarioTests(unittest.TestCase):
    """A readout of every device in the account (cli.py readout --all), each
    readout sent to the API's own code, judged by what the API was asked and
    kept."""

    DEVICE = "BASICR4"
    RUN = ("[10:00:00] Reading out 'Test device 1' (TESTLIGHT) (1 of 3)...\n"
           "[10:00:20] Reading out 'Test device 2' (TESTLIGHT) (2 of 3)...\n"
           "[10:00:30] Reading out 'Device (stub)' (BASICR4) (3 of 3)...\n")
    WANTED = '2026-11-09T10:00:01.000Z POST /v1/wanted 140 -> 200 {"wanted":true,"held":[]}'
    HELD = '2026-11-09T10:00:21.000Z POST /v1/wanted 140 -> 200 {"wanted":false,"why":"held","held":[]}'
    LIMITED = '2026-11-09T10:00:40.000Z POST /v1/readout 9000 -> 429 {"error":"too many readouts at once"}'

    def taken(self, digest):
        return f'2026-11-09T10:00:10.000Z POST /v1/readout 9000 -> 201 {{"id":"2026-11-09-{digest[:12]}"}}'

    def scenario(self, account="mixed", step="start", fault="none", arg=None):
        arg = account if arg is None else arg
        sc = fs.Scenario(25, {"id": fs.sid(fs.READOUT_ALL, step, fault, arg), "route": fs.READOUT_ALL,
                              "step": step, "fault": fault, "arg": arg, "at": 40, "account": account}, "/dev/null")
        sc.started = 0
        return sc

    def test_each_account_listed_on_the_stub_and_a_fault_in_the_devices_read(self):
        every = [sc for sc in fs.scenarios() if sc["route"] == fs.READOUT_ALL]
        self.assertEqual([sc["id"] for sc in every], [
            "sonoff>readout-all:start:none-mixed", "sonoff>readout-all:start:none-alike",
            "sonoff>readout-all:start:none-enough", "sonoff>readout-all:start:none-many",
            "sonoff>readout-all:read:deviceoff-1", "sonoff>readout-all:read:crash-1"])
        self.assertEqual({fs.kind_of(sc) for sc in every}, {"stub"})   # the stub lists the other devices
        self.assertTrue(all(sc["account"] in fs.ACCOUNTS and fs.is_readout(sc["route"]) for sc in every))
        for sc in every:
            if sc["step"] != "start":
                self.assertIn(sc["step"], fs.STARTS)

    def test_the_accounts_are_made_up_and_each_what_it_is_named_for(self):
        def alike(device):
            return device["productModel"], device["model"], device["fwVersion"]
        for name, devices in fs.ACCOUNTS.items():
            with self.subTest(name):
                self.assertEqual(len({d["deviceid"] for d in devices}), len(devices))
                # None the tool converts: each only a first stage, as no other is emulated.
                self.assertFalse(any(readout.reads_out(stub_server.Stub.other_item(d)["itemData"]) for d in devices))
        self.assertLess(len({alike(d) for d in fs.ACCOUNTS["alike"]}), len(fs.ACCOUNTS["alike"]))
        self.assertEqual(len({alike(d) for d in fs.ACCOUNTS["many"]}), len(fs.ACCOUNTS["many"]))
        self.assertGreater(len(fs.ACCOUNTS["many"]), 2)   # more than the API takes in a minute

    def test_a_readout_of_every_device_is_run_with_its_account_and_the_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp, "scenario.log")
            sc = self.scenario(account="enough")
            sc.log = str(log)
            with mock.patch.object(fs.subprocess, "call", return_value=0) as call, \
                    mock.patch.object(fs, "device_product", return_value=self.DEVICE), \
                    mock.patch.object(fs, "STUB_CLOUD", True), \
                    mock.patch.dict(os.environ, {"AUTOFLASH_API_ENOUGH": "X"}):
                sc.run_tool("readout", fresh=True)
                other = fs.Scenario(26, {"id": "x", "route": "sonoff>readout", "step": "start", "fault": "none",
                                         "arg": "unattended"}, str(log))
                other.run_tool("readout")
            (args, ), options = call.call_args_list[0]
            self.assertIn("--readout", args)
            self.assertNotIn("--no-api", args)   # sent to the API, which judges them
            self.assertEqual(args[args.index("--"):], ["--", "--all"])
            env = options["env"]
            self.assertEqual(json.loads(Path(env["AUTOFLASH_EMU_STUB_OTHERS"]).read_text(encoding="utf-8")),
                             fs.ACCOUNTS["enough"])
            self.assertEqual(env["AUTOFLASH_API_ENOUGH"], f"{self.DEVICE},{fs.ACCOUNTS['enough'][0]['productModel']}")
            self.assertEqual(env["AUTOFLASH_EMU_STUB_PRODUCT_MODEL"], self.DEVICE)
            # Any other scenario's account is the device alone, whatever this computer has set.
            (args, ), options = call.call_args_list[1]
            self.assertIn("--no-api", args)   # kept unsent, for readout_problems
            self.assertNotIn("--all", args)
            self.assertNotIn("AUTOFLASH_API_ENOUGH", options["env"])
            self.assertNotIn("AUTOFLASH_EMU_STUB_OTHERS", options["env"])
            self.assertNotIn("AUTOFLASH_LOCAL_READOUT_LIMIT", env)   # the website's own limit
        script = (fs.REPO / "tools/espressif/emulator/unattended-test.sh").read_text(encoding="utf-8")
        self.assertIn("--no-api) api=0", script)
        self.assertIn('"${AUTOFLASH_API_ENOUGH:-}"', script)
        self.assertIn('cp "$AUTOFLASH_EMU_STUB_OTHERS"',
                      (fs.REPO / "tools/espressif/emulator/emu.sh").read_text(encoding="utf-8"))

    def test_every_run_on_the_stub_has_the_devices_product_as_ewelink_lists_it(self):
        # On a checkout with no earlier runs the stub listed no product, as eWeLink
        # never does: a readout of a converted device then found nothing to read.
        with tempfile.TemporaryDirectory() as tmp:
            sc = fs.Scenario(26, {"id": "x", "route": "tasmota>readout", "step": "start", "fault": "none",
                                  "arg": "first-stage"}, str(Path(tmp, "scenario.log")))
            with mock.patch.object(fs.subprocess, "call", return_value=0) as call, \
                    mock.patch.object(fs, "device_product", return_value=self.DEVICE), \
                    mock.patch.object(fs, "STUB_CLOUD", True):
                sc.run_tool("readout")
            self.assertEqual(call.call_args.kwargs["env"]["AUTOFLASH_EMU_STUB_PRODUCT_MODEL"], self.DEVICE)

    def test_the_many_account_meets_the_apis_limit_however_slowly_its_readouts_come(self):
        # Its readouts came 10 to 75 s apart, never three in one minute: the 429
        # it is there for never came (NOT-FIRED) at the website's two a minute.
        with tempfile.TemporaryDirectory() as tmp:
            sc = self.scenario(account="many")
            sc.log = str(Path(tmp, "scenario.log"))
            with mock.patch.object(fs.subprocess, "call", return_value=0) as call, \
                    mock.patch.object(fs, "device_product", return_value=self.DEVICE):
                sc.run_tool("readout", fresh=True)
            self.assertEqual(call.call_args.kwargs["env"]["AUTOFLASH_LOCAL_READOUT_LIMIT"], "1")
        self.assertIn("-e AUTOFLASH_LOCAL_READOUT_LIMIT",
                      (fs.REPO / "tools/repo/emulator-room.sh").read_text(encoding="utf-8"))
        self.assertIn("process.env.AUTOFLASH_LOCAL_READOUT_LIMIT",
                      (fs.REPO / "website/api/local.mjs").read_text(encoding="utf-8"))

    def problems(self, requests, run=RUN, finished=True, stored=(), whole=(), kept=()):
        """readout_all_problems' verdicts of one run, the API's store holding
        `stored` (digests), the device's readout lacking `whole`."""
        with tempfile.TemporaryDirectory() as tmp:
            api = Path(tmp)
            (api / "requests.log").write_text("".join(line + "\n" for line in requests), encoding="utf-8")
            for digest in stored:
                (api / f"readouts__2026-11-09__{digest[:24]}.tar").write_bytes(b"tar")
            with mock.patch.object(fs.fault_verdicts, "readout_problems", return_value=list(whole)):
                problems, summary = fs.readout_all_problems(run, finished, api, 3, self.DEVICE, kept)
        return [verdict for verdict, _ in problems], summary

    A, B = "a" * 64, "b" * 64

    def test_every_device_asked_each_wanted_one_taken_and_kept(self):
        verdicts, summary = self.problems([self.WANTED, self.taken(self.A), self.HELD, self.WANTED, self.LIMITED,
                                           self.LIMITED, self.taken(self.B)], stored=(self.A, self.B))
        self.assertEqual(verdicts, [])
        self.assertEqual((summary["asked"], summary["wanted"], summary["taken"], summary["429"]), (3, 2, 2, 2))
        self.assertTrue(summary["device_whole"])

    def test_a_device_not_asked(self):
        self.assertEqual(self.problems([self.WANTED, self.taken(self.A), self.HELD], stored=(self.A,))[0],
                         ["FAIL-NOT-ASKED"])
        # Nor read out at all: the account's three, two of them.
        run = self.RUN.rsplit("[10:00:30]", 1)[0]
        self.assertEqual(self.problems([self.WANTED, self.taken(self.A), self.HELD], run=run, stored=(self.A,))[0],
                         ["FAIL-NOT-ASKED"])

    def test_a_device_not_wanted_but_sent(self):
        verdicts, _ = self.problems([self.WANTED, self.taken(self.A), self.HELD, self.taken(self.B), self.WANTED,
                                     self.taken(self.B)], stored=(self.A, self.B))
        self.assertEqual(verdicts, ["FAIL-SENT-UNWANTED"])

    def test_a_wanted_device_never_sent_or_never_taken(self):
        self.assertEqual(self.problems([self.WANTED, self.HELD, self.WANTED, self.taken(self.B)],
                                       stored=(self.B,))[0], ["FAIL-NOT-SENT"])
        self.assertEqual(self.problems([self.WANTED, self.taken(self.A), self.HELD, self.WANTED] + [self.LIMITED] * 7,
                                       stored=(self.A,))[0], ["FAIL-NOT-SENT"])

    def test_a_stopped_run_is_let_off_the_device_it_stopped_on(self):
        requests = [self.WANTED, self.taken(self.A), self.HELD, self.WANTED]
        self.assertEqual(self.problems(requests, finished=False, stored=(self.A,))[0], [])
        self.assertEqual(self.problems(requests, finished=True, stored=(self.A,))[0], ["FAIL-NOT-SENT"])

    def test_an_answer_the_tool_could_not_read_is_wanted(self):
        failed = '2026-11-09T10:00:01.000Z POST /v1/wanted 140 -> 500 {"error":"x"}'
        self.assertEqual(self.problems([failed, self.HELD, self.WANTED, self.taken(self.B)], stored=(self.B,))[0],
                         ["FAIL-NOT-SENT"])

    def test_one_taken_but_not_in_the_store_or_the_devices_not_whole(self):
        requests = [self.WANTED, self.taken(self.A), self.HELD, self.WANTED, self.taken(self.B)]
        self.assertEqual(self.problems(requests, stored=(self.A,))[0], ["FAIL-NOT-STORED"])
        self.assertEqual(self.problems(requests, stored=(self.A, self.B), whole=["no efuse.bin.xz"])[0],
                         ["FAIL-READOUT"])

    def test_one_kept_unsent(self):
        requests = [self.WANTED, self.taken(self.A), self.HELD, self.WANTED, self.taken(self.B)]
        self.assertEqual(self.problems(requests, stored=(self.A, self.B), kept=["readout-1000000002.tar"])[0],
                         ["FAIL-KEPT-UNSENT"])

    def finish(self, judged, account="mixed", firmware="sonoff", code=0):
        sc = self.scenario(account=account)
        sc.target = "sonoff"
        sc.memory = mock.Mock(note=mock.Mock(return_value={}))
        with mock.patch.object(sc, "leftovers", return_value=[]), \
                mock.patch.object(sc, "names_seen", return_value=([], [])), \
                mock.patch.object(sc, "firmware_now", return_value=firmware), \
                mock.patch.object(sc, "bricked", return_value=False), mock.patch.object(fs, "run_logs", return_value=[]), \
                mock.patch.object(sc, "screen_problems", return_value=[]), \
                mock.patch.object(sc, "keep", side_effect=lambda results, verdict, left: verdict):
            return sc.finish_readout_all([(code, "")], judged, None)

    def test_the_verdict(self):
        clean = {"problems": [], "429": 0}
        self.assertEqual(self.finish([clean]), "PASS")
        self.assertEqual(self.finish([{"problems": [("FAIL-NOT-SENT", "x"), ("FAIL-NOT-STORED", "y")], "429": 0},
                                      clean]), "FAIL-NOT-SENT")
        self.assertEqual(self.finish([clean], code=1), "FAIL-NOT-FINISHED")
        self.assertEqual(self.finish([clean], firmware="installer"), "FAIL-NOT-SONOFF")
        # The many account is for the API's limit: never reached, it proves nothing.
        self.assertEqual(self.finish([clean], account="many"), "NOT-FIRED")
        self.assertEqual(self.finish([clean, {"problems": [], "429": 1}], account="many"), "PASS")

    def test_run_again_until_a_run_finishes_as_the_run_advised(self):
        sc = self.scenario(step="read", fault="crash", arg=1, account="mixed")
        sc.fired = 1.0
        advice = ["the device is on this tool's installer from an earlier run ... switch the device off and on", "", ""]
        with mock.patch.object(fs.threading, "Thread"), mock.patch.object(fs.time, "sleep"), \
                mock.patch.object(sc, "run_readout_all", side_effect=[(137, ""), (1, ""), (0, "")]) as runs, \
                mock.patch.object(sc, "last_said", side_effect=advice), \
                mock.patch.object(sc, "ex") as ex, mock.patch.object(sc, "finish_readout_all") as finish:
            sc.go_readout_all()
        self.assertEqual(runs.call_args_list[0].kwargs, {"fresh": True})
        self.assertEqual(len(runs.call_args_list), 3)
        self.assertEqual(ex.call_args_list, [mock.call(fs.DEVICE_CMD, "restart")])
        self.assertEqual([code for code, _ in finish.call_args.args[0]], [137, 1, 0])

    def test_a_fault_that_never_came_proves_nothing(self):
        sc = self.scenario(step="read", fault="deviceoff", arg=1)
        with mock.patch.object(fs.threading, "Thread"), mock.patch.object(sc, "run_readout_all", return_value=(0, "")), \
                mock.patch.object(sc, "finish_readout_all") as finish:
            sc.go_readout_all()
        self.assertEqual(finish.call_args.args[2], "NOT-FIRED")

    def test_each_run_judged_from_its_own_api_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = self.scenario()
            sc.state, sc.log = Path(tmp, "state"), str(Path(tmp, "scenario.log"))
            run = sc.state / "work/captures/devices/unidentified/runs/1/run.log"
            run.parent.mkdir(parents=True)
            run.write_text("=== run started ===\n" + self.RUN, encoding="utf-8")
            (run.parent / "readout-1000000001.tar").write_bytes(b"tar")
            (sc.state / "api").mkdir()
            (sc.state / "api/requests.log").write_text(self.WANTED + "\n", encoding="utf-8")
            (sc.state / "api-server.out").write_text("Error: column index out of range\n", encoding="utf-8")
            judged = []
            with mock.patch.object(sc, "run_tool", return_value=0), \
                    mock.patch.object(fs, "device_product", return_value=self.DEVICE), \
                    mock.patch.object(fs.time, "time", return_value=0):
                code, said = sc.run_readout_all(judged)
            self.assertEqual(code, 0)
            self.assertTrue(Path(tmp, "scenario-api-1/requests.log").exists())   # kept: the next run starts afresh
            self.assertIn("out of range", Path(tmp, "scenario-api-1/api-server.out").read_text(encoding="utf-8"))
            self.assertIn("FAIL-KEPT-UNSENT", [verdict for verdict, _ in judged[0]["problems"]])
            self.assertEqual((judged[0]["devices"], judged[0]["asked"]), (3, 1))


TH = kinds.SONOFF_TH_ELITE  # its layout leaves Tasmota through a temporary table


class DeviceRoutesTests(unittest.TestCase):
    """Each route's steps, as the tool takes them on the device under test."""

    def routes_of(self, kind):
        with mock.patch.object(fs, "DEVICE", kind), mock.patch.object(fs, "ROUTES", fs.routes_for(kind)):
            return fs.ROUTES, {sc["id"] for sc in fs.scenarios()}

    def tool_steps(self, route, kind):
        """The steps the tool takes on `route` that write to or wait on the device."""
        source, target = route.split(">")
        source = fs.STARTS_ON.get(source, source)
        if (source, target) == ("tasmota", "esphome") and not fs.layouts.of(kind.layout).needs_temporary:
            source = espressif.DIRECT_FROM_TASMOTA
        asks = {"A5", "A2"} | ({"C2"} if route in fs.C2_ROUTES else set())
        steps = [step for step, _, _ in espressif.steps_for(target, source) if step not in asks]
        # With no backup, only going back differs from tasmota>sonoff, whose
        # steps before it are faulted already.
        return [step for step in steps if step.startswith("R")] if route.startswith("nobackup>") else steps

    def test_every_step_the_tool_takes_is_faulted(self):
        for kind in (kinds.SONOFF_S60, TH):
            routes, _ = self.routes_of(kind)
            for route, (_, _, steps) in routes.items():
                if route.startswith("update>"):
                    continue
                with self.subTest(kind=kind.key, route=route):
                    self.assertEqual([fs.STEP_OF.get(step, step) for step in steps if step != "C3fb"],
                                     self.tool_steps(route, kind))

    def test_the_identity_is_kept_on_each_route_that_keeps_it(self):
        _, ids = self.routes_of(kinds.SONOFF_S60)
        for route in ("sonoff>tasmota", "sonoff>esphome", "esphome>tasmota"):
            for fault in ("deviceoff-1", "crash-1", "corrupt-once"):
                self.assertIn(f"{route}:K1:{fault}", ids)

    def test_back_to_sonoff_with_no_backup(self):
        _, ids = self.routes_of(kinds.SONOFF_S60)
        for wanted in ("nobackup>sonoff:R1:deviceoff-1", "nobackup>sonoff:R1@90:deviceoff-1",
                       "nobackup>sonoff:R1:double-deviceoff-1", "nobackup>sonoff:R2:outage-30"):
            self.assertIn(wanted, ids)

    def test_a_device_on_a_temporary_table_goes_to_esphome_by_the_hub(self):
        routes, ids = self.routes_of(TH)
        self.assertNotIn("M1", routes["tasmota>esphome"][2])
        self.assertIn("tasmota>esphome:D5:deviceoff-1", ids)
        for route in ("tasmota>sonoff", "tasmota>esphome"):
            for at in (10, 50, 90):
                self.assertIn(f"{route}:D5ot@{at}:deviceoff-1", ids)

    def test_its_own_lengths_on_a_temporary_table(self):
        routes, _ = self.routes_of(TH)
        self.assertEqual(routes["sonoff>tasmota"][2]["E3"], fs.TEMPORARY_TABLE_LENGTHS["E3"])
        self.assertEqual(fs.routes_for(kinds.SONOFF_S60)["sonoff>tasmota"][2]["E3"], 5)

    def test_a_starting_device_is_made_with_its_own_kinds_firmware(self):
        # It once was the S60's and the ESP32-C3's whatever the device, so a
        # classic ESP32's starting devices were never found stale.
        pins = json.loads((fs.REPO / "artifacts/firmware.json").read_text(encoding="utf-8"))["espressif"]
        for kind in (kinds.SONOFF_S60, kinds.SONOFF_POW_ELITE, TH):
            with self.subTest(kind.key), mock.patch.object(fs, "DEVICE", kind):
                self.assertEqual(fs.installs("esphome"), {"image": pins[kind.key]["esphome"],
                                                          "bootloader": pins[kind.chip]["bootloader"],
                                                          "mover": pins[kind.chip]["mover"]})
                self.assertEqual(fs.installs("tasmota"), pins[kind.chip]["tasmota"])

    def test_the_own_table_only_where_there_is_a_temporary_one(self):
        for kind in (kinds.SONOFF_S60, kinds.SONOFF_POW_ELITE):
            self.assertFalse([i for i in self.routes_of(kind)[1] if ":D5ot" in i], kind.key)

    def test_earlier_releases_only_on_an_s60(self):
        for kind in (TH, kinds.SONOFF_POW_ELITE):
            routes, ids = self.routes_of(kind)
            self.assertNotIn("update>esphome", routes)
            self.assertFalse([i for i in ids if ":legacy-" in i], kind.key)
        self.assertIn("sonoff>tasmota:C2:legacy-v1.2.0", self.routes_of(kinds.SONOFF_S60)[1])

    def test_the_kind_is_the_device_records(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(records, "DEVICES", Path(tmp)):
            self.assertIs(fs.device_kind(MAC), kinds.SONOFF_S60)
            records.device_dir(MAC).mkdir()
            (records.device_dir(MAC) / records.DEVICE_FILE).write_text('{"product_model": "THR320D"}', encoding="utf-8")
            self.assertIs(fs.device_kind(MAC), TH)
        self.assertIs(fs.device_kind(None), kinds.SONOFF_S60)

    def test_the_start_with_no_backup_is_a_copy_without_it(self):
        with tempfile.TemporaryDirectory() as emu, mock.patch.object(fs, "EMU", Path(emu)), \
                mock.patch.object(fs, "MAC", MAC):
            device = fs.converted_start("tasmota") / "captures/devices" / records.folder_name(MAC)
            (device / records.BACKUP).mkdir(parents=True)
            (device / records.BACKUP / "flash.bin").write_bytes(b"stock")
            (device / records.DEVICE_FILE).write_text("{}", encoding="utf-8")
            (fs.converted_start("tasmota") / "flash.bin").write_bytes(b"tasmota")
            fs.no_backup_start()
            copy = fs.converted_start("nobackup")
            self.assertEqual((copy / "flash.bin").read_bytes(), b"tasmota")
            self.assertTrue((copy / "captures/devices" / records.folder_name(MAC) / records.DEVICE_FILE).is_file())
            self.assertFalse((copy / "captures/devices" / records.folder_name(MAC) / records.BACKUP).exists())
            self.assertEqual((device / records.BACKUP / "flash.bin").read_bytes(), b"stock")

    def test_the_identity_is_the_devices_own_as_the_report_words_it(self):
        self.assertTrue(fs.kept_identity_reported(["EWeLink account", "Device "]))
        self.assertFalse(fs.kept_identity_reported(["Rebuilt from your eWeLink account"]))
        self.assertFalse(fs.kept_identity_reported([]))

    def test_the_template_is_the_devices_own(self):
        # Seen: a TH Elite on its own template judged BAD-TEMPLATE, for not being an S60's.
        module = {"Module": {"0": "Sonoff-THR320D"}}
        for kind, name, own in ((TH, "Sonoff-THR320D", True), (TH, kinds.SONOFF_S60.display_name, False),
                                (kinds.SONOFF_S60, kinds.SONOFF_S60.display_name, True),
                                (kinds.SONOFF_S60, "Generic", False)):
            with self.subTest(kind=kind.key, name=name), mock.patch.object(fs, "DEVICE", kind):
                self.assertEqual(fs.own_template({"NAME": name}, module), own)
        with mock.patch.object(fs, "DEVICE", TH):
            self.assertFalse(fs.own_template({"NAME": "Sonoff-THR320D"}, {"Module": {"1": "Sonoff Basic"}}))

    def test_its_start_counts_as_the_tasmota_one(self):
        with mock.patch.object(fs, "installs", side_effect=lambda target: {"tasmota": {"v": 1}}[target]), \
                mock.patch.object(fs, "converted_start", return_value=Path("/nonexistent")):
            self.assertEqual(fs.stale_starts([{"route": "nobackup>sonoff", "fault": "deviceoff"}]),
                             [fs.saved("suite-tasmota-nobackup")])


if __name__ == "__main__":
    unittest.main()
