"""The screen a whole run leaves, route by route: every route's own stages,
run by the run's own loop (runner.run_steps) and drawn by output.py as on a
terminal, each step standing in for what steps do - a note, a section, a
question answered, a try that fails on the network, a later
step finding an earlier one did not take - then read back as a terminal
shows it (tools/repo/unattended_run.py).  Every stage must leave one line,
and nothing it showed on the way; and the run ends as report.py draws it -
its report, the links to support the project, nothing copied - as
unattended_run.end_problems reads it."""

import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import errors, firewall, join, output, report, routes, runner, runs, safety_locks, unlock
from tools.autoflash.routes import update
from tools.autoflash.phases import PhaseError
from tools.autoflash.state import RunState
from tools.repo import unattended_run

SIZE = (100, 40)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class Screen(unattended_run.Screen):
    """A terminal that counts the rows it had to wrap itself: a row the
    tool wrote past its edge, which output.py should have wrapped first."""

    def __init__(self, width, height):
        super().__init__(width, height)
        self.wrapped = 0

    def _put(self, char):
        self.wrapped += self.wrap_next
        super()._put(char)


def every_route():
    """(family name, family, source, firmware) for every route a device can take."""
    for name, family in routes.FAMILIES.items():
        for source in family.SOURCES:
            for firmware in family.FIRMWARES:
                try:
                    steps = routes.steps_for(firmware, source, family)
                except ValueError:
                    continue
                if steps:
                    yield name, family, source, firmware


