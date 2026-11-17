"""The device on its new firmware: started, on the Wi-Fi, answering as what was
installed, and backed up (B4)."""
import dataclasses
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.test_openbeken import FakeOpenBeken
from tests.autoflash.routes.beken.test_beken import a_context
from tools.autoflash import detect, errors, kinds, openbeken, records, state
from tools.autoflash.routes import arrival, beken
from tools.autoflash.routes.beken import arrived, images, onward


MAC = "12:34:56:ab:cd:ef"


class ArrivedTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenBeken()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for patch in (mock.patch.object(arrived.output, "say"), mock.patch.object(arrived.output, "progress"),
                      mock.patch.object(arrived.output, "progress_clear"), mock.patch.object(arrived.output, "tell"),
                      mock.patch.object(openbeken.time, "sleep"),
                      # ESPHome's own network not to be seen, unless a test says so.
                      mock.patch.object(arrived.join, "set_up_through_own_network", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_k1_finds_the_openbeken_that_says_it_is_on_an_s61(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host)
            result = arrived.step_openbeken_check(ctx)
            self.assertEqual((result["version"], result["name"]), ("1.0.0", "openbeken-abcdef"))
            self.assertEqual(ctx.state.input("device_name"), "openbeken-abcdef")
            # Its module was asked, and said it is not the interim firmware: nothing changed.
            self.assertIn("AutoflashInterim", self.server.commands)
            self.assertNotIn("told", result)
            self.assertFalse([c for c in self.server.commands if c.startswith("AutoflashInterim ")])
            # And whether its first start took the meter's calibration from the stock store, for the report.
            self.assertEqual(result["calibration"], "stock store")
            with mock.patch.object(openbeken, "info", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not answered.*switch the device off and on"):
                    arrived.step_openbeken_check(ctx, wait=10)

    def test_k1_times_the_last_look_and_a_restart_asked_for_is_done_with_once_found(self):
        clock = iter([100.0, 110.0, 115.04])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host)
            ctx.state.set_input("restart_asked", True)
            found = {"build": "OpenBK7238 1.0.0", "shortName": "openbeken-abcdef", "interim": False}
            with mock.patch.object(arrived.join, "wait_for", return_value=found), \
                    mock.patch.object(openbeken, "calibration_state", return_value="stock store"), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=lambda: next(clock)):
                result = arrived.step_openbeken_check(ctx)
            self.assertEqual(result["seconds"], 5.0)
            self.assertIsNone(ctx.state.input("restart_asked"))

    def test_k1_says_the_meter_calibration_was_not_carried_over_or_cannot_say(self):
        for calibration, said in (("missing", "missing"), (None, "not the stock store's"),
                                  # An OpenBeken whose driver predates the command.
                                  ("unknown", None)):
            with self.subTest(said=said), tempfile.TemporaryDirectory() as tmp:
                self.server.calibration = calibration
                self.server.unknown = {"AutoflashCalibration"} if calibration == "unknown" else set()
                result = arrived.step_openbeken_check(a_context(tmp, device_ip=self.server.host))
                self.assertEqual(result["calibration"], said)

    def test_k1_asks_nothing_of_a_meter_on_a_device_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, device_kind="sonoff-basic-1gs")
            self.server.kind = "sonoff-basic-1gs"
            result = arrived.step_openbeken_check(ctx)
            self.assertNotIn("calibration", result)
            self.assertFalse([c for c in self.server.commands if c.startswith("AutoflashCalibration")])

    def test_j1_asks_the_interim_firmware_nothing_of_the_meter(self):
        self.server.interim = True
        with tempfile.TemporaryDirectory() as tmp:
            result = arrived.step_interim_check(a_context(tmp, device_ip=self.server.host))
            self.assertNotIn("calibration", result)

    def test_k1_on_the_interim_firmware_makes_it_the_devices_own_and_restarts_it(self):
        # Not sent as one on a run to OpenBeken; were it so, it is told it is
        # not, and restarted so its network's name for it follows.
        self.server.interim, self.server.short_name = True, "autoflash-recovery-CDEF"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host)
            with mock.patch.object(onward.output, "say") as said:
                result = arrived.step_openbeken_check(ctx)
            self.assertEqual((result["name"], result["told"]), ("openbeken-abcdef", "not interim"))
            self.assertEqual(ctx.state.input("device_name"), "openbeken-abcdef")
            # The meter asked of it once it has restarted under its name.
            self.assertEqual(self.server.commands[-3:], ["AutoflashInterim off", "reboot", "AutoflashCalibration stock"])
            self.assertIn("Restarting it, so your network knows it by its new name",
                          " ".join(str(call.args[0]) for call in said.call_args_list))
            self.assertFalse(self.server.interim)

    def waiting_on_sonoff(self, ctx, answers):
        """J1 with the device answering as Sonoff's firmware throughout, and the
        question after each wait answered with `answers` in turn."""
        with mock.patch.object(openbeken, "info", return_value=None), \
                mock.patch.object(detect, "identify", return_value="sonoff"), \
                mock.patch.object(arrived.join.time, "sleep"), \
                mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 100000, 100))), \
                mock.patch.object(arrival.prompts, "section"), mock.patch.object(arrived.output, "transient_begin"), \
                mock.patch.object(arrived.output, "transient_end"), \
                mock.patch.object(arrival.prompts, "ask_choice", side_effect=answers) as asked:
            try:
                arrived.step_interim_check(ctx, wait=1000)
            finally:
                self.asked = asked.call_count

    def test_j1_after_b2_sends_the_run_back_to_it_when_a_restart_leaves_sonoffs_firmware(self):
        # The image was sent whole, but the device, switched off and on, starts
        # Sonoff's firmware again: it was not installed, and never will be.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("B2", state.DONE)
            with self.assertRaises(errors.GoBack) as back:
                self.waiting_on_sonoff(ctx, [0])
            self.assertEqual((back.exception.step, back.exception.outcome), ("B2", "not installed"))
            self.assertIn("The interim firmware was not installed", back.exception.reason)
            # Before the restart, Sonoff's firmware answering said nothing: asked once, to switch it off and on.
            self.assertEqual(self.asked, 1)

    def test_a_switch_off_and_on_asked_for_is_taken_as_done_by_the_next_run(self):
        # An unattended run stops to ask; run again after it, Sonoff's firmware
        # answering means the image was not taken - at once, not after a wait.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("B2", state.DONE)
            with mock.patch.object(arrival.prompts, "UNATTENDED", True), self.assertRaises(errors.Abort):
                self.waiting_on_sonoff(ctx, [])
            self.assertTrue(ctx.state.input("restart_asked"))
            with self.assertRaises(errors.GoBack):
                self.waiting_on_sonoff(ctx, [])
            self.assertEqual(self.asked, 0)
            self.assertIsNone(ctx.state.input("restart_asked"))   # the image sent again starts afresh

    def k2_with_openbeken_up_for(self, ctx, uptime):
        """K2 with the device answering as OpenBeken, up `uptime` seconds."""
        with mock.patch.object(arrived.esphome, "is_esphome", return_value=False), \
                mock.patch.object(openbeken, "info", return_value={"uptime_s": uptime}), \
                mock.patch.object(arrived.join.time, "sleep"), \
                mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 100000, 100))), \
                mock.patch.object(arrival.prompts, "UNATTENDED", True):
            arrived.step_esphome_check(ctx, wait=1000)

    def test_k2_after_j2_sends_the_run_back_when_openbeken_restarted_since_the_fetch(self):
        # OpenBeken restarts by itself once it has ESPHome: back on OpenBeken
        # since, its bootloader did not take the image - no restart to ask for.
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("J2", state.DONE)
            ctx.state.capture("J2.fetched_at", round(time.time()) - 120)
            with self.assertRaises(errors.GoBack) as back:
                self.k2_with_openbeken_up_for(ctx, 30)
            self.assertEqual(back.exception.step, "J2")
            self.assertIn("ESPHome was not installed", back.exception.reason)
            # Up since before the fetch: not restarted yet, only waited for.
            with self.assertRaisesRegex(errors.Abort, "switch the device off and on"):
                self.k2_with_openbeken_up_for(ctx, 600)

    def test_k2_sees_the_restart_by_the_uptime_before_the_fetch_whatever_its_clock(self):
        # The emulated device's clock runs fast: its uptime soon outgrows the time
        # since the fetch, but restarted it is still below what it was before.
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("J2", state.DONE)
            ctx.state.capture("J2.fetched_at", round(time.time()) - 20)
            ctx.state.capture("J2.uptime_before", 900)
            with self.assertRaises(errors.GoBack):
                self.k2_with_openbeken_up_for(ctx, 300)
            with self.assertRaisesRegex(errors.Abort, "switch the device off and on"):
                self.k2_with_openbeken_up_for(ctx, 950)

    def test_k2_sees_a_restart_by_its_uptime_falling_below_the_highest_seen(self):
        # The image sent again just after a refusal: OpenBeken was only just up,
        # so its uptime before is small, and the emulated device's fast clock soon
        # outgrows both that and the time since. Seen in the fault suite: the
        # second refusal in a row waited out, and a switch off and on asked for.
        import time
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            ctx.state.mark("O1", state.DONE)
            ctx.state.capture("O1.fetched_at", round(time.time()) - 20)
            ctx.state.capture("O1.uptime_before", 15)
            # Seen still up after the fetch, then back after a restart, its clock fast.
            with mock.patch.object(arrived.esphome, "is_esphome", return_value=False), \
                    mock.patch.object(openbeken, "info", side_effect=[{"uptime_s": 90}, None, {"uptime_s": 60}]), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 100000, 100))), \
                    mock.patch.object(arrived.join.prompts, "find_ssid", return_value=None), \
                    mock.patch.object(arrival.prompts, "UNATTENDED", True):
                with self.assertRaises(errors.GoBack) as back:
                    arrived.step_esphome_check(ctx, wait=1000)
            self.assertEqual(back.exception.step, "O1")

    def test_k2_waits_for_esphome_to_answer_with_its_relay(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            with mock.patch.object(arrived.esphome, "is_esphome", return_value=True), \
                    mock.patch.object(arrived.esphome, "relay", side_effect=[None, False]) as relay, \
                    mock.patch.object(arrived.esphome, "calibration_state", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(50_000, 150_000, 100))):
                arrived.step_esphome_check(ctx, wait=1000)
            self.assertEqual(relay.call_count, 2)

    def test_k2_sets_up_an_esphome_that_cannot_join_through_its_own_network(self):
        # As the Espressif route's I2.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            with mock.patch.object(arrived.join, "wait_for", side_effect=errors.Abort("not on your Wi-Fi")), \
                    mock.patch.object(arrived.join, "set_up_through_own_network", return_value=("Home", "pw")) as own, \
                    mock.patch.object(arrived.esphome, "calibration_state", return_value=None):
                result = arrived.step_esphome_check(ctx, wait=1000)
            self.assertEqual(result["set_up_through"], "its own network")
            self.assertEqual(own.call_args.args[1], "ESPHome")

    def test_k2_carries_on_when_esphome_is_back_on_your_wifi_by_itself(self):
        # Its own network raised while the router was off, it rejoined once the
        # router was back, before this computer joined that network.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            with mock.patch.object(arrived.join, "wait_for", side_effect=errors.Abort("not on your Wi-Fi")), \
                    mock.patch.object(arrived.join, "set_up_through_own_network", side_effect=arrived.join.BackByItself), \
                    mock.patch.object(arrived.esphome, "calibration_state", return_value=None):
                result = arrived.step_esphome_check(ctx, wait=1000)
            self.assertNotIn("set_up_through", result)

    def test_k2_of_a_device_with_no_relay_waits_for_esphome_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            bare = dataclasses.replace(kinds.SONOFF_BASIC_1GS, has_relay=False)
            with mock.patch.object(arrived.kinds, "of_run", return_value=bare), \
                    mock.patch.object(arrived.esphome, "is_esphome", return_value=True), \
                    mock.patch.object(arrived.esphome, "relay", return_value=None) as relay, \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(50_000, 150_000, 100))):
                arrived.step_esphome_check(ctx, wait=1000)
            relay.assert_not_called()

    def test_k2_of_a_device_with_no_meter_says_nothing_of_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            with mock.patch.object(arrived.kinds, "of_run", return_value=kinds.SONOFF_BASIC_1GS), \
                    mock.patch.object(arrived.esphome, "is_esphome", return_value=True), \
                    mock.patch.object(arrived.esphome, "relay", return_value=False), \
                    mock.patch.object(arrived.esphome, "calibration_state") as calibration, \
                    mock.patch.object(arrived.output, "say") as said, \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(50_000, 150_000, 100))):
                result = arrived.step_esphome_check(ctx, wait=1000)
            calibration.assert_not_called()
            self.assertNotIn("calibration", result)
            self.assertNotIn("meter", " ".join(str(call.args[0]) for call in said.call_args_list))

    def test_without_b2_a_device_on_sonoffs_firmware_is_only_waited_for(self):
        # Nothing this run sent to go back to: asked again, as before.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            with self.assertRaises(errors.Abort):
                self.waiting_on_sonoff(ctx, [0, errors.Abort("stopped")])
            self.assertEqual(self.asked, 2)

    def test_j1_finds_the_interim_firmware_and_names_it_so(self):
        self.server.interim, self.server.short_name = True, "autoflash-recovery-CDEF"
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, firmware="esphome")
            with mock.patch.object(arrived.output, "say") as said:
                result = arrived.step_interim_check(ctx)
            self.assertEqual(result, {"seconds": result["seconds"], "version": "1.0.0"})
            self.assertNotIn("reboot", self.server.commands)
            self.assertEqual(said.call_args.args[0][:46], "    The interim firmware 1.0.0 answered after ")
            # Its name is not the device's: ESPHome, next, names it.
            self.assertIsNone(ctx.state.input("device_name"))
            with mock.patch.object(openbeken, "info", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "^The interim firmware has not answered"):
                    arrived.step_interim_check(ctx, wait=10)

    def test_j1_on_an_openbeken_that_does_not_know_itself_the_interim_firmware_tells_it(self):
        # Its update cut off in its last few kilobytes: the image whole, so
        # installed, but not the marker after it. Told, so the hook wipes it
        # after J2 - but not restarted: never named wrongly on the network,
        # only not renamed for its few minutes.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, firmware="esphome")
            with mock.patch.object(onward.output, "say") as said:
                result = arrived.step_interim_check(ctx)
            self.assertEqual(result["told"], "interim")
            self.assertEqual(self.server.commands[-1], "AutoflashInterim on")
            self.assertNotIn("reboot", self.server.commands)
            self.assertTrue(self.server.interim)
            shown = " ".join(str(call.args[0]) for call in said.call_args_list)
            self.assertIn("it is the interim firmware now, under the name it started with", shown)
            self.assertNotIn("Restarting", shown)
            self.assertNotIn("openbeken", shown.lower())

    def test_j1_and_k1_wait_for_an_openbeken_that_will_not_say_which_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, firmware="esphome")
            with mock.patch.object(openbeken, "is_interim", return_value=None), \
                    mock.patch.object(arrived.join.time, "sleep"), \
                    mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.join.time, "monotonic", side_effect=iter(range(0, 10000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not answered"):
                    arrived.step_interim_check(ctx, wait=10)
            self.assertFalse([c for c in self.server.commands if c.startswith("AutoflashInterim ")])

    def not_back_then_back(self, ctx, choices, *, switch=None):
        """K2 on a device that does not answer until it has been switched off and
        on - by the person, or by the tool where the emulator says how - with
        `choices` the answers given at the question; what it offered."""
        offered = []
        looks = []

        def look(*args, **kwargs):
            looks.append(1)
            if len(looks) == 1:
                raise errors.Abort("did not answer")
            return True

        def choose(question, options):
            offered.append(list(options))
            answer = choices.pop(0)
            if answer == "q":
                raise arrival.prompts.QuitRun("stopped at an operator prompt")
            return answer

        env = {arrival.EMULATED_POWER: str(switch)} if switch else {}
        # K2's start, the first look's, when the person said it was back on, the
        # next look's, when that look found it, and when K2 had its answer.
        clock = iter([100.0, 110.0, 115.0, 120.0, 122.0, 125.04])
        with mock.patch.object(arrived.join, "wait_for", side_effect=look), \
                mock.patch.object(arrived.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(arrived.esphome, "calibration_state", return_value=None), \
                mock.patch.object(arrival.prompts, "ask_choice", side_effect=choose), \
                mock.patch.object(arrival.prompts, "section"), \
                mock.patch.object(arrived.output, "transient_begin"), mock.patch.object(arrived.output, "transient_end"), \
                mock.patch.object(arrived.time, "sleep") as self.slept, \
                mock.patch.dict(arrival.os.environ, env, clear=False), \
                mock.patch.object(records, "DEVICES", ctx.state.path.parent / "devices"):
            if not switch:
                arrival.os.environ.pop(arrival.EMULATED_POWER, None)
            self.result = arrived.step_esphome_check(ctx)
        return offered, len(looks)

    def test_an_attended_run_waits_for_the_device_switched_off_and_on_then_looks_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip="192.0.2.50")
            offered, looks = self.not_back_then_back(ctx, [0])
            self.assertEqual((len(offered[0]), looks), (1, 2))   # no emulator: one choice, and q
            # Counted for the report, by whom.
            self.assertEqual(ctx.state.input("power_cycles"), {"by hand": 1})
            # Timed from the look that found it, not from before the person was asked.
            self.assertEqual(self.result, {"seconds": 5.0, "calibration": None})
            # And for the report, from the person saying it was back on.
            self.assertEqual(ctx.state.input("back_after_power_cycle"), 7)

    def test_in_the_emulator_the_tool_can_switch_the_device_off_and_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip="192.0.2.50")
            switch = Path(tmp, "power")
            switch.write_text("on\n", encoding="utf-8")
            written = []
            real_write = Path.write_text

            def write_text(path, text, *args, **kwargs):
                if path == switch:
                    written.append(text.strip())
                return real_write(path, text, *args, **kwargs)

            with mock.patch.object(Path, "write_text", write_text):
                offered, looks = self.not_back_then_back(ctx, [1], switch=switch)
            self.assertEqual(len(offered[0]), 2)
            self.assertEqual((written, looks), (["off", "on"], 2))
            self.slept.assert_called_once_with(arrival.EMULATED_OFF)  # off long enough to stop it
            self.assertEqual(ctx.state.input("power_cycles"), {"by the tool": 1})
            self.assertEqual(switch.read_text(encoding="utf-8"), "on\n")

    def test_q_at_the_question_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip="192.0.2.50")
            with self.assertRaises(arrival.prompts.QuitRun):
                self.not_back_then_back(ctx, ["q"])

    def test_b4_backs_up_the_devices_own_data_with_sonoffs_firmware_beside_it(self):
        from tests.autoflash.test_openbeken import FLASH
        region = FLASH[:0x20000] if len(FLASH) >= 0x20000 else FLASH
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, stock_version="1.0.2")
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(records, "BEKEN_REGION", (0x3E0000, len(region))), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                    mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                result = arrived.step_backup(ctx)
                self.assertEqual(result, {"outcome": "saved", "stage_outcome": "complete"})
                self.assertIs(ctx.state.data["captures"]["B4.flash_matched"], True)
                found = records.find_backup(MAC)
                self.assertEqual((found.family, found.version), ("beken", "1.0.2"))
                self.assertEqual((found.directory / records.BEKEN_BACKUP_BIN).read_bytes(), region)
                self.assertEqual((found.directory / "stock-app-1.0.2.rbl").read_bytes(), b"RBL stock")
                manifest = json.loads((found.directory / records.BEKEN_MANIFEST).read_text())
                self.assertEqual(manifest["regions"][records.BEKEN_BACKUP_BIN]["sha256"], hashlib.sha256(region).hexdigest())
                # Taken once: a second run keeps the first, and says where it is.
                with mock.patch.object(arrived.output, "say") as said:
                    self.assertEqual(arrived.step_backup(ctx)["outcome"], "existing")
                self.assertIn(found.directory.name, said.call_args.args[0])
                self.assertEqual(ctx.state.input("backup_outcome"), "existing")
                # None wanted is asked first, as C3 asks it.
                ctx.args.no_backup = True
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "not wanted")

    def test_b4_without_sonoffs_firmware_says_only_what_it_backed_up(self):
        from tests.autoflash.test_openbeken import FLASH
        region = FLASH[:0x20000] if len(FLASH) >= 0x20000 else FLASH
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, stock_version="1.0.2")
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(records, "BEKEN_REGION", (0x3E0000, len(region))), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                    mock.patch.object(images, "stock_rbl", side_effect=errors.Abort("not fetched")), \
                    mock.patch.object(arrived.output, "say") as said:
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "saved")
            backed_up = [call.args[0] for call in said.call_args_list if "Backed up" in call.args[0]]
            self.assertEqual(len(backed_up), 1)
            self.assertNotIn("Sonoff's firmware", backed_up[0])

    def b4_reading(self, tmp, reads):
        ctx = a_context(tmp, device_ip=self.server.host, stock_version="1.0.2")
        with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                mock.patch.object(arrived, "_read_region", side_effect=reads) as read, \
                mock.patch.object(arrived.openbeken, "info", return_value={"build": "x"}), \
                mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")), \
                mock.patch.object(arrived.time, "sleep") as sleep:
            return arrived.step_backup(ctx), read.call_count, sleep, ctx

    def test_b4_tries_again_after_a_dropped_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            gone = OSError("short read of the device's flash")
            result, reads, sleep, ctx = self.b4_reading(tmp, [gone, gone, b"\x01" * 0x100])
            self.assertEqual(result["outcome"], "saved")
            self.assertEqual(reads, 3)
            self.assertEqual(sleep.call_args_list.count(mock.call(arrived.BACKUP_PAUSE)), 2)
            self.assertEqual(ctx.state.data["captures"]["B4.attempts"], 3)

    def test_b4_goes_on_without_a_backup_after_three_tries(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(arrived.output, "tell") as told, \
                mock.patch.object(arrived.output, "say") as said:
            result, reads, _, _ = self.b4_reading(tmp, OSError("short read of the device's flash"))
            self.assertEqual(result["outcome"], "failed")
            self.assertEqual(reads, 3)
        # On screen, no promise of a next time: a run from ESPHome to Sonoff's firmware has no B4.
        shown = told.call_args.args[0]
        self.assertTrue(shown.endswith("going on without a backup. The device keeps it."), shown)
        self.assertIn("A later run that installs OpenBeken, or starts from it, tries the backup again.",
                      said.call_args.args[0])
        for firmware, source in (("esphome", "openbeken"), ("sonoff", "openbeken"), ("openbeken", "sonoff"),
                                 ("openbeken", "esphome")):
            self.assertIn("B4", [step for step, _, _ in beken.steps_for(firmware, source)], (source, firmware))
        self.assertNotIn("B4", [step for step, _, _ in beken.steps_for("sonoff", "esphome")])

    def test_b4_waits_for_the_device_to_answer_before_trying_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            answers = [None]

            def read(_host):
                if answers[0] is None and read.calls:
                    self.fail("read again before the device answered")
                read.calls += 1
                if read.calls == 1:
                    raise OSError("gone")
                return b"\x01" * 0x100
            read.calls = 0

            def sleeping(seconds):
                if seconds != arrived.BACKUP_PAUSE:
                    answers[0] = {"build": "x"}  # it answers after the first wait

            ctx = a_context(tmp, device_ip=self.server.host, stock_version="1.0.2")
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(arrived, "_read_region", read), \
                    mock.patch.object(arrived.openbeken, "info", side_effect=lambda *a, **k: answers[0]), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                    mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")), \
                    mock.patch.object(arrived.time, "sleep", side_effect=sleeping):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "saved")

    def test_b4_keeps_nothing_that_is_not_this_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host)
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(records, "BEKEN_REGION", (0x3E0000, 0x100)), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value="aa:bb:cc:dd:ee:ff"):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "unreadable")
                self.assertIsNone(records.find_backup(MAC))
                self.assertIs(ctx.state.data["captures"]["B4.flash_matched"], False)
            ctx.args.no_backup = True
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "not wanted")

    def test_b4_with_no_hardware_address_takes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, device_mac=None)
            with mock.patch.object(arrived, "_back_up") as backed_up:
                self.assertEqual(arrived.step_backup(ctx), {"outcome": "not taken", "stage_outcome": "skipped"})
            backed_up.assert_not_called()

    def test_b4_on_a_run_to_esphome_reads_through_the_interim_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_ip=self.server.host, firmware="esphome", stock_version="1.0.2")
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(records, "BEKEN_REGION", (0x3E0000, 0x100)), \
                    mock.patch.object(arrived.backup, "factory_mac", return_value=MAC), \
                    mock.patch.object(images, "stock_rbl", return_value=(b"RBL stock", "1.0.2")):
                self.assertEqual(arrived.step_backup(ctx)["outcome"], "saved")
                manifest = json.loads((records.find_backup(MAC).directory / records.BEKEN_MANIFEST).read_text())
                self.assertEqual(manifest["source"], "OpenBeken GET /api/flash after conversion")

    def k3_context(self, tmp):
        ctx = a_context(tmp, device_ip="192.0.2.50")
        ctx.email = lambda: "someone@example.invalid"
        ctx.ewelink = mock.sentinel.account
        return ctx

    def test_k3_takes_the_device_back_only_once_it_answers_through_ewelink(self):
        # Online from before it left - eWeLink keeps a device that went silent
        # online for minutes - until Sonoff's firmware is back and answers.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.k3_context(tmp)
            ctx.account_devices = lambda: [{"deviceid": "1001234abc", "online": True}]
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value=None), \
                    mock.patch.object(arrival.ewelink_relay, "answers", side_effect=[False, None, True]) as asked, \
                    mock.patch.object(arrived.time, "sleep") as slept, \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(50_000, 60_000, 5))):
                records.remember_device(MAC, ewelink_id="1001234abc")
                result = arrived.step_sonoff_check(ctx)
                self.assertEqual(result, {"seconds": 30, "confirmed_by": "eWeLink"})
                self.assertEqual(asked.call_count, 3)
                self.assertEqual(slept.call_count, 2)  # a pause before asking eWeLink again
                self.assertEqual(asked.call_args.kwargs["device_id"], "1001234abc")

    def test_k3_waits_for_ewelink_to_show_the_device_online(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.k3_context(tmp)
            answers = iter([[{"deviceid": "1001234abc", "online": False}], [{"deviceid": "1001234abc", "online": True}]])
            ctx.account_devices = lambda: next(answers)
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value=None), \
                    mock.patch.object(arrival.ewelink_relay, "answers", return_value=True), \
                    mock.patch.object(arrived.time, "sleep"), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(50_000, 60_000, 5))):
                records.remember_device(MAC, ewelink_id="1001234abc")
                self.assertEqual(arrived.step_sonoff_check(ctx)["confirmed_by"], "eWeLink")
            # Still answering as the firmware it was to leave: not back.
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value="openbeken"), \
                    mock.patch.object(arrived.time, "sleep"), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(50_000, 60_000, 100))):
                with self.assertRaisesRegex(errors.Abort, "still answers"):
                    arrived.step_sonoff_check(ctx, wait=50)
            # Never shown online: an unattended run stops, saying to switch it off and on.
            ctx.account_devices = lambda: [{"deviceid": "1001234abc", "online": False}]
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(detect, "identify", return_value=None), \
                    mock.patch.object(arrived.time, "sleep"), mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                    mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(50_000, 60_000, 100))):
                with self.assertRaisesRegex(errors.Abort, "has not come back on your Wi-Fi or in eWeLink.*switch it off and on"):
                    arrived.step_sonoff_check(ctx, wait=50)


    def k3_waiting(self, ctx, tmp, wait=50):
        with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                mock.patch.object(detect, "identify", return_value=None), \
                mock.patch.object(arrival.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(arrived.time, "sleep") as self.slept, \
                mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                mock.patch.object(arrived.time, "monotonic", side_effect=iter(range(50_000, 60_000, 5))):
            return arrived.step_sonoff_check(ctx, wait=wait)

    def test_k3_takes_only_this_device_online_as_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.k3_context(tmp)
            ctx.account_devices = lambda: [{"deviceid": "1001234abc", "online": False},
                                           {"deviceid": "1009999999", "online": True}]
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"):
                records.remember_device(MAC, ewelink_id="1001234abc")
            with self.assertRaisesRegex(errors.Abort, "has not come back on your Wi-Fi or in eWeLink"):
                self.k3_waiting(ctx, tmp)

    def test_k3_waits_through_an_account_that_does_not_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.k3_context(tmp)
            answers = iter([None, [{"deviceid": "1001234abc", "online": True}]])
            ctx.account_devices = lambda: next(answers)
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"):
                records.remember_device(MAC, ewelink_id="1001234abc")
            self.assertEqual(self.k3_waiting(ctx, tmp)["confirmed_by"], "eWeLink")

    def test_k3_without_its_ewelink_id_takes_the_wifi_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.k3_context(tmp)
            ctx.account_devices = mock.Mock(side_effect=AssertionError("eWeLink not asked"))
            with mock.patch.object(arrival.output, "tell"):
                self.assertEqual(self.k3_waiting(ctx, tmp)["confirmed_by"], "nothing answering")
            # Answering at its hardware address, as nothing it ran: back, at once.
            ctx.finder = SimpleNamespace(poll=lambda: "192.0.2.50")
            self.assertEqual(self.k3_waiting(ctx, tmp)["confirmed_by"], "your Wi-Fi")
            self.slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
