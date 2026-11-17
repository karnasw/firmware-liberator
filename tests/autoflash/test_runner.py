"""Running a run's steps (tools/autoflash/runner.py): a step done is skipped
only as far as the device agrees, one that may pass is tried again, and one
that fails says why."""
import contextlib
import errno
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import device, errors, firewall, join, output, prompts, report, routes, runner, runs
from tools.autoflash import safety_locks, unlock
from tools.autoflash.phases import PhaseError
from tools.autoflash.routes import espressif, update
from tools.autoflash.routes.espressif import bundle, evidence, tasmota
from tools.autoflash.state import ATTEMPTED, DONE, FAILED, RunState


class ConfirmSkipTests(unittest.TestCase):
    """A step recorded as done is skipped only as far as the device agrees."""

    def skip(self, verifier):
        from types import SimpleNamespace
        verifiers = {} if verifier is None else {"D1": verifier}
        with mock.patch.dict(espressif.STEP_VERIFIERS, verifiers, clear=True), \
             mock.patch.object(prompts, "ask_yes_no") as asked, \
             mock.patch.object(output, "say") as said, mock.patch.object(output, "transient_begin"), \
             mock.patch.object(output, "transient_end"):
            skipped = runner.confirm_skip(SimpleNamespace(), "D1", "Install Tasmota")
        return skipped, asked, said.call_args.args[0] if said.called else ""

    def test_a_device_that_disagrees_has_the_step_done_again_unasked(self):
        # Asked "run E3 again?", a person converting their device could not
        # answer better than the step's own checks of the device.
        skipped, asked, said = self.skip(lambda ctx: (False, "the device says otherwise"))
        self.assertFalse(skipped)
        asked.assert_not_called()
        self.assertIn("the device says otherwise: doing it again", said)

    def test_f2_is_doubted_only_on_the_recovery_firmware_where_doing_it_again_helps(self):
        # Anywhere else F2 would wait for a recovery firmware that never comes.
        from types import SimpleNamespace
        self.assertIs(tasmota.STEP_VERIFIERS["F2"], tasmota.verify_canonical)
        on = SimpleNamespace(device=SimpleNamespace(is_safeboot=lambda: True))
        off = SimpleNamespace(device=SimpleNamespace(is_safeboot=lambda: False))
        self.assertFalse(tasmota.verify_canonical(on)[0])
        self.assertEqual(tasmota.verify_canonical(off), (True, ""))

    def test_a_step_with_no_check_is_taken_at_its_record(self):
        skipped, asked, said = self.skip(None)
        self.assertTrue(skipped)
        asked.assert_not_called()
        self.assertIn("recorded as done", said)

    def test_a_step_the_device_agrees_with_is_skipped(self):
        skipped, asked, said = self.skip(lambda ctx: (True, ""))
        self.assertTrue(skipped)
        asked.assert_not_called()
        self.assertIn("the device still agrees", said)

    def test_a_check_that_cannot_run_leaves_the_record_standing(self):
        for error in (device.DeviceError("refused"), OSError("unreachable"), ValueError("bad reply")):
            def check(ctx, error=error):
                raise error
            skipped, asked, said = self.skip(check)
            self.assertTrue(skipped, error)
            asked.assert_not_called()
            self.assertIn("could not re-check", said)


class WhatToExpectTests(unittest.TestCase):
    """Above a run's first stage, a conversion's or a readout's: one note."""

    def said(self):
        rows = []
        with mock.patch.object(runner.output, "tell", side_effect=lambda text="": rows.append(text)):
            runner.say_what_to_expect()
        return rows

    def test_two_rows_between_blank_ones(self):
        rows = self.said()
        self.assertEqual((rows[0], rows[-1]), ("", ""))   # a blank row above and below
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[1].startswith("Usually under 10 minutes"))
        self.assertTrue(rows[2].endswith("until the last stage completes."))
        # Fits an 80-column window with the margin, unwrapped by it.
        self.assertLess(max(len(row) for row in rows), 80 - len(runner.output.MARGIN))


