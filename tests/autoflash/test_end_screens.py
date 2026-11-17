"""Every way a run ends, as the person sees it: a conversion that finished or
failed, in each chip family, one that crashed, one that failed before any
stage, and a readout that read devices, read none or failed - drawn by the
code that draws them (report.py, readout.py, output.py) on a terminal of
each width a run goes on in, then read back as a terminal shows it
(tools/repo/unattended_run.py) and checked as the attended suite checks a
run in the emulator (unattended_run.end_problems): its report lined up, the
links or the way to open an issue under it, the report ID and the clipboard
said where they should be and nowhere else.

Each ending at 100 columns is kept in end_screens/, as it should look: an
ending drawn otherwise fails here, with the difference. After a change
meant to show, write them again and read the difference in git:

    AUTOFLASH_WRITE_END_SCREENS=1 python3 -m unittest tests.autoflash.test_end_screens
"""

import difflib
import io
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.repo.test_report_answers import FIELDS as LONGEST
from tools.autoflash import hostsys, output, readout, report, runner, runs
from tools.repo import unattended_run

SAVED = Path(__file__).with_name("end_screens")
WRITE = bool(os.environ.get("AUTOFLASH_WRITE_END_SCREENS"))
# The narrowest a run goes on in (report.report_screen's one column under
# about 70), the usual ones, and a wide one.
WIDTHS = (60, 80, 100, 160)
# Where a run's log is: in the checkout, which names it from there (runs.display_path).
LOG = runs.ROOT / "captures/devices/123456abcdef/runs/1/run.log"
REPORT_ID = "0123456789abcdef"

# A conversion's report as report.report_fields gives it, in its order: an
# Espressif device's, then a Beken one's, which has no Wi-Fi typed or setup
# network, but its power cycles.
ESPRESSIF = [("Result", "Converted"), ("Firmware", "Tasmota"), ("From", "Sonoff 1.2.0"), ("Liberator", "3.0.0"),
             ("Model", "S60TPG"), ("Backup", "Completed"), ("Identity", "Kept"),
             ("OS", "Ubuntu 24.04.5 LTS"), ("Python", "3.12.3 (venv)"), ("Retries", "None"),
             ("Signal", "-23 dBm"), ("Wi-Fi typed", "No"), ("Setup network", "No"), ("Checks", "Pass")]
BEKEN = [("Result", "Converted"), ("Firmware", "OpenBeken"), ("From", "Sonoff 1.0.2"), ("Liberator", "3.0.0"),
         ("Model", "S61STPF"), ("Backup", "Completed"), ("OS", "Ubuntu 24.04.5 LTS"),
         ("Python", "3.12.3 (venv)"), ("Retries", "None"), ("Signal", "-45 dBm"), ("Power cycled", "False"),
         ("Checks", "Pass")]
FAILURE = "the device did not answer within 30s"
STOPPED = f"Stopped at stage 8 of 18 (Installing interim firmware): {FAILURE}"


def failed(fields):
    """A report of a run that stopped part way: no checks run."""
    return [("Result", STOPPED) if label == "Result" else ("Checks", "Not run") if label == "Checks"
            else (label, value) for label, value in fields]


def device(name, product, version, outcome, report_id=None):
    return readout.Became(f"'{name}' ({product})", product, f"SN-{product}-01", version, True, outcome,
                          report_id=report_id)


