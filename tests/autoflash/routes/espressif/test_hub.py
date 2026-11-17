"""The hub (tools/autoflash/routes/espressif/hub.py): Tasmota restarted before
a promote or an upload cut off is tried again, and an upload it refused."""
import itertools
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import output
from tools.autoflash.routes.espressif import hub
from tools.autoflash.state import FAILED, RunState


class PromoteTests(unittest.TestCase):
    """D4 - tried again after an upload cut off, Tasmota is restarted first:
    it still holds that upload, and the next hangs."""

    def promote(self, failed_before, slots=(0, 1)):
        from types import SimpleNamespace

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        if failed_before:
            state.mark("D4", FAILED, error="Berry Tasmota was not uploaded")
        events = []
        slots = iter(slots)
        device = SimpleNamespace(host="192.168.60.50",
            wait_reachable=lambda **_: None, started_at=lambda: "t", uptime_seconds=lambda: 10,
            current_ota=lambda: next(slots), wait_for_reboot=lambda **_: events.append("rebooted"),
            # What the device is told, in order; a status read changes nothing.
            command=lambda c, timeout=None: {} if c.startswith("Status") else events.append(c))
        ctx = SimpleNamespace(device=device, state=state, dry_run=False)
        with mock.patch.object(hub, "upload_or_prompt", side_effect=lambda *a: events.append("upload")), \
             mock.patch.object(hub.join, "device_signal", return_value=-60), \
             mock.patch.object(time, "sleep"), mock.patch.object(output, "say"):
            self.result = hub.step_promote(ctx)
        self.captures = state.data["captures"]
        return events

    def test_one_upload_lands_it_on_slot_1_and_its_settings_are_frozen(self):
        self.assertEqual(self.promote(False), ["upload", "rebooted", "SaveData 0"])
        self.assertEqual(self.result, {"current_ota": 1, "uploads": 1})
        self.assertEqual(self.captures["D4.signal_dbm"], -60)   # for the report

    def test_already_on_slot_1_nothing_is_uploaded(self):
        self.assertEqual(self.promote(False, slots=(1,)), ["SaveData 0"])
        self.assertEqual(self.result, {"current_ota": 1, "uploads": 0})

    def test_from_an_odd_start_a_second_upload_and_no_more(self):
        self.assertEqual(self.promote(False, slots=(0, 0, 1)).count("upload"), 2)
        self.assertEqual(self.result["uploads"], 2)
        with self.assertRaisesRegex(hub.Abort, "kept running its old firmware after 2 uploads"):
            self.promote(False, slots=(0, 0, 0, 0))

    def test_a_slot_not_known_yet_after_the_restart_is_waited_for(self):
        # Seen in the fault suite: just restarted, Berry not up, the slot read
        # as unknown - taken for "not slot 1", and a second upload sent.
        events = self.promote(False, slots=(0, None, None, 1))
        self.assertEqual(events.count("upload"), 1)

    def test_a_first_try_uploads_at_once(self):
        self.assertEqual(self.promote(False)[0], "upload")

    def test_tried_again_it_restarts_first(self):
        events = self.promote(True)
        self.assertEqual(events[:3], ["Restart 1", "rebooted", "upload"])


