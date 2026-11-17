"""What a step cut off left on the device, and whether a step recorded done
still holds: judged by what the device answers as."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.routes.beken.test_beken import a_context
from tools.autoflash import detect
from tools.autoflash.routes import beken
from tools.autoflash.routes.beken import evidence


class EvidenceInstalledTests(unittest.TestCase):
    def test_a_device_still_on_the_interim_firmware_did_not_take_esphome(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome", source="sonoff")
            for running, expected in (
                    ("interim", (False, "The device still answers as the interim firmware: the image was not "
                                        "applied.")),
                    ("esphome", (True, "The device answers as ESPHome."))):
                with mock.patch.object(detect, "identify", return_value=running):
                    self.assertEqual(evidence.evidence_installed(ctx), expected)

    def judge(self, running, tasmota=False, **inputs):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, **inputs)
            with mock.patch.object(detect, "identify", return_value=running), \
                    mock.patch.object(detect, "answers_as_tasmota", return_value=tasmota):
                return evidence.evidence_installed(ctx)[0]

    def test_a_run_from_openbeken_to_esphome_still_on_openbeken_did_not_take_it(self):
        self.assertIs(self.judge("openbeken", firmware="esphome", source="openbeken"), False)

    def test_a_device_not_answering_as_the_new_firmware_yet_is_undecided(self):
        self.assertIsNone(self.judge(None, firmware="esphome", source="sonoff"))

    def test_back_to_sonoff_is_a_device_answering_as_neither_firmware(self):
        self.assertIs(self.judge(None, firmware="sonoff", source="openbeken"), True)
        # What answers Tasmota's commands is OpenBeken still, not Sonoff's.
        self.assertIsNone(self.judge(None, tasmota=True, firmware="sonoff", source="openbeken"))


class StillInstalledTests(unittest.TestCase):
    """A power cut while the image is written leaves the device on the firmware
    it was leaving, its step recorded done (seen in the fault suite)."""

    def judge(self, *answers):
        with mock.patch.object(evidence, "evidence_installed", side_effect=list(answers)) as asked, \
                mock.patch.object(evidence.time, "sleep") as self.slept, \
                mock.patch.object(evidence.time, "monotonic", side_effect=[0, 0, 10, 100, 200]):
            judged = evidence.still_installed(SimpleNamespace(), wait=60)
        self.asked = asked.call_count
        return judged

    def test_still_on_the_firmware_it_left_is_doubted(self):
        ok, message = self.judge((False, "The device still answers as OpenBeken: the image was not applied."))
        self.assertFalse(ok)
        self.assertEqual(message, "the device still answers as OpenBeken: the image was not applied")

    def test_on_the_new_firmware_it_stands(self):
        self.assertTrue(self.judge((True, "The device answers as ESPHome."))[0])

    def test_a_device_not_answering_yet_is_waited_for_then_left_be(self):
        waiting = (None, "The device does not answer as the new firmware yet.")
        self.assertTrue(self.judge(waiting, waiting, waiting)[0])
        self.assertEqual(self.asked, 3)   # at 0s, 10s, and 100s, past the wait
        self.assertEqual(self.slept.call_count, 2)

    def test_the_onward_steps_are_checked_on_resume(self):
        self.assertEqual(set(beken.STEP_VERIFIERS), {"J2", "J4", "O1", "O2", "E1", "E2", "U1", "U3"})


if __name__ == "__main__":
    unittest.main()