class RunStepsTests(unittest.TestCase):
    def test_a_full_disk_is_said_plainly_and_any_other_error_is_unexpected(self):
        for code in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
            with self.subTest(code=code):
                self.assertIn("free some disk space", runner.unexpected(OSError(code, "no room")))
        self.assertIn("something unexpected", runner.unexpected(OSError(errno.EACCES, "refused")))
        self.assertIn("something unexpected", runner.unexpected(KeyError("x")))

    """The run's loop: what it skips, carries on, asks about and stops at."""

    STEPS = [("S1", "First", None), ("S2", "Second", None), ("S3", "Third", None)]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = RunState.open(self.root)

    def run_steps(self, *, codes=None, check=None, answer=0, unattended=False, skip=True, dry_run=False,
                  research_bridge=False, **overrides):
        """run_steps over STEPS, each step's code from `codes` (0 if not
        given), S2's evidence if interrupted, and the operator's answer:
        what it returned, the steps it ran, and the stand-ins, each of which
        `overrides` can replace."""
        from types import SimpleNamespace
        codes = codes or {}
        ran = []
        stand_ins = {
            "restore_left_locks": mock.Mock(), "hold_device": mock.Mock(), "unlock_tasmota": mock.Mock(),
            "settle_route": mock.Mock(return_value=("tasmota", "sonoff")), "apply_target": mock.Mock(),
            "bundle_of_this_version": mock.Mock(), "wait_while_device_busy": mock.Mock(),
            "begin_stage": mock.Mock(), "confirm_skip": mock.Mock(return_value=skip),
            "execute_with_retries": mock.Mock(side_effect=lambda ctx, step: ran.append(step) or codes.get(step, 0)),
            "stop_here_after_ctrl_c": mock.Mock(return_value=False), "close_firewall_rules": mock.Mock(),
            "bring_up_to_date": mock.Mock(return_value=False),
            "tell_how_to_carry_on": mock.Mock(), "invite_report": mock.Mock(),
            "device_page_url": mock.Mock(return_value=None), "rerun_on_tasmota": mock.Mock(return_value=0),
            "ask_choice": mock.Mock(return_value=answer),
            "say": mock.Mock(), "tell": mock.Mock(), "section": mock.Mock(),
            "output": mock.Mock(waited=mock.Mock(return_value=0.0)),
        } | overrides
        owners = {"restore_left_locks": safety_locks, "hold_device": runs, "unlock_tasmota": unlock,
                  "settle_route": routes, "apply_target": routes, "begin_stage": routes,
                  "bundle_of_this_version": bundle, "wait_while_device_busy": join,
                  "close_firewall_rules": firewall, "invite_report": report, "device_page_url": report,
                  "ask_choice": prompts, "section": prompts, "say": output, "tell": output,
                  "run_source": runs}
        with contextlib.ExitStack() as patches:
            for name, stand_in in stand_ins.items():
                patches.enter_context(mock.patch.object(owners.get(name, runner), name, stand_in))
            patches.enter_context(mock.patch.object(routes, "STEPS", self.STEPS))
            patches.enter_context(mock.patch.dict(evidence.ATTEMPTED_EVIDENCE,
                                                  {} if check is None else {"S2": check}, clear=True))
            patches.enter_context(mock.patch.object(prompts, "UNATTENDED", unattended))
            patches.enter_context(mock.patch.object(prompts, "answered_unattended"))
            self.ctx = SimpleNamespace(state=self.state, research_bridge=research_bridge,
                                       bridge=SimpleNamespace(name="the bridge"), dry_run=dry_run)
            code = runner.run_steps(self.ctx)
        return code, ran, stand_ins

    def test_a_step_gone_back_to_is_drawn_afresh_with_nothing_of_why(self):
        # For a person who knows nothing of the steps: a step a later one found
        # had not taken is done again unseen, as if the first time.
        import io
        import os
        from tools.repo import unattended_run

        class Terminal(io.StringIO):
            def isatty(self):
                return True
        found = []

        def execute(ctx, step):
            number = [name for name, _, _ in self.STEPS].index(step) + 1
            output.stage_begin(number, dict((name, title) for name, title, _ in self.STEPS)[step])
            if step == "S3" and not found:
                found.append(step)
                output.stage_end("not installed")
                output.tell("S2's firmware was not installed. Doing S2 again.")
                ctx.go_back = "S2"
                return 0
            output.stage_end("complete")
            return 0
        terminal = Terminal()
        with mock.patch("sys.stdout", terminal), mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
            output.start(self.root / "run.log", verbose=False)
            try:
                code, _, _ = self.run_steps(execute_with_retries=mock.Mock(side_effect=execute), output=output,
                                            say=output.say, tell=output.tell, skip=False)
            finally:
                output.finish()
        screen = unattended_run.Screen(100, 40)
        screen.feed(terminal.getvalue().replace("\n", "\r\n"))
        rows = [row for row in screen.render(colour=False).splitlines()
                if unattended_run.STAGE_ROW.match(row)]
        self.assertEqual(code, 0)
        self.assertEqual([row.split(" - ")[-1] for row in rows], ["Complete"] * 3, rows)
        self.assertNotIn("Doing S2 again", screen.render(colour=False))
        drawn = output.ANSI_RE.sub("", terminal.getvalue())
        self.assertIn("Stage 2 - Second - Retrying", drawn)   # each step done again says so
        self.assertIn("Stage 3 - Third - Retrying", drawn)
        self.assertNotIn("Stage 1 - First - Retrying", drawn)
        self.assertIn("Doing S2 again", (self.root / "run.log").read_text(encoding="utf-8"))  # kept

    def test_every_step_runs_in_turn_and_the_rules_are_closed(self):
        code, ran, stand_ins = self.run_steps()
        self.assertEqual((code, ran), (0, ["S1", "S2", "S3"]))
        stand_ins["close_firewall_rules"].assert_called_once()
        stand_ins["invite_report"].assert_called_once()
        stand_ins["bundle_of_this_version"].assert_not_called()  # a new run's bundle is the one built

    def test_before_the_first_step_the_locks_are_restored_tasmota_unlocked_and_the_device_let_settle(self):
        _, _, stand_ins = self.run_steps()
        stand_ins["restore_left_locks"].assert_called_once()
        stand_ins["unlock_tasmota"].assert_called_once()
        stand_ins["wait_while_device_busy"].assert_called_once()
        # A converted device's firmware is chosen at A3, not before the run starts.
        self.assertIs(stand_ins["settle_route"].call_args.kwargs.get("defer"), True)

    def test_the_report_is_offered_the_runs_time_without_the_keyboard_waits(self):
        clock = iter([100.0])
        with mock.patch.object(time, "monotonic", side_effect=lambda: next(clock, 160.0)):
            _, _, stand_ins = self.run_steps(output=mock.Mock(waited=mock.Mock(return_value=20.0)))
        self.assertEqual(stand_ins["invite_report"].call_args.args[1], 40.0)

    def test_an_update_ends_saying_it_was_an_update(self):
        with mock.patch.object(update, "versions", return_value=("14.0.0", "15.0.0")):
            _, _, stand_ins = self.run_steps(run_source=mock.Mock(return_value="outdated"))
        shown = " ".join(str(call.args[0]) for call in stand_ins["output"].show.call_args_list)
        self.assertIn("Update complete!", shown)
        self.assertNotIn("Conversion complete", shown)

    def test_a_research_bridge_stops_the_run_once_it_is_installed(self):
        with mock.patch.object(routes.FAMILY, "POINT_OF_NO_RETURN", "S2"), \
                mock.patch.object(runner.names, "our_setup_network", return_value="autoflash-setup"):
            code, ran, stand_ins = self.run_steps(research_bridge=True)
        self.assertEqual((code, ran), (0, ["S1", "S2"]))
        stand_ins["close_firewall_rules"].assert_called_once()
        stand_ins["invite_report"].assert_not_called()

    def test_a_dry_run_ends_saying_nothing_was_written_and_asks_for_no_report(self):
        with mock.patch.object(routes.FAMILY, "READ_ONLY_STEPS", {"S1", "S2", "S3"}):
            code, ran, stand_ins = self.run_steps(dry_run=True)
        self.assertEqual((code, ran), (0, ["S1", "S2", "S3"]))
        stand_ins["invite_report"].assert_not_called()
        shown = " ".join(str(call.args[0]) for call in stand_ins["output"].show.call_args_list)
        self.assertIn("Dry run complete: nothing was written", shown)
        self.assertNotIn("Conversion complete", shown)

    def test_a_dry_run_stops_before_the_first_step_that_writes(self):
        # Espressif's C2 sent the installer on a dry run: no step that writes is run at all.
        for family in routes.FAMILIES.values():
            with self.subTest(family=family.__name__), mock.patch.object(routes, "FAMILY", family), \
                    mock.patch.object(family, "READ_ONLY_STEPS", {"S1"}):
                code, ran, stand_ins = self.run_steps(dry_run=True)
                self.assertEqual((code, ran), (0, ["S1"]))
                said = " ".join(str(call.args[0]) for call in stand_ins["output"].say.call_args_list)
                self.assertIn("[dry-run] stopping before S2", said)
                stand_ins["invite_report"].assert_not_called()

    def test_each_familys_dry_run_reads_only_the_steps_before_its_first_write(self):
        for family in routes.FAMILIES.values():
            self.assertNotIn(family.POINT_OF_NO_RETURN, family.READ_ONLY_STEPS)
            self.assertFalse(family.READ_ONLY_STEPS & family.DESTRUCTIVE_STEPS, family.__name__)

    def test_a_device_put_back_ends_saying_so_not_converted(self):
        self.state.set_input("put_back", {"came_from": "esphome", "why": "no identity"})
        code, _, stand_ins = self.run_steps()
        self.assertEqual(code, 0)
        shown = " ".join(str(call.args[0]) for call in stand_ins["output"].show.call_args_list)
        self.assertIn("Sonoff's firmware could not be put back, so the device is back on", shown)
        self.assertNotIn("Conversion complete", shown)

    def test_a_ctrl_c_just_before_the_first_stage_still_counts(self):
        # Seen on the emulator: the first press forgotten here, so the second
        # only asked, at the end of its stage, instead of stopping the run.
        from tools.autoflash import interrupt

        def press(*_, **__):
            interrupt._on_interrupt(None, None)
            return "tasmota", "sonoff"
        self.addCleanup(interrupt.clear)
        with mock.patch.object(interrupt, "_say"), self.assertRaises(KeyboardInterrupt):
            self.run_steps(settle_route=mock.Mock(side_effect=press), execute_with_retries=mock.Mock(side_effect=press))

    def test_a_route_refused_stops_before_anything_is_done(self):
        code, ran, stand_ins = self.run_steps(settle_route=mock.Mock(side_effect=errors.Abort("no device")))
        self.assertEqual((code, ran), (2, []))
        stand_ins["apply_target"].assert_not_called()

    def test_kept_on_tasmota_at_a3_checks_its_meter(self):
        def keep(ctx, step):
            ctx.finished = routes.NothingToDo("on Tasmota")
            return 0
        code, ran, stand_ins = self.run_steps(execute_with_retries=mock.Mock(side_effect=keep),
                                              run_source=mock.Mock(return_value="tasmota"))
        self.assertEqual(code, 0)
        stand_ins["rerun_on_tasmota"].assert_called_once()
        stand_ins["invite_report"].assert_not_called()
        stand_ins["close_firewall_rules"].assert_called_once()

    def test_a_run_ended_by_choice_on_tasmota_but_not_kept_on_it_checks_no_meter(self):
        # A readout chosen instead (errors.Finished): the run just ends.
        def read_out(ctx, step):
            ctx.finished = errors.Finished("read out")
            return 0
        code, _, stand_ins = self.run_steps(execute_with_retries=mock.Mock(side_effect=read_out),
                                            run_source=mock.Mock(return_value="tasmota"))
        self.assertEqual(code, 0)
        stand_ins["rerun_on_tasmota"].assert_not_called()
        stand_ins["close_firewall_rules"].assert_called_once()

    def test_nothing_to_do_on_tasmota_checks_its_meter(self):
        code, ran, stand_ins = self.run_steps(settle_route=mock.Mock(side_effect=routes.NothingToDo("on Tasmota")),
                                              run_source=mock.Mock(return_value="tasmota"))
        self.assertEqual((code, ran), (0, []))
        stand_ins["rerun_on_tasmota"].assert_called_once()

    def test_steps_done_and_agreed_with_are_skipped(self):
        self.state.mark("S1", DONE)
        code, ran, stand_ins = self.run_steps()
        self.assertEqual((code, ran), (0, ["S2", "S3"]))
        stand_ins["confirm_skip"].assert_called_once()
        stand_ins["bundle_of_this_version"].assert_called_once()  # a resumed run's bundle is checked

    def test_a_step_done_but_doubted_runs_again(self):
        self.state.mark("S1", DONE)
        _, ran, _ = self.run_steps(skip=False)
        self.assertEqual(ran, ["S1", "S2", "S3"])

    def test_a_resumed_run_whose_bundle_is_refused_stops_there(self):
        self.state.mark("S1", DONE)
        code, ran, _ = self.run_steps(bundle_of_this_version=mock.Mock(side_effect=errors.Abort("changed")))
        self.assertEqual((code, ran), (2, []))

    def test_an_interrupted_step_the_device_shows_finished_is_not_run_again(self):
        self.state.mark("S2", ATTEMPTED)
        code, ran, stand_ins = self.run_steps(check=lambda ctx: (True, "it answers as Tasmota"))
        self.assertEqual((code, ran), (0, ["S1", "S3"]))
        self.assertTrue(RunState.open(self.root).is_done("S2"))
        stand_ins["ask_choice"].assert_not_called()

    def test_an_interrupted_step_shown_unfinished_is_done_again_without_asking(self):
        # Seen: asked, "it did finish" for a B2 that had not left the run
        # waiting for good on an interim firmware never sent.
        self.state.mark("S2", ATTEMPTED)
        _, ran, stand_ins = self.run_steps(check=lambda ctx: (False, "it still runs Sonoff"))
        self.assertEqual(ran, ["S1", "S2", "S3"])
        stand_ins["ask_choice"].assert_not_called()

    def test_an_interrupted_step_whose_check_fails_is_done_again_without_asking(self):
        self.state.mark("S2", ATTEMPTED)

        def check(ctx):
            raise device.DeviceError("no answer")
        _, ran, stand_ins = self.run_steps(check=check)
        self.assertEqual(ran, ["S1", "S2", "S3"])
        stand_ins["ask_choice"].assert_not_called()

    def test_an_interrupted_step_with_no_check_is_done_again_without_asking(self):
        self.state.mark("S2", ATTEMPTED)
        _, ran, stand_ins = self.run_steps()
        self.assertEqual(ran, ["S1", "S2", "S3"])
        stand_ins["ask_choice"].assert_not_called()

    def test_unattended_an_interrupted_step_is_done_again_without_asking(self):
        self.state.mark("S2", ATTEMPTED)
        _, ran, stand_ins = self.run_steps(unattended=True)
        self.assertEqual(ran, ["S1", "S2", "S3"])
        stand_ins["ask_choice"].assert_not_called()

    def test_a_step_sent_back_to_runs_again_and_the_run_carries_on_from_it(self):
        sent_back = []

        def execute(ctx, step):
            ran.append(step)
            if step == "S2" and not sent_back:
                sent_back.append(step)
                ctx.go_back = "S1"   # as runner._execute does on errors.GoBack
            return 0
        ran = []
        code, _, _ = self.run_steps(execute_with_retries=mock.Mock(side_effect=execute))
        self.assertEqual((code, ran), (0, ["S1", "S2", "S1", "S2", "S3"]))

    def test_a_failing_step_stops_the_run_and_says_how_to_carry_on(self):
        code, ran, stand_ins = self.run_steps(codes={"S2": 1})
        self.assertEqual((code, ran), (1, ["S1", "S2"]))
        stand_ins["close_firewall_rules"].assert_called_once()
        stand_ins["tell_how_to_carry_on"].assert_called_once()
        stand_ins["invite_report"].assert_not_called()

    def test_quitting_leaves_the_clean_up_to_main(self):
        code, ran, stand_ins = self.run_steps(codes={"S2": 130})
        self.assertEqual((code, ran), (130, ["S1", "S2"]))
        stand_ins["close_firewall_rules"].assert_not_called()

    def test_a_run_begun_here_records_its_version_and_one_carried_on_does_not(self):
        # For its report: another version carrying a run on is where resuming goes wrong.
        self.run_steps()
        self.assertEqual(self.state.input("begun_on"), runner.__version__)
        self.state.set_input("begun_on", None)
        self.state.mark("S1", DONE)
        self.run_steps()   # carried on, begun before versions were recorded: unknown
        self.assertIsNone(self.state.input("begun_on"))

    def test_a_ctrl_c_confirmed_after_a_stage_stops_there(self):
        code, ran, stand_ins = self.run_steps(stop_here_after_ctrl_c=mock.Mock(side_effect=lambda step: step == "S1"))
        self.assertEqual((code, ran), (130, ["S1"]))
        stand_ins["close_firewall_rules"].assert_called_once()
        self.assertEqual(self.ctx.stopped_by, "ctrl-c")   # for its report, not a quit at a question