class RefusedUploadTests(unittest.TestCase):
    """An upload Tasmota took whole and refused (damaged on the way, say) is
    sent again, three tries in all, before it is left to the person."""

    def upload(self, outcomes, boots=None, answers=True, restarts=True, **kwargs):
        """upload_or_prompt with each upload's outcome from `outcomes`, and the
        boot the device says it is on, in turn, from `boots` (one boot throughout
        if not given); a restart asked for is seen if `restarts`.  What the
        device was sent, in order, is in self.events."""
        from types import SimpleNamespace
        from tools.autoflash.device import DeviceError
        tries = iter(outcomes)
        boot = iter(boots or ["boot 1"] * 20)
        self.events = []

        def upload_firmware(path, timeout=None):
            self.events.append("upload")
            outcome = next(tries)
            if isinstance(outcome, Exception):
                raise outcome

        def wait_reachable(timeout=None):
            if not answers:
                raise DeviceError("did not answer")

        def wait_for_reboot(**_):
            if not restarts:
                raise DeviceError("did not restart within 180s")
            self.events.append("rebooted")

        ctx = SimpleNamespace(device=SimpleNamespace(
            upload_firmware=upload_firmware, started_at=lambda: next(boot), uptime_seconds=lambda: 10,
            wait_reachable=wait_reachable, wait_for_reboot=wait_for_reboot,
            command=lambda c, timeout=None: self.events.append(c)))
        with mock.patch.object(hub, "upload_or_prompt_by_hand", return_value="manual") as by_hand, \
             mock.patch.object(time, "sleep"), mock.patch.object(hub.time, "monotonic", side_effect=itertools.count(100.0, 9.54)), \
             mock.patch.object(output, "tell") as told, mock.patch.object(output, "say"):
            result = hub.upload_or_prompt(ctx, Path("berry.bin"), "Berry Tasmota", **kwargs)
        return (result, self.events.count("upload"), by_hand.called,
                " ".join(str(c.args[0]) for c in told.call_args_list))

    def test_an_upload_cut_off_and_not_taken_is_sent_again_after_a_restart(self):
        # Seen in the emulator: sent again at once, to a Tasmota that still
        # held the upload cut off, the second hung without a word for 600 s.
        result, sent, by_hand, told = self.upload([ConnectionResetError("reset"), None])
        self.assertEqual(result, "http:9.5s")   # how long the upload that went through took
        self.assertEqual((sent, by_hand), (2, False))
        self.assertEqual(self.events, ["upload", "Restart 1", "rebooted", "upload"])
        self.assertIn("cut off", told)

    def test_an_upload_cut_off_is_sent_again_without_a_restart_where_one_is_not_safe(self):
        result, sent, by_hand, _ = self.upload([TimeoutError("timed out"), None], restart_to_retry=False)
        self.assertTrue(result.startswith("http:"))
        self.assertEqual(self.events, ["upload", "upload"])

    def test_an_upload_cut_off_from_a_device_that_will_not_restart_is_left_to_the_person(self):
        result, sent, by_hand, _ = self.upload([TimeoutError("timed out")], restarts=False)
        self.assertEqual((result, sent, by_hand), ("manual", 1, True))

    def test_an_upload_cut_off_three_times_is_left_to_the_person(self):
        result, sent, by_hand, _ = self.upload([ConnectionResetError("reset")] * 3)
        self.assertEqual((result, sent, by_hand), ("manual", 3, True))

    def test_a_device_that_cannot_say_whether_it_took_one_cut_off_is_left_to_the_person(self):
        # It answers, but not which boot it is on: sent again, a taken upload is written over.
        result, sent, by_hand, _ = self.upload([ConnectionResetError("reset"), None],
                                               boots=["boot 1", None, "boot 1", "boot 1"])
        self.assertEqual((result, sent, by_hand), ("manual", 1, True))
        self.assertNotIn("Restart 1", self.events)

    def test_the_device_is_waited_for_before_it_is_asked_anything_again(self):
        # Cut off, it may be restarting; restarted, it is not up at once.
        from types import SimpleNamespace
        from tools.autoflash.device import DeviceError
        events, up = [], [True]

        def asked(what):
            def answer(*_, **__):
                if not up[0]:
                    raise DeviceError("no answer")
                events.append(what)
                return "boot 1" if what == "boot" else 10
            return answer

        def upload_firmware(path, timeout=None):
            asked("upload")()
            up[0] = False
            if events.count("upload") == 1:
                raise ConnectionResetError("reset")

        ctx = SimpleNamespace(device=SimpleNamespace(
            upload_firmware=upload_firmware, started_at=asked("boot"), uptime_seconds=asked("uptime"),
            wait_reachable=lambda timeout=None: up.__setitem__(0, True),
            wait_for_reboot=lambda **_: up.__setitem__(0, False), command=lambda c, timeout=None: asked(c)()))
        with mock.patch.object(hub, "upload_or_prompt_by_hand", return_value="manual") as by_hand, \
             mock.patch.object(time, "sleep"), mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertTrue(hub.upload_or_prompt(ctx, Path("berry.bin"), "Berry Tasmota").startswith("http:"))
        by_hand.assert_not_called()
        self.assertEqual(events, ["boot", "upload", "boot", "boot", "uptime", "Restart 1", "boot", "upload"])

    def test_an_upload_cut_off_but_taken_is_not_sent_again(self):
        # Taken, it is booted: another would write the other slot and boot that.
        result, sent, by_hand, _ = self.upload([ConnectionResetError("reset")], boots=["boot 1", "boot 2"])
        self.assertEqual((result, sent, by_hand), ("http:answer lost", 1, False))

    def test_an_upload_cut_off_from_a_device_that_does_not_answer_is_left_to_the_person(self):
        result, sent, by_hand, _ = self.upload([TimeoutError("timed out")], answers=False)
        self.assertEqual((result, sent, by_hand), ("manual", 1, True))

    def test_one_refusal_is_sent_again(self):
        from tools.autoflash.device import UploadRefused
        result, sent, by_hand, told = self.upload([UploadRefused("Bad magic byte"), None])
        self.assertTrue(result.startswith("http:"))
        self.assertEqual(sent, 2)
        self.assertIn("refused the upload (Bad magic byte)", told)
        self.assertFalse(by_hand)

    def test_three_refusals_leave_it_to_the_person(self):
        from tools.autoflash.device import UploadRefused
        result, sent, by_hand, _ = self.upload([UploadRefused("Not enough space")] * 3)
        self.assertEqual((result, sent, by_hand), ("manual", 3, True))

    def test_any_other_failure_goes_to_the_person_at_once(self):
        result, sent, by_hand, _ = self.upload([OSError("no route")])
        self.assertEqual((result, sent, by_hand), ("manual", 1, True))


if __name__ == "__main__":
    unittest.main()
