"""Unattended runs (tools/repo/unattended_run.py): the screen kept for review."""

import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.repo import unattended_run

ROOT = Path(__file__).resolve().parents[2]


class ScreenTests(unittest.TestCase):
    def render(self, text, width=20, height=5, colour=True):
        screen = unattended_run.Screen(width, height)
        screen.feed(text)
        return screen.render(colour)

    def test_a_line_redrawn_in_place_shows_its_last_drawing(self):
        self.assertEqual(self.render("Stage 1  underway 3s\rStage 1  complete   \n", colour=False),
                         "Stage 1  complete\n")

    def test_rows_taken_away_are_gone(self):
        text = "kept\r\nnote\r\nquestion: y\r\n" + "\x1b[1F\x1b[2K" * 2 + "after\r\n"
        self.assertEqual(self.render(text, colour=False), "kept\nafter\n")

    def test_the_cursor_cannot_climb_into_the_scrollback(self):
        # Seven rows - six written, and the cursor's - in a window of five:
        # the window's top is row 2, and the cursor stops there.
        text = "".join(f"row {n}\r\n" for n in range(6)) + "\x1b[1F" * 9 + "X"
        self.assertEqual(self.render(text, colour=False).splitlines()[:3], ["row 0", "row 1", "Xow 2"])

    def test_long_lines_wrap_at_the_width(self):
        self.assertEqual(self.render("abcdefgh\r\n", width=5, colour=False), "abcde\nfgh\n")

    def test_colours_are_kept_and_links_left_as_text(self):
        text = ("\x1b[32mComplete!\x1b[0m \x1b]8;;http://x/\x1b\\http://x/\x1b]8;;\x1b\\\r\n")
        self.assertEqual(self.render(text, width=40), "\x1b[0m\x1b[32mComplete!\x1b[0m http://x/\n")


class StageProblemsTests(unittest.TestCase):
    """A stage that passed leaves one line; one that stopped, what it showed."""

    def problems(self, *rows):
        return unattended_run.stage_problems("".join(f"{row}\n" for row in rows))

    def test_stages_that_each_ended_once_leave_nothing(self):
        self.assertEqual(self.problems(
            "Stage 12 - Verifying memory layout - Complete",
            "Stage 13 - Staging startup firmware - Complete",
            "",
            "    The meter is left as it is: the meter did not report.",   # kept, under its line
            "Stage 14 - Committing memory layout - Underway",
            "",
            "    The device stopped part way",
            "Stage 14 - Committing memory layout - Stopped",
            "",
            "FAILED: the device did not answer"), [])

    def test_a_try_that_failed_left_on_the_screen_is_found(self):
        # As a run on 2026-11-08 left it: the retry drew the stage again
        # under the failed try's lines.
        self.assertEqual(self.problems(
            "Stage 13 - Staging startup firmware     - Underway",
            "",
            "    The device's console: 00:00:00.328 BRY: Berry initialized",
            "Stage 13 - Staging startup firmware     - Stopped",
            "",
            "FAILED: the device stopped the stage without reaching this computer",
            "    That may have been the network - trying again in 10s (attempt 2 of 3).",
            "Stage 13 - Staging startup firmware     - Complete",
            "Stage 14 - Committing memory layout     - Complete"),
            ["Stage 13 - Staging startup firmware: shown complete, after an earlier outcome - "
             "its first go was left on the screen"])

    def test_a_stage_never_ended_is_found(self):
        self.assertEqual(self.problems("Stage  9 - Preparing memory layout - Underway",
                                       "Stage 10 - Updating the bootloader - Complete"),
                         ["Stage 9 - Preparing memory layout: left underway when the next stage began"])

    def test_a_stage_gone_back_to_drawn_under_its_first_go_is_found(self):
        problems = self.problems("Stage 6 - Sending bridge firmware - Complete",
                                 "Stage 7 - Checking it arrived - Not installed",
                                 "",
                                 "The bridge was not installed. Doing B2 again.",
                                 "Stage 6 - Sending bridge firmware - Complete",
                                 "Stage 7 - Checking it arrived - Complete")
        self.assertEqual(len(problems), 2, problems)

    def test_a_stage_that_passed_with_its_notes_left_above_it_is_found(self):
        # As the fault suites' screens showed: instructions to join a network,
        # long since done with, above a stage that had passed.
        self.assertEqual(self.problems(
            "Stage  8 - Installing interim firmware  - Underway",
            "",
            "-- Back to your usual Wi-Fi",
            "    Your computer should rejoin your usual Wi-Fi by itself.",
            "Stage  8 - Installing interim firmware  - Complete"),
            ["Stage 8 - Installing interim firmware: passed, with what it showed while under way left above it"])

    def test_a_stage_left_retrying_is_found_as_one_left_underway(self):
        self.assertEqual(self.problems("Stage  9 - Preparing memory layout - Retrying",
                                       "Stage 10 - Updating the bootloader - Complete"),
                         ["Stage 9 - Preparing memory layout: left underway when the next stage began"])

    def test_each_run_of_a_sitting_is_looked_at_on_its_own(self):
        # Seen in attended emulator runs: a readout after a readout was taken
        # for one done twice, and the next run's start for two blank rows.
        run = ["Stage 1 - Querying eWeLink for devices - Complete", "", "Readout saved, not sent.", "",
               "    1. Convert a device", "    2. Read out another device", "", "What next (q to quit): 2"]
        self.assertEqual(self.problems(*run, *[""] * 40, *run[:-1], "What next (q to quit): q"), [])
        # Within one run, as before.
        self.assertEqual(self.problems(*run[:1], *run), ["Stage 1 - Querying eWeLink for devices: shown complete, "
                                                         "after an earlier outcome - its first go was left on the "
                                                         "screen"])

    def test_each_device_of_a_readout_of_several_has_its_own_stages(self):
        # As the fault suite's readout --all scenarios showed (FAIL-SCREEN):
        # the second device's stages were taken for the first's shown twice.
        def device(number):
            return [f"Reading out 'Test device {number}' (TESTLIGHT) ({number} of 2)...",
                    "Stage 2 - Locating the device on your network - Complete",
                    "Stage 3 - Probing the device                  - Complete", ""]
        self.assertEqual(self.problems("Stage 1 - Querying eWeLink for devices        - Complete", "",
                                       *device(1), *device(2), "Readout complete!"), [])
        # Within one device, as before.
        self.assertEqual(len(self.problems(*device(1)[:3], *device(1)[1:])), 2)

    def test_two_blank_rows_together_are_found(self):
        self.assertEqual(self.problems("Stage 1 - Checking - Complete", "", "", "Done"),
                         ["two blank rows one after another"])


