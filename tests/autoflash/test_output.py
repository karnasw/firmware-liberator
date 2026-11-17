import dataclasses
import contextlib
import io
import os
import signal
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import context, hostsys, join, output, phases, records, prompts, report, routes
from tools.autoflash.state import RunState


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


class OutputTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "run.log"
        patcher = mock.patch.dict(os.environ, {"NO_COLOR": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("sys.stdin", io.StringIO())   # never the terminal running the tests
        patcher.start()
        self.addCleanup(patcher.stop)
        # A question asked and never answered leaves the output waiting for the keyboard.
        patcher = mock.patch.multiple(output._s, paused=False, held=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        output.finish()
        self.tmp.cleanup()

    def run_with(self, stdout, verbose, body):
        with mock.patch("sys.stdout", stdout):
            output.start(self.log, verbose=verbose)
            body()
            output.finish()
        return stdout.getvalue(), self.log.read_text()


class ConciseTests(OutputTestCase):
    """By default the screen gets stage lines and what matters; the log gets everything."""

    def stages(self):
        output.stage_begin(1, "Checking the files")
        output.detail("bundle and pinned artifacts: OK")
        output.stage_end("complete")
        output.stage_begin(2, "Installing Tasmota")
        output.show("Join the bridge's Wi-Fi network")
        output.stage_end("complete")

    def test_piped_output_gets_one_line_per_change(self):
        screen, log = self.run_with(io.StringIO(), False, self.stages)
        self.assertIn("Stage 1 - Checking the files - Underway", screen)
        self.assertIn("Stage 1 - Checking the files - Complete", screen)
        self.assertIn("Join the bridge's Wi-Fi network", screen)
        self.assertNotIn("bundle and pinned artifacts", screen)
        self.assertIn("bundle and pinned artifacts: OK", log)
        self.assertRegex(log, r"\[\d\d:\d\d:\d\d\] Stage 2 - Installing Tasmota - complete")

    def test_on_a_terminal_underway_becomes_complete_on_the_same_line(self):
        def body():
            output.stage_begin(1, "Checking the files")
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        drawn = screen.split("\n")[0]
        self.assertIn("Checking the files - Underway", drawn)
        self.assertTrue(drawn.split("\r")[-1].startswith(" Stage 1 - Checking the files - Complete"))

    def test_stage_lines_line_up_in_columns(self):
        def body():
            output.set_stage_width(len("Installing official Tasmota"), 14)
            for number, name in ((1, "Checking you have everything"[:20]), (10, "Installing official Tasmota")):
                output.stage_begin(number, name)
                output.stage_end("complete")
        screen, log = self.run_with(io.StringIO(), False, body)
        done = [line for line in screen.splitlines() if line.endswith("Complete")]
        self.assertEqual(len(done), 2)
        self.assertEqual(done[0].index(" - Complete"), done[1].index(" - Complete"))
        self.assertEqual(done[0].index(" - Checking"), done[1].index(" - Installing"))
        self.assertTrue(done[0].startswith(" Stage  1 - "))
        self.assertIn("Stage 1 - Checking you have ev - complete", log)  # the log is not padded

    def test_no_stage_says_a_total(self):
        # A run from Sonoff's firmware learns its route only after its third stage.
        def body():
            output.set_stage_width(len("Querying Sonoff firmware"), 17)
            for number, name in ((3, "Choosing the firmware"), (4, "Querying Sonoff firmware")):
                output.stage_begin(number, name)
                output.stage_end("complete")
        screen, log = self.run_with(io.StringIO(), False, body)
        done = [line for line in screen.splitlines() if line.endswith("Complete")]
        self.assertTrue(done[0].startswith(" Stage  3 - Choosing the firmware "))
        self.assertTrue(done[1].startswith(" Stage  4 - Querying Sonoff firmware "))
        self.assertNotIn(" of ", screen + log)
        self.assertIn("Stage 3 - Choosing the firmware - complete (0s)", log)   # its time kept

    def test_each_run_lays_its_stages_out_afresh(self):
        output.set_stage_width(30, 17)   # the last run's
        screen, _ = self.run_with(io.StringIO(), False, lambda: (output.stage_begin(1, "Checking the files"),
                                                                 output.stage_end("complete")))
        self.assertIn(" Stage 1 - Checking the files - Complete", screen)

    def test_an_underway_stage_shows_a_spinner_not_its_seconds(self):
        frames = []

        def body():
            frames.append(output.spinner_frames())   # this terminal's
            output.stage_begin(1, "Checking the files")
            with mock.patch.object(output.time, "monotonic", return_value=output._s.stage_started + 75.0):
                output.tick()
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        underway = [part for part in screen.split("\r") if "Underway" in part]
        self.assertTrue(underway)
        self.assertFalse(any(re.search(r"Underway \S*\d", part) for part in underway))
        self.assertTrue(all(part.rstrip()[-1] in frames[0] for part in underway))
        self.assertTrue(all(re.search(r"Underway \S \s*$", part) for part in underway))   # the cursor clear of it
        self.assertIn("Stage 1 - Checking the files - complete (1m 15s)", log)

    def test_only_the_stage_under_way_has_a_spinner(self):
        # A message under an underway line leaves that line where it is: without its spinner.
        def body():
            output.stage_begin(1, "Checking the files")
            output.tick()
            output.show("Something to read")
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        rows = [row.split("\r")[-1].rstrip() for row in screen.split("\n")]
        self.assertIn(" Stage 1 - Checking the files - Underway", rows)
        self.assertIn(" Stage 1 - Checking the files - Complete", rows)

    @unittest.skipIf(output.IS_WINDOWS, "a POSIX terminal's echo")
    def test_keys_pressed_while_nothing_asks_are_not_echoed(self):
        # An Enter echoed under the spinner left the underway line, spinner
        # and all, above the stage's Complete.
        import termios
        leader, follower = os.openpty()
        self.addCleanup(os.close, leader)
        keyboard = os.fdopen(follower, "r", encoding="utf-8")
        self.addCleanup(keyboard.close)

        def echoing():
            return bool(termios.tcgetattr(follower)[3] & termios.ECHO)
        seen = []

        def body():
            output.stage_begin(1, "Sending the new firmware")
            seen.append(echoing())
            output.before_input("What now")
            seen.append(echoing())
            output.answered("1")
            seen.append(echoing())
        with mock.patch("sys.stdin", keyboard):
            self.run_with(FakeTerminal(), False, body)
            self.assertEqual(seen, [False, True, False])
            self.assertTrue(echoing())   # as it was, once the run is over

    def test_a_ctrl_c_at_a_question_is_not_shown_as_a_caret_c(self):
        # It stops the run there: "^C" after the question said nothing more.
        # Before a run starts too: the first question is asked before it.
        import termios
        leader, follower = os.openpty()
        self.addCleanup(os.close, leader)
        keyboard = os.fdopen(follower, "r", encoding="utf-8")
        self.addCleanup(keyboard.close)
        found = termios.tcgetattr(follower)
        found[3] |= termios.ECHO | termios.ECHOCTL
        termios.tcsetattr(follower, termios.TCSANOW, found)

        def flags():
            return termios.tcgetattr(follower)[3] & (termios.ECHO | termios.ECHOCTL)
        seen = []

        def answer(prompt):
            seen.append(flags())
            return "y"

        def pressed(prompt):
            seen.append(flags())
            raise KeyboardInterrupt
        with mock.patch("sys.stdin", keyboard), mock.patch("sys.stdout", FakeTerminal()):
            with mock.patch("builtins.input", side_effect=answer):
                prompts.ask_yes_no("Go on")                  # no run started
            with mock.patch("builtins.input", side_effect=pressed), self.assertRaises(KeyboardInterrupt):
                prompts.ask_yes_no("Go on")
        self.assertEqual(seen, [termios.ECHO, termios.ECHO])          # typing echoed, ^C not
        self.assertEqual(flags(), termios.ECHO | termios.ECHOCTL)     # as it was, however it ended

    def test_a_question_cut_short_by_ctrl_c_ends_its_row(self):
        from tools.autoflash import interrupt
        screen = FakeTerminal()

        def pressed(prompt):
            screen.write(prompt)   # input() shows the question, then Ctrl-C
            raise KeyboardInterrupt

        def body():
            with mock.patch("builtins.input", side_effect=pressed), self.assertRaises(KeyboardInterrupt):
                prompts.ask_yes_no("Go on")
            output.show("Stopped with Ctrl-C.")
        self.run_with(screen, False, body)
        self.assertIn("\n Stopped with Ctrl-C.", screen.getvalue())
        self.assertNotIn("(y/n, q to quit):  Stopped", screen.getvalue())
        self.assertEqual(interrupt._state["asking"], 0)

    def test_the_cursor_shows_only_while_a_question_waits(self):
        def body():
            output.stage_begin(1, "Sending the new firmware")
            output.before_input("What now")
            output.answered("1")
            output.stage_end("complete")
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}), mock.patch.object(output, "IS_WINDOWS", False):
            screen, _ = self.run_with(FakeTerminal(), False, body)
        hide, show = "\x1b[?25l", "\x1b[?25h"
        self.assertEqual(re.findall(r"\x1b\[\?25[lh]", screen), [hide, show, hide, show])
        self.assertLess(screen.index(hide), screen.index("Underway"))
        self.assertTrue(screen.endswith(show))   # back once the run is over

    def test_the_spinner_is_braille_where_the_terminal_can_show_it(self):
        utf8 = mock.Mock(encoding="utf-8")
        with mock.patch("sys.stdout", utf8), mock.patch.dict(os.environ, {"TERM": "xterm-256color"}), \
                mock.patch.object(output, "IS_WINDOWS", False):
            self.assertEqual(output.spinner_frames(), output.BRAILLE_FRAMES)
            with mock.patch.dict(os.environ, {"TERM": "linux"}):
                self.assertEqual(output.spinner_frames(), output.PLAIN_FRAMES)
        with mock.patch("sys.stdout", mock.Mock(encoding="cp1252")):
            self.assertEqual(output.spinner_frames(), output.PLAIN_FRAMES)
        with mock.patch("sys.stdout", utf8), mock.patch.object(output, "IS_WINDOWS", True), \
                mock.patch.dict(os.environ, {"WT_SESSION": ""}):
            self.assertEqual(output.spinner_frames(), output.PLAIN_FRAMES)   # the classic console
            os.environ["WT_SESSION"] = "1"
            self.assertEqual(output.spinner_frames(), output.BRAILLE_FRAMES)

    def test_an_answered_question_is_taken_off_the_screen(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(5, "Installing the installer firmware")
                output.transient_begin()          # the underway line: 1 row
                prompts.warning_box("Before going on", ["One line."])  # 3 rows
                output.before_input("Type PROCEED to go ahead: ")  # a blank row before it
                output.answered("proceed")        # prompt and answer: 1 row
                output.transient_end()
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn("\x1b[1F\x1b[2K" * 6, screen)
        self.assertNotIn("\x1b[1F\x1b[2K" * 7, screen)
        self.assertNotIn("\x1b[J", screen)   # never an erase that could clear the whole screen
        self.assertIn("Before going on", log)
        self.assertIn("-> proceed", log)

    def test_an_answer_wider_than_the_window_is_taken_off_with_its_question(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(5, "Installing the installer firmware")
                output.transient_begin()          # the underway line: 1 row
                output.before_input("Where is it: ")  # a blank row before it
                output.answered("x" * 90)         # prompt and answer: 2 rows
                output.transient_end()
                output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        self.assertIn("\x1b[1F\x1b[2K" * 4, screen)
        self.assertNotIn("\x1b[1F\x1b[2K" * 5, screen)

    def test_a_question_taller_than_the_window_stays_whole(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 5))):
                output.stage_begin(5, "Installing the installer firmware")
                output.transient_begin()          # the underway line: 1 row
                prompts.warning_box("Before going on", ["One line."])  # 3 rows
                output.before_input("Type PROCEED to go ahead: ")
                output.answered("proceed")        # prompt and answer: 1 row - 5 in all
                output.transient_end()
                output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        # The cursor cannot reach the block's first row: erasing would strand it.
        self.assertNotIn("\x1b[1F", screen)
        self.assertIn("Before going on", screen)
        self.assertIn("Installing the installer firmware - Complete", output.ANSI_RE.sub("", screen))

    def ask_address(self, rows):
        """Two addresses that do not answer, then one that does, in a window `rows` tall."""
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, rows))), \
                 mock.patch.object(prompts, "ping_ok", side_effect=[False, False, True]), \
                 mock.patch("builtins.input", side_effect=["10.0.0.1", "10.0.0.2", "10.0.0.3"]):
                output.stage_begin(3, "Discovering the device")
                output.transient_begin()
                self.assertEqual(prompts.ask_ipv4("The device's address", help_lines=["Where to look."],
                                                  must_respond=True), "10.0.0.3")
                output.transient_end()
                output.stage_end("complete")
        return self.run_with(FakeTerminal(), False, body)

    def test_an_address_asked_again_takes_the_last_try_away(self):
        # A short window, as a terminal panel often is: three tries piled up
        # would be taller than it, and stay on the screen for good.
        screen, log = self.ask_address(12)
        self.assertEqual(screen.count("Where to look."), 3)  # explained again each time
        tail = screen.rsplit("Where to look.", 1)[1]
        self.assertIn("\x1b[1F\x1b[2K", tail)                # the block went at the end
        self.assertEqual(log.count("Nothing answered at"), 2)    # the log keeps every try

    def test_time_waiting_for_an_answer_is_left_out(self):
        now = [100.0]
        def body():
            with mock.patch.object(output.time, "monotonic", side_effect=lambda: now[0]):
                output.stage_begin(3, "Discovering the device")
                now[0] += 5                     # working
                output.before_input("The device's address: ")
                now[0] += 60                    # the person thinking
                self.assertEqual(output.waited(), 60)
                output.answered("192.0.2.50")
                now[0] += 5                     # working again
                self.assertEqual(output.waited(), 60)
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn("Discovering the device - complete (10s)", log)

    def test_a_question_left_unanswered_is_not_the_next_runs_wait(self):
        now = [100.0]
        with mock.patch.object(output.time, "monotonic", side_effect=lambda: now[0]):
            output.before_input("The device's address: ")   # never answered
            now[0] += 1000
            def body():
                output.stage_begin(3, "Discovering the device")
                output.before_input("The device's address: ")
                now[0] += 60
                self.assertEqual(output.waited(), 60)
                output.answered("192.0.2.50")
                output.stage_end("complete")
            self.run_with(FakeTerminal(), False, body)

    def test_no_clock_drawn_in_an_open_block(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(3, "Discovering the device")
                output.transient_begin()
                before = sys.stdout.getvalue()
                output._tick()  # while the block's note is still being worked out
                self.assertEqual(sys.stdout.getvalue(), before)
                output.transient_end()
                output._tick()  # the block gone, the clock goes on
                self.assertIn("Underway", sys.stdout.getvalue()[len(before):])
                output.stage_end("complete")
        self.run_with(FakeTerminal(), False, body)

    def test_a_stage_stopped_after_its_notes_were_taken_off_has_one_blank_row_above_why(self):
        # Seen in the fault suite: a note in a block, its clock drawn above it
        # once an inner block went, then the note taken off and the stage
        # stopped - "FAILED: ..." came two blank rows under the stage line.
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(7, "Backing up Sonoff firmware")
                output.transient_begin()
                output.tell("Back on its Sonoff firmware: sending the installer again.")
                output.transient_begin()
                output.tell("-- Let the device through this computer's firewall")
                output.transient_end()   # the clock drawn above the note
                output.transient_end()
                output._tick()
                output.stage_end("stopped")
                output.tell("FAILED: the connection to the device was lost")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        self.assertEqual(rendered(screen), ["Stage 7 - Backing up Sonoff firmware - Stopped", "",
                                            "FAILED: the connection to the device was lost"])

    def test_a_short_window_is_noticed(self):
        found = {}
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                for rows in (12, output.MIN_ROWS):
                    with mock.patch("shutil.get_terminal_size",
                                    return_value=os.terminal_size((80, rows))):
                        found[rows] = output.short_window()
        self.run_with(FakeTerminal(), False, body)
        self.assertEqual(found, {12: 12, output.MIN_ROWS: None})

    def test_a_short_window_does_not_matter_when_nothing_is_cleared(self):
        found = []
        def body():
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 12))):
                found.append(output.short_window())
        self.run_with(FakeTerminal(), True, body)   # verbose: everything stays anyway
        self.run_with(io.StringIO(), False, body)  # not a terminal
        self.assertEqual(found, [None, None])

    def test_a_short_window_is_asked_to_grow_and_waited_for(self):
        height = [12]
        size = lambda *a: os.terminal_size((80, height[0]))
        waited = []
        def wait(prompt, *, until):
            for rows in (output.MIN_ROWS - 4, output.MIN_ROWS):  # made taller: still short, then tall enough
                self.assertFalse(until())
                height[0] = rows
            self.assertTrue(until())
            waited.append(prompt)
        stdout = FakeTerminal()
        with mock.patch("sys.stdout", stdout), \
             mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", side_effect=size), \
             mock.patch.object(prompts, "wait_for_key", side_effect=wait):
            prompts.wait_for_taller_window()
        self.assertEqual(len(waited), 1)
        self.assertIn(f"It is 12 rows tall; make it at least {output.MIN_ROWS}", stdout.getvalue())

    def test_a_tall_enough_window_is_not_mentioned(self):
        stdout = FakeTerminal()
        with mock.patch("sys.stdout", stdout), \
             mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, output.GOOD_ROWS))), \
             mock.patch.object(prompts, "wait_for_key") as wait:
            prompts.wait_for_taller_window()
        wait.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")

    def test_a_window_too_short_for_the_report_is_told_in_a_line_and_not_waited_for(self):
        stdout = FakeTerminal()
        with mock.patch("sys.stdout", stdout), \
             mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, output.MIN_ROWS + 5))), \
             mock.patch.object(prompts, "wait_for_key") as wait:
            prompts.wait_for_taller_window()
        wait.assert_not_called()
        self.assertIn("Tip: at 40 rows or more", stdout.getvalue())

    def test_the_window_advice_changes_at_its_two_heights_exactly(self):
        # Waited for below MIN_ROWS, told in a line below GOOD_ROWS, else nothing.
        for rows, expected in ((output.MIN_ROWS - 1, "waited"), (output.MIN_ROWS, "line"),
                               (output.GOOD_ROWS - 1, "line"), (output.GOOD_ROWS, "nothing")):
            with self.subTest(rows=rows):
                stdout = FakeTerminal()
                with mock.patch("sys.stdout", stdout), \
                     mock.patch.object(output, "colour_ok", return_value=True), \
                     mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, rows))), \
                     mock.patch.object(prompts, "wait_for_key") as wait:
                    prompts.wait_for_taller_window()
                shown = "waited" if wait.called else "line" if "Tip: at" in stdout.getvalue() \
                    else "nothing" if stdout.getvalue() == "" else stdout.getvalue()
                self.assertEqual(shown, expected)

    def test_a_checked_address_must_pass_with_no_using_it_anyway(self):
        asked = []
        def check(address):
            return [] if address == "10.0.0.3" else [f"{address} is not the device."]
        def answer(prompt):
            asked.append(prompt)
            return {0: "10.0.0.1", 1: "10.0.0.2"}.get(len(asked) - 1, "10.0.0.3")
        stdout = FakeTerminal()
        with mock.patch("sys.stdout", stdout), \
             mock.patch.object(prompts, "ping_ok", side_effect=lambda a, *k: a != "10.0.0.1"), \
             mock.patch("builtins.input", side_effect=answer):
            self.assertEqual(prompts.ask_ipv4("The device's address", check=check), "10.0.0.3")
        self.assertEqual(len(asked), 3)                     # three addresses, nothing else
        self.assertNotIn("Use it anyway", "".join(asked))
        screen = stdout.getvalue()
        self.assertIn("Nothing answered at 10.0.0.1: check the address", screen)
        self.assertIn("10.0.0.2 is not the device.", screen)

    def test_no_clock_under_an_address_while_it_is_pinged(self):
        # Its line and the gap after it made a try two rows taller: in a short
        # window the try's first row scrolled away, where it could not be erased.
        held = []
        def ping(address, *args):
            held.append(output._s.held)
            return address == "10.0.0.2"
        with mock.patch.object(prompts, "ping_ok", side_effect=ping), \
             mock.patch("builtins.input", side_effect=["10.0.0.1", "10.0.0.2"]):
            self.assertEqual(prompts.ask_ipv4("The device's address", must_respond=True), "10.0.0.2")
        self.assertEqual(held, [True, True])
        self.assertFalse(output._s.held)

    def test_wrong_answers_replace_each_other(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 6))), \
                 mock.patch("builtins.input", side_effect=["maybe", "192.0.2.50", "n"]):
                output.stage_begin(3, "Discovering the device")
                output.transient_begin()
                self.assertFalse(prompts.ask_yes_no("Use it anyway?", default=False))
                output.transient_end()
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        # Piled up, three tries and two complaints would outgrow the window and stay.
        tail = screen.rsplit("Please answer y or n", 1)[1]
        self.assertIn("\x1b[1F\x1b[2K", tail)
        self.assertEqual(log.count("Please answer y or n"), 2)

    def test_a_try_taller_than_the_window_stays(self):
        screen, _ = self.ask_address(4)
        self.assertEqual(screen.count("Where to look."), 1)  # still there: not repeated
        self.assertNotIn("\x1b[1F", screen)

    def failed_try(self, rows):
        """A stage that stops with a note under it, its failure and the note
        that it is tried again, then tried again and passing - in a window
        `rows` tall."""
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, rows))):
                output.stage_begin(12, "Verifying memory layout")
                output.stage_end("complete")
                mark = output.mark()
                output.stage_begin(13, "Staging startup firmware")
                output.tell("    The device's console: BRY: Berry initialized")
                output.stage_end("stopped")
                output.tell("FAILED: the device stopped the stage")
                output.tell("    That may have been the network - trying again in 10s (attempt 2 of 3).")
                self.assertEqual(output.rewind(mark), rows > 8)
                output.stage_begin(13, "Staging startup firmware")
                output.stage_end("complete")
        return self.run_with(FakeTerminal(), False, body)

    def test_a_failed_try_is_taken_off_the_screen(self):
        screen, log = self.failed_try(24)
        self.assertEqual(rendered(screen), ["Stage 12 - Verifying memory layout - Complete",
                                            "Stage 13 - Staging startup firmware - Complete"])
        self.assertIn("trying again", log)                   # the log keeps it

    def test_a_failed_try_taller_than_the_window_stays(self):
        screen, _ = self.failed_try(6)
        self.assertNotIn("\x1b[1F", screen)                  # its first row is out of reach
        self.assertIn("Stopped", screen)

    def test_nothing_is_taken_off_a_screen_that_cannot_be_drawn_over(self):
        def body():
            mark = output.mark()
            output.tell("FAILED: the device stopped the stage")
            self.assertFalse(output.rewind(mark))
        screen, _ = self.run_with(io.StringIO(), False, body)   # piped: no terminal
        self.assertIn("FAILED", screen)

    def drawn(self, body, rows=24):
        def wrapped():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, rows))):
                body()
        screen, _ = self.run_with(FakeTerminal(), False, wrapped)
        return rendered(screen)

    def test_a_block_inside_another_is_taken_off_and_the_outer_one_too(self):
        # Seen in context.py: the Wi-Fi section's block, and the credentials
        # asked in one of their own inside it. The inner one began afresh and
        # the section stayed on the screen.
        def body():
            output.stage_begin(12, "Verifying memory layout")
            output.transient_begin()
            output.show("Your Wi-Fi details are needed")
            output.transient_begin()
            output.show("Wi-Fi network name:")
            output.transient_end()
            self.assertEqual(len(output._s.blocks), 1)
            output.show("Wi-Fi password:")
            output.transient_end()
            output.stage_end("complete")
        self.assertEqual(self.drawn(body), ["Stage 12 - Verifying memory layout - Complete"])

    def test_a_try_inside_a_block_can_be_taken_off(self):
        def body():
            output.stage_begin(3, "Discovering the device")
            output.transient_begin()
            output.show("Where to look.")
            mark = output.mark()
            output.show("FAILED: not answering")
            self.assertTrue(output.rewind(mark))
            output.show("Asked again")
            output.transient_end()
            output.stage_end("complete")
        self.assertEqual(self.drawn(body), ["Stage 3 - Discovering the device - Complete"])

    def test_a_question_asked_as_its_block_began_keeps_its_block_when_asked_again(self):
        # Seen: a yes/no question answered wrongly first, with nothing shown
        # between its block's start and the question, stayed on the screen
        # once answered - the rewind closed the block it was asked in.
        def body():
            output.stage_begin(3, "Discovering the device")
            output.show("A note")
            output.transient_begin()
            mark = output.mark()
            output.show("Use it anyway (y/N): maybe")
            self.assertTrue(output.rewind(mark))
            self.assertEqual(len(output._s.blocks), 1)
            output.show("Please answer y or n.")
            output.show("Use it anyway (y/N): y")
            output.transient_end()
            output.stage_end("stopped")
        self.assertEqual(self.drawn(body), ["Stage 3 - Discovering the device - Underway", "", "A note",
                                            "Stage 3 - Discovering the device - Stopped"])

    def going_back(self):
        output.stage_begin(3, "Discovering the device")
        output.stage_end("complete")
        mark = output.mark()
        output.stage_begin(4, "Choosing the firmware")
        output.transient_begin()
        output.show("A question cut short")
        self.assertTrue(output.rewind(mark))
        self.assertEqual(output._s.blocks, [])
        output.stage_begin(4, "Choosing the firmware")
        output.stage_end("complete")

    def test_going_back_past_a_block_s_start_closes_it(self):
        self.assertEqual(self.drawn(self.going_back), ["Stage 3 - Discovering the device - Complete",
                                                       "Stage 4 - Choosing the firmware - Complete"])

    def test_a_question_an_earlier_run_left_waiting_is_not_erased_by_the_next(self):
        # Seen in the tests, one run in a few: a run stopped at a question of
        # two rows left it waiting, and the next run's first rewind erased two
        # rows for it - its stage line among them.
        with mock.patch("sys.stdout", FakeTerminal()):
            output.start(self.log, verbose=False)
            output.question("Which network?\n  1) Home  2) Other\nChoose", ": ")
            output.finish()
        self.assertEqual(self.drawn(self.going_back), ["Stage 3 - Discovering the device - Complete",
                                                       "Stage 4 - Choosing the firmware - Complete"])

    def test_a_stage_that_ends_takes_off_every_block_still_open(self):
        def body():
            output.stage_begin(5, "Installing Tasmota")
            output.transient_begin()
            output.show("A note")
            output.transient_begin()
            output.show("Its question")
            output.stage_end("stopped")
        self.assertEqual(self.drawn(body), ["Stage 5 - Installing Tasmota - Stopped"])

    def test_a_stage_that_passed_leaves_just_its_line(self):
        # Seen in the fault suites: instructions to join a network, and the
        # installer sent again, left above a stage long since complete.
        def body():
            output.stage_begin(8, "Installing interim firmware")
            prompts.section("Join the bridge's Wi-Fi network", ["In your Wi-Fi menu, join it."])
            output.tell("    Sending the installer again (attempt 2 of 3).")
            output.stage_end("complete")
            output.stage_begin(9, "Preparing memory layout")
            output.stage_end("complete")
        self.assertEqual(self.drawn(body), ["Stage 8 - Installing interim firmware - Complete",
                                            "Stage 9 - Preparing memory layout - Complete"])

    def test_a_stage_s_line_is_left_once_above_what_it_shows(self):
        # Seen in the fault suites: the line again, and a gap, under each note:
        # 10 of a 48-row stage's rows.
        def body():
            output.stage_begin(8, "Installing interim firmware")
            for n in range(3):
                output.tell(f"    note {n}")
                output.tick()   # the clock drawn again under it, as the ticker does
            output.stage_end("stopped")
        rows = self.drawn(body)
        self.assertEqual(rows, ["Stage 8 - Installing interim firmware - Underway", "",
                                "note 0", "", "note 1", "", "note 2",   # each said after a wait
                                "Stage 8 - Installing interim firmware - Stopped"])

    def test_the_stage_s_one_line_turns_above_what_it_shows(self):
        # Seen in an attended emulator run: the line was drawn again, turning,
        # under the device's report, two "Stage 2" lines until the stage ended.
        def body():
            output.stage_begin(2, "Discovering the device")
            prompts.section("What the device reported", ["Model    SN-ESP32C3-BASICR4-01"])
            output.tick()
            screen.append(rendered(terminal.getvalue()))
            output.stage_end("complete")
        screen, terminal = [], FakeTerminal()
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "spinner_frames", return_value="|/-\\"), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            self.run_with(terminal, False, body)
        rows = screen[0]
        self.assertEqual(sum("Discovering the device" in row for row in rows), 1, "\n".join(rows))
        self.assertRegex(rows[0], r"^Stage 2 - Discovering the device - Underway [|/\\-]$")
        self.assertEqual(rows[-1], "Model SN-ESP32C3-BASICR4-01")

    def test_a_ctrl_c_notice_goes_under_what_the_stage_shows_and_off_before_the_next_note(self):
        from tools.autoflash import interrupt
        self.addCleanup(interrupt.clear)

        def body():
            output.stage_begin(2, "Discovering the device")
            output.tell("    note 0")
            interrupt._state["first"] = time.monotonic()   # as a first Ctrl-C leaves it
            output.tick()
            screen.append(rendered(terminal.getvalue()))
            interrupt.clear()
            output.tell("    note 1")
            screen.append(rendered(terminal.getvalue()))
            output.stage_end("stopped")
        screen, terminal = [], FakeTerminal()
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            self.run_with(terminal, False, body)
        drawn, after = screen
        self.assertEqual(sum("Discovering the device" in row for row in drawn), 1, "\n".join(drawn))
        self.assertEqual(drawn[-4:], ["note 0", "", interrupt.NOTICE_TITLE,
                                      f"Press Ctrl-C again within {interrupt.GRACE:.0f} seconds to stop now."])
        self.assertEqual(after[-2:], ["", "note 1"])   # said after a wait: a gap, as any note
        self.assertFalse([row for row in after if "Ctrl-C" in row], "\n".join(after))

    def test_a_question_after_the_stage_ended_leaves_its_line_be(self):
        # Seen in an attended emulator run: "What next", after a stage that had
        # shown notes, drew it "Underway" again over its "Complete"; and the
        # next run's first question drew it among that run's own rows.
        def body():
            output.stage_begin(2, "Discovering the device")
            output.tell("    note 0")
            output.tick()
            output.stage_end("complete")
            output.tell("Readout saved.")
            output.tell("    1. Convert another device")
            output.before_input("What next")
            output.after_input()
            screen.append(rendered(terminal.getvalue()))
        screen, terminal = [], FakeTerminal()
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            self.run_with(terminal, False, body)
        self.assertFalse([row for row in screen[0] if "Underway" in row], "\n".join(screen[0]))
        self.assertIn("Stage 2 - Discovering the device - Complete", screen[0])

    def test_out_of_reach_the_stage_s_line_turns_under_what_it_shows(self):
        # Pushed past the window's top by what the stage shows, the line
        # cannot be reached: it is drawn again under the last note, as before.
        def body():
            output.stage_begin(2, "Discovering the device")
            for n in range(12):
                output.tell(f"    note {n}")
            output.tick()
            screen.append(rendered(terminal.getvalue()))
            output.stage_end("stopped")
        screen, terminal = [], FakeTerminal()
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 10))):
            self.run_with(terminal, False, body)
        rows = screen[0]
        self.assertEqual(sum("Discovering the device" in row for row in rows), 2, "\n".join(rows))
        self.assertTrue(rows[-1].startswith("Stage 2 - Discovering the device - Underway"), rows[-1])

    def test_a_note_said_after_a_wait_stands_apart_its_own_lines_together(self):
        # Seen in the fault suites' screens: notes of different moments - the
        # device gone back to Sonoff's firmware, then nothing answering - ran on
        # as one.
        def body():
            output.stage_begin(7, "Backing up Sonoff firmware")
            output.tell("    While waiting, the device went back to Sonoff's own firmware (switched off")
            output.tell("    and on, say).")
            output.tick()   # the clock drawn under it: time has passed
            output.tell("    Nothing is answering at the device's address.")
            output.stage_end("stopped")
        self.assertEqual(self.drawn(body)[1:6], ["", "While waiting, the device went back to Sonoff's own firmware "
                                                 "(switched off", "and on, say).", "",
                                                 "Nothing is answering at the device's address."])

    def test_what_is_shown_while_waiting_goes_once_done_and_stays_if_the_wait_stops_the_stage(self):
        def body(fail):
            output.stage_begin(8, "Installing interim firmware")
            try:
                with output.until_done():
                    output.tell("-- Join the bridge's Wi-Fi network")
                    if fail:
                        raise RuntimeError("never joined")
            except RuntimeError:
                output.stage_end("stopped")
                return
            output.tell("    The installer joined your network.")
            output.stage_end("stopped")
        joined = self.drawn(lambda: body(False))
        self.assertNotIn("-- Join the bridge's Wi-Fi network", joined)
        self.assertIn("The installer joined your network.", joined)
        self.assertIn("-- Join the bridge's Wi-Fi network", self.drawn(lambda: body(True)))

    def test_a_stage_that_stopped_leaves_what_it_showed(self):
        def body():
            output.stage_begin(8, "Installing interim firmware")
            output.tell("    The bridge was not delivered")
            output.stage_end("stopped")
        rows = self.drawn(body)
        self.assertIn("The bridge was not delivered", rows)
        self.assertEqual(rows[-1], "Stage 8 - Installing interim firmware - Stopped")

    def test_what_a_stage_showed_taller_than_the_window_stays(self):
        def body():
            output.stage_begin(8, "Installing interim firmware")
            for n in range(8):
                output.tell(f"    note {n}")
            output.stage_end("complete")
        rows = self.drawn(body, rows=6)
        self.assertIn("note 0", rows)
        self.assertEqual(rows[-1], "Stage 8 - Installing interim firmware - Complete")

    def test_success_is_green_and_underway_is_bold(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                output.stage_begin(1, "Checking the files")
                output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"{output.BOLD}Underway{output.RESET}", screen)
        self.assertIn(f"{output.GREEN}Stage 1{output.RESET} - Checking the files - "
                      f"{output.GREEN}Complete{output.RESET}", screen)
        self.assertIn("Stage 1 - Checking the files - complete (0s)", log)

    def test_a_stage_done_on_an_earlier_run_is_green_too(self):
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                output.stage_begin(1, "Checking the files")
                output.stage_end("already done")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"{output.GREEN}Stage 1{output.RESET} - Checking the files - "
                      f"{output.GREEN}Already done{output.RESET}", screen)

    def test_a_skipped_stage_is_green(self):
        # The backup, when the person carries on without one: nothing went wrong.
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True):
                output.stage_begin(6, "Backing up the Sonoff firmware")
                output.stage_end("skipped")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"{output.GREEN}Stage 6{output.RESET} - Backing up the Sonoff firmware - "
                      f"{output.GREEN}Skipped{output.RESET}", screen)
        self.assertIn("Stage 6 - Backing up the Sonoff firmware - skipped", log)

    def test_a_question_cut_short_is_taken_off_with_its_note_when_the_stage_stops(self):
        # Seen: Ctrl-C at PROCEED left the note, the question and the stage's
        # underway line, and its Stopped line came under them.
        def pressed(prompt):
            screen.write(prompt)
            raise KeyboardInterrupt

        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
                output.stage_begin(5, "Sending interim firmware")
                output.transient_begin()
                prompts.warning_box("Before going on", ["DO NOT CUT ITS POWER during the process."])
                with mock.patch("builtins.input", side_effect=pressed), self.assertRaises(KeyboardInterrupt):
                    prompts.require_word(prompts.CONSENT_WORD)
                output.stage_end("stopped")
                output.show("Stopped with Ctrl-C.")
        screen = FakeTerminal()
        text, log = self.run_with(screen, False, body)
        rows = [row for row in rendered(text) if row]
        self.assertEqual(rows, ["Stage 5 - Sending interim firmware - Stopped", "Stopped with Ctrl-C."])
        self.assertIn("Before going on", log)   # the log keeps it

    def test_a_run_stopped_at_a_question_leaves_neither_it_nor_blank_rows_under_the_stage(self):
        # Seen: a readout stopped at "The device was not found on this
        # network. Its address" - its help taken off, the question left at the
        # bottom with five blank rows between it and the stage's Stopped line.
        for stop in (KeyboardInterrupt, SystemExit):
            def pressed(prompt):
                screen.write(prompt)
                raise stop

            def body():
                with mock.patch.object(output, "colour_ok", return_value=True), \
                     mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
                    output.show("A readout usually takes under 10 minutes.")
                    output.stage_begin(2, "Locating the device on your network")
                    output.transient_begin()
                    with mock.patch("builtins.input", side_effect=pressed), self.assertRaises(stop):
                        prompts.ask_ipv4("The device was not found on this network. Its address",
                                         help_lines=["It answers no local discovery, so it was looked for",
                                                     "by the hardware address eWeLink has for it."])
                    output.stage_end("stopped")
            with self.subTest(stop=stop.__name__):
                screen = FakeTerminal()
                text, _ = self.run_with(screen, False, body)
                rows = rendered(text)
                self.assertEqual(rows[-1], "Stage 2 - Locating the device on your network - Stopped", rows)
                self.assertFalse([row for row in rows if "Its address" in row or "hardware address" in row], rows)
                self.assertNotIn(("", ""), list(zip(rows, rows[1:])), rows)

    def test_a_run_that_ends_with_its_stage_under_way_shows_it_stopped_and_its_question_gone(self):
        # Seen in the emulator: SIGTERM at the firmware question (its SystemExit
        # ends no stage) left the stage "Underway" and the question waiting.
        def pressed(prompt):
            screen.write(prompt)
            raise SystemExit(143)

        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
                output.stage_begin(2, "Choosing the firmware")
                output.transient_begin()
                output.show("-- Choose the device's new firmware")
                output.tell("    1. Tasmota")
                with mock.patch("builtins.input", side_effect=pressed), self.assertRaises(SystemExit):
                    prompts.ask_choice("Which firmware would you like", ["Tasmota", "ESPHome"])
        screen = FakeTerminal()
        text, _ = self.run_with(screen, False, body)
        rows = rendered(text)
        self.assertEqual(rows[-1], "Stage 2 - Choosing the firmware - Stopped", rows)
        self.assertFalse([row for row in rows if "firmware would you like" in row or "Tasmota" in row], rows)

    def test_a_ctrl_c_notice_outside_a_stage_goes_with_its_block(self):
        # Seen: Ctrl-C while the run looked for devices, before any stage,
        # left "! Are you sure?" on the screen for the rest of the run - the
        # signal handler wrote it uncounted, so the block was taken off short.
        from tools.autoflash import interrupt
        self.addCleanup(interrupt._state.update, dict(interrupt._state))
        interrupt._state.update(first=None, asking=0, deferring=0, deferred=False)

        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))), \
                 mock.patch.object(interrupt.sys, "stderr", screen):
                output.show("A note before the block.")
                output.transient_begin()
                output.tell("Looking for supported devices on your network...")
                interrupt._on_interrupt(signal.SIGINT, None)
                output.tell("A question, and its answer.")
                output.transient_end()
                output.stage_begin(1, "Confirming prerequisites")
                output.stage_end("complete")
        screen = FakeTerminal()
        text, _ = self.run_with(screen, False, body)
        self.assertIn(interrupt.NOTICE_TITLE, text)   # it was shown
        rows = [row for row in rendered(text) if row]
        self.assertEqual(rows, ["A note before the block.", "Stage 1 - Confirming prerequisites - Complete"])

    def test_without_cursor_control_the_question_stays(self):
        def body():
            output.stage_begin(5, "Installing the installer firmware")
            output.transient_begin()
            prompts.warning_box("Before going on", ["One line."])
            output.transient_end()
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)   # NO_COLOR is set
        self.assertNotIn("\x1b[", screen)
        self.assertIn("Before going on", screen)

    def test_a_message_mid_stage_starts_on_its_own_line(self):
        def body():
            output.stage_begin(1, "Installing Tasmota")
            output.show("Join the bridge's Wi-Fi network")
            output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        lines = [line.split("\r")[-1] for line in screen.split("\n")]
        self.assertIn(" Join the bridge's Wi-Fi network", lines)
        self.assertTrue(any(line.startswith(" Stage 1 - Installing Tasmota - Complete") for line in lines))

    def test_progress_stays_off_the_concise_screen_and_the_log(self):
        frames = []

        def body():
            frames.append(output.spinner_frames())
            output.stage_begin(1, "Installing Tasmota")
            output.progress("looking for the installer on your network... 12s")
            output.progress_clear()
            output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertNotIn("looking for the installer", screen)
        self.assertNotIn("looking for the installer", log)
        self.assertIn(f"Installing Tasmota - Underway {frames[0][0]}", screen)

    def test_a_stage_begun_twice_keeps_one_line(self):
        def body():
            output.stage_begin(1, "Installing Tasmota")
            output.stage_begin(1, "Installing Tasmota")
            output.stage_end("already done")
        screen, _ = self.run_with(io.StringIO(), False, body)
        self.assertEqual(screen.count("Underway"), 1)
        self.assertIn("Installing Tasmota - Already done", screen)

    def test_quiet_sections_are_logged_only(self):
        def body():
            prompts.section("What the device reported", ["Firmware now 1.2.0"], quiet=True)
            prompts.section("Join the device's setup network", ["Connect to it now."])
        screen, log = self.run_with(io.StringIO(), False, body)
        self.assertNotIn("What the device reported", screen)
        self.assertIn("What the device reported", log)
        self.assertIn("Join the device's setup network", screen)

    def test_the_warning_is_a_short_note(self):
        screen, _ = self.run_with(io.StringIO(), False,
                                  lambda: prompts.warning_box("Before going on", ["One line."]))
        self.assertEqual(screen.strip().splitlines(), ["! Before going on", "     One line."])
        self.assertNotIn("!!", screen)

    def test_block_counters_stay_off_the_concise_screen(self):
        counter = "\r    Copying the startup firmware: block 3/198"
        screen, _ = self.run_with(io.StringIO(), False, lambda: output.raw_progress(counter))
        self.assertNotIn("block 3", screen)