class CarryOnTests(unittest.TestCase):
    def test_how_to_carry_on_links_nowhere(self):
        # The report sent says how it went: no Discussions to ask in, and the
        # issue link comes with the stopped run's report (report.offer_report).
        with mock.patch.object(output, "tell") as told, mock.patch.object(output, "show_links") as links, \
                mock.patch.object(output, "show_link") as link:
            runner.tell_how_to_carry_on()
        links.assert_not_called()
        link.assert_not_called()
        self.assertNotIn("discussion", " ".join(str(c.args) for c in told.call_args_list).lower())


class RetryTests(unittest.TestCase):
    """Safe steps that failed for a reason that may pass are run again."""

    def run_with(self, step, errors):
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state, dry_run=False)
        outcomes = iter(errors)

        def fake_execute(_, name):
            error = next(outcomes)
            if error is None:
                state.mark(name, DONE)
                return 0
            state.mark(name, FAILED, error=error)
            return 1
        with mock.patch.object(runner, "execute", side_effect=fake_execute) as ran, \
             mock.patch.object(time, "sleep"), mock.patch.object(output, "tell"):
            code = runner.execute_with_retries(ctx, step)
        return code, ran.call_count

    def test_a_network_failure_is_tried_again(self):
        code, runs = self.run_with("E2", ["stage did not pass: the device says: POST /capture/staged/22 "
                                          "returned HTTP -1 after 21 attempts", None])
        self.assertEqual((code, runs), (0, 2))

    def test_a_wifi_drop_is_tried_again(self):
        # The device now gives up after 20s, and this computer waits out the rest.
        code, runs = self.run_with("E2", ["stage did not pass: the device says: migration_refused: "
                                          "the device's Wi-Fi went down and was not back within "
                                          "20 seconds", None])
        self.assertEqual((code, runs), (0, 2))

    def test_three_attempts_at_most(self):
        code, runs = self.run_with("E1", ["timed out"] * 5)
        self.assertEqual((code, runs), (1, 3))

    def test_a_refusal_is_not_tried_again(self):
        code, runs = self.run_with("E2", ["stage did not pass: the device says: "
                                          "migration_refused: NVS changed since preflight"])
        self.assertEqual((code, runs), (1, 1))

    def test_the_commit_is_never_tried_again(self):
        code, runs = self.run_with("E3", ["timed out", None])
        self.assertEqual((code, runs), (1, 1))

    def test_a_step_done_keeps_what_it_found_and_its_tries(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state, dry_run=False)
        title, _ = routes.STEP_INDEX["E1"]
        with mock.patch.dict(routes.STEP_INDEX, {"E1": (title, mock.Mock(return_value={"read": 7}))}), \
                mock.patch.object(routes, "begin_stage"), mock.patch.object(output, "stage_end"), \
                mock.patch.object(output, "say"), mock.patch.object(output, "event") as event:
            self.assertEqual(runner.execute_with_retries(ctx, "E1"), 0)
        self.assertTrue(ctx.stage_begun)   # a report is worth asking for (cli.main)
        # As the fault suites follow it (output.EVENTS).
        self.assertEqual(event.call_args_list, [mock.call("began", step="E1"), mock.call("done", step="E1")])
        self.assertEqual(state.data["captures"]["E1.read"], 7)
        self.assertEqual(state.data["captures"]["E1.attempts"], 1)

    def test_a_phase_with_no_result_keeps_the_commit_attempted(self):
        from types import SimpleNamespace
        from unittest import mock

        for step, expected in (("E3", "attempted"), ("E2", FAILED)):
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            state = RunState.open(Path(directory.name))
            title, _ = routes.STEP_INDEX[step]
            failing = mock.Mock(side_effect=PhaseError("the device stopped the commit without reaching this computer"))
            with mock.patch.dict(routes.STEP_INDEX, {step: (title, failing)}), \
                 mock.patch.object(routes, "begin_stage"), \
                 mock.patch.object(output, "stage_end"), \
                 mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
                runner.execute(SimpleNamespace(state=state, dry_run=False), step)
            self.assertEqual(state.status(step), expected, step)