@unittest.skipUnless(hasattr(os, "fork"), "a pseudo-terminal needs Linux or macOS")
class RecordingTests(unittest.TestCase):
    def test_the_screen_a_person_would_see_is_rebuilt(self):
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from tools.autoflash import output, prompts
            output.start(None, verbose=False)
            output.stage_begin(1, "Checking the files")
            output.transient_begin()
            prompts.warning_box("Read this first", ["taken away once read"])
            prompts.UNATTENDED = True
            prompts.wait_for_key()
            output.transient_end()
            output.stage_end("complete")
            output.show("Conversion complete!", output.GREEN)
            output.finish()
        """)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):  # the run gets colour regardless
            code, written = unattended_run.record([sys.executable, "-c", script],
                                                  (80, 24), live=False, timeout=60)
        self.assertEqual(code, 0, written)
        screen = unattended_run.Screen(80, 24)
        screen.feed(written.decode("utf-8", errors="replace"))
        plain = screen.render(colour=False)
        self.assertIn("Checking the files", plain)
        self.assertIn("complete", plain)
        self.assertNotIn("Read this first", plain)
        self.assertIn("\x1b[32mConversion complete!", screen.render())



@unittest.skipUnless(hasattr(os, "fork"), "a pseudo-terminal needs Linux or macOS")
class AnswerTests(unittest.TestCase):
    def test_each_question_is_answered_as_it_shows(self):
        script = textwrap.dedent("""
            name = input("Your name: ")
            word = input("Type PROCEED to go ahead: ")
            print(f"got {name} and {word}")
        """)
        answers = [unattended_run.Answer(r"Type PROCEED", "PROCEED\r"), unattended_run.Answer(r"Your name", "Ada\r")]
        code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                              timeout=60, answers=answers)
        self.assertEqual(code, 0, written)
        self.assertIn(b"got Ada and PROCEED", written)
        self.assertEqual([answer.left for answer in answers], [0, 0])   # each once, as told
        name, word = answers[1].typed_at[0], answers[0].typed_at[0]
        self.assertLess(name, word)   # where each was typed, in the bytes written

    def test_an_answer_waits_for_its_question_not_a_note_that_says_its_words(self):
        # Seen: q typed at the firmware question, whose note said "your eWeLink
        # account", for the sign-in's "eWeLink account email" asked later.
        script = textwrap.dedent("""
            print("Its note: going back uses your eWeLink account email record.")
            print("A second line, and a third,")
            print("then the question itself.")
            first = input("Which firmware would you like: ")
            account = input("eWeLink account email: ")
            print(f"got {first} and {account}")
        """)
        answers = [unattended_run.Answer(r"eWeLink account email", "you@example.com\r"),
                   unattended_run.Answer(r"Which firmware", "1\r")]
        code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                              timeout=60, answers=answers)
        self.assertIn(b"got 1 and you@example.com", written)

    def test_a_question_taken_off_and_asked_again_as_it_was_is_answered_again(self):
        # Seen: a wrong sudo password taken off the screen, the question asked
        # again just as before, and nothing typed: the run waited for ever.
        script = textwrap.dedent("""
            import sys
            first = input("Password: ")
            sys.stdout.write("\\x1b[1F\\x1b[2K"); sys.stdout.flush()
            second = input("Password: ")
            print(f"got {first} then {second}")
        """)
        answers = [unattended_run.Answer(r"Password", "wrong\r"), unattended_run.Answer(r"Password", "right\r")]
        code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                              timeout=30, answers=answers)
        self.assertIn(b"got wrong then right", written)

    def test_a_question_redrawn_as_it_is_answered_is_not_answered_twice(self):
        # Seen: a password's field redrawn as its typing was taken, looking for
        # a moment as it did, and the next answer typed into the question after.
        script = textwrap.dedent("""
            import os, sys, termios, time, tty
            # Read as a password is (prompts._read_secret): nothing echoed,
            # the field redrawn in place, a new row only once it is done.
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            sys.stdout.write("Password:\\n > "); sys.stdout.flush()
            first = ""
            while not first.endswith(("\\r", "\\n")):
                first += os.read(fd, 1).decode()
            for _ in range(3):
                sys.stdout.write("\\r\\x1b[K"); sys.stdout.flush(); time.sleep(0.2)
                sys.stdout.write(" > "); sys.stdout.flush(); time.sleep(0.3)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            first = first.strip()
            sys.stdout.write("\\n"); sys.stdout.flush()
            second = input("Name: ")
            print(f"got {first} then {second}")
        """)
        answers = [unattended_run.Answer(r"Password", "secret\r"), unattended_run.Answer(r"Password", "wrong\r"),
                   unattended_run.Answer(r"Name", "Ada\r")]
        code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                              timeout=30, answers=answers)
        self.assertIn(b"got secret then Ada", written)
        self.assertEqual(answers[1].typed_at, [])

    def test_a_password_answered_before_its_field_is_drawn_is_answered_once(self):
        # Seen, 1 run in 25: typed on "Password:" alone, then typed again as its
        # field appeared under it, a row down.
        script = textwrap.dedent("""
            import os, sys, termios, time, tty
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            sys.stdout.write("Password:"); sys.stdout.flush()
            time.sleep(1.5)   # the field long after its prompt: typed at the prompt, always
            sys.stdout.write("\\n > "); sys.stdout.flush()
            first = ""
            while not first.endswith(("\\r", "\\n")):
                first += os.read(fd, 1).decode()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write("\\n"); sys.stdout.flush()
            second = input("Name: ")
            print(f"got {first.strip()} then {second}")
        """)
        answers = [unattended_run.Answer(r"Password", "secret\r"), unattended_run.Answer(r"Password", "wrong\r"),
                   unattended_run.Answer(r"Name", "Ada\r")]
        code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                              timeout=30, answers=answers)
        self.assertIn(b"got secret then Ada", written)

    def test_a_question_is_its_own_row_not_the_answered_one_above(self):
        # Seen: "What now: 1" answered, the next question drawn under it, and
        # "What now" typed again.
        screen = unattended_run.Screen(80, 24)
        screen.feed(" What now (q to quit): 1\r\n ESPHome web user name (q to quit): ")
        self.assertEqual(unattended_run.question(screen), " ESPHome web user name (q to quit):")

    def test_a_long_prompt_with_its_hint_under_it_is_one_question(self):
        screen = unattended_run.Screen(80, 24)
        screen.feed(" Answered: 1\r\n More than one device answered. Which one is this run for\r\n (q to quit): ")
        self.assertEqual(unattended_run.question(screen),
                         " More than one device answered. Which one is this run for\n (q to quit):")

    def test_a_question_wrapped_over_two_rows_is_one(self):
        screen = unattended_run.Screen(40, 24)
        screen.feed(" A question long enough to wrap, or press Enter now (q to quit) ")
        self.assertIn("press Enter now", unattended_run.question(screen))

    def test_a_question_is_its_last_two_rows_on_the_screen(self):
        screen = unattended_run.Screen(80, 24)
        screen.feed("a note\r\nmore\r\n Wi-Fi password (q to quit):\r\n > ")
        self.assertEqual(unattended_run.question(screen), " Wi-Fi password (q to quit):\n >")

    def test_a_question_waits_until_its_prompt_ends_the_row(self):
        for asked, expected in ((" Which firmware would you like (q to quit):", True),
                                (" Wi-Fi password (q to quit):\n >", True),
                                (" Press any key to continue (q to quit)", True),
                                (" Wi-Fi password (q to quit):\n > ***", False),
                                (" Which firmware would you like (q to quit): 1", False)):
            with self.subTest(asked=asked):
                self.assertEqual(unattended_run.waiting(asked), expected)

    def test_answers_are_read_from_a_file(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "answers.json"
            path.write_text(json.dumps([{"when": "Wi-Fi password", "type": "secret\r", "times": 2}]))
            loaded = unattended_run.Answer.load(path)
        self.assertEqual((loaded[0].type, loaded[0].left), ("secret\r", 2))

    def test_an_attended_run_is_not_told_it_is_unattended(self):
        self.assertNotIn("--unattended", unattended_run.tool_command("run", ["--new"], attended=True))


# A conversion's report and its ending, as report.py draws them in a window
# 100 wide, with output's margin of one.
REPORT = """\
 Conversion complete!

     Liberator: 3.0.0                                 Firmware: Tasmota
     OS:        Ubuntu 24.04.5 LTS                    From:     Sonoff 1.2.0
     Python:    3.12.3 (venv)                         Model:    S60TPG
     Retries:   None                                  Backup:   Completed
     Checks:    Pass                                  Signal:   -23 dBm