class VerboseTests(OutputTestCase):
    def test_everything_is_shown_and_logged(self):
        def body():
            output.stage_begin(1, "Checking the files")
            output.detail("bundle and pinned artifacts: OK")
            output.stage_end("complete")
        screen, log = self.run_with(io.StringIO(), True, body)
        self.assertIn("== Stage 1 - Checking the files", screen)
        self.assertIn("bundle and pinned artifacts: OK", screen)
        self.assertIn("bundle and pinned artifacts: OK", log)
        self.assertIn("(verbose)", log)

    def test_outside_a_run_everything_is_shown(self):
        screen = io.StringIO()
        with mock.patch("sys.stdout", screen):
            output.detail("No conversions recorded yet.")
        self.assertIn("No conversions recorded yet.", screen.getvalue())


class StreamTests(OutputTestCase):
    """A helper tool's output goes to the log, and to the screen only when verbose."""

    CHILD = ("import sys, time\n"
             "print('Serving the firmware', flush=True)\n"
             "sys.stdout.write('\\r10%'); sys.stdout.flush()\n"
             "sys.stdout.write('\\r100%\\n'); sys.stdout.flush()\n"
             "print('Done')\n")

    def child(self):
        return subprocess.Popen([sys.executable, "-c", self.CHILD],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_concise_keeps_it_off_the_screen(self):
        codes = []
        screen, log = self.run_with(io.StringIO(), False, lambda: codes.append(output.stream(self.child())))
        self.assertEqual(codes, [0])
        self.assertNotIn("Serving the firmware", screen)
        self.assertIn("| Serving the firmware", log)
        self.assertIn("| 100%", log)          # where the redrawn line ended up
        self.assertNotIn("10%\r", log)

    def test_verbose_passes_it_through(self):
        screen, log = self.run_with(io.StringIO(), True, lambda: output.stream(self.child()))
        self.assertIn("Serving the firmware", screen)
        self.assertIn("| Done", log)

    def test_windows_line_endings_keep_the_line(self):
        # A child on Windows ends its lines "\r\n": the "\r" is not a redraw.
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'Served 10 bytes\\r\\nDone\\r\\n')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        _, log = self.run_with(io.StringIO(), False, lambda: output.stream(child))
        self.assertIn("| Served 10 bytes", log)
        self.assertIn("| Done", log)
        self.assertEqual(output.tool_problems(), ["Served 10 bytes", "Done"])


