"""The go-ahead before a converted device's first write (tools/autoflash/handover.py)."""
import unittest
from unittest import mock

from tools.autoflash import errors, handover, output, prompts


class ConsentTests(unittest.TestCase):
    """Only the word itself goes ahead, and q stops; anything else is asked again."""

    def consent(self, *replies):
        from unittest import mock
        import tools.autoflash.prompts as prompts
        self.asked = []
        answers = iter(replies)

        def answer(prompt):
            self.asked.append(prompt)
            return next(answers)
        with mock.patch("builtins.input", side_effect=answer), \
             mock.patch.object(prompts, "_emit"), mock.patch.object(output, "say"):
            return prompts.require_word(prompts.CONSENT_WORD)

    def test_the_go_ahead_is_asked_once_in_a_run(self):
        # Seen: X1 tried again by itself asked for it again, and Enter stopped the run.
        from types import SimpleNamespace
        from unittest import mock
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda key: None))   # the S60's, proven
        with mock.patch.object(prompts, "require_word", return_value=True) as asked, \
             mock.patch.object(prompts, "warning_box"):
            handover.consent_to_leave(ctx, "Tasmota")
            handover.consent_to_leave(ctx, "Tasmota")
        self.assertEqual(asked.call_count, 1)

    def test_refused_it_says_nothing_was_written(self):
        # A run stopped there does not say what the device is (past_the_commit).
        from types import SimpleNamespace
        with mock.patch.object(prompts, "require_word", return_value=False), \
             mock.patch.object(prompts, "warning_box"), self.assertRaises(errors.NothingWritten):
            handover.consent_to_leave(SimpleNamespace(state=SimpleNamespace(input=lambda key: None)), "Tasmota")

    def test_proceed_in_any_case_goes_ahead(self):
        for reply in ("PROCEED", "proceed", " Proceed "):
            with self.subTest(reply=reply):
                self.assertTrue(self.consent(reply))

    def test_anything_else_is_asked_again_rather_than_stopping(self):
        # An Enter or a y pressed by habit gets a second chance.
        for reply in ("y", "yes", "proceed now", "procede", ""):
            with self.subTest(reply=reply):
                self.assertTrue(self.consent(reply, "proceed"))
                self.assertEqual(len(self.asked), 2)
                self.assertIn("(q to quit):", self.asked[1])   # how to quit, as at every question
        with mock.patch.object(output, "question", wraps=output.question) as laid_out:
            self.consent("", "PROCEED")
        self.assertEqual(laid_out.call_args.args[0], "Only the TYPED word PROCEED is accepted")

    def test_q_stops(self):
        for reply in ("q", "quit", "Q"):
            with self.subTest(reply=reply):
                self.assertFalse(self.consent("y", reply))

    def test_ctrl_c_there_stops_at_once_as_at_every_question(self):
        # Read as any question is: a Ctrl-C there is not a first press of
        # one during work, whose notice could never be taken away again.
        import signal
        from tools.autoflash import interrupt

        def press(prompt):
            interrupt._on_interrupt(signal.SIGINT, None)
        interrupt.clear()
        with mock.patch("builtins.input", side_effect=press), mock.patch.object(interrupt, "_say") as said, \
                self.assertRaises(KeyboardInterrupt):
            prompts.require_word(prompts.CONSENT_WORD)
        said.assert_not_called()
        self.assertFalse(interrupt.pending())


class LostUpdateTests(unittest.TestCase):
    """A device still held by an update cut off part way is waited for, once."""

    def send(self, *outcomes):
        from types import SimpleNamespace
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda key: "192.0.2.50"),
                              esphome_password=None, esphome_key=None)
        with mock.patch.object(handover.esphome_ota, "upload", side_effect=outcomes) as upload, \
                mock.patch.object(handover, "wait_out_lost_update") as waited, \
                mock.patch.object(output, "progress_clear"):
            try:
                self.took = handover.send_as_esphome_update(ctx, b"image", "ESPHome 1")
            finally:
                self.uploads, self.waits = upload.call_count, waited.call_count

    def test_it_waits_for_the_lost_update_and_sends_again(self):
        self.send(handover.esphome_ota.Busy("its update service did not answer"), None)
        self.assertEqual((self.uploads, self.waits), (2, 1))

    def test_still_held_after_the_wait_it_stops(self):
        busy = handover.esphome_ota.Busy("its update service did not answer")
        with self.assertRaises(errors.Abort):
            self.send(busy, busy)
        self.assertEqual((self.uploads, self.waits), (2, 1))

    def test_the_wait_outlasts_the_devices_own(self):
        clock = iter(range(5000, 6000))
        with mock.patch.object(handover.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(handover.time, "sleep"), mock.patch.object(output, "tell"), \
                mock.patch.object(output, "progress"), mock.patch.object(output, "progress_clear"):
            handover.wait_out_lost_update()
        self.assertGreater(next(clock) - 5000, handover.esphome_ota.DEVICE_DATA_TIMEOUT)

    def test_the_time_sending_leaves_out_the_time_a_question_took(self):
        # For the report: how long the device was busy, not the person.
        with mock.patch.object(handover.time, "monotonic", side_effect=[0.0, 100.0, 112.34]), \
                mock.patch.object(handover.unlock, "ask_esphome_password"):
            self.send(handover.esphome_ota.NeedsPassword(False), None)
        self.assertEqual(self.took, 12.3)


if __name__ == "__main__":
    unittest.main()