class StageTimingTests(unittest.TestCase):
    """Every stage's time is kept for the report, however it ends."""

    def test_a_stage_keeps_how_long_it_waited_on_the_person_apart_from_its_own_time(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        waited = iter([100.0, 145.0, 145.0])   # at its start, then twice at its end: 45 s at its questions
        with mock.patch.object(output, "waited", side_effect=lambda: next(waited)), \
                mock.patch.object(runner, "_execute", return_value=0):
            runner.execute(SimpleNamespace(state=state), "E1")
        self.assertEqual(state.data["captures"]["E1.waited"], 45)

    def test_a_setup_network_joined_or_a_power_cycle_is_kept_against_its_stage_over_its_tries(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("power_cycles", {"by hand": 1})   # an earlier stage's
        state.set_input(join.OWN_NETWORK, [join.NOT_GIVEN])
        reasons = iter([join.SETTINGS_LOST, join.NOT_JOINED])

        def joins_and_cycles(ctx, _step):
            join.note_own_network(ctx, next(reasons))
            ctx.state.set_input("power_cycles", {"by hand": ctx.state.input("power_cycles")["by hand"] + 1})
            return 0

        ctx = SimpleNamespace(state=state)
        with mock.patch.object(runner, "_execute", side_effect=joins_and_cycles):
            runner.execute(ctx, "F4")
            runner.execute(ctx, "F4")   # tried again
        with mock.patch.object(runner, "_execute", return_value=0):
            runner.execute(ctx, "G1")   # neither
        captures = state.data["captures"]
        # The first reason in the stage: what brought it to the setup network.
        self.assertEqual((captures[f"F4.{join.OWN_NETWORK}"], captures["F4.power_cycles"]), (join.SETTINGS_LOST, 2))
        self.assertNotIn("G1.power_cycles", captures)
        self.assertNotIn(f"G1.{join.OWN_NETWORK}", captures)

    def test_where_an_unexpected_error_came_from_is_the_tools_own_code_alone(self):
        from tools.autoflash import records as tool_code
        try:
            tool_code.folder_name("../etc")   # raises in the tool's code
        except ValueError as exc:
            site = runner.crash_site(exc)
        self.assertRegex(site, r"^ValueError at tools/autoflash/records\.py:\d+$")
        try:
            raise KeyError("only this test's code")
        except KeyError as exc:
            self.assertIsNone(runner.crash_site(exc), "no line of the tool's")

    def test_a_stage_that_fails_unexpectedly_keeps_where_and_one_that_is_refused_does_not(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state, dry_run=False, last_failure="", crash_site="stale")
        title, _ = routes.STEP_INDEX["E1"]
        for raised, expected in ((KeyError("model"), r"^KeyError at tools/autoflash/"), (errors.Abort("refused"), None)):
            def handler(_ctx, raised=raised):
                raise raised   # from a test, so the tool's last line is the runner's
            with self.subTest(raised=type(raised).__name__), \
                    mock.patch.dict(routes.STEP_INDEX, {"E1": (title, handler)}), \
                    mock.patch.object(routes, "begin_stage"), mock.patch.object(output, "stage_end"), \
                    mock.patch.object(output, "say"), mock.patch.object(output, "tell"), \
                    mock.patch.object(output, "log_only"):
                self.assertEqual(runner.execute(ctx, "E1"), 1)
                if expected:
                    self.assertRegex(ctx.crash_site, expected)
                else:
                    self.assertIsNone(ctx.crash_site)

    def test_a_stage_that_fails_has_its_time_and_the_devices_busy_time_kept(self):
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state, dry_run=False)

        def stage(context):
            context.last_runner = SimpleNamespace(busy_seconds=41.5)
            raise PhaseError("the device stopped the stage without reaching this computer")
        title, _ = routes.STEP_INDEX["E2"]
        with mock.patch.dict(routes.STEP_INDEX, {"E2": (title, stage)}), \
             mock.patch.object(routes, "begin_stage"), \
             mock.patch.object(output, "stage_end"), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(runner.execute(ctx, "E2"), 1)
        captures = state.data["captures"]
        self.assertIsInstance(captures["E2.seconds"], int)
        self.assertEqual(captures["E2.device_busy_seconds"], 41.5)
        self.assertIn("without reaching", ctx.last_failure)

        # A quicker try after it keeps the longest: how near the device came to stopping.
        def quicker(context):
            context.last_runner = SimpleNamespace(busy_seconds=3.0)
            raise PhaseError("the device did not fetch the stage instructions")
        with mock.patch.dict(routes.STEP_INDEX, {"E2": (title, quicker)}), \
             mock.patch.object(routes, "begin_stage"), \
             mock.patch.object(output, "stage_end"), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            runner.execute(ctx, "E2")
        self.assertEqual(state.data["captures"]["E2.device_busy_seconds"], 41.5)


class GoBackTests(unittest.TestCase):
    """A step that finds an earlier one did not take on the device sends the run
    back to it, unasked, a few times at most."""

    def setUp(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = RunState.open(Path(directory.name))
        self.ctx = SimpleNamespace(state=self.state, dry_run=False, go_back=None)

    def execute(self):
        def stage(context):
            raise errors.GoBack("E1", "ESPHome was not installed: the device restarted on the firmware it had",
                                "not installed")
        title, _ = routes.STEP_INDEX["E2"]
        with mock.patch.dict(routes.STEP_INDEX, {"E2": (title, stage)}), \
             mock.patch.object(routes, "begin_stage"), mock.patch.object(output, "stage_end") as ended, \
             mock.patch.object(output, "tell") as told, mock.patch.object(output, "say"), \
             mock.patch.object(output, "event") as self.event:
            return runner.execute(self.ctx, "E2"), ended, told

    def events(self):
        return [(c.args[0], c.kwargs) for c in self.event.call_args_list]

    def test_the_run_goes_back_to_the_step_not_taken(self):
        self.state.mark("E1", DONE)
        code, ended, told = self.execute()
        self.assertEqual((code, self.ctx.go_back), (0, "E1"))
        self.assertEqual((self.state.status("E1"), self.state.status("E2")), ("pending", "pending"))
        ended.assert_called_once_with("not installed")   # not "stopped": the run goes on
        self.assertIn(f"doing stage {routes.stage_number('E1')} again", told.call_args.args[0])
        self.assertNotIn("E1", told.call_args.args[0])   # a step's code means nothing to the person
        # As the fault suites follow it (output.EVENTS).
        self.assertEqual(self.events(), [("began", {"step": "E2"}), ("again", {"step": "E1", "restarted": False})])

    def test_up_to_the_limit_it_is_sent_again_without_a_restart(self):
        self.state.set_input("device_ip", "192.0.2.77")
        self.state.capture("E1.sent_again", runner.GO_BACK_LIMIT - 1)
        with mock.patch.object(runner.restart, "restart") as restarted:
            code, _, _ = self.execute()
        restarted.assert_not_called()
        self.assertEqual((code, self.ctx.go_back), (0, "E1"))
        self.assertEqual(self.state.data["captures"]["E1.sent_again"], runner.GO_BACK_LIMIT)

    def test_one_that_still_does_not_take_stops_the_run(self):
        self.state.capture("E1.sent_again", runner.GO_BACK_LIMIT)
        code, ended, _ = self.execute()
        self.assertEqual((code, self.ctx.go_back), (1, None))
        self.assertEqual(self.state.status("E2"), "failed")
        ended.assert_called_once_with("stopped")
        self.assertIn(f"after it was sent {runner.GO_BACK_LIMIT + 1} times", self.ctx.last_failure)


class GoBackRestartTests(unittest.TestCase):
    """A step sent again and again without taking: before the run stops, the
    device - answering, but not taking it - is restarted, once, and the step
    sent afresh."""
    execute, events = GoBackTests.execute, GoBackTests.events

    def setUp(self):
        GoBackTests.setUp(self)
        self.state.set_input("device_ip", "192.0.2.77")
        self.state.capture("E1.sent_again", runner.GO_BACK_LIMIT)

    def test_the_device_restarted_the_step_is_sent_afresh(self):
        self.state.mark("E1", DONE)
        with mock.patch.object(runner.restart, "restart", return_value=True) as restarted:
            code, ended, told = self.execute()
        restarted.assert_called_once_with("192.0.2.77")
        self.assertEqual((code, self.ctx.go_back), (0, "E1"))
        self.assertEqual((self.state.status("E1"), self.state.status("E2")), ("pending", "pending"))
        self.assertEqual(self.state.data["captures"]["E1.sent_again"], 0)
        self.assertIn(f"restarted the device; doing stage {routes.stage_number('E1')} again",
                      told.call_args.args[0])
        self.assertEqual(self.events()[-1], ("again", {"step": "E1", "restarted": True}))

    def test_once_only_then_the_run_stops(self):
        self.state.capture("E1.restarted", True)
        with mock.patch.object(runner.restart, "restart") as restarted:
            code, ended, _ = self.execute()
        restarted.assert_not_called()
        self.assertEqual(code, 1)
        ended.assert_called_once_with("stopped")

    def test_one_that_cannot_be_restarted_stops_the_run_asking_for_it(self):
        with mock.patch.object(runner.restart, "restart", return_value=False):
            code, _, _ = self.execute()
        self.assertEqual(code, 1)
        self.assertIn("Switch the device off and on", self.ctx.last_failure)


class ScreenTests(unittest.TestCase):
    """However a stage ends, it leaves the screen as it should once the run
    moves on: one line for it, its outcome, and nothing of a try that failed
    (tools/repo/unattended_run.py, stage_problems).  Drawn as on a terminal
    and read back as one shows it."""

    SIZE = (100, 40)

    def setUp(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "run.log"
        self.state = RunState.open(Path(directory.name))
        self.ctx = SimpleNamespace(state=self.state, dry_run=False, go_back=None, finished=False)

    def screen(self, stages):
        """The screen once each of `stages` - (step, handler) - is run with
        its retries, as run_steps() does, and then one that passes."""
        import io
        import os
        from tools.repo import unattended_run

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        handlers = {step: (routes.STEP_INDEX[step][0], handler) for step, handler in stages}
        handlers["E3"] = (routes.STEP_INDEX["E3"][0], lambda ctx: {})
        with mock.patch("sys.stdout", terminal), mock.patch("sys.stdin", io.StringIO()),              mock.patch.dict(os.environ, {"NO_COLOR": ""}), mock.patch.object(output, "colour_ok", return_value=True),              mock.patch("shutil.get_terminal_size", return_value=os.terminal_size(self.SIZE)),              mock.patch.dict(routes.STEP_INDEX, handlers),              mock.patch.object(runner, "set_up_if_erased"), mock.patch.object(runner, "wait_for_device_back"),              mock.patch.object(runner.time, "sleep"), mock.patch.object(firewall, "firewall_hint", return_value=[]):
            output.start(self.log, verbose=False)
            try:
                for step, _ in stages:
                    runner.execute_with_retries(self.ctx, step)
                runner.execute_with_retries(self.ctx, "E3")
            finally:
                output.finish()
        self.drawn = terminal.getvalue()
        screen = unattended_run.Screen(*self.SIZE)
        screen.feed(self.drawn.replace("\n", "\r\n"))  # as a terminal's line discipline does
        return screen.render(colour=False)

    def test_a_try_that_failed_is_gone_once_the_stage_is_tried_again(self):
        # As a run on 2026-11-08 left it: the device's console line under the
        # stage, the stage stopped, the note it would be tried again - and
        # the stage again under them all.
        tries = iter([PhaseError("the device stopped the stage without reaching this computer"), None])

        def stage(ctx):
            failure = next(tries)
            if failure:
                output.tell("    The device's console: 00:00:00.328 BRY: Berry initialized, RAM used 3885 bytes")
                raise failure
            return {}
        screen = self.screen([("E2", stage)])
        from tools.repo import unattended_run
        self.assertEqual(unattended_run.stage_problems(screen), [], screen)
        for gone in ("Stopped", "console", "FAILED", "trying again"):
            self.assertNotIn(gone, screen)
        self.assertEqual([row.split(" - ")[-1] for row in screen.splitlines() if row.strip()],
                         ["Complete", "Complete"], screen)
        self.assertIn("trying again", self.log.read_text(encoding="utf-8"))  # the log keeps it

    def test_a_run_that_turned_into_a_readout_says_nothing_after_its_report(self):
        # readout.from_run has shown the readout's ending and report: an empty
        # Finished adds no line under them.
        for said in ("", "Read out instead."):
            def stage(ctx):
                raise errors.Finished(said)
            with self.subTest(said=said):
                rows = [row.strip() for row in self.screen([("E2", stage)]).splitlines() if row.strip()]
                self.assertEqual("Read out instead." in rows, bool(said))
                self.assertEqual(len(rows), 3 if said else 2)   # E2's and E3's lines, and what it said

    def test_a_stage_tried_again_says_retrying_while_it_runs(self):
        tries = iter([PhaseError("the device stopped the stage without reaching this computer"), None])

        def stage(ctx):
            failure = next(tries)
            if failure:
                raise failure
            output.tell("    A note while it runs again")   # its line drawn, as it was then
            return {}
        screen = self.screen([("E2", stage)])
        self.assertRegex(output.ANSI_RE.sub("", self.drawn), r"Staging startup firmware +- Retrying")
        self.assertNotIn("Retrying", screen)   # once done, it says Complete like any other

    def test_the_last_try_stays_to_say_why_the_run_stopped(self):
        def stage(ctx):
            raise PhaseError("the device stopped the stage without reaching this computer")
        screen = self.screen([("E2", stage)])
        from tools.repo import unattended_run
        self.assertEqual(unattended_run.stage_problems(screen), [], screen)
        self.assertEqual(screen.count("Stopped"), 1, screen)
        self.assertEqual(screen.count("FAILED"), 1, screen)

    def test_every_way_a_stage_ends_leaves_one_line_before_the_next(self):
        from tools.autoflash.errors import Abort
        from tools.autoflash.phases import DeviceNeverConnected

        def raising(exc, note=True):
            def stage(ctx):
                if note:
                    output.tell("    A note shown under the stage")
                raise exc
            return stage
        ways = {
            "passed": lambda ctx: {},
            "passed, with a note": lambda ctx: output.tell("    A note") or {},
            "skipped": lambda ctx: {"stage_outcome": "skipped"},
            "refused": raising(Abort("the device refused")),
            "never connected": raising(DeviceNeverConnected("the device never connected")),
            "went wrong": raising(KeyError("unexpected")),
            "network": raising(ConnectionResetError("connection reset")),
            "went back": raising(errors.GoBack("E1", "ESPHome was not installed", "not installed")),
        }
        from tools.repo import unattended_run
        for way, handler in ways.items():
            with self.subTest(way):
                self.setUp()
                screen = self.screen([("E2", handler)])
                self.assertEqual(unattended_run.stage_problems(screen), [], screen)
                stages = [row for row in screen.splitlines() if unattended_run.STAGE_ROW.match(row)]
                self.assertNotIn("Underway", stages[-1], screen)


class BringUpToDateTests(unittest.TestCase):
    """A device the route has just taken to its firmware, but on an earlier
    release of it than this version installs, goes on to that firmware's
    update in the same run (bring_up_to_date)."""

    def bring(self, family_name, firmware, release, *, prepared=True, dry_run=False):
        from types import SimpleNamespace
        family = routes.FAMILIES[family_name]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_kind", "sonoff-s60" if family_name == "espressif" else "sonoff-s61")
        ctx = SimpleNamespace(state=state, dry_run=dry_run, esphome_firmware=None)
        routes.apply_target(firmware, "sonoff", family)
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        prepare = mock.Mock(return_value=prepared)
        with mock.patch.dict(update.RELEASE, {firmware: lambda ctx: release}), \
                mock.patch.dict(family.__dict__, {"PREPARE_UPDATE": {firmware: prepare}}), \
                mock.patch.object(output, "say"):
            brought = runner.bring_up_to_date(ctx)
        return brought, ctx, prepare, family

    def test_each_firmware_on_an_earlier_release_goes_on_to_its_update(self):
        for family_name, firmware in (("espressif", "tasmota"), ("espressif", "esphome"),
                                      ("beken", "openbeken"), ("beken", "esphome")):
            with self.subTest(family=family_name, firmware=firmware):
                brought, ctx, prepare, family = self.bring(family_name, firmware, "0.0.1")
                self.assertTrue(brought)
                prepare.assert_called_once()
                steps = [step for step, _, _ in family.UPDATE_STEPS[firmware]]
                self.assertEqual([step for step, _, _ in routes.STEPS[-len(steps):]], steps)
                published = family.published_version(runner.kinds.of_run(ctx.state), firmware)
                self.assertEqual(update.versions(ctx), ("0.0.1", published))

    def test_a_device_on_this_release_is_left_as_it_is(self):
        published = routes.FAMILIES["espressif"].published_version(runner.kinds.SONOFF_S60, "esphome")
        brought, _, prepare, _ = self.bring("espressif", "esphome", published)
        self.assertFalse(brought)
        prepare.assert_not_called()
        self.assertNotIn("U1", routes.STEP_INDEX)

    def test_nor_when_what_it_needs_first_cannot_be_had(self):
        brought, _, _, _ = self.bring("espressif", "tasmota", "0.0.1", prepared=False)
        self.assertFalse(brought)
        self.assertNotIn("U5", routes.STEP_INDEX)

    def test_nor_a_device_back_on_sonoff_nor_a_dry_run(self):
        self.assertFalse(self.bring("espressif", "sonoff", "0.0.1")[0])
        self.assertFalse(self.bring("espressif", "esphome", "0.0.1", dry_run=True)[0])

    def test_the_run_goes_on_to_the_update_once_its_route_is_done(self):
        # As a run resumed after it stopped part way through the update: its
        # route done, the device still on the earlier release.
        ran = []

        def bring(ctx):
            routes.continue_with([("U9", "An update", None)])
            return True
        stand_ins = RunStepsTests()
        stand_ins.setUp()
        stand_ins.STEPS = list(RunStepsTests.STEPS)   # its own, to be added to
        with mock.patch.dict(routes.STEP_INDEX):
            code, ran, _ = stand_ins.run_steps(bring_up_to_date=mock.Mock(side_effect=bring))
        self.assertEqual((code, ran), (0, ["S1", "S2", "S3", "U9"]))


class DeviceBackTests(unittest.TestCase):
    """Before a step is tried again, a device that is not answering is waited for."""

    def wait(self, answers, d1_done=True, step=None):
        from types import SimpleNamespace
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_ip", "10.0.0.63")
        if d1_done:
            state.mark("D1", DONE)
        device = mock.Mock(answers=mock.Mock(side_effect=list(answers)))
        clock = iter(range(1000, 100_000, 3))   # not from 0: times are measured from the start
        with mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "tell") as told, \
             mock.patch.object(output, "progress"), \
             mock.patch.object(output, "progress_clear"), \
             mock.patch.object(runner, "set_up_if_erased", return_value=False) as self.looked:
            runner.wait_for_device_back(SimpleNamespace(state=state, device=device), step)
        return device, told

    def test_an_answering_device_is_tried_again_at_once(self):
        device, told = self.wait([True])
        told.assert_not_called()

    def test_a_device_back_after_a_router_restart_is_tried_again_then(self):
        device, told = self.wait([False] * 30 + [True])
        self.assertEqual(device.answers.call_count, 31)
        self.assertIn("answering again", told.call_args.args[0])

    def test_a_long_wait_says_it_is_waiting_for_the_device_to_rejoin_once(self):
        said = lambda told: " ".join(call.args[0] for call in told.call_args_list)
        device, told = self.wait([False] * 30 + [True], step="E2")
        self.assertEqual(said(told).count("Waiting for it to rejoin your Wi-Fi"), 1)
        device, told = self.wait([False] * 5 + [True], step="E2")
        self.assertNotIn("rejoin your Wi-Fi", said(told))  # back before it was worth saying
        # F4's official Tasmota may not have the Wi-Fi yet, so may have one.
        device, told = self.wait([False] * 30 + [True], step="F4")
        self.assertNotIn("rejoin your Wi-Fi", said(told))

    def test_one_that_never_answers_is_tried_anyway_after_five_minutes(self):
        device, told = self.wait([False] * 1000)
        self.assertIn("trying anyway", told.call_args.args[0])
        self.assertLess(device.answers.call_count, 1000)

    def test_while_it_waits_it_looks_for_a_device_that_lost_its_settings_now_and_then(self):
        # Each look scans for Wi-Fi networks: every ERASED_LOOK_EVERY seconds, not every ask.
        self.wait([False] * 1000, step="G1")
        self.assertTrue(self.looked.called)
        self.assertLessEqual(self.looked.call_count, runner.DEVICE_BACK_WAIT / runner.ERASED_LOOK_EVERY)

    def test_before_tasmota_nothing_is_waited_for(self):
        device, _ = self.wait([], d1_done=False)
        device.answers.assert_not_called()


class UnexpectedErrorTests(unittest.TestCase):
    """An error nothing anticipated is recorded and explained, not a traceback."""

    def run_step(self, step, error):
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        ctx = SimpleNamespace(state=state, dry_run=False)
        title, _ = routes.STEP_INDEX[step]
        failing = mock.Mock(side_effect=error)
        with mock.patch.dict(routes.STEP_INDEX, {step: (title, failing)}), \
             mock.patch.object(routes, "begin_stage"), \
             mock.patch.object(output, "stage_end") as ended, \
             mock.patch.object(output, "log_only") as logged, \
             mock.patch.object(output, "tell") as told, \
             mock.patch.object(output, "say"):
            code = runner.execute(ctx, step)
        return code, state, ended, logged, told

    def test_it_stops_the_step_with_a_message_and_the_traceback_in_the_log(self):
        code, state, ended, logged, told = self.run_step("E1", KeyError("x"))
        self.assertEqual(code, 1)
        self.assertEqual(state.status("E1"), FAILED)
        ended.assert_called_once_with("stopped")
        self.assertIn("Traceback", logged.call_args.args[0])
        self.assertIn("something unexpected went wrong: KeyError", told.call_args.args[0])

    def test_the_look_for_a_device_that_lost_its_settings_stops_the_step_not_the_run(self):
        # Seen in the fault suite: not joined to the device's network in time,
        # its Abort crashed the run, put as "something unexpected went wrong".
        from unittest import mock
        gave_up = errors.Abort("this computer did not join tasmota-ABCDEF-1234 within 15 minutes")
        with mock.patch.object(runner, "set_up_if_erased", side_effect=gave_up):
            code, state, ended, _, told = self.run_step("D4", gave_up)
        self.assertEqual(code, 1)
        ended.assert_called_once_with("stopped")
        self.assertEqual(told.call_args.args[0], f"FAILED: {gave_up}")

    def test_a_request_the_network_dropped_is_no_surprise_and_is_tried_again(self):
        # Seen in the fault suite: an outage reset H1's read of a region part
        # way, and the run said "something unexpected went wrong" - then
        # converted the device on the next try.
        import http.client
        for error in (ConnectionResetError(104, "Connection reset by peer"), http.client.IncompleteRead(b""),
                      http.client.RemoteDisconnected("Remote end closed connection without response"),
                      TimeoutError("timed out")):
            code, state, ended, logged, told = self.run_step("H1", error)
            self.assertEqual(code, 1)
            self.assertEqual(state.status("H1"), FAILED)
            self.assertIn("Traceback", logged.call_args.args[0])
            message = told.call_args.args[0]
            self.assertIn("the connection to the device was lost", message)
            self.assertNotIn("unexpected", message)
            self.assertRegex(message, errors.TRANSIENT_RE)

    def test_a_step_that_writes_stays_attempted(self):
        # Whether the commit got as far as writing is for the next run to find
        # out from the device - so it must not be recorded as failed.
        code, state, *_ = self.run_step("E3", RuntimeError("boom"))
        self.assertEqual(code, 1)
        self.assertEqual(state.status("E3"), "attempted")

    def test_a_send_stopped_part_way_is_said_plainly_and_stays_attempted(self):
        # Seen in the fault suite: a power cut part way through B2's delivery
        # said "something unexpected went wrong: OSError: ...".
        stopped = errors.StoppedPartWay("the delivery stopped part way: the device fetched 1 of 2 bytes")
        code, state, ended, _, told = self.run_step("E3", stopped)
        self.assertEqual(code, 1)
        self.assertEqual(state.status("E3"), "attempted")
        self.assertEqual(told.call_args.args[0], f"FAILED: {stopped}")

    def test_a_run_stopped_as_it_sent_through_ewelink_is_told_to_wait(self):
        # Seen on the real cloud: a run cancelled during the send, then one
        # started at once, did not find the device, away from eWeLink for minutes.
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState.open(Path(tmp))
            send = routes.FAMILY.POINT_OF_NO_RETURN
            state.mark(send, "attempted", nothing_written=False)
            self.assertTrue(runner.stopped_mid_send(SimpleNamespace(state=state, consented=True)))
            # At the go-ahead itself nothing was sent.
            self.assertFalse(runner.stopped_mid_send(SimpleNamespace(state=state, consented=False)))
            state.mark(send, "done")
            self.assertFalse(runner.stopped_mid_send(SimpleNamespace(state=state, consented=True)))

    def test_a_step_that_writes_and_is_refused_part_way_is_not_taken_for_one_that_wrote_nothing(self):
        # Marked attempted, nothing_written False, before it starts: a refusal
        # then must not let the next run treat it as never begun.
        code, state, *_ = self.run_step("E2", errors.Abort("the device refused"))
        self.assertEqual((code, state.status("E2")), (1, FAILED))
        self.assertIs(state.data["steps"]["E2"]["nothing_written"], False)

    def test_a_device_that_never_connected_stops_the_step_with_the_firewall_hint(self):
        from tools.autoflash.phases import DeviceNeverConnected
        with mock.patch.object(prompts, "section") as shown, \
                mock.patch.object(firewall, "firewall_hint", return_value=["the hint"]):
            code, state, ended, _, told = self.run_step("E1", DeviceNeverConnected("the device never connected"))
        self.assertEqual((code, state.status("E1")), (1, FAILED))
        ended.assert_called_once_with("stopped")
        self.assertEqual(shown.call_args.args[1], ["the hint"])
        self.assertEqual(told.call_args.args[0], "FAILED: the device never connected")

    def test_ctrl_c_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_step("E1", KeyboardInterrupt())


class ErasedTasmotaTests(unittest.TestCase):
    """A Tasmota that lost its settings part way is set up through its own
    network again - and set up again (F4), when it had been already."""

    def run_it(self, step="G1", answers=False, network="tasmota-ABCDEF-1234", f4_done=True, in_place=False):
        from types import SimpleNamespace
        from unittest import mock
        done = {"D1", "F4"} if f4_done else {"D1"}
        inputs = {"device_mac": "12:34:56:ab:cd:ef", "device_ip": "192.0.2.51", "product_model": "S60TPG"}
        # Answers in turn, the last for every ask after; a network in turn likewise.
        replies = list(answers) if isinstance(answers, list) else [answers]
        self.waited, self.captured = [], {}
        device = SimpleNamespace(answers=lambda timeout=None: replies.pop(0) if len(replies) > 1 else replies[0],
                                 wait_reachable=lambda timeout=None: self.waited.append(timeout))
        ctx = SimpleNamespace(device=device, state=SimpleNamespace(
            input=inputs.get, is_done=lambda s: s in done, capture=self.captured.__setitem__))
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(prompts, "find_ssid", side_effect=network if isinstance(network, list)
                               else lambda *a, **k: network) as looked, \
             mock.patch.object(join, "configure_device_wifi") as by_hand, \
             mock.patch.object(tasmota, "template_in_place", return_value=in_place), \
             mock.patch.object(tasmota, "step_configure") as configured, \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "progress"), \
             mock.patch.object(output, "progress_clear"):
            result = runner.set_up_if_erased(ctx, step)
        return result, looked, by_hand, configured

    def test_one_just_starting_up_is_left_alone(self):
        # Seen in the fault suite: D1 had just restarted the device onto the
        # Wi-Fi, and the network it showed for a moment as it started
        # (ESP_ and its access-point MAC) was taken for its setup network.
        result, looked, by_hand, _ = self.run_it(answers=[False, False, False, True], network="ESP_ABCDF0")
        self.assertFalse(result)
        by_hand.assert_not_called()

    def test_one_whose_network_went_as_it_started_is_left_alone(self):
        result, looked, by_hand, _ = self.run_it(network=["ESP_ABCDF0", None])
        self.assertFalse(result)
        self.assertEqual(looked.call_count, 2)
        by_hand.assert_not_called()

    def test_it_is_set_up_through_the_network_it_settled_on(self):
        _, _, by_hand, _ = self.run_it(network=["ESP_ABCDF0", "tasmota-ABCDEF-1234"])
        self.assertEqual(by_hand.call_args.args[1], "tasmota-ABCDEF-1234")

    def test_one_answering_is_left_alone(self):
        result, looked, by_hand, _ = self.run_it(answers=True)
        self.assertFalse(result)
        looked.assert_not_called()

    def test_on_its_own_network_it_is_set_up_through_it_and_f4_again(self):
        # Seen in the fault suite: switched off and on 7 times during G1, the
        # run asked for Wi-Fi details for a device it could not reach, run after run.
        result, _, by_hand, configured = self.run_it()
        self.assertTrue(result)
        by_hand.assert_called_once()
        configured.assert_called_once()
        self.assertTrue(self.waited)   # back on the Wi-Fi before its template is looked at
        self.assertEqual(self.captured, {"F4.again_after_erase": True})   # for the report

    def test_a_device_answering_once_started_up_is_left_alone(self):
        clock = iter(range(1000, 100_000, 5))   # times are measured from the start
        device = mock.Mock(answers=mock.Mock(side_effect=[False, False, True]))
        with mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(time, "sleep"), mock.patch.object(output, "progress"), \
                mock.patch.object(output, "progress_clear"):
            self.assertTrue(runner.answers_within(device, runner.ERASED_SETTLE))
        self.assertEqual(device.answers.call_count, 3)

    def test_its_template_still_there_needs_no_f4(self):
        _, _, by_hand, configured = self.run_it(in_place=True)
        by_hand.assert_called_once()
        configured.assert_not_called()

    def test_before_f4_only_the_wifi(self):
        _, _, by_hand, configured = self.run_it(step="D4", f4_done=False)
        by_hand.assert_called_once()
        configured.assert_not_called()

    def test_no_network_of_its_own_is_left_to_the_step(self):
        result, _, by_hand, _ = self.run_it(network=None)
        self.assertFalse(result)
        by_hand.assert_not_called()

    def test_on_a_route_with_no_d1_as_well(self):
        # From ESPHome, Tasmota comes by the mover and the startup firmware:
        # seen in the fault suite, G1 there was never helped.
        from types import SimpleNamespace
        from unittest import mock
        inputs = {"device_mac": "12:34:56:ab:cd:ef", "device_ip": "192.0.2.51", "product_model": "S60TPG"}
        ctx = SimpleNamespace(device=SimpleNamespace(answers=lambda timeout=None: False,
                                                     wait_reachable=lambda timeout=None: None),
                              state=SimpleNamespace(input=inputs.get, is_done=lambda s: s == "F4",
                                                    capture=lambda *a: None))
        with mock.patch.object(prompts, "find_ssid", return_value="tasmota-ABCDEF-1234"), \
             mock.patch.object(join, "configure_device_wifi") as by_hand, \
             mock.patch.object(tasmota, "template_in_place", return_value=True), \
             mock.patch.object(runner, "ERASED_SETTLE", 0.0), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "progress_clear"):
            self.assertTrue(runner.set_up_if_erased(ctx, "G1"))
        by_hand.assert_called_once()

    def test_not_at_a_step_of_a_tasmota_with_the_wifi(self):
        for step in ("F2", "F4", "L1", "X1"):
            result, looked, _, _ = self.run_it(step=step)
            self.assertFalse(result, step)
            looked.assert_not_called()


class RunningStepTests(unittest.TestCase):
    """The step under way, for a crash that escapes it to say where it was
    (cli.py, report_data.outcome): none once it is done."""

    def ctx(self):
        from types import SimpleNamespace
        state = mock.Mock(data={"captures": {}}, input=mock.Mock(return_value=None))
        return SimpleNamespace(state=state, last_runner=None, running_step=None)

    def test_a_step_done_leaves_none_under_way(self):
        ctx = self.ctx()
        with mock.patch.object(runner, "_execute", return_value=0), \
                mock.patch.object(runner, "asked_of_the_run", return_value=([], 0)):
            self.assertEqual(runner.execute(ctx, "K2"), 0)
        self.assertIsNone(ctx.running_step)

    def test_an_error_that_escapes_a_step_leaves_it_under_way(self):
        ctx = self.ctx()
        with mock.patch.object(runner, "_execute", side_effect=RuntimeError("escaped")), \
                mock.patch.object(runner, "asked_of_the_run", return_value=([], 0)):
            with self.assertRaises(RuntimeError):
                runner.execute(ctx, "K2")
        self.assertEqual(ctx.running_step, "K2")


if __name__ == "__main__":
    unittest.main()