class InputTests(OutputTestCase):
    def test_questions_and_answers_are_logged(self):
        def body():
            with mock.patch("builtins.input", return_value="y"):
                self.assertTrue(prompts.ask_yes_no("Have you joined it"))
        _, log = self.run_with(io.StringIO(), False, body)
        self.assertIn("? Have you joined it", log)
        self.assertIn("-> y", log)

    def test_each_run_on_the_same_log_is_marked_where_it_starts_and_ends(self):
        # A run carried on appends to its log; one cut off has no end mark.
        self.log = Path(self.tmp.name) / "devices" / "run" / "run.log"
        for verbose in (False, True):
            self.run_with(io.StringIO(), verbose, lambda: None)
        log = self.log.read_text(encoding="utf-8")
        self.assertEqual((log.count("=== run started"), log.count("=== run ended ===")), (2, 2))
        self.assertIn("(verbose) ===", log)

    def test_a_password_is_never_logged(self):
        def body():
            with mock.patch.object(prompts, "_read_secret", return_value="Secret-9x"):
                self.assertEqual(prompts.ask_secret("eWeLink password"), "Secret-9x")
        _, log = self.run_with(io.StringIO(), False, body)
        self.assertIn("eWeLink password", log)
        self.assertNotIn("Secret-9x", log)

    def test_a_blank_line_stands_between_a_question_and_what_is_above_it(self):
        answers = iter(["1", "y", "y"])

        def body():
            prompts.section("Choose the device's new firmware")
            with mock.patch("builtins.input", side_effect=lambda prompt: print(prompt) or next(answers)):
                prompts.ask_choice("Which firmware would you like", ["OpenBeken", "ESPHome"])
                prompts.ask_yes_no("Is it plugged in")   # straight after the answer above
                output.show("")
                prompts.ask_yes_no("Go on")              # a blank line already: not two
        screen, _ = self.run_with(io.StringIO(), False, body)
        self.assertIn("2. ESPHome\n\n Which firmware would you like (q to quit): \n", screen)
        self.assertIn("would you like (q to quit): \n\n Is it plugged in", screen)
        self.assertNotIn("\n\n\n", screen)