"""
HAT = """
 You've reached the bit where I hold out my hat!
 Free software, not free biscuits... Throw your friendly neighbourhood dev a biscuit:
     GitHub: https://github.com/sponsors/adamsthws
     Ko-fi:  https://ko-fi.com/adamsthws
     (Ctrl+click to open)
"""
ISSUE = """
 If it should not have stopped, please open an issue here:
     https://firmwareliberator.com/issues
     Report ID: 0123456789abcdef
     This report is on your clipboard.

 This run's log:
     /home/someone/autoflash/captures/devices/123456abcdef/runs/1/run.log
"""
COPIED = "Result: Stopped at stage 8\nLiberator: 3.0.0\nReport ID: 0123456789abcdef\n"


class EndProblemsTests(unittest.TestCase):
    def problems(self, screen, clipboard=None):
        return unattended_run.end_problems(screen, clipboard)

    def test_a_run_that_finished_or_failed_as_drawn_has_none(self):
        self.assertEqual(self.problems(REPORT + HAT, ""), [])
        self.assertEqual(self.problems(REPORT.replace("Conversion complete!", "FAILED: no answer") + ISSUE, COPIED), [])

    def test_a_report_out_of_line_is_found(self):
        left = REPORT.replace("     OS:        Ubuntu", "     OS:       Ubuntu")
        self.assertIn("the report's values on the left start in 2 columns, not one", self.problems(left + HAT))
        right = REPORT.replace("From:     Sonoff", "From:    Sonoff")
        self.assertIn("the report's right-hand column does not line up", self.problems(right + HAT))

    def test_a_conversions_columns_out_of_balance_are_found(self):
        longer = REPORT.replace("     Checks:    Pass                                  Signal:   -23 dBm\n",
                                "                                                      Signal:   -23 dBm\n")
        self.assertIn("the report's right-hand column the longer: 5 rows to 4", self.problems(longer + HAT))
        left = REPORT + "     Wi-Fi:     No\n     Setup:     No\n"
        self.assertIn("the report's left-hand column 2 rows the longer", self.problems(left + HAT))

    def test_a_readouts_counts_may_run_below_its_left_column(self):
        # readout.report_screen(balance=False): the counts stay together.
        readout = """\
 Readout complete!

     Liberator: 3.0.0                                 Devices:    2
     OS:        Ubuntu 24.04.5 LTS                    Accepted:   1
     Python:    3.12.3 (venv)                         Not needed: 0
                                                      Not sent:   1
     Accepted:  S61STPF 1.0.2