ACCEPTED = device("Kitchen", "S60TPG", "1.2.0", readout.ACCEPTED)
UNNEEDED = device("Porch", "BASICR4", "1.1.0", readout.UNNEEDED)
KEPT = device("Hall", "S61STPF", "1.0.2", readout.KEPT)
FAILED = device("Garage", "S60TPG", "1.2.0", readout.FAILED, REPORT_ID)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class EndScreenTests(unittest.TestCase):
    def drawn(self, draw, columns, rows=40, copies=True):
        """The screen `draw` leaves in a window `columns` wide, without its
        colours; what it put on the clipboard; and how many rows the terminal
        had to wrap itself."""
        copied = []
        terminal = Terminal()
        with ExitStack() as patches:
            for owner, name, value in (
                    (output, "colour_ok", mock.Mock(return_value=True)),
                    (output, "log_path", mock.Mock(return_value=LOG)),
                    (hostsys, "copy_to_clipboard", mock.Mock(side_effect=lambda text: copied.append(text) or copies)),
                    (report.report_upload, "send", mock.Mock()),
                    (report, "end_of_run_checks", mock.Mock(return_value={})),
                    (report, "computer_description", mock.Mock(return_value="Ubuntu 24.04.5 LTS")),
                    (report, "python_description", mock.Mock(return_value="3.12.3 (venv)")),
                    (readout, "__version__", "3.0.0")):
                patches.enter_context(mock.patch.object(owner, name, value))
            patches.enter_context(mock.patch("sys.stdout", terminal))
            patches.enter_context(mock.patch("shutil.get_terminal_size",
                                             return_value=os.terminal_size((columns, rows))))
            directory = patches.enter_context(tempfile.TemporaryDirectory())
            output.start(Path(directory) / "run.log", verbose=False)
            try:
                draw()
            finally:
                output.finish()
        screen = unattended_run.Screen(columns, rows)
        screen.feed(terminal.getvalue().replace("\n", "\r\n"))   # as a terminal's line discipline does
        return screen.render(colour=False), "".join(copied), screen.wrapped

    # ------------------------------------------------------------ the endings

    @staticmethod
    def conversion(fields, headline="Conversion complete!"):
        def draw():
            ctx = SimpleNamespace(reported=False, state=mock.Mock(input=mock.Mock(return_value=None)))
            output.stage_begin(18, "Verifying conversion")
            output.stage_end("complete")
            output.tell("")
            output.show(headline, output.GREEN)
            output.tell(f"The device is running {dict(fields).get('Firmware', 'Tasmota')}.")
            with mock.patch.object(report, "report_fields", return_value=fields):
                report.invite_report(ctx, 0.0, False)
        return draw

    @staticmethod
    def conversion_failed(fields, report_id=REPORT_ID, ending="failed"):
        def draw():
            ctx = SimpleNamespace(reported=False, state=mock.Mock(input=mock.Mock(return_value=None)),
                                  run_started=time.monotonic(), run_resumed=False, last_report_id=report_id)
            output.stage_begin(8, "Installing interim firmware")
            output.tell("    Waiting for the device to answer...")
            output.stage_end("stopped")
            output.tell(f"FAILED: {FAILURE}")
            runner.tell_how_to_carry_on()
            with mock.patch.object(report, "report_fields", return_value=failed(fields)), \
                    mock.patch.object(report, "run_result", return_value=STOPPED):
                report.invite_stopped_report(ctx, ending)
        return draw

    @staticmethod
    def failed_before_any_stage():
        output.tell("FAILED: no network to look for the device on")
        report.ask_for_an_issue()

    @staticmethod
    def readout_end(done, said="", failed_with=None):
        def draw():
            ctx = SimpleNamespace(readouts=list(done))
            output.stage_begin(7, "Saving the readout")
            output.stage_end("complete")
            if failed_with:
                output.tell(f"FAILED: {failed_with}")
            readout.end(ctx, said, failed=bool(failed_with))
        return draw

    ENDINGS = {
        "conversion-espressif": (conversion(ESPRESSIF), "hat"),
        "conversion-beken": (conversion(BEKEN), "hat"),
        "update-espressif": (conversion([("From", "Tasmota 14.5.0, outdated") if label == "From"
                                         else (label, value) for label, value in ESPRESSIF],
                                        "Update complete!"), "hat"),
        "conversion-failed-espressif": (conversion_failed(ESPRESSIF), "issue"),
        "conversion-failed-beken": (conversion_failed(BEKEN), "issue"),
        "conversion-failed-unsent": (conversion_failed(ESPRESSIF, report_id=None), "issue"),
        "conversion-crashed": (conversion_failed(ESPRESSIF, ending="crashed"), "issue"),
        "failed-before-any-stage": (failed_before_any_stage, "issue"),
        "readout-accepted": (readout_end([ACCEPTED]), "hat"),
        "readout-mixed": (readout_end([ACCEPTED, UNNEEDED, KEPT]), "hat"),
        "readout-nothing-read": (readout_end([UNNEEDED], readout.SWITCH_OFF_AND_ON), "hat"),
        "readout-failed": (readout_end([ACCEPTED, FAILED], failed_with=FAILURE), "issue"),
        "readout-failed-alone": (readout_end([FAILED], failed_with=FAILURE), "issue"),
    }
    # The longest each answer can be, and a long sitting's readout: checked, not kept.
    LONG = {
        "conversion-longest": (conversion(LONGEST), "hat"),
        "conversion-failed-longest": (conversion_failed(LONGEST), "issue"),
        "readout-many": (readout_end([device(f"Device {n}", model, f"1.{n}.0", outcome)
                                      for n, (model, outcome) in enumerate(
                                          [("S60TPG", readout.ACCEPTED), ("BASICR4", readout.UNNEEDED),
                                           ("S61STPF", readout.KEPT), ("MINIR4", readout.ACCEPTED),
                                           ("POWR320D", readout.UNNEEDED), ("THR316D", readout.ACCEPTED)] * 3)]),
                         "hat"),
    }

    # ------------------------------------------------------------ the checks

    def test_every_ending_is_drawn_as_it_should_be_in_every_width(self):
        for name, (draw, ends) in {**self.ENDINGS, **self.LONG}.items():
            for columns in WIDTHS:
                with self.subTest(ending=name, columns=columns):
                    screen, copied, wrapped = self.drawn(draw, columns)
                    self.assertEqual(unattended_run.end_problems(screen, copied), [], screen)
                    self.assertEqual(unattended_run.stage_problems(screen), [], screen)
                    self.assertEqual(wrapped, 0, f"a row the terminal wrapped itself:\n{screen}")
                    self.assertFalse([row for row in screen.splitlines() if len(row) >= columns], screen)
                    if ends == "hat":
                        self.assertIn(unattended_run.HAT, screen)
                        self.assertEqual(copied, "", "a run that did not fail leaves the clipboard alone")
                    else:
                        self.assertIn(unattended_run.ISSUE, screen)
                        self.assertNotIn(unattended_run.HAT, screen)
                        # Named once, under the way to open an issue.
                        self.assertEqual(screen.count(runs.display_path(LOG)), 1, screen)

    def test_a_report_missing_any_one_of_its_fields_is_still_lined_up_and_balanced(self):
        # Which fields a report has depends on the run (a Beken device's, an
        # update's, one stopped early): each left out in turn, so the columns
        # balance whichever side loses a row.
        for family, fields in (("espressif", ESPRESSIF), ("beken", BEKEN)):
            for left_out, _ in fields:
                if left_out in report.LOG_ONLY:
                    continue
                kept = [(label, value) for label, value in fields if label != left_out]
                for columns in (80, 100):
                    with self.subTest(family=family, left_out=left_out, columns=columns):
                        screen, copied, wrapped = self.drawn(self.conversion(kept), columns)
                        self.assertEqual(unattended_run.end_problems(screen, copied), [], screen)
                        self.assertEqual(wrapped, 0, screen)

    def test_a_failed_runs_report_on_the_clipboard_is_as_on_the_screen_with_its_id(self):
        for name in ("conversion-failed-espressif", "conversion-failed-beken", "readout-failed"):
            with self.subTest(ending=name):
                screen, copied, _ = self.drawn(self.ENDINGS[name][0], 80)
                self.assertTrue(copied.rstrip().endswith(f"Report ID: {REPORT_ID}"), copied)
                self.assertIn(f"Report ID: {REPORT_ID}", screen)
                # Each of its rows unindented, none wider than the clipboard's columns.
                self.assertFalse([row for row in copied.splitlines() if row.startswith(" ") and ":" in row[:12]])
                self.assertFalse([row for row in copied.splitlines() if len(row) > report.CLIPBOARD_COLUMNS])

    def test_a_failed_run_whose_report_could_not_be_copied_does_not_say_it_was(self):
        screen, copied, _ = self.drawn(self.ENDINGS["conversion-failed-espressif"][0], 100, copies=False)
        self.assertNotIn(unattended_run.CLIPBOARD_SAID, screen)
        self.assertEqual(unattended_run.end_problems(screen, ""), [])

    def test_each_ending_looks_as_kept(self):
        SAVED.mkdir(exist_ok=True)
        for name, (draw, _) in self.ENDINGS.items():
            with self.subTest(ending=name):
                screen, _, _ = self.drawn(draw, 100)
                screen = "\n".join(row.rstrip() for row in screen.splitlines()).strip("\n") + "\n"
                path = SAVED / f"{name}.txt"
                if WRITE:
                    path.write_text(screen, encoding="utf-8")
                    continue
                self.assertTrue(path.exists(), f"no {path.name}: write it with AUTOFLASH_WRITE_END_SCREENS=1")
                kept = path.read_text(encoding="utf-8")
                self.assertEqual(screen, kept, "drawn otherwise than kept (- kept, + drawn):\n" + "".join(
                    difflib.unified_diff(kept.splitlines(True), screen.splitlines(True), "kept", "drawn")))

    def test_every_ending_kept_is_one_drawn_here(self):
        self.assertEqual(sorted(path.stem for path in SAVED.glob("*.txt")), sorted(self.ENDINGS))


if __name__ == "__main__":
    unittest.main()