class NarrowTerminalTests(OutputTestCase):
    def test_a_stage_line_too_long_for_the_terminal_is_cut_short(self):
        def body():
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((40, 24))):
                output.stage_begin(6, "Installing the official startup firmware")
                output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        drawn = screen.split("\r")[1]
        self.assertTrue(drawn.endswith("..."))
        self.assertLessEqual(len(drawn), 39)

    def test_a_terminal_that_reports_no_size_is_not_taken_as_narrow(self):
        # Python 3.10 hands back the 0 a pseudo-terminal nobody sized reports.
        def body():
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((0, 0))):
                output.stage_begin(6, "Installing the official startup firmware")
                output.stage_end("complete")
        screen, _ = self.run_with(FakeTerminal(), False, body)
        self.assertIn("Installing the official startup firmware", screen)
        self.assertNotIn("...", screen)


class FallbackScreenTests(OutputTestCase):
    """The Wi-Fi fallbacks still say everything the person has to read or do."""

    def test_typing_the_wifi_shows_why_and_every_question(self):
        from tools.autoflash.device import DeviceError

        class Ctx:
            args = mock.Mock(wifi=False)
            _wifi = None
            esphome_api_locked = False
            manifest_path = Path("m.json")
            state = mock.Mock()

            @property
            def device(self):
                raise DeviceError("no answer")

            def adopt_wifi(self, ssid, password):
                self._wifi = (ssid, password)

        secrets = iter(["password-one", "password-oops", "password-one", "password-one"])

        def body():
            with mock.patch.object(context, "bundle_ssid", return_value=None), \
                 mock.patch("builtins.input", return_value="Home-2G"), \
                 mock.patch.object(prompts, "_read_secret", side_effect=lambda q, r: next(secrets)):
                self.assertEqual(context.Context.wifi(Ctx()), ("Home-2G", "password-one"))
        screen, log = self.run_with(io.StringIO(), False, body)
        self.assertIn("could not be read off the device: it did not answer as Tasmota", screen)
        self.assertIn("Your Wi-Fi details are needed", screen)
        self.assertIn("The two passwords did not match", screen)
        self.assertNotIn("password-one", log)

    def test_the_setup_network_steps_are_all_on_screen(self):
        from tools.autoflash import setup_network
        polls = iter([None, "192.0.2.77"])
        ctx = mock.Mock(wifi=lambda why=None: ("Home-2G", "password-one"))
        ctx.finder.poll = lambda: next(polls)

        def body():
            with mock.patch.object(hostsys, "wifi_menu_hint", return_value="the Wi-Fi menu"), \
                 mock.patch.object(setup_network, "wait_for_port", return_value=True), \
                 mock.patch.object(setup_network, "send_wifi", return_value=("saved", "192.0.2.77")), \
                 mock.patch.object(join, "name_with_the_wifi"), \
                 mock.patch.object(time, "sleep"):
                join.configure_device_wifi(ctx, "tasmota-5A3C1F-7340", join.NOT_GIVEN)
        screen, _ = self.run_with(io.StringIO(), False, body)
        for words in ("Join the device's setup network", "tasmota-5A3C1F-7340", "(open - no password)",
                      "stay on its network for about 20 s",
                      "The device connected to 'Home-2G' and is restarting onto it",
                      "Back to your usual Wi-Fi"):
            self.assertIn(words, screen)



    def send(self, outcomes, unattended=False, gone=False):
        from tools.autoflash import prompts, setup_network
        polls = iter([None, "192.0.2.77"])
        ctx = mock.Mock(wifi=lambda why=None: ("Home-2G", "password-one"))
        ctx.finder.poll = lambda: next(polls, "192.0.2.77")
        results = iter(outcomes)
        sent = []

        def send_wifi(ssid, password):
            sent.append(ssid)
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        def body():
            with mock.patch.object(hostsys, "wifi_menu_hint", return_value="the Wi-Fi menu"), \
                 mock.patch.object(setup_network, "wait_for_port", return_value=True), \
                 mock.patch.object(setup_network, "send_wifi", side_effect=send_wifi), \
                 mock.patch.object(join, "ask_credentials", return_value=("Home-New", "pw")) as asked, \
                 mock.patch.object(prompts, "UNATTENDED", unattended), \
                 mock.patch.object(join, "name_with_the_wifi"), \
                 mock.patch.object(join, "setup_network_gone", return_value=gone), \
                 mock.patch.object(time, "sleep"):
                join.configure_device_wifi(ctx, "tasmota-5A3C1F-7340", join.NOT_GIVEN)
                self.asked = asked.called
        screen, _ = self.run_with(io.StringIO(), False, body)
        return sent, screen

    def test_no_word_from_the_device_it_is_sent_again(self):
        from tools.autoflash import setup_network
        sent, screen = self.send([setup_network.SetupError("no answer"), ("unknown", None),
                                  ("saved", "192.0.2.77")])
        self.assertEqual(sent, ["Home-2G"] * 3)
        self.assertIn("sending them again (attempt 3 of 3)", screen)
        self.assertIn("restarting onto it", screen)

    def test_no_word_and_its_network_gone_it_is_looked_for_at_home(self):
        # Seen in the fault suite: the device took them, saved them and restarted
        # onto the Wi-Fi before its answer was asked for. Sent again, the run
        # waited a quarter of an hour for this computer to join a network gone.
        sent, screen = self.send([("unknown", None)], gone=True)
        self.assertEqual(sent, ["Home-2G"])
        self.assertIn("its network went", screen)
        self.assertNotIn("sending them again", screen)
        self.assertIn("Back to your usual Wi-Fi", screen)

    def test_rejected_details_are_asked_for_again(self):
        sent, _ = self.send([("rejected", None), ("saved", "192.0.2.77")])
        self.assertEqual(sent, ["Home-2G", "Home-New"])
        self.assertTrue(self.asked)

    def test_unattended_rejected_details_go_to_the_devices_page(self):
        sent, screen = self.send([("rejected", None)], unattended=True)
        self.assertEqual(sent, ["Home-2G"])
        self.assertFalse(self.asked)
        self.assertIn("Set the Wi-Fi up on the device's page instead", screen)