"""
        self.assertEqual(self.problems(readout + HAT, ""), [])

    def test_a_report_with_no_ending_under_it_or_both_is_found(self):
        self.assertIn("a report with neither the links to support the project nor the way to open an issue "
                      "under it", self.problems(REPORT))
        self.assertIn("both the links to support the project and the way to open an issue",
                      self.problems(REPORT + ISSUE + HAT, COPIED))

    def test_a_run_that_says_it_failed_and_is_complete_is_found(self):
        said = " FAILED: the device did not answer\n\n" + REPORT.replace("Conversion", "Readout") + ISSUE
        self.assertEqual(self.problems(said, COPIED), ["said it failed, and that it is complete"])

    def test_a_report_id_or_the_clipboard_said_by_a_run_that_did_not_fail_is_found(self):
        said = REPORT + "     Report ID: 0123456789abcdef\n" + HAT
        self.assertIn("a report ID shown by a run that did not fail", self.problems(said))
        self.assertIn("put a report on the clipboard, though no run failed", self.problems(REPORT + HAT, COPIED))

    def test_the_issue_link_and_what_is_under_it_drawn_otherwise_is_found(self):
        for wrong, problem in (
                (ISSUE.replace("     Report ID", "  Report ID"), "'Report ID: 0123456789abcdef' not as far in"),
                (ISSUE.replace("     https", " https"), "the issue link not on the row under"),
                (ISSUE.replace("     Report ID: 0123456789abcdef\n     This report is on your clipboard.\n",
                               "     This report is on your clipboard.\n"
                               "     Report ID: 0123456789abcdef\n"), "not the report ID then the clipboard"),
                (ISSUE.replace(" This run's log:\n", ""), "the run's log not under")):
            with self.subTest(problem=problem):
                found = self.problems(REPORT + wrong, COPIED)
                self.assertTrue(any(problem in said for said in found), found)
        self.assertIn("the way to open an issue above the report, not under it", self.problems(ISSUE + REPORT, COPIED))

    def test_what_was_copied_must_be_what_the_screen_says(self):
        failed = REPORT + ISSUE
        self.assertIn("said its report is on the clipboard, and nothing was put there", self.problems(failed, ""))
        quiet = failed.replace("     This report is on your clipboard.\n", "")
        self.assertIn("put its report on the clipboard without saying so", self.problems(quiet, COPIED))
        self.assertIn("the report on the clipboard has not the report ID the screen shows",
                      self.problems(failed, COPIED.replace("0123", "4567")))
        self.assertEqual(self.problems(failed), [])   # nothing known of the clipboard: not judged

    def test_a_question_left_unanswered_above_what_came_after_is_found(self):
        stranded = (" Stage  4 - Leaving Tasmota   - Underway\n\n Which firmware would you like (q to quit):\n"
                    " Stage  4 - Leaving Tasmota   - Stopped\n")
        self.assertEqual(self.problems(stranded), ["a question left unanswered above what came after it: "
                                                   "'Which firmware would you like (q to quit):'"])
        answered = " Which firmware would you like (q to quit): 2\n Stage  4 - Leaving Tasmota   - Stopped\n"
        password = " Wi-Fi password (q to quit):\n >\n Stage  4 - Leaving Tasmota   - Complete\n"
        waiting = " Stage  4 - Leaving Tasmota   - Underway\n Which firmware would you like (q to quit):\n"
        for fine in (answered, password, waiting):   # a question waiting is the attended suite's to judge
            self.assertEqual(self.problems(fine), [], fine)

    def test_it_reads_a_report_run_as_a_script_too(self):
        # As the emulators run it: tools/repo/unattended_run.py, its checkout not on the path.
        import subprocess
        code = (f"import runpy; found = runpy.run_path({str(ROOT / 'tools/repo/unattended_run.py')!r}); "
                f"print(found['end_problems']({REPORT + HAT!r}, ''))")
        done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd="/")
        self.assertEqual((done.stdout.strip(), done.returncode), ("[]", 0), done.stderr)

    def test_each_run_of_a_sitting_ends_on_its_own(self):
        sitting = REPORT + HAT + "\n What next (q to quit): 1\n" + REPORT + ISSUE
        self.assertEqual(self.problems(sitting, COPIED), [])


@unittest.skipUnless(hasattr(os, "fork"), "a pseudo-terminal needs Linux or macOS")
class StandInTests(unittest.TestCase):
    def test_what_the_run_copies_is_kept_and_the_clipboard_left_alone(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            kept = Path(directory, "it's kept", "clipboard.txt")
            kept.parent.mkdir()
            environment = unattended_run.clipboard_stand_in(Path(directory), kept)
            found = subprocess.run(["sh", "-c", f"command -v {unattended_run.CLIPBOARD_TOOL}"], env=environment,
                                   capture_output=True, text=True).stdout.strip()
            self.assertEqual(found, str(Path(directory, unattended_run.CLIPBOARD_TOOL)))
            subprocess.run([unattended_run.CLIPBOARD_TOOL], input="Report ID: 0123\n", text=True,
                           env=environment, check=True)
            self.assertEqual(kept.read_text(encoding="utf-8"), "Report ID: 0123\n")

    def test_the_tool_copies_to_the_stand_in(self):
        from tools.autoflash import hostsys
        if hostsys.IS_WINDOWS or hostsys.IS_MAC:
            self.skipTest("Linux's clipboard tools")
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            kept = Path(directory, "clipboard.txt")
            environment = unattended_run.clipboard_stand_in(Path(directory), kept)
            with mock.patch.dict(os.environ, environment), mock.patch.dict(os.environ, {"AUTOFLASH_NO_CLIPBOARD": ""}):
                self.assertTrue(hostsys.copy_to_clipboard("Report ID: 0123\n"))
            self.assertEqual(kept.read_text(encoding="utf-8"), "Report ID: 0123\n")

    def test_an_answer_can_switch_the_device_or_signal_the_run(self):
        import json
        import tempfile
        import time
        script = textwrap.dedent("""
            import signal, sys, time
            signal.signal(signal.SIGTERM, lambda *_: (print("terminated"), sys.exit(143)))
            print("Stage 1 - Underway", flush=True)
            time.sleep(2)
            print("Stage 2 - Underway", flush=True)
            time.sleep(30)
        """)
        with tempfile.TemporaryDirectory() as directory:
            power = Path(directory, "power")
            power.write_text("on\n", encoding="utf-8")
            answers = [unattended_run.Answer("Stage 1", power="off", anywhere=True),
                       unattended_run.Answer("Stage 2", signal="TERM", anywhere=True)]
            started = time.monotonic()
            with mock.patch.dict(os.environ, {unattended_run.POWER_FILE: str(power)}):
                code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                                      timeout=60, answers=answers)
            self.assertLess(time.monotonic() - started, 20, "signalled by its answer, not the timeout")
            self.assertEqual(power.read_text(encoding="utf-8"), "off\n")
        self.assertEqual(code, 143, written)
        self.assertIn(b"terminated", written)
        saved = [answer.saved() for answer in answers]
        self.assertEqual((saved[0]["power"], saved[1]["signal"]), ("off", "TERM"))
        json.dumps(saved)

    def test_an_answer_can_switch_the_router_or_move_the_computers_wifi(self):
        import tempfile
        script = textwrap.dedent("""
            import time
            print("Stage 2 - ESPHome started and on the Wi-Fi", flush=True)
            time.sleep(2)
            print("Join the device's own network", flush=True)
            time.sleep(3)
        """)
        with tempfile.TemporaryDirectory() as directory:
            router, moved = Path(directory, "router"), Path(directory, "moved")
            router.write_text("on\n", encoding="utf-8")
            # The emulator's `wifi`, as the run's computer has it on its PATH.
            wifi = Path(directory, "wifi")
            wifi.write_text(f'#!/bin/sh\necho "$@" >> {moved}\n', encoding="utf-8")
            wifi.chmod(0o755)
            answers = [unattended_run.Answer("ESPHome started", router="on new-password-1", anywhere=True),
                       unattended_run.Answer("own network", wifi="join", anywhere=True)]
            with mock.patch.dict(os.environ, {unattended_run.ROUTER_FILE: str(router),
                                              "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}):
                code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                                      timeout=60, answers=answers)
            self.assertEqual(router.read_text(encoding="utf-8"), "on new-password-1\n")
            self.assertEqual(moved.read_text(encoding="utf-8"), "join\n")
        self.assertEqual(code, 0, written)
        self.assertEqual([answer.saved().get("router") or answer.saved().get("wifi") for answer in answers],
                         ["on new-password-1", "join"])

    def test_an_answer_shown_anywhere_acts_again_only_once_shown_anew(self):
        import tempfile
        # As the tool's own-network step: its notes still on the screen while
        # it waits, then shown again for a second try.
        script = textwrap.dedent("""
            import time
            print("Join the device's setup network", flush=True)
            for _ in range(6):
                time.sleep(0.5)
                print("waiting...", flush=True)
            print("Join the device's setup network", flush=True)
            time.sleep(3)
        """)
        with tempfile.TemporaryDirectory() as directory:
            moved = Path(directory, "moved")
            wifi = Path(directory, "wifi")
            wifi.write_text(f'#!/bin/sh\necho "$@" >> {moved}\n', encoding="utf-8")
            wifi.chmod(0o755)
            answers = [unattended_run.Answer("setup network", wifi="join", anywhere=True, times=3)]
            with mock.patch.dict(os.environ, {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}):
                code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                                      timeout=60, answers=answers)
            self.assertEqual(moved.read_text(encoding="utf-8"), "join\njoin\n", "once a showing, not each redraw")
        self.assertEqual(code, 0, written)
        second = written.index(b"setup network", written.index(b"setup network") + 1)
        self.assertGreater(answers[0].typed_at[1], second)

    def test_an_answer_shown_anywhere_waits_its_after(self):
        import tempfile
        script = textwrap.dedent("""
            import time
            print("Stage 9 - Checking ESPHome", flush=True)
            for n in range(10):
                time.sleep(0.5)
                print(f"waiting {n}", flush=True)
        """)
        with tempfile.TemporaryDirectory() as directory:
            wifi = Path(directory, "wifi")
            wifi.write_text("#!/bin/sh\n", encoding="utf-8")
            wifi.chmod(0o755)
            answers = [unattended_run.Answer("Checking ESPHome", wifi="join", anywhere=True, after=2)]
            with mock.patch.dict(os.environ, {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}):
                code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                                      timeout=60, answers=answers)
        self.assertEqual(code, 0, written)
        self.assertEqual(len(answers[0].typed_at), 1)
        acted = written[:answers[0].typed_at[0]]
        self.assertIn(b"waiting 3", acted, "not before two seconds of it showing")
        self.assertEqual(answers[0].saved()["after"], 2)

    def test_an_answer_that_fails_is_kept_with_why_and_the_run_goes_on(self):
        import tempfile
        script = 'import time\nprint("Join the device\'s setup network", flush=True)\ntime.sleep(2)\nprint("done")\n'
        with tempfile.TemporaryDirectory() as directory:
            wifi = Path(directory, "wifi")
            wifi.write_text("#!/bin/sh\necho \"No network of the device's own is on the air.\" >&2\nexit 1\n",
                            encoding="utf-8")
            wifi.chmod(0o755)
            answers = [unattended_run.Answer("setup network", wifi="join", anywhere=True)]
            with mock.patch.dict(os.environ, {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}):
                code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False,
                                                      timeout=60, answers=answers)
        self.assertEqual(code, 0, written)
        self.assertIn(b"done", written)
        self.assertEqual(answers[0].saved()["failed"], ["wifi join: No network of the device's own is on the air."])

    def test_the_espressif_router_keeps_its_password(self):
        with mock.patch.dict(os.environ, {unattended_run.ROUTER_FILE: ""}):
            with self.assertRaises(ValueError):
                unattended_run.switch_router("on new-password-1")

class CommandTests(unittest.TestCase):
    def test_a_conversion_unless_told_otherwise(self):
        self.assertEqual(unattended_run.tool_command("run", ["--new"])[2:], ["run", "--unattended", "--new"])

    def test_a_readout_unattended_too(self):
        # The fault suites' readout scenarios (docs/testing.md).
        self.assertEqual(unattended_run.tool_command("readout", ["--device-ip", "192.0.2.50"])[2:],
                         ["readout", "--unattended", "--device-ip", "192.0.2.50"])
        self.assertIn("readout", unattended_run.COMMANDS)


if __name__ == "__main__":
    unittest.main()