class RouteScreenTests(unittest.TestCase):
    def run_route(self, family, source, firmware, size=SIZE, instead=None):
        """The screen and the log of a whole run of the route, begun as a
        real one is: only the start known, the rest once A3 has chosen
        the firmware (routes.settle_route, step_choose). `instead`: a step,
        and what it does in place of the stand-in's last word."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state, dry_run=False, research_bridge=False, go_back=None,
                              finished=False, last_runner=None)
        steps = [step for step, _, _ in routes.steps_for(firmware, source, family)]
        retried = next((step for step in steps if step in family.RETRY_STEPS), None)
        back_from = steps[-1] if len(steps) > 1 else None
        calls: dict[str, int] = {}

        def stand_in(step):
            def handler(context):
                calls[step] = calls.get(step, 0) + 1
                index = steps.index(step) if step in steps else 0   # a step of a route gone on to instead
                output.tell(f"    A note shown while {step} is under way.")
                if index % 3 == 1:
                    output.transient_begin()
                    output.show("-- A section, and its question")
                    output.before_input("Go ahead? ")
                    sys.stdout.write("Go ahead? y\n")   # as input() shows it, and the answer echoed
                    output.answered("y")
                    output.transient_end()
                if step == retried and calls[step] == 1:
                    output.tell("    The device's console: 00:00:00.328 BRY: Berry initialized")
                    raise PhaseError(f"the device stopped the {step} without reaching this computer")
                if step == back_from and calls[step] == 1:
                    raise errors.GoBack(steps[-2], f"{steps[-2]}'s firmware was not installed", "not installed")
                if step == "A3":
                    apply_target(firmware, source, family)
                if instead and step == instead[0]:
                    done = instead[1](context)
                    routes.STEP_INDEX.update({name: (title, stand_in(name))
                                              for name, (title, _) in routes.STEP_INDEX.items()})
                    return done
                return {}
            return handler

        real_apply = routes.apply_target

        def apply_target(*args):
            real_apply(*args)
            routes.STEP_INDEX.update({step: (title, stand_in(step)) for step, (title, _) in routes.STEP_INDEX.items()})

        terminal = Terminal()
        copied = self.copied = []
        with ExitStack() as patches:
            for owner, name, value in (
                    (safety_locks, "restore_left_locks", mock.Mock()), (unlock, "unlock_tasmota", mock.Mock()),
                    (routes, "settle_route", mock.Mock(return_value=(None, source))),
                    (routes, "route_source", mock.Mock(return_value=source)),
                    (routes, "family_of", mock.Mock(return_value=family)),
                    (routes, "apply_target", apply_target),
                    (join, "wait_while_device_busy", mock.Mock()),
                    (runner, "set_up_if_erased", mock.Mock(return_value=False)),
                    (runner, "wait_for_device_back", mock.Mock()),
                    (runner, "stop_here_after_ctrl_c", mock.Mock(return_value=False)),
                    (runner, "bring_up_to_date", mock.Mock(return_value=False)),
                    (runner.time, "sleep", mock.Mock()),
                    (firewall, "close_firewall_rules", mock.Mock()),
                    (report, "device_page_url", mock.Mock(return_value=None)),
                    # The end of the run as it is drawn: its checks and its send stood in for.
                    (report, "end_of_run_checks", mock.Mock(return_value={})),
                    (report.report_upload, "send", mock.Mock()),
                    (report.hostsys, "copy_to_clipboard", mock.Mock(side_effect=copied.append)),
                    (runs, "run_source", mock.Mock(return_value=source)),
                    (runs, "run_firmware", mock.Mock(return_value=firmware)),
                    (update, "versions", mock.Mock(return_value=("1.0.0", "1.1.0"))),
                    (output, "colour_ok", mock.Mock(return_value=True))):
                patches.enter_context(mock.patch.object(owner, name, value))
            patches.enter_context(mock.patch("sys.stdout", terminal))
            patches.enter_context(mock.patch("sys.stdin", io.StringIO()))
            patches.enter_context(mock.patch("shutil.get_terminal_size", return_value=os.terminal_size(size)))
            patches.enter_context(mock.patch.object(routes, "STEPS", routes.STEPS))
            patches.enter_context(mock.patch.object(routes, "STEP_INDEX", routes.STEP_INDEX))
            patches.enter_context(mock.patch.object(routes, "FAMILY", routes.FAMILY))
            patches.enter_context(mock.patch.object(routes, "ROUTE", routes.ROUTE))
            log = Path(directory.name) / "run.log"
            output.start(log, verbose=False)
            try:
                code = runner.run_steps(ctx)
            finally:
                output.finish()
        screen = Screen(*size)
        screen.feed(terminal.getvalue().replace("\n", "\r\n"))  # as a terminal's line discipline does
        self.wrapped = screen.wrapped
        return code, steps, screen.render(colour=False), log.read_text(encoding="utf-8")

    def test_every_route_leaves_one_line_a_stage_and_nothing_else_of_them(self):
        routes_run = 0
        for name, family, source, firmware in every_route():
            with self.subTest(route=f"{name} {source}>{firmware}"):
                code, steps, screen, log = self.run_route(family, source, firmware)
                routes_run += 1
                self.assertEqual(code, 0, screen)
                self.assertEqual(unattended_run.stage_problems(screen), [], screen)
                self.assertEqual(unattended_run.end_problems(screen, "".join(self.copied)), [], screen)
                self.assertEqual(self.wrapped, 0, screen)
                self.assertIn("You've reached the bit where I hold out my hat!", screen)
                lines = [row for row in screen.splitlines() if unattended_run.STAGE_ROW.match(row)]
                self.assertEqual([row.rsplit(" - ", 1)[-1] for row in lines], ["Complete"] * len(steps), screen)
                # Lined up, from the first stage to the last. Seen in the emulator:
                # a converted device's first two laid out for a route two stages
                # long, the rest once A3 had chosen it.
                self.assertEqual(len({(row.index(" - "), row.rindex(" - ")) for row in lines}), 1, screen)
                for gone in ("under way", "A section", "Go ahead", "console", "FAILED", "trying again",
                             "not installed", "again."):
                    self.assertNotIn(gone, screen)
                self.assertIn("under way", log)   # the log keeps everything
                if any(step in family.RETRY_STEPS for step in steps):
                    self.assertIn("trying again", log)
        self.assertGreater(routes_run, 10)

    def test_a_device_put_back_shows_why_and_carries_on_numbered_after(self):
        # Sonoff's firmware not to be put together at R1 (sonoff.put_back): its
        # line stopped, with why under it, and the route back to ESPHome on.
        from tools.autoflash.routes import espressif

        def put_back(ctx):
            ctx.state.set_input("put_back", {"came_from": "esphome", "why": "no identity"})
            output.tell("    Sonoff's firmware cannot be put back: no identity.")
            routes.go_on_instead("R1", "esphome", "midway")
            return {"stage_outcome": "stopped"}
        code, steps, screen, _ = self.run_route(espressif, "esphome", "sonoff", instead=("R1", put_back))
        self.assertEqual(code, 0, screen)
        self.assertEqual(unattended_run.stage_problems(screen), [], screen)
        lines = [row for row in screen.splitlines() if unattended_run.STAGE_ROW.match(row)]
        said = [(int(unattended_run.STAGE_ROW.match(row)["number"]), row.rsplit(" - ", 1)[-1]) for row in lines]
        # A stage stopped keeps its line as it was under way, what it showed, then its outcome.
        self.assertEqual(said, [(number, "Complete") for number in range(1, 8)] + [(8, "Underway"), (8, "Stopped")]
                         + [(number, "Complete") for number in range(9, 12)], screen)
        self.assertIn("Sonoff's firmware cannot be put back: no identity.", screen)
        self.assertIn("Sonoff's firmware could not be put back, so the device is back on", screen)
        self.assertNotIn("Conversion complete", screen)

    def test_a_second_run_in_the_same_sitting_draws_as_it_would_alone(self):
        # "What next" runs one conversion after another in the same process:
        # nothing one leaves behind - in output's screen, the route tables - may
        # change how the next is drawn.
        from tools.autoflash.routes import beken, espressif
        alone = {route: self.run_route(*route)[2] for route in ((espressif, "sonoff", "tasmota"),
                                                                (beken, "sonoff", "openbeken"),
                                                                (espressif, "tasmota", "esphome"))}
        for first in alone:
            for then in alone:
                with self.subTest(first=first[1:], then=then[1:]):
                    self.run_route(*first)
                    self.assertEqual(self.run_route(*then)[2], alone[then])

    def test_every_route_fits_each_window_it_goes_on_in(self):
        # The shortest a run goes on in and a wide one, as well as SIZE: every
        # row wrapped by the tool, none by the terminal, and no stage left.
        for size in ((80, output.MIN_ROWS), (132, 50)):
            for name, family, source, firmware in every_route():
                with self.subTest(size=size, route=f"{name} {source}>{firmware}"):
                    code, steps, screen, _ = self.run_route(family, source, firmware, size)
                    self.assertEqual(code, 0, screen)
                    self.assertEqual(self.wrapped, 0, screen)
                    self.assertEqual(unattended_run.stage_problems(screen), [], screen)
                    self.assertEqual(unattended_run.end_problems(screen, "".join(self.copied)), [], screen)


if __name__ == "__main__":
    unittest.main()