class FailureReasonTests(OutputTestCase):
    """When something fails, the reason reaches the screen, not only the log."""

    def test_a_helper_tools_own_reason_is_kept(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "print('Logging in...'); print('Error: login failed'); raise SystemExit(1)"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        codes = []
        self.run_with(io.StringIO(), False, lambda: codes.append(output.stream(child)))
        self.assertEqual(codes, [1])
        self.assertEqual(output.tool_problems(), ["Error: login failed"])

    def test_without_an_error_line_the_last_lines_are_given(self):
        child = subprocess.Popen([sys.executable, "-c", "print('one'); print('two'); raise SystemExit(2)"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.run_with(io.StringIO(), False, lambda: output.stream(child))
        self.assertEqual(output.tool_problems(), ["one", "two"])

    def test_a_phase_refusal_is_shown_and_its_progress_is_not(self):

        def body():
            phases.phase_log("    The device fetched its instructions.")
            phases.phase_log("    REFUSED: preflight evidence has the wrong source table")
            phases.phase_log("    The server's full output is in captures/run/preflight-server.log")
        screen, log = self.run_with(io.StringIO(), False, body)
        self.assertNotIn("fetched its instructions", screen)
        self.assertIn("fetched its instructions", log)
        self.assertIn("REFUSED: preflight evidence has the wrong source table", screen)
        self.assertIn("preflight-server.log", screen)

    def test_the_bundle_download_is_logged_not_drawn_over_the_stage(self):
        from tools.espressif.safeboot.prepare_safeboot_migration import obtain
        source = Path(self.tmp.name) / "source.bin"
        source.write_bytes(b"x" * 10)

        def body():
            output.stage_begin(2, "Checking the files on this computer")
            obtain(source, "http://unused.example/", Path(self.tmp.name) / "pinned.bin")
            output.stage_end("complete")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertNotIn("Pinned", screen)
        self.assertIn("Pinned pinned.bin: 10 bytes", log)



class ReportTests(OutputTestCase):
    """The end of a run asks for a report, filled in from the run and nothing private."""

    def setUp(self):
        super().setUp()
        # The release and sensor checks ask a device, which these runs have
        # none of: test_report.EndOfRunCheckTests has them.
        for name in ("check_release", "check_sensors"):
            patcher = mock.patch.object(report, name, return_value={})
            patcher.start()
            self.addCleanup(patcher.stop)

    def ctx(self, **inputs):
        from types import SimpleNamespace
        from tools.autoflash.state import RunState
        state = RunState.open(Path(self.tmp.name) / "run")
        for key, value in {"product_model": "S60TPG", "stock_version": "1.2.0",
                           "email": "me@example.com", "device_ip": "192.0.2.50",
                           "device_name": "tasmota-1A2B3C-4567", **inputs}.items():
            state.set_input(key, value)
        state.capture("D1.upload_route", "home network")
        state.capture("D1.wifi", "preset")
        state.capture("F4.wifi_route", "joined by itself")
        return SimpleNamespace(state=state)

    def test_the_report_is_filled_in_from_the_run(self):
        fields = dict(report.report_fields(self.ctx()))
        self.assertEqual(fields["Result"], "Converted")
        self.assertEqual(fields["Model"], "S60TPG")
        self.assertNotIn("Device type / region", fields)  # the model says it
        self.assertEqual(fields["From"], "Sonoff 1.2.0")
        self.assertNotIn("Stock", fields)
        self.assertNotIn("Python", fields["OS"])
        self.assertRegex(fields["Python"], r"^3\.\d+\.\d+ \(")
        self.assertNotIn("Time", fields)   # in the report sent, not this one
        self.assertEqual(fields["Wi-Fi typed"], "No")
        self.assertEqual(fields["Setup network"], "No")

    def test_a_run_from_a_converted_device_takes_the_model_from_its_record(self):
        # Back to Sonoff from OpenBeken: neither Sonoff's firmware nor
        # eWeLink was asked, but the device's record has both.
        ctx = self.ctx(device_kind="sonoff-s61", product_model="", stock_version="", firmware="sonoff",
                       source="openbeken", device_mac="12:34:56:ab:cd:ef")
        with mock.patch.object(records, "read_device", return_value={"model": "S61STPF", "stock_version": "1.3.0"}) as read:
            fields = dict(report.report_fields(ctx))
        read.assert_called_with("12:34:56:ab:cd:ef")
        self.assertEqual(fields["Model"], "S61STPF")
        self.assertEqual(fields["From"], "OpenBeken")
        with mock.patch.object(records, "read_device", return_value={}):
            fields = dict(report.report_fields(ctx))
        self.assertEqual(fields["Model"], "Sonoff S61")
        # On Sonoff's route the kind is only a default until eWeLink has been asked.
        with mock.patch.object(records, "read_device", return_value={}):
            fields = dict(report.report_fields(self.ctx(device_kind="sonoff-s61", product_model="", stock_version="",
                                                        source="sonoff")))
        self.assertEqual(fields["Model"], "__")
        # What the run itself found comes first.
        with mock.patch.object(records, "read_device", return_value={"model": "S61STPF", "stock_version": "1.3.0"}):
            fields = dict(report.report_fields(self.ctx(device_kind="sonoff-s61", product_model="S61STPE",
                                                        stock_version="1.2.0", source="sonoff")))
        self.assertEqual((fields["Model"], fields["From"]), ("S61STPE", "Sonoff 1.2.0"))

    def test_a_run_stopped_before_its_firmware_is_chosen_names_none(self):
        self.assertEqual(dict(report.report_fields(self.ctx()))["Firmware"], "Not chosen")
        chosen = dict(report.report_fields(self.ctx(firmware="esphome")))
        self.assertEqual(chosen["Firmware"], "ESPHome")

    def test_a_report_says_whether_the_device_was_power_cycled(self):
        self.assertEqual(dict(report.report_fields(self.ctx()))["Power cycled"], "False")  # an S60's too
        s61 = dict(device_kind="sonoff-s61", product_model="S61STPF", firmware="openbeken", source="sonoff")
        self.assertEqual(dict(report.report_fields(self.ctx(**s61)))["Power cycled"], "False")
        ctx = self.ctx(**s61, power_cycles={"by hand": 1})
        self.assertEqual(dict(report.report_fields(ctx))["Power cycled"], "True")

    def test_no_report_gives_the_name_the_firmware_gave_the_device(self):
        # OpenBeken's holds the MAC's last three bytes; the model says the rest.
        for firmware, kind in (("tasmota", "sonoff-s60"), ("esphome", "sonoff-s60"), ("openbeken", "sonoff-s61"),
                               ("esphome", "sonoff-s61")):
            fields = report.report_fields(self.ctx(device_kind=kind, firmware=firmware, source="sonoff",
                                                   device_name="Sonoff-S61-abcdef"))
            self.assertFalse([label for label, _ in fields if label.startswith("Name in")], firmware)
            self.assertNotIn("abcdef", "".join(value for _, value in fields), firmware)

    def test_an_openbeken_run_checks_the_relay_and_meter_as_tasmotas_does(self):
        ctx = self.ctx(device_kind="sonoff-s61", product_model="S61STPF", firmware="openbeken", source="esphome")
        with mock.patch.object(report, "check_hardware", return_value={"Relay": "works"}) as checked, \
                mock.patch.object(report, "offer_report"), mock.patch.object(output, "show_links"):
            report.invite_report(ctx, 441, False)
        checked.assert_called_once_with(ctx)

    def test_a_device_with_no_relay_is_not_switched_but_its_release_is_checked(self):
        ctx = self.ctx(device_kind="sonoff-s61", product_model="S61STPF", firmware="openbeken", source="esphome")
        no_relay = dataclasses.replace(report.kinds.SONOFF_S61, has_relay=False)
        with mock.patch.object(report.kinds, "of_run", return_value=no_relay), \
                mock.patch.object(report, "check_hardware", side_effect=AssertionError("nothing to switch")), \
                mock.patch.object(report, "check_release", return_value={"Release": "1.18.320-1, as installed"}), \
                mock.patch.object(report, "offer_report") as offered, \
                mock.patch.object(report.report_upload, "send") as sent, mock.patch.object(output, "show_links"):
            report.invite_report(ctx, 441, False)
        self.assertEqual(dict(offered.call_args.args[1])["Checks"], "Pass")
        self.assertEqual(sent.call_args.args[4], {"Release": "1.18.320-1, as installed"})

    def test_a_device_with_nothing_to_check_has_no_checks_line(self):
        # No relay, no sensors, and back on Sonoff's firmware, whose release is eWeLink's.
        ctx = self.ctx(device_kind="sonoff-s61", product_model="S61STPF", firmware="sonoff", source="openbeken")
        bare = dataclasses.replace(report.kinds.SONOFF_S61, has_relay=False, sensors=())
        with mock.patch.object(report.kinds, "of_run", return_value=bare), \
                mock.patch.object(report, "check_ewelink_hardware", side_effect=AssertionError("nothing to switch")), \
                mock.patch.object(report, "offer_report") as offered, \
                mock.patch.object(report.report_upload, "send") as sent, mock.patch.object(output, "show_links"):
            report.invite_report(ctx, 441, False)
        self.assertNotIn("Checks", dict(offered.call_args.args[1]))
        self.assertEqual(sent.call_args.args[4], {}, "the report sent says the check was not run")

    def test_a_conversion_is_numbered_on_this_computer_whether_its_report_is_sent_or_not(self):
        with tempfile.TemporaryDirectory() as devices, mock.patch.object(records, "DEVICES", Path(devices)), \
                mock.patch.object(report, "check_hardware", return_value={}), mock.patch.object(report, "offer_report"), \
                mock.patch.object(report.report_upload, "send"), mock.patch.object(output, "show_links"):
            for firmware, mac in (("tasmota", "12:34:56:ab:cd:01"), ("sonoff", "12:34:56:ab:cd:02"),
                                  ("tasmota", "12:34:56:ab:cd:03")):
                report.invite_report(self.ctx(run=f"number-{mac}", firmware=firmware, device_mac=mac), 441, False)
            numbers = [records.read_device(mac).get(records.CONVERTED_NUMBER)
                       for mac in ("12:34:56:ab:cd:01", "12:34:56:ab:cd:02", "12:34:56:ab:cd:03")]
        self.assertEqual(numbers, [1, None, 2], "back to Sonoff's is no conversion")

    def test_a_run_back_to_sonoff_checks_the_relay_through_ewelink(self):
        ctx = self.ctx(device_kind="sonoff-s61", product_model="S61STPF", firmware="sonoff", source="openbeken",
                       device_mac="12:34:56:ab:cd:ef")
        ctx.email = lambda: "someone@example.invalid"
        ctx.ewelink = mock.sentinel.account
        ctx.account_devices = lambda: [{"deviceid": "1000abcdef", "online": True,
                                        "params": {"switches": [{"outlet": 0, "switch": "off"}]}}]
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(report.ewelink_relay, "check_relay", return_value=report.ewelink_relay.RelayCheck(True, True)) as switched, \
                mock.patch.object(report, "offer_report") as offered, mock.patch.object(output, "show_links"):
            report.invite_report(ctx, 441, False)
        self.assertEqual(switched.call_args.kwargs["device_id"], "1000abcdef")
        fields = dict(offered.call_args.args[1])
        self.assertEqual(fields["Checks"], "Pass")
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(report.ewelink_relay, "check_relay", return_value=report.ewelink_relay.RelayCheck(True, False)):
            self.assertEqual(report.check_ewelink_hardware(ctx)["Relay"], "did not switch")

    def test_a_run_back_to_sonoff_reads_the_meter_through_ewelink(self):
        # Sonoff's S60 firmware in the emulator, a 100 W load on 230 V.
        ctx = self.ewelink_ctx([True])
        checked = report.ewelink_relay.RelayCheck(True, True, {"Voltage": 230.08, "Power": 99.99},
                                                  {"Voltage": 0.0, "Power": 0.0})
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(report.ewelink_relay, "check_relay", return_value=checked), \
                mock.patch.object(output, "say") as said:
            hardware = report.check_ewelink_hardware(ctx)
        self.assertEqual(hardware, {"Relay": "switches (meter confirms)",
                                    "Power meter": "on: 230.08 V, 99.99 W; off: 0 V, 0 W"})
        self.assertEqual(report.checks_passed(hardware), "pass")
        lines = [c.args[0] for c in said.call_args_list]
        self.assertIn("    Relay: Switches (meter confirms)", lines)
        self.assertIn("    Power meter: On: 230.08 V, 99.99 W; off: 0 V, 0 W", lines)

    def test_a_dualr3_driving_a_motor_says_both_answers(self):
        ctx = self.ewelink_ctx([True])
        ctx.account_devices = lambda: [{"deviceid": "1000abcdef", "online": True, "params": {"workMode": 2}}]
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(output, "say") as said:
            report.check_ewelink_hardware(ctx)
        lines = [c.args[0] for c in said.call_args_list]
        self.assertIn("    Relay: Motor mode", lines)
        self.assertIn("    Power meter: Not checked", lines)

    def ewelink_ctx(self, seen: list[bool]):
        """A run back to Sonoff on an S60, whose account shows the device as
        `seen` says, one look at a time (then as the last)."""
        ctx = self.ctx(firmware="sonoff", source="tasmota", device_mac="12:34:56:ab:cd:ef")
        ctx.email = lambda: "someone@example.invalid"
        ctx.ewelink = mock.sentinel.account
        looks = iter(seen)
        self.online = False

        def account_devices():
            self.online = next(looks, self.online)
            return [{"deviceid": "1000abcdef", "online": self.online,
                     "params": {"switches": [{"outlet": 0, "switch": "off"}]}}]
        ctx.account_devices = account_devices
        return ctx

    def test_a_device_back_online_is_switched_once_its_record_says_its_relays_state(self):
        # Seen in the emulator: the device answered, and its first report of its
        # relay came 1.5 s after the check read its record, which said nothing.
        ctx = self.ewelink_ctx([True])
        looks = iter([{}, {}, {"switches": [{"outlet": 0, "switch": "off"}]}])
        state = {"params": {}}

        def account_devices():
            state["params"] = next(looks, state["params"])
            return [{"deviceid": "1000abcdef", "online": True, "params": state["params"]}]
        ctx.account_devices = account_devices
        reached = lambda **kwargs: report.ewelink_relay.RelayCheck(True, True) if state["params"] else None  # noqa: E731
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(report.ewelink_relay, "check_relay", side_effect=reached), \
                mock.patch.object(report.time, "sleep") as slept:
            self.assertEqual(report.check_ewelink_hardware(ctx)["Relay"], "switches (through eWeLink)")
        self.assertEqual(slept.call_count, 2)

    def test_a_run_back_to_sonoff_waits_for_ewelink_before_switching_the_relay(self):
        # An Espressif run's last step sees the device on the network, seconds
        # before Sonoff's firmware has dialled eWeLink: switched at once, it could
        # not be (seen in the emulator, every run back to Sonoff).
        ctx = self.ewelink_ctx([False, False, True])
        reached = lambda **kwargs: report.ewelink_relay.RelayCheck(True, True) if self.online else None  # noqa: E731
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(report.ewelink_relay, "check_relay", side_effect=reached), \
                mock.patch.object(report.time, "sleep") as slept:
            self.assertEqual(report.check_ewelink_hardware(ctx)["Relay"], "switches (through eWeLink)")
        self.assertEqual(slept.call_count, 2)

    def test_a_device_never_back_in_ewelink_is_not_switched_and_the_wait_ends(self):
        ctx = self.ewelink_ctx([False])
        clock = iter(range(0, 10_000, 30))
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "check_relay") as switched, \
                mock.patch.object(report.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(report.time, "sleep"):
            self.assertEqual(report.check_ewelink_hardware(ctx)["Relay"], "could not be checked")
        switched.assert_not_called()

    def test_ewelink_out_of_reach_is_not_waited_for(self):
        ctx = self.ewelink_ctx([False])
        ctx.account_devices = lambda: None
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "check_relay") as switched, \
                mock.patch.object(report.time, "sleep") as slept:
            self.assertEqual(report.check_ewelink_hardware(ctx)["Relay"], "could not be checked")
        switched.assert_not_called()
        slept.assert_not_called()

    def test_a_dualr3_driving_a_motor_has_no_relay_to_switch(self):
        # Set up for a curtain motor (workMode 2), a DualR3 reports no relay and
        # refuses every switch: seen in the emulator, it was waited for three
        # minutes and the report said "Checks passed: False".
        ctx = self.ewelink_ctx([True])
        ctx.account_devices = lambda: [{"deviceid": "1000abcdef", "online": True,
                                        "params": {"workMode": 2, "motorTurn": 0}}]
        clock = iter(range(0, 10_000, 30))
        with mock.patch.object(records, "read_device", return_value={"ewelink_id": "1000abcdef"}), \
                mock.patch.object(report.ewelink_relay, "answers", return_value=True), \
                mock.patch.object(report.ewelink_relay, "check_relay") as switched, \
                mock.patch.object(report.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(report.time, "sleep") as slept:
            hardware = report.check_ewelink_hardware(ctx)
        self.assertEqual(hardware["Relay"], "motor mode")
        self.assertEqual(report.checks_passed(hardware), "not run")
        switched.assert_not_called()
        slept.assert_not_called()

    def test_a_report_gives_only_what_its_route_has(self):
        # The Wi-Fi typed is an Espressif route's; a device's own network
        # joined, either family's.
        fields = dict(report.report_fields(self.ctx()))
        self.assertIn("Wi-Fi typed", fields)
        self.assertIn("Setup network", fields)
        s61 = self.ctx(device_kind="sonoff-s61", product_model="S61STPF", firmware="openbeken", source="sonoff")
        fields = dict(report.report_fields(s61))
        self.assertNotIn("Wi-Fi typed", fields)
        self.assertIn("Setup network", fields)
        self.assertEqual(fields["Model"], "S61STPF")

    def test_the_report_shows_retries_but_not_each_stages_time_or_how_long_the_device_was_busy(self):
        # The report sent carries those (report_data.stages).
        ctx = self.ctx()
        for key, value in {"E2.seconds": 36, "E2.device_busy_seconds": 34.2, "E2.attempts": 2,
                           "G1.seconds": 43, "G1.device_busy_seconds": 61, "G1.attempts": 1,
                           "E1.signal_dbm": -61}.items():
            ctx.state.capture(key, value)
        fields = dict(report.report_fields(ctx))
        number = {step: n for n, (step, _, _) in enumerate(routes.STEPS, 1)}
        self.assertNotIn("Stage times", fields)
        self.assertNotIn("Busy", fields)
        self.assertEqual(fields["Retries"], f"{number['E2']} (1 retry)")
        self.assertEqual(fields["Signal"], "-61 dBm")

    def test_on_the_way_to_esphome_the_signal_is_from_its_install(self):
        ctx = self.ctx(firmware="esphome")
        ctx.state.capture("I1.signal_dbm", -58)
        self.assertEqual(dict(report.report_fields(ctx))["Signal"], "-58 dBm")

    def test_from_tasmota_to_esphome_the_signal_is_from_leaving_tasmota(self):
        ctx = self.ctx(firmware="esphome", source="tasmota")
        ctx.state.capture("L1.signal_dbm", -63)
        self.assertEqual(dict(report.report_fields(ctx))["Signal"], "-63 dBm")

    def test_with_several_readings_the_report_gives_the_lowest(self):
        ctx = self.ctx()
        for key, value in {"A2.signal_dbm": -48, "D4.signal_dbm": -71, "E1.signal_dbm": -55,
                           "I1.signal_dbm": None}.items():
            ctx.state.capture(key, value)
        self.assertEqual(dict(report.report_fields(ctx))["Signal"], "-71 dBm")

    def test_a_run_with_no_retries_says_so(self):
        fields = dict(report.report_fields(self.ctx()))
        self.assertEqual(fields["Retries"], "None")

    def test_the_result_says_where_and_why_a_run_stopped(self):
        from tools.autoflash.state import DONE
        # Values other tests made private ("wrong", as a password) are not this run's.
        patcher = mock.patch.dict(output._private, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = self.ctx()
        ctx.last_failure = ""
        for step, _, _ in routes.STEPS[:9]:
            ctx.state.mark(step, DONE)
        step = routes.STEPS[9][0]
        where = f"stage 10 of {len(routes.STEPS)} ({routes.stage_name(step)})"
        ctx.state.mark(step, "failed", error="GET /chunk/safeboot/20 from 192.0.2.50 "
                                             "(58:cf:79:01:02:03) returned HTTP -1 after 8 attempts")
        self.assertEqual(report.run_result(ctx, "failed"),
                         f"stopped at {where}: GET /chunk/safeboot/20 from <address> (<mac>) "
                         "returned HTTP -1 after 8 attempts")
        ctx.last_failure = "something unexpected went wrong: KeyError: 'x'"
        self.assertEqual(report.run_result(ctx, "crashed"),
                         f"crashed at {where}: something unexpected went wrong: KeyError: 'x'")
        self.assertEqual(report.run_result(ctx, "converted"), "converted")

    def test_the_result_names_the_stage_that_failed_not_one_it_sent_back(self):
        # A refused commit sends E1 and E2 back to be done again: the run still
        # stopped at the commit.
        from tools.autoflash.state import DONE
        patcher = mock.patch.dict(output._private, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ctx = self.ctx()
        for step, _, _ in routes.STEPS:
            if step == "E3":
                break
            ctx.state.mark(step, DONE)
        for step in ("E1", "E2", "E3"):
            ctx.state.mark(step, "failed")
        ctx.last_failure, ctx.last_failed_step = "commit refused", "E3"
        number = [step for step, _, _ in routes.STEPS].index("E3") + 1
        self.assertEqual(report.run_result(ctx, "failed"),
                         f"stopped at stage {number} of {len(routes.STEPS)} "
                         f"({routes.stage_name('E3')}): commit refused")

    def test_a_run_stopped_before_its_firmware_is_chosen_gives_no_total(self):
        # Until A3 the route is only the shared start: its length is no total.
        from tools.autoflash.state import DONE
        routes.apply_target(None, "sonoff")
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        ctx = self.ctx()
        ctx.last_failure = ("an unattended run needs to be told which firmware: "
                            "--firmware tasmota or --firmware esphome")
        for step in ("A0", "A1"):
            ctx.state.mark(step, DONE)
        self.assertEqual(report.run_result(ctx, "failed"),
                         f"stopped at stage 3 ({routes.stage_name('A3')}): {ctx.last_failure}")

    def test_only_a_run_that_failed_offers_its_report_not_one_the_operator_stopped(self):
        self.assertEqual(report.report_ending(1, None), "failed")       # a stage failed
        self.assertEqual(report.report_ending(1, "crashed"), "crashed")  # something unexpected
        self.assertIsNone(report.report_ending(130, None))               # quit at a question
        self.assertIsNone(report.report_ending(None, "cancelled"))       # Ctrl-C
        self.assertIsNone(report.report_ending(0, None))                 # converted: offered already
        self.assertIsNone(report.report_ending(None, None))              # the terminal was closed

    def test_a_long_reason_is_cut_short_on_one_line(self):
        text = report.public_text("first line\n" + "x" * 500)
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), 240)
        self.assertTrue(text.endswith("..."))

    def test_python_says_where_it_came_from(self):
        portable = Path(self.tmp.name) / ".python"
        with mock.patch.dict(os.environ, {"AUTOFLASH_PYTHON_HOME": str(portable)}):
            with mock.patch.object(sys, "executable", str(portable / "linux-x86_64/python/bin/python3")):
                self.assertIn("(portable)", report.python_description())
            with mock.patch.object(sys, "executable", "/usr/bin/python3"), \
                 mock.patch.object(sys, "prefix", "/usr"), mock.patch.object(sys, "base_prefix", "/usr"):
                self.assertIn("(system)", report.python_description())
            with mock.patch.object(sys, "executable", "/home/me/venv/bin/python3"), \
                 mock.patch.object(sys, "prefix", "/home/me/venv"), \
                 mock.patch.object(sys, "base_prefix", "/usr"):
                self.assertIn("(venv)", report.python_description())

    def test_a_run_that_stopped_offers_its_report_without_the_hat(self):
        ctx = self.ctx()
        ctx.last_failure, ctx.run_resumed, ctx.reported = "timed out", False, False
        ctx.run_started = time.monotonic() - 60
        with mock.patch.object(output, "tell") as told, mock.patch.object(output, "show") as shown, \
             mock.patch.object(output, "show_link"), mock.patch.object(output, "show_links") as links:
            report.invite_stopped_report(ctx, "failed")
        self.assertTrue(ctx.reported)
        self.assertTrue(any("please open an issue" in str(call) for call in told.call_args_list))
        links.assert_not_called()  # no donation links after a failure
        self.assertFalse(any("hold out my hat" in str(call) for call in told.call_args_list + shown.call_args_list))

    def test_the_report_says_how_the_backup_went_but_not_where_it_is(self):
        mac_path = "captures/devices/58-cf-79-01-02-03/sonoff-backup"
        for outcome, said in (("saved", "Completed"), ("failed", "Failed"),
                              ("existing", "Earlier run's")):
            with self.subTest(outcome=outcome):
                ctx = self.ctx(backup_outcome=outcome, stock_backup=mac_path,
                               device_mac="58:cf:79:01:02:03")
                fields = dict(report.report_fields(ctx))
                self.assertEqual(fields["Backup"], said)
                shown = "\n".join(f"{label}: {value}" for label, value in fields.items())
                self.assertNotIn("58", shown.split("Backup")[1].splitlines()[0])
                self.assertNotIn("sonoff-backup", shown)
        # A run from before the backup existed.
        self.assertEqual(dict(report.report_fields(self.ctx(backup_outcome="")))["Backup"],
                         "Not taken")

    def test_a_run_from_a_converted_device_reports_its_backup_from_before(self):
        # Seen: "Not taken" in the report of a run whose log said the device
        # was backed up already - a run from Tasmota never reaches C3.
        from unittest import mock
        ctx = self.ctx(source="tasmota", device_mac="12:34:56:ab:cd:ef")
        with mock.patch.object(records, "find_backup", return_value=object()):
            self.assertEqual(dict(report.report_fields(ctx))["Backup"], "Earlier run's")
        with mock.patch.object(records, "find_backup", return_value=None):
            self.assertEqual(dict(report.report_fields(ctx))["Backup"], "Not taken")

    def test_hardware_that_was_not_checked_says_so(self):
        fields = dict(report.report_fields(self.ctx()))
        self.assertEqual(fields["Checks"], "Not run")
        self.assertNotIn("Relay", fields)
        self.assertNotIn("Power meter", fields)
        self.assertNotIn("System prompts", fields)
        self.assertNotIn("__", fields.values())

    def test_every_answer_starts_with_a_capital_and_only_that_changes(self):
        fields = dict(report.report_fields(self.ctx(), {"Relay": "switches (meter confirms)"}))
        self.assertEqual(fields["Checks"], "Pass")
        self.assertEqual(fields["Model"], "S60TPG")

    def test_the_checks_pass_when_the_relay_switches_whatever_the_meter_says(self):
        # What each check found goes to the log; the report gives one answer,
        # the same for any kind of device.
        for relay, passed in (("switches (meter confirms)", "pass"), ("switches (meter unchanged)", "pass"),
                              ("switches (through eWeLink)", "pass"), ("did not switch", "fail"),
                              ("could not be checked", "fail"), ("not checked", "not run")):
            self.assertEqual(report.checks_passed({"Relay": relay, "Power meter": "on: 0 V, 0 W"}), passed, relay)
        self.assertEqual(report.checks_passed({}), "not run")

    def test_the_device_page_is_the_device_or_the_emulators_forward(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTOFLASH_EMULATOR_URL", None)
            self.assertEqual(report.device_page_url("192.0.2.50"), "http://192.0.2.50")   # no trailing slash
            self.assertIsNone(report.device_page_url(None))
        with mock.patch.dict(os.environ, {"AUTOFLASH_EMULATOR_URL": "http://localhost:8080"}):
            self.assertEqual(report.device_page_url("192.168.60.50"), "http://localhost:8080")

    def test_the_report_holds_nothing_private(self):
        shown = "\n".join(report.report_screen(report.report_fields(self.ctx()), columns=100))
        for private in ("me@example.com", "192.0.2.50", "tasmota-1A2B3C-4567", "1001234abc"):
            self.assertNotIn(private, shown)

    def test_fallbacks_and_unknown_models_are_reported_as_such(self):
        ctx = self.ctx(product_model="", wifi_typed=True)
        ctx.state.set_input(join.OWN_NETWORK, [join.NOT_JOINED])
        fields = dict(report.report_fields(ctx))
        self.assertEqual(fields["Model"], "__")
        self.assertEqual(fields["Wi-Fi typed"], "Yes")
        self.assertEqual(fields["Setup network"], "Yes")

    def test_a_converted_run_shows_its_report_and_asks_nothing_of_anyone(self):
        def body():
            with mock.patch.object(report, "check_hardware", return_value={"Relay": "switches (meter confirms)"}):
                report.invite_report(self.ctx(), 441, False)
        screen, _ = self.run_with(io.StringIO(), False, body)
        self.assertNotIn("How it went", screen, "the report under what the run said, with no heading")
        self.assertNotIn("Result:", screen, "the line above it says how it ended")
        self.assertIn("Liberator:", screen)
        self.assertNotIn(report.ISSUES_URL, screen, "a run that worked needs no issue")
        self.assertNotIn("This run's log:", screen)
        self.assertNotIn("discussions", screen)
        self.assertIn("Throw your friendly neighbourhood dev a biscuit:", screen)
        self.assertGreater(screen.index(report.DONATE_URL), screen.index("Model:"))  # after the report
        # GitHub Sponsors first, and the URLs lined up under one another.
        self.assertIn(f"     GitHub: {report.SPONSORS_URL}\n"
                      f"     Ko-fi:  {report.KOFI_URL}\n"
                      f"     PayPal: {report.DONATE_URL}\n", screen)
        self.assertNotIn("**", screen)                       # the terminal shows no Markdown
        self.assertRegex(screen, r"Model: +S60TPG")



class LinkTests(OutputTestCase):
    URL = "https://github.com/adamsthws/firmware-liberator/discussions/1"

    def test_a_terminal_gets_a_clickable_link_and_the_log_the_plain_url(self):
        def body():
            with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}), \
                 mock.patch.object(output, "IS_WINDOWS", False):
                os.environ.pop("NO_COLOR", None)
                output.show_link(self.URL)
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"\x1b]8;;{self.URL}\x1b\\{self.URL}\x1b]8;;\x1b\\", screen)
        self.assertIn(f"  {self.URL}\n", log)
        self.assertNotIn("\x1b", log)

    def test_without_colour_or_on_the_classic_windows_console_it_is_plain(self):
        screen, _ = self.run_with(FakeTerminal(), False, lambda: output.show_link(self.URL))  # NO_COLOR set
        self.assertEqual(screen.strip(), self.URL)
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WT_SESSION", None)
            text = output.hyperlink(self.URL)
        self.assertNotIn("\x1b]8;;", text)                    # coloured, not a link
        self.assertEqual(output.ANSI_RE.sub("", text), self.URL)

    def test_a_link_with_no_title_is_purple_itself_as_the_titled_ones_titles_are(self):
        def body():
            with self.links_on():
                output.show_link(self.URL)
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"{output.PURPLE}\x1b]8;;{self.URL}", screen)
        self.assertEqual(output.ANSI_RE.sub("", screen).strip(), self.URL)
        self.assertIn(f"    {self.URL}\n", log)

    def links_on(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {"TERM": "xterm-256color"}))
        stack.enter_context(mock.patch.object(output, "IS_WINDOWS", False))
        stack.enter_context(mock.patch.object(output.platform, "system", return_value="Linux"))
        os.environ.pop("NO_COLOR", None)
        os.environ.pop("TERM_PROGRAM", None)
        return stack

    def test_a_titled_link_shows_the_title_and_the_url_both(self):
        def body():
            with self.links_on():
                output.show_link(self.URL, "Test reports")
        screen, log = self.run_with(FakeTerminal(), False, body)
        self.assertIn(f"\x1b]8;;{self.URL}\x1b\\{self.URL}\x1b]8;;\x1b\\", screen)  # only the URL is the link
        self.assertLess(screen.index(f"{output.PURPLE}Test reports:"), screen.index("\x1b]8;;"))
        self.assertEqual(output.ANSI_RE.sub("", screen).strip(), f"Test reports: {self.URL}")  # no hint
        self.assertIn(f"  Test reports: {self.URL}\n", log)

    def test_a_group_of_links_has_the_hint_once_underneath(self):
        def body():
            with self.links_on():
                output.show_links([(self.URL, "Ko-fi"), (self.URL, "PayPal")], hint=True)
        screen, log = self.run_with(FakeTerminal(), False, body)
        lines = output.ANSI_RE.sub("", screen).strip().splitlines()
        self.assertEqual([line.split(":")[0].strip() for line in lines], ["Ko-fi", "PayPal", "(Ctrl+click to open)"])
        self.assertNotIn("click", log)

    def test_a_titled_link_is_plain_text_where_links_do_not_work(self):
        screen, _ = self.run_with(FakeTerminal(), False, lambda: output.show_link(self.URL, "Ko-fi"))  # NO_COLOR set
        self.assertEqual(screen.strip(), f"Ko-fi: {self.URL}")  # no hint: clicking may not work
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", False), \
             mock.patch.dict(os.environ, {"TERM_PROGRAM": "Apple_Terminal"}):
            text = output.hyperlink(self.URL, "Ko-fi")
        self.assertNotIn("\x1b]8;;", text)
        self.assertEqual(output.ANSI_RE.sub("", text), f"Ko-fi: {self.URL}")

    def test_with_only_basic_colours_the_title_is_magenta_not_rounded_to_blue(self):
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", False), \
             mock.patch.dict(os.environ, {"TERM": "screen"}):
            os.environ.pop("COLORTERM", None)
            self.assertIn(f"{output.MAGENTA}Ko-fi:", output.hyperlink(self.URL, "Ko-fi"))
            os.environ["TERM"] = "tmux-256color"
            self.assertIn(f"{output.PURPLE}Ko-fi:", output.hyperlink(self.URL, "Ko-fi"))

    def test_the_title_is_purple_on_the_classic_windows_console_too(self):
        with mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch.object(output, "IS_WINDOWS", True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WT_SESSION", None)
            text = output.hyperlink(self.URL, "Ko-fi")
        self.assertIn(f"{output.PURPLE}Ko-fi:", text)
        self.assertNotIn("\x1b]8;;", text)                    # coloured, not a link
        self.assertEqual(output.ANSI_RE.sub("", text), f"Ko-fi: {self.URL}")


def rendered(text):
    """The rows a terminal shows once `text` is written to it: carriage
    returns, erasing a row or its end, and moving the cursor up or down
    (A, B; F, E to the row's start) applied."""
    import re
    rows, row, col = [""], 0, 0
    for token in re.findall(r"\x1b\[2K|\x1b\[K|\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\|[\s\S]", text):
        move = re.fullmatch(r"\x1b\[(\d*)([ABEF])", token)
        if move:
            count = int(move.group(1) or 1)
            row = max(0, row - count) if move.group(2) in "AF" else min(len(rows) - 1, row + count)
            col = 0 if move.group(2) in "EF" else col
        elif token == "\x1b[2K":
            rows[row] = ""
        elif token == "\x1b[K":
            rows[row] = rows[row][:col]
        elif token.startswith("\x1b"):
            continue
        elif token == "\r":
            col = 0
        elif token == "\n":
            row, col = row + 1, 0
            if row == len(rows):
                rows.append("")
        else:
            line = rows[row].ljust(col)
            rows[row] = line[:col] + token + line[col + 1:]
            col += 1
    rows = [" ".join(line.split()) for line in rows]  # columns padded or not
    while rows and not rows[-1]:
        rows.pop()
    return rows


class StartAtTopTests(OutputTestCase):
    def test_what_the_window_showed_is_scrolled_away_not_cleared(self):
        screen = FakeTerminal()
        with mock.patch("sys.stdout", screen), mock.patch.object(output, "colour_ok", return_value=True), \
                mock.patch.dict(os.environ, {"TERM_PROGRAM": "gnome-terminal"}), \
                mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 30))):
            output.start_at_top()
        # A newline a row scrolls it all into the scrollback; then the top,
        # a blank row left there.
        self.assertEqual(screen.getvalue(), "\n" * 30 + "\x1b[H\n")
        self.assertNotIn("\x1b[2J", screen.getvalue())

    def test_in_vscodes_terminal_the_run_starts_under_its_command(self):
        # Seen: VS Code pinned the command, once scrolled away, over the
        # window's top rows - the run's first two were under it.
        screen = FakeTerminal()
        with mock.patch("sys.stdout", screen), mock.patch.object(output, "colour_ok", return_value=True), \
                mock.patch.dict(os.environ, {"TERM_PROGRAM": "vscode"}):
            output.start_at_top()
        self.assertEqual(screen.getvalue(), "\n")   # the blank row alone

    def test_a_resumed_run_has_one_blank_row_at_the_top_as_a_new_one_does(self):
        # Seen: the question of which run to continue, asked before the run
        # starts, sat on the window's top row; a new run had a blank one.
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 30))):
                output.start_at_top()     # its blank row: the test above
                output.transient_begin(before_start=True)
                with mock.patch("builtins.input", side_effect=typed):
                    prompts.ask_choice("What now", ["Continue the device"])
                output.transient_end()
                output.tell("")         # the run's own, before its first words
                output.show("Converting a device usually takes under 10 minutes.")
        def typed(prompt):
            screen.write(prompt + "1\n")   # as the terminal shows it: the question, the answer
            return "1"
        screen = FakeTerminal()
        with mock.patch("sys.stdout", screen), mock.patch("sys.stdin", io.StringIO()):
            body()
        self.assertEqual(rendered(screen.getvalue())[30:],
                         ["", "Converting a device usually takes under 10 minutes."])   # one, not two

    def test_nothing_where_the_cursor_cannot_move_or_there_is_no_terminal(self):
        for stdout in (FakeTerminal(), io.StringIO()):   # NO_COLOR is set; not a terminal
            with mock.patch("sys.stdout", stdout):
                output.start_at_top()
            self.assertEqual(stdout.getvalue(), "")


class BlankRowTests(OutputTestCase):
    def test_never_two_blank_rows_in_a_row(self):
        # Seen: "Looking for supported devices..." taken off the screen left
        # the blank row above it, and the firmware's heading added its own.
        def body():
            with mock.patch.object(output, "colour_ok", return_value=True), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 30))):
                output.show("Converting a device usually takes under 10 minutes.")
                output.tell("")
                output.transient_begin()
                output.show("Looking for supported devices on your network...")
                output.transient_end()
                prompts.section("Choose the device's new firmware", ["1. ESPHome"])
        text, _ = self.run_with(FakeTerminal(), False, body)
        self.assertEqual(rendered(text), ["Converting a device usually takes under 10 minutes.", "",
                                          "-- Choose the device's new firmware", "1. ESPHome"])


class WorkingTests(OutputTestCase):
    """A wait outside any stage shows what it is, with the stages' spinner,
    and goes again once it is over."""

    def test_a_run_never_finished_leaves_no_spinner_for_the_next(self):
        # Seen in the tests: a run begun on a terminal and never finished
        # left its ticker, and the next run, on no terminal, drew a wait's
        # spinner as if on one.
        with mock.patch("sys.stdout", FakeTerminal()):
            output.start(self.log, verbose=False)   # its ticker started; no finish()
        self.test_nothing_is_drawn_where_the_screen_is_not_a_terminal()

    def test_the_report_being_put_together_is_shown_then_taken_away(self):
        def body():
            with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
                output.show("Conversion complete!")
                with output.working("Generating your report..."):
                    output.say("Checking the relay through eWeLink...")   # logged only
                    output._tick()
                    seen.append(rendered(screen.getvalue()))
                output.tell("")
                output.show("Please share how it went!")

        seen = []
        screen = FakeTerminal()
        with mock.patch.object(output, "_tick_forever"):   # drawn here, not by the ticker
            text, log = self.run_with(screen, False, body)
        self.assertEqual(seen[0][-2:], ["", "Generating your report... |"])   # a spinner, a blank row above
        self.assertEqual(rendered(text), ["Conversion complete!", "", "Please share how it went!"])
        self.assertNotIn("Generating", log)

    def test_a_run_ended_just_after_its_clock_was_drawn_leaves_no_gap_above_the_next_ones_first_line(self):
        def ended_after_a_wait():
            output._s.waited = True   # as the stage clock drawn under its last note leaves it

        self.run_with(FakeTerminal(), False, ended_after_a_wait)
        text, _ = self.run_with(FakeTerminal(), False, lambda: output.show("Conversion complete!"))
        self.assertEqual(rendered(text), ["Conversion complete!"])

    def test_nothing_is_drawn_where_the_screen_is_not_a_terminal(self):
        def body():
            with output.working("Generating your report..."):
                pass
            output.tell("")
            output.show("Please share how it went!")
        text, _ = self.run_with(io.StringIO(), False, body)
        self.assertEqual(text, "\n Please share how it went!\n")


    def test_in_verbose_mode_or_within_a_stage_it_is_a_line_as_any_other(self):
        def body():
            with output.working("Reading the device's memory - about a minute..."):
                pass
        text, log = self.run_with(FakeTerminal(), True, body)
        self.assertIn("Reading the device's memory - about a minute...", rendered(text))
        self.assertIn("Reading the device's memory", log)


@unittest.skipUnless(hasattr(os, "fork"), "a pseudo-terminal needs Linux or macOS")
class StrayKeyTests(unittest.TestCase):
    """Keys pressed by accident while nothing is asked - letters, the arrows,
    Enter, Backspace - on a real terminal: never echoed, so neither the
    cursor nor a stage line moves (_hide_keys), and dropped before the next
    question, which takes only what is typed at it (prompts.discard_typed_ahead)."""

    STRAY = "zq\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1bOP\t\x1b[5~\x7f\r\n x\r"

    def test_keys_pressed_while_nothing_is_asked_leave_no_mark_and_answer_nothing(self):
        import codecs
        import json
        import textwrap
        from tools.repo import unattended_run
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            answer = Path(directory) / "answer.json"
            script = textwrap.dedent(f"""
                import json, sys, time
                from pathlib import Path
                sys.path.insert(0, {str(root)!r})
                from tools.autoflash import output, prompts
                output.start(Path({str(Path(directory) / "run.log")!r}), verbose=False)
                output.set_stage_width(30, 15)
                output.stage_begin(1, "Installing interim firmware")
                output.tell("    A note under the stage: WHILE-A-NOTE")
                time.sleep(1.5)
                output.stage_end("complete")
                output.tell("")
                with output.working("Generating your report: WHILE-WORKING"):
                    time.sleep(1.5)
                output.tell("Between stages: BETWEEN")
                time.sleep(1.5)
                output.stage_begin(2, "Choosing the firmware")
                chosen = prompts.ask_choice("Which firmware would you like", ["Tasmota", "ESPHome"])
                output.stage_end("complete")
                open({str(answer)!r}, "w").write(json.dumps(chosen))
                output.finish()
            """)
            answers = [unattended_run.Answer(when, self.STRAY, anywhere=True)
                       for when in ("WHILE-A-NOTE", "WHILE-WORKING", "BETWEEN")]
            answers.append(unattended_run.Answer(r"would you like.*\(q to quit\)", "2\r"))
            code, written = unattended_run.record([sys.executable, "-c", script], (80, 30), live=False,
                                                  timeout=60, answers=answers)
            chosen = json.loads(answer.read_text(encoding="utf-8")) if answer.exists() else None
        self.assertEqual([bool(item.typed_at) for item in answers], [True] * 4, "every stray key typed")
        self.assertEqual((code, chosen), (0, 1))   # what was typed at the question, not the Enters before it
        screen = unattended_run.Screen(80, 30)
        screen.feed(codecs.decode(written, "utf-8", errors="replace"))
        rows = [" ".join(row.split()) for row in screen.render(colour=False).splitlines() if row.strip()]
        self.assertEqual(rows, ["Stage 1 - Installing interim firmware - Complete", "Between stages: BETWEEN",
                                "Stage 2 - Choosing the firmware - Complete"])
        self.assertEqual(screen.wrapped, 0)


class StageGapTests(OutputTestCase):
    """Whatever is shown under a stage's line - a message, a question, a
    helper's output - has one blank line between it and the stage line."""

    def screen(self, body, verbose=False, colour=False):
        def wrapped():
            with mock.patch.object(output, "colour_ok", return_value=colour), \
                 mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
                body()
        screen, _ = self.run_with(FakeTerminal(), verbose, wrapped)
        return rendered(screen)

    def test_a_message_under_the_stage_line_has_a_blank_line_above_it(self):
        def body():
            output.stage_begin(3, "Discovering the device")
            output.show("Looking for it on your network...")
            output.stage_end("complete")
        rows = self.screen(body)
        self.assertTrue(rows[0].startswith("Stage 3 - Discovering the device - Underway"))
        self.assertEqual(rows[1:3], ["", "Looking for it on your network..."])
        self.assertTrue(rows[3].startswith("Stage 3 - Discovering the device - Complete"))

    def test_a_section_s_own_blank_line_is_the_gap(self):
        def body():
            output.stage_begin(4, "Querying Sonoff firmware")
            prompts.section("What the device reported", ["Firmware now 1.2.0"])
        rows = self.screen(body)
        self.assertEqual(rows[1:3], ["", "-- What the device reported"])

    def test_a_question_has_the_gap_too(self):
        def body():
            output.stage_begin(3, "Discovering the device")
            output.before_input("eWeLink account email: ")
            sys.stdout.write("eWeLink account email: someone@example.com\n")   # as input() echoes it
            output.answered("someone@example.com")
        rows = self.screen(body)
        self.assertEqual(rows[1], "")
        self.assertEqual(rows[2], "eWeLink account email: someone@example.com")

    def test_the_gap_leaves_the_screen_with_an_answered_question(self):
        def body():
            output.stage_begin(3, "Discovering the device")
            output.transient_begin()
            prompts.warning_box("Before going on", ["One line."])
            output.before_input("Type PROCEED to go ahead: ")
            sys.stdout.write("Type PROCEED to go ahead: proceed\n")
            output.answered("proceed")
            output.transient_end()
            output.show("Sending the bridge...")   # the stage still under way
        rows = self.screen(body, colour=True)
        self.assertTrue(rows[0].startswith("Stage 3 - Discovering the device - Underway"))
        self.assertEqual(rows[1:3], ["", "Sending the bridge..."])
        self.assertNotIn("Before going on", "\n".join(rows))

    def test_stage_lines_follow_one_another_without_a_gap(self):
        def body():
            for number in (1, 2):
                output.stage_begin(number, f"Stage number {number}")
                output.stage_end("already done")
        rows = self.screen(body)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith("Stage 2"))

    def test_a_failure_under_a_stopped_stage_has_the_gap(self):
        def body():
            output.stage_begin(5, "Installing bridge firmware")
            output.stage_end("stopped")
            output.show("FAILED: bridge delivery failed")
        rows = self.screen(body)
        self.assertTrue(rows[0].endswith("Stopped"))
        self.assertEqual(rows[1:], ["", "FAILED: bridge delivery failed"])

    def test_verbose_output_under_a_stage_heading_has_the_gap(self):
        def body():
            output.stage_begin(4, "Querying Sonoff firmware")
            output.detail("$ python tools/ewelink/query_ota.py")
        rows = self.screen(body, verbose=True)
        heading = rows.index("== Stage 4 - Querying Sonoff firmware")
        self.assertEqual(rows[heading + 1:heading + 3], ["", "$ python tools/ewelink/query_ota.py"])

    def test_piped_output_gets_the_gap_too(self):
        def body():
            output.stage_begin(3, "Discovering the device")
            output.show("Found it.")
        with mock.patch("sys.stdout", io.StringIO()) as piped:
            output.start(self.log, verbose=False)
            body()
            output.finish()
        rows = piped.getvalue().splitlines()
        self.assertEqual(rows[1:3], ["", " Found it."])



class MarginTests(OutputTestCase):
    """Every row starts one space in, and nothing is wider than the terminal."""

    def narrow(self, body, columns=40):
        def run():
            with mock.patch.dict(os.environ, {"COLUMNS": str(columns), "LINES": "24"}):
                body()
        screen, _ = self.run_with(FakeTerminal(), True, run)
        return [row for row in output.ANSI_RE.sub("", screen).split("\n") if row.strip()]

    def test_a_long_message_wraps_between_words_inside_the_margin(self):
        text = "Fix the cause, then carry on with the same command again, and it picks up where it stopped."
        rows = self.narrow(lambda: output.show(text))
        self.assertGreater(len(rows), 1)
        for row in rows:
            self.assertTrue(row.startswith(" ") and not row.startswith("  "), row)
            self.assertLessEqual(len(row), 39)
        self.assertEqual(" ".join(row.strip() for row in rows), text)

    def test_lined_up_labels_carry_on_under_their_value(self):
        rows = self.narrow(lambda: output.show("    Relay:      switches, seen on the meter with a lamp plugged in"))
        self.assertEqual(rows[0].index("switches"), rows[1].index(rows[1].strip()))

    def test_a_url_is_never_broken(self):
        url = "https://github.com/adamsthws/firmware-liberator/discussions/1"
        rows = self.narrow(lambda: output.show_link(url, "Test reports"))
        self.assertIn(url, [row.strip() for row in rows])

    def test_a_long_question_wraps_and_leaves_its_last_row_for_the_answer(self):
        shown = []
        rows = self.narrow(lambda: shown.append(output.question(
            "Type YES to go ahead - anything else stops, changing nothing", ": ")))
        self.assertTrue(shown[0].endswith(": "))
        self.assertTrue(shown[0].startswith(" "))
        self.assertTrue(all(len(row) <= 39 for row in rows + shown))

    def test_piped_output_keeps_the_margin_but_is_not_wrapped(self):
        text = "word " * 40
        screen, _ = self.run_with(io.StringIO(), True, lambda: output.show(text.strip()))
        self.assertEqual(screen.strip("\n"), " " + text.strip())


class EventTests(OutputTestCase):
    """What a run did, beside its log (output.EVENTS), for the fault suites to
    follow it by in place of its words."""

    def events(self):
        return [{k: v for k, v in e.items() if k != "at"} for e in output.events(self.log.parent)]

    def test_a_run_writes_its_events_beside_its_log(self):
        def body():
            output.event("began", step="B2")
            output.say("B2 done")
            output.event("done", step="B2")
        self.run_with(io.StringIO(), True, body)
        from tools.autoflash import __version__
        self.assertEqual(self.events(), [{"event": "run", "version": __version__}, {"event": "began", "step": "B2"},
                                         {"event": "done", "step": "B2"}, {"event": "ended"}])
        self.assertTrue(all(isinstance(e["at"], float) for e in output.events(self.log.parent)))

    def test_no_events_outside_a_run(self):
        output.event("began", step="B2")
        self.assertIsNone(output.events_path())
        self.assertFalse((self.log.parent / output.EVENTS).exists())

    def test_advice_to_switch_the_device_off_and_on_is_an_event_however_wrapped(self):
        def body():
            output.tell("Nothing to see here.")
            output.tell("FAILED: the device did not answer. Switch the device off and on, then run this again")
            for line in ("It has not come back on its new firmware yet. Switch it off for a few",
                         "seconds at the socket, then on: it then starts the new firmware."):
                output.tell(f"    {line}")
            with output.asking_to_switch_now():
                output.tell("    No answer from the device: switch it off and on - it is safe now.")
            with output.asking_to_switch_now(key=True):
                output.tell("    Switch it off for five seconds, then on (a wall switch at its breaker).")
            output.tell("Switch the device off and on once more.")
        self.run_with(io.StringIO(), True, body)
        self.assertEqual([e for e in self.events() if e["event"] == "switch"],
                         [{"event": "switch"}, {"event": "switch"}, {"event": "switch", "now": True},
                          {"event": "switch", "now": True, "key": True}, {"event": "switch"}])

    def test_a_cut_line_or_another_kind_of_file_is_passed_over(self):
        (self.log.parent / output.EVENTS).write_text('{"event": "began", "step": "C2"}\n[1]\n{"event": "do',
                                                    encoding="utf-8")
        self.assertEqual(self.events(), [{"event": "began", "step": "C2"}])
        self.assertEqual(output.events(self.log.parent / "nowhere"), [])

    def test_every_word_of_the_tool_to_switch_the_device_off_and_on_is_matched(self):
        """Each string in the tool asking the person to switch the device off,
        with the line after it where it is wrapped by hand, matches
        SWITCH_OFF_AND_ON - reworded otherwise, the fault suites would no longer
        see the advice and switch it for them."""
        import ast
        asked = re.compile(r"\bswitch (the device|it) off\b", re.IGNORECASE)
        # Not advice to switch it off and on: off and left off, and phases.py's
        # pattern for a helper's lines worth showing.
        not_advice = ("switch the device off first", "|switch it off|")
        for path in sorted((Path(__file__).parents[2] / "tools/autoflash").rglob("*.py")):
            texts = []
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                for value in getattr(node, "elts", None) or [node]:
                    if isinstance(value, ast.JoinedStr):
                        texts.append("".join(v.value if isinstance(v, ast.Constant) else "{}" for v in value.values))
                    elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                        texts.append(value.value)
            for at, text in enumerate(texts):
                if asked.search(text) and not any(words in text for words in not_advice):
                    with self.subTest(path=path.name, text=text):
                        wrapped = " ".join(f"{text} {texts[at + 1] if at + 1 < len(texts) else ''}".split())
                        self.assertRegex(wrapped, output.SWITCH_OFF_AND_ON)


class ToolSaidTests(unittest.TestCase):
    """What a helper tool said decides the advice: a part-served download is
    the device's Wi-Fi going, not the firewall."""

    def test_it_finds_text_in_the_tools_kept_lines(self):
        from tools.autoflash import output
        output._s.tool_tail.clear()
        output._s.tool_tail.extend(["  Served /user1.bin bytes 0-23 (24 bytes)",
                                    "  WARNING: the device did not request every firmware byte."])
        self.assertTrue(output.tool_said("Served /user"))
        self.assertFalse(output.tool_said("Upgrade command sent"))
        output._s.tool_tail.clear()


class PrivacyTestCase(unittest.TestCase):
    def setUp(self):
        saved = dict(output._private)
        output._private.clear()
        self.addCleanup(lambda: (output._private.clear(), output._private.update(saved)))


class ScrubTests(PrivacyTestCase):
    def test_what_identifies_someone_is_replaced(self):
        cases = {
            "--email someone@gmail.com --listen-ip 192.0.2.110": "--email <email> --listen-ip 192.0.2.110",
            '"GET /user1.bin?deviceid=1001234abc&ts=4708736&sign=fafd2f9d3bcc HTTP/1.1"':
                '"GET /user1.bin?deviceid=<device-id>&ts=4708736&sign=<signature> HTTP/1.1"',
            "Tasmota is at 192.0.2.50 as tasmota-1A2B3C-4567.": "Tasmota is at 192.0.2.50 as <device-name>.",
            "Join tasmota-1a2b3c or esphome-1a2b3c.": "Join <device-name> or <device-name>.",
            "ESPHome 1.4.0 is Sonoff S60 1a2b3c.": "ESPHome 1.4.0 is <device-name>.",
            "OpenBeken answered, named 'openbeken-1a2b3c'; ESPHome is esphome 1a2b3c.":
                "OpenBeken answered, named '<device-name>'; ESPHome is <device-name>.",
            "the device's address is AA:BB:CC:12:34:56": "the device's address is <mac>",
            '{"apikey": "abc-123", "at": "token", "action": "update"}':
                '{"apikey": "<redacted>", "at": "<redacted>", "action": "update"}',
            "[sudo] password for someone: ": "[sudo] password for <user>: ",
            '{"phoneNumber": "+447700900123", "countryCode": "+44"}':
                '{"phoneNumber": "<phone>", "countryCode": "+44"}',
            "this computer is 81.2.69.160 on the internet": "this computer is <public-ip> on the internet",
        }
        for line, expected in cases.items():
            self.assertEqual(output.scrub(line), expected)

    def test_what_is_needed_to_follow_a_run_stays(self):
        for line in ("The device is at 192.0.2.50; this computer is 10.0.0.2 and 172.16.4.1",
                     "Tasmota 15.6.0(release-tasmota), template tasmota-settings-template-15.6.0.0.bin",
                     "Stage 3 - Discovering the device - Complete (4s)",
                     "Sonoff S60 Device model S60TPG"):
            self.assertEqual(output.scrub(line), line)

    def test_the_home_folder_is_shortened(self):
        path = str(Path.home() / "Git" / "repo" / "captures" / "run.log")
        expected = os.sep.join(["~", "Git", "repo", "captures", "run.log"])
        self.assertEqual(output.scrub(f"saved to {path}"), f"saved to {expected}")

    def test_values_the_run_learns_are_replaced(self):
        output.private("Home & Garden 2G", "wifi-name")
        output.private("hunter22", "wifi-password")
        output.private("Kitchen", "device-name")
        self.assertEqual(output.scrub("joined 'Home & Garden 2G' with hunter22; Kitchen answered"),
                         "joined '<wifi-name>' with <wifi-password>; <device-name> answered")

    def test_too_short_to_replace_safely_is_not_registered(self):
        output.private("ab", "wifi-name")
        output.private(None, "wifi-name")
        self.assertEqual(output.scrub("ab cd"), "ab cd")


class LogFileTests(PrivacyTestCase):
    def test_the_log_file_holds_the_scrubbed_text_and_the_screen_the_real_one(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
             mock.patch("sys.stdout", io.StringIO()) as screen:
            log = Path(directory) / "run.log"
            output.private("Home-2G", "wifi-name")
            output.start(log, verbose=True)
            output.show("Network check: the device is on 'Home-2G'; account someone@gmail.com")
            output.answered("someone@gmail.com")
            output.finish()
            written = log.read_text()
        self.assertIn("'Home-2G'", screen.getvalue())
        self.assertIn("Network check: the device is on '<wifi-name>'; account <email>", written)
        self.assertIn("-> <email>", written)
        self.assertNotIn("Home-2G", written)
        self.assertNotIn("someone@gmail.com", written)

    def test_a_resumed_run_knows_its_private_inputs_again(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("device_name", "kitchen-plug")
            state.set_input("device_ip", "192.0.2.50")
            output._private.clear()
            RunState.open(Path(directory))  # as a new run reads it back
        self.assertEqual(output.scrub("kitchen-plug at 192.0.2.50"), "<device-name> at 192.0.2.50")

    def test_an_account_made_with_a_phone_number_is_written_as_one(self):
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("email", "+447700900123")
        self.assertEqual(output.scrub("account +447700900123"), "account <phone>")
        self.assertEqual(output._private["+447700900123"], "phone")


class ElapsedTests(unittest.TestCase):
    def test_a_minute_or_more_is_given_in_minutes_and_seconds(self):
        self.assertEqual([output.elapsed(seconds) for seconds in (59, 59.6, 60, 125)],
                         ["59s", "1m 00s", "1m 00s", "2m 05s"])


if __name__ == "__main__":
    unittest.main()

