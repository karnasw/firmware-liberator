"""The command line's own handling, before a run starts."""
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import cli, credentials, hostsys, interrupt, output, prompts, records, routes, runs
from tools.autoflash.routes.beken import images as beken_images
from tools.autoflash.routes.espressif import identity, installer, sonoff_firmware
from tools.autoflash.routes.espressif import images as espressif_images
from tools.autoflash.state import DONE, RunState


class BeforeTheRunTests(unittest.TestCase):
    def main(self, *, choosing, more=()):
        """cli.main() for `run`, the question of which run answered by `choosing`.
        What main() sets for the whole process is put back afterwards;
        self.top and self.taller are the window's two calls."""
        screen = io.StringIO()
        self.top, self.taller = mock.Mock(), mock.Mock()
        # More patches than one `with` may nest (CPython allows 20 blocks).
        with contextlib.ExitStack() as patched:
            for patch in (
                    mock.patch("sys.argv", ["cli.py", "run", *more]), mock.patch("sys.stdout", screen),
                    mock.patch.object(hostsys, "password_asker"), mock.patch.object(prompts, "UNATTENDED"),
                    mock.patch.object(installer, "SIMULATED_BACKUP_FAILURES"),
                    mock.patch.object(sonoff_firmware, "SIMULATE_NONE"),
                    mock.patch.object(identity, "SIMULATE_NONE"),
                    mock.patch.object(beken_images, "SPOIL_STEP"), mock.patch.object(beken_images, "SPOIL_TIMES"),
                    mock.patch.object(espressif_images, "SPOIL_STEP"), mock.patch.object(espressif_images, "SPOIL_TIMES"),
                    mock.patch.object(beken_images, "UNKNOWN_TABLE"),
                    mock.patch.object(cli.signal, "signal"), mock.patch.object(interrupt, "install"),
                    mock.patch.object(credentials, "load", return_value={}),
                    mock.patch.object(cli, "adopt_older_runs"), mock.patch.object(output, "start_at_top", self.top),
                    mock.patch.object(records, "take_over_earlier_names", self.taken_over),
                    mock.patch.object(prompts, "wait_for_taller_window", self.taller),
                    mock.patch.object(cli, "resolve_run_name", side_effect=choosing),
                    mock.patch.object(cli.signup, "ask_on_exit", self.exit_question),
            ):
                patched.enter_context(patch)
            return cli.main(), screen.getvalue()

    def setUp(self):
        self.taken_over = mock.Mock()
        self.exit_question = mock.Mock()

    def test_records_an_earlier_release_kept_are_renamed_before_a_run_is_chosen(self):
        seen = []
        code, _ = self.main(choosing=lambda run, *_args, **_kwargs: seen.append(
            (run, self.taken_over.call_count)) or (_ for _ in ()).throw(KeyboardInterrupt),
            more=["--run", "plugs/12-34-56-ab-cd-ef/runs/20261013-031500"])
        self.assertEqual(seen, [("devices/12-34-56-ab-cd-ef/runs/20261013-031500", 1)])

    def test_records_that_cannot_be_renamed_stop_it_before_anything_else(self):
        self.taken_over.side_effect = records.EarlierNames("captures/plugs and captures/devices both exist.")
        code, screen = self.main(choosing=AssertionError("went on"))
        self.assertEqual(code, 2)
        self.assertIn("REFUSING: captures/plugs and captures/devices both exist.", screen)

    def test_ctrl_c_at_the_first_question_stops_without_a_traceback(self):
        # Seen: Ctrl-C at "A conversion did not finish last time. What now"
        # ended in a KeyboardInterrupt traceback.
        code, screen = self.main(choosing=KeyboardInterrupt)
        self.assertEqual(code, 130)
        self.assertIn("Stopped. Nothing was changed.", screen)

    def test_a_test_can_ask_for_a_beken_table_not_known(self):
        seen = []

        def choosing(*_args, **_kwargs):
            seen.append(beken_images.UNKNOWN_TABLE)
            raise KeyboardInterrupt
        self.main(choosing=choosing)
        self.main(choosing=choosing, more=["--simulate-unknown-table"])
        self.assertEqual(seen, [False, True])

    def test_a_test_can_ask_for_an_image_spoilt_a_number_of_times(self):
        seen = []

        def choosing(*_args, **_kwargs):
            seen.append((beken_images.SPOIL_STEP, beken_images.SPOIL_TIMES,
                         espressif_images.SPOIL_STEP, espressif_images.SPOIL_TIMES))
            raise KeyboardInterrupt
        self.main(choosing=choosing, more=["--simulate-bad-image", "O1:3"])
        self.main(choosing=choosing, more=["--simulate-bad-image", "D1"])
        self.main(choosing=choosing)
        # Each family's: the step named is one family's or the other's.
        self.assertEqual(seen, [("O1", 3, "O1", 3), ("D1", 1, "D1", 1), (None, 1, None, 1)])

    def test_a_test_can_have_sonoffs_firmware_out_of_reach(self):
        seen = []

        def choosing(*_args, **_kwargs):
            seen.append(sonoff_firmware.SIMULATE_NONE)
            raise KeyboardInterrupt
        self.main(choosing=choosing, more=["--simulate-no-sonoff-firmware"])
        self.main(choosing=choosing)
        self.assertEqual(seen, [True, False])

    def test_a_test_can_have_a_device_with_no_identity_anywhere(self):
        seen = []

        def choosing(*_args, **_kwargs):
            seen.append(identity.SIMULATE_NONE)
            raise KeyboardInterrupt
        self.main(choosing=choosing, more=["--simulate-no-identity"])
        self.main(choosing=choosing)
        self.assertEqual(seen, [True, False])

    def test_a_test_can_have_the_backup_fail_every_round(self):
        seen = []

        def choosing(*_args, **_kwargs):
            seen.append(installer.SIMULATED_BACKUP_FAILURES)
            raise KeyboardInterrupt
        self.main(choosing=choosing, more=["--simulate-backup-failure"])
        self.main(choosing=choosing)
        self.assertEqual(seen, [2 * installer.BACKUP_ROUNDS, 0])

    def test_every_option_of_the_command_line_is_taken(self):
        # Scripts, the docs and the emulator suites give these: one missing
        # would stop them at the command line.
        every = ["--manifest", "m.json", "--device-ip", "192.0.2.50", "--listen-ip", "192.0.2.1",
                 "--email", "someone@example.com", "--web-user", "admin", "--web-password", "secret",
                 "--esphome-ota-password", "secret", "--esphome-web-user", "admin",
                 "--esphome-web-password", "secret", "--esphome-api-key", "key", "--all", "--wifi",
                 "--no-backup", "--firmware", "esphome", "--esphome", "firmware.ota.bin", "--dry-run",
                 "--no-report", "--verbose", "--bridge", "bridge"]
        code, _ = self.main(choosing=KeyboardInterrupt, more=every)
        self.assertEqual(code, 130)

    def test_replacing_its_own_build_goes_with_no_other_firmware(self):
        with mock.patch("sys.stderr", io.StringIO()):
            for more in (["--firmware", "tasmota"], ["--esphome", "firmware.ota.bin"]):
                with self.subTest(more=more), self.assertRaises(SystemExit):
                    self.main(choosing=AssertionError("went on"), more=["--replace-own-build", *more])
        for more in ([], ["--firmware", "esphome"]):
            with self.subTest(more=more):
                self.assertEqual(self.main(choosing=KeyboardInterrupt, more=["--replace-own-build", *more])[0], 130)

    def test_only_a_run_at_the_keyboard_waits_for_a_taller_window(self):
        # An unattended run has nobody to make the window taller: waiting
        # would hang it.
        for more, waits, top in (([], True, True), (["--verbose"], False, True),
                                 (["--unattended"], False, False)):
            with self.subTest(more=more):
                self.main(choosing=KeyboardInterrupt, more=more)
                self.assertEqual(self.taller.called, waits)
                self.assertEqual(self.top.called, top)

    def test_stopping_and_sudo_are_handled_before_a_run_is_chosen(self):
        # Being stopped (SIGTERM, Ctrl-C) still runs the cleanup; sudo asks in
        # the tool's own words.
        seen = []

        def choosing(*_args, **_kwargs):
            handlers = {call.args[0]: call.args[1] for call in cli.signal.signal.call_args_list}
            seen.append((hostsys.password_asker, interrupt.install.called, handlers))
            raise KeyboardInterrupt
        self.main(choosing=choosing)
        asker, installed, handlers = seen[0]
        self.assertIs(asker, cli.admin_password)
        self.assertTrue(installed)
        with self.assertRaises(SystemExit) as stopped:
            handlers[cli.signal.SIGTERM]()
        self.assertEqual(stopped.exception.code, 143)

    def test_secrets_are_forgotten_from_this_repositorys_bundle(self):
        with mock.patch.object(runs, "run_directories", return_value=[]), \
                mock.patch.object(cli, "forget_stored_password", return_value=False) as forget:
            self.assertEqual(cli.forget_all_secrets(), [])
        repository = Path(__file__).resolve().parents[2]
        self.assertEqual(forget.call_args.args[0], (repository / cli.bundle.DEFAULT_MANIFEST).resolve())

    def test_q_there_stops_the_same_way(self):
        code, screen = self.main(choosing=prompts.QuitRun("stopped at an operator prompt"))
        self.assertEqual(code, 130)
        self.assertIn("Stopped. Nothing was changed.", screen)

    def test_no_email_question_where_the_run_never_began(self):
        # q or Ctrl-C at the first question, or REFUSING: nothing was used yet.
        self.main(choosing=prompts.QuitRun("q"))
        self.main(choosing=KeyboardInterrupt)
        self.taken_over.side_effect = records.EarlierNames("captures/plugs and captures/devices both exist.")
        self.main(choosing=AssertionError("went on"))
        self.exit_question.assert_not_called()


class AnotherDeviceTests(unittest.TestCase):
    """A conversion or a readout that finished asks what is next - another
    device converted, or one read out - and goes on to it in the same
    sitting: a new run, found afresh, the eWeLink sign-in kept.  Not after
    one that stopped, nor in an unattended one."""

    def main(self, *, codes, answers, command="run", more=()):
        from types import SimpleNamespace
        made, names = [], iter(f"run-{number}" for number in range(2, 9))

        def context(args):
            ctx = SimpleNamespace(args=args, run=args.run, device_ip=args.device_ip, dir=Path("unused"),
                                  state=mock.Mock(input=mock.Mock(return_value=None)), keep_sign_in=mock.Mock())
            made.append(ctx)
            return ctx
        codes, commands, self.ran = iter(codes), [], []

        def running(kind):
            def ran(ctx, *_):
                commands.append(ctx.args.command)
                self.ran.append(kind)
                return next(codes)
            return ran
        top, asked = mock.Mock(), mock.Mock(side_effect=list(answers))
        self.exit_question = mock.Mock()
        self.applied, self.looked = mock.Mock(), mock.Mock(return_value="run-latest")
        with contextlib.ExitStack() as patches:
            for patch in (
                    mock.patch("sys.argv", ["cli.py", command, "--device-ip", "192.0.2.50", *more]),
                    mock.patch("sys.stdout", io.StringIO()),
                    # What the sitting draws between its runs left as it found it,
                    # for the next test in this process (tests/run.py reuses them).
                    mock.patch.multiple(cli.output._s, after_blank=False, after_stage=False, waited=False),
                    mock.patch.object(hostsys, "password_asker"), mock.patch.object(prompts, "UNATTENDED"),
                    mock.patch.object(installer, "SIMULATED_BACKUP_FAILURES"),
                    mock.patch.object(sonoff_firmware, "SIMULATE_NONE"),
                    mock.patch.object(identity, "SIMULATE_NONE"),
                    mock.patch.object(beken_images, "SPOIL_STEP"), mock.patch.object(beken_images, "SPOIL_TIMES"),
                    mock.patch.object(espressif_images, "SPOIL_STEP"), mock.patch.object(espressif_images, "SPOIL_TIMES"),
                    mock.patch.object(beken_images, "UNKNOWN_TABLE"),
                    mock.patch.object(cli.signal, "signal"), mock.patch.object(interrupt, "install"),
                    mock.patch.object(credentials, "load", return_value={}),
                    mock.patch.object(cli, "adopt_older_runs"), mock.patch.object(output, "start_at_top", top),
                    mock.patch.object(records, "take_over_earlier_names"),
                    mock.patch.object(prompts, "wait_for_taller_window"),
                    mock.patch.object(cli, "resolve_run_name", return_value="run-1"),
                    mock.patch.object(cli, "start_new_run", side_effect=lambda _: next(names)),
                    mock.patch.object(cli, "Context", side_effect=context),
                    mock.patch.object(routes, "apply_target", self.applied),
                    mock.patch.object(cli, "run_to_look_at", self.looked),
                    mock.patch.object(cli, "cmd_status", return_value=0),
                    mock.patch.object(cli, "refused_for_another_run", return_value=False),
                    mock.patch.object(cli, "convert", running("convert")),
                    mock.patch.object(cli, "read_out", running("read_out")),
                    mock.patch.object(prompts, "ask_choice", asked),
                    mock.patch.object(cli.signup, "SITTING", cli.signup.Sitting()),
                    mock.patch.object(cli.signup, "ask_on_exit", self.exit_question)):
                patches.enter_context(patch)
            code = cli.main()
            self.stopped = cli.signup.SITTING.stopped
        self.commands = commands
        return code, made, asked, top

    CONVERT, READ_OUT = 0, 1   # the options' indexes

    def test_after_one_another_is_offered_until_q(self):
        code, made, asked, top = self.main(codes=[0, 0, 0], answers=[self.CONVERT, self.CONVERT,
                                                                     prompts.QuitRun("q")])
        self.assertEqual(code, 0)
        self.assertEqual([ctx.run for ctx in made], ["run-1", "run-2", "run-3"])
        # The address was the first device's: the next is found afresh.
        self.assertEqual([ctx.device_ip for ctx in made], ["192.0.2.50", None, None])
        made[1].keep_sign_in.assert_called_once_with(made[0])
        made[2].keep_sign_in.assert_called_once_with(made[1])
        self.assertEqual(asked.call_args.args, ("What next", ["Convert another device", "Read out a device"]))
        self.assertEqual(top.call_count, 1 + 2)   # each conversion at the top of the window
        self.assertEqual(self.applied.call_count, 3)   # each run's route settled at its start

    def test_a_readout_is_read_out_and_a_conversion_converted(self):
        self.main(codes=[0, 0, 0], answers=[self.READ_OUT, self.CONVERT, KeyboardInterrupt()])
        self.assertEqual(self.ran, ["convert", "read_out", "convert"])

    def test_whichever_is_next_starts_at_the_top_of_the_window(self):
        # Seen: a readout after a readout began under the last one's
        # "Readout saved in ...".
        _, _, _, top = self.main(codes=[0, 0, 0], answers=[self.READ_OUT, self.READ_OUT, KeyboardInterrupt()],
                                 command="readout")
        self.assertEqual((self.ran, top.call_count), (["read_out"] * 3, 2))

    def test_a_readout_starts_a_run_of_its_own(self):
        # Never an unfinished conversion carried on.
        _, made, _, _ = self.main(codes=[1], answers=[], command="readout")
        self.assertEqual((made[0].run, self.ran), ("run-2", ["read_out"]))

    def test_status_looks_at_the_run_given_else_the_devices_latest(self):
        self.main(codes=[], answers=[], command="status", more=["--run", "run-9"])
        self.looked.assert_not_called()
        _, made, _, _ = self.main(codes=[], answers=[], command="status")
        self.assertEqual(made[0].run, "run-latest")
        self.looked.assert_called_once_with("192.0.2.50")

    def test_a_conversion_and_a_readout_lead_to_each_other(self):
        code, made, asked, _ = self.main(codes=[0, 0, 0], answers=[self.READ_OUT, self.CONVERT, KeyboardInterrupt()])
        self.assertEqual((code, self.commands), (0, ["run", "readout", "run"]))
        self.assertEqual(asked.call_args_list[1].args[1], ["Convert a device", "Read out another device"])
        made[1].keep_sign_in.assert_called_once_with(made[0])

    def test_not_after_one_that_stopped(self):
        for command in ("run", "readout"):
            for code in (1, 2, 130):
                with self.subTest(command=command, code=code):
                    ended, made, asked, _ = self.main(codes=[code], answers=[], command=command)
                    self.assertEqual((ended, len(made)), (code, 1))
                    asked.assert_not_called()

    def test_the_email_question_comes_once_as_the_sitting_ends_however_it_ends(self):
        for codes, answers in (([0, 0], [self.CONVERT, prompts.QuitRun("q")]), ([1], []), ([130], [])):
            with self.subTest(codes=codes):
                self.main(codes=codes, answers=answers)
                self.exit_question.assert_called_once_with()

    def test_not_after_ctrl_c_at_what_next(self):
        self.main(codes=[0], answers=[KeyboardInterrupt()])
        self.assertTrue(self.stopped)   # ask_on_exit then asks nothing
        self.main(codes=[0], answers=[prompts.QuitRun("q")])
        self.assertFalse(self.stopped)

    def test_the_email_question_comes_after_a_readout_sent_again_too(self):
        self.main(codes=[0], answers=[], command="readout", more=["--send", "readout.tar"])
        self.exit_question.assert_called_once_with()

    def test_not_asked_unattended_nor_after_a_readout_sent_again(self):
        for command, more in (("run", ["--unattended"]), ("readout", ["--send", "readout.tar"])):
            with self.subTest(command=command):
                code, made, asked, _ = self.main(codes=[0], answers=[], command=command, more=more)
                self.assertEqual((code, len(made)), (0, 1))
                asked.assert_not_called()


class CtrlCEndsTheSittingTests(unittest.TestCase):
    """Ctrl-C that stops a readout or a conversion stops the sitting: no
    email question after it (signup), as it was a way out."""

    def stopped_by(self, run, command):
        from types import SimpleNamespace
        ctx = SimpleNamespace(args=SimpleNamespace(verbose=False, send=None, unattended=False), dir=Path("/x"),
                              state=mock.Mock(), readouts=[])
        with mock.patch.object(cli.signup, "SITTING", cli.signup.Sitting()), \
                mock.patch.object(cli.output, "start"), mock.patch.object(cli.output, "finish"), \
                mock.patch.object(cli.output, "tell"), mock.patch.object(cli.output, "say"), \
                mock.patch.object(cli.output, "log_only"), mock.patch.object(cli.output, "stage_end"), \
                mock.patch.object(cli.firewall, "release_firewall"), mock.patch.object(cli.firewall, "say_quit"), \
                mock.patch.object(cli.runs, "forget_run_secrets"), mock.patch.object(cli, "log_run_choice"), \
                mock.patch.object(cli.report_upload, "begin"), mock.patch.object(cli, "stopped_report", return_value=None), \
                mock.patch.object(cli, "issue_alone", return_value=False), mock.patch.object(cli, "stopped_by", return_value=None), \
                mock.patch.object(cli.runner, "stopped_mid_send", return_value=False), \
                mock.patch.object(cli, command, side_effect=KeyboardInterrupt):
            self.assertEqual(run(ctx), 130)
            return cli.signup.SITTING.stopped

    def test_in_a_readout(self):
        self.assertTrue(self.stopped_by(cli.read_out, "cmd_readout"))

    def test_in_a_conversion(self):
        self.assertTrue(self.stopped_by(lambda ctx: cli.convert(ctx, []), "cmd_run"))


class StoppedReportTests(unittest.TestCase):
    """A run that did not finish asks for its report - but not one stopped
    before any stage began, which says nothing of how the tool does."""

    def ask(self, code, ending, begun, reported=False):
        from types import SimpleNamespace
        return cli.stopped_report(SimpleNamespace(reported=reported, stage_begun=begun), code, ending)

    def test_a_run_that_failed_in_a_stage_asks(self):
        self.assertEqual(self.ask(1, None, begun=True), "failed")

    def test_one_stopped_before_any_stage_does_not(self):
        # Seen: a wrong --device-ip, stopped at once, still asked for its report.
        self.assertIsNone(self.ask(2, None, begun=False))

    def test_a_crash_asks_however_early(self):
        self.assertEqual(self.ask(1, "crashed", begun=False), "crashed")

    def test_nor_twice(self):
        self.assertIsNone(self.ask(1, None, begun=True, reported=True))

    def test_a_run_the_person_stopped_says_how_for_its_report(self):
        from types import SimpleNamespace
        from unittest import mock

        def how(code, ending, begun=True, window=False, at_stage_end=None):
            state = mock.Mock(input=mock.Mock(side_effect=lambda key: "window" if window and key == "stopped_by"
                                              else None))
            ctx = SimpleNamespace(reported=False, stage_begun=begun, state=state, stopped_by=at_stage_end)
            return cli.stopped_by(ctx, code, ending)
        self.assertEqual(how(130, None), "quit")                              # q at a question
        self.assertEqual(how(130, None, at_stage_end="ctrl-c"), "ctrl-c")     # confirmed at a stage's end
        self.assertEqual(how(None, "cancelled"), "ctrl-c")                    # at once
        self.assertEqual(how(None, "cancelled", window=True), "window-closed")
        self.assertIsNone(how(130, None, begun=False))   # before any stage, as a failure that early
        for code, ending in ((1, None), (1, "crashed"), (0, None), (None, None)):
            with self.subTest(code=code, ending=ending):
                self.assertIsNone(how(code, ending))     # not stopped by the person

    def test_one_failed_with_no_report_is_given_the_issue_link_alone(self):
        from types import SimpleNamespace

        def alone(code, ending, begun=False, reported=False):
            return cli.issue_alone(SimpleNamespace(reported=reported, stage_begun=begun), code, ending)
        self.assertTrue(alone(2, None))                     # failed before any stage
        self.assertFalse(alone(1, None, begun=True))         # its report carries the link
        self.assertFalse(alone(1, "crashed"))                # so does a crash's
        for code, ending in ((130, None), (130, "cancelled"), (0, None), (None, None)):
            with self.subTest(code=code, ending=ending):
                self.assertFalse(alone(code, ending))        # stopped by choice, or finished


class PipeTests(unittest.TestCase):
    """Each in a process of its own: what it does to signals stays there."""

    @staticmethod
    def child(code, run=None, **kwargs):
        import subprocess
        from tools.autoflash.runs import ROOT
        return (run or subprocess.run)([sys.executable, "-c", "from tools.autoflash import cli\n" + code],
                                       cwd=ROOT, **kwargs)

    def test_a_device_hanging_up_on_a_write_raises_rather_than_ending_the_run(self):
        # A device restarting under an update closes the connection a check of it
        # is writing to; ended there, the run cleans up nothing.
        done = self.child(
            "import socket\n"
            "cli.handle_signals()\n"
            "ours, devices = socket.socketpair()\n"
            "devices.close()\n"
            "try:\n"
            "    for _ in range(4):\n"
            "        ours.sendall(bytes(65536))\n"
            "except BrokenPipeError:\n"
            "    raise SystemExit(7)\n", capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 7, done.stderr)

    def test_piped_into_a_reader_that_stops_it_ends_quietly(self):
        # As head does: a row read, then the pipe closed.
        import subprocess
        child = self.child("import sys\n"
                           "cli._main = lambda: [print('a row of output') for _ in range(100_000)] and 0\n"
                           "sys.exit(cli.main())\n", run=subprocess.Popen,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.stdout.readline()
        child.stdout.close()
        with child.stderr:
            said = child.stderr.read()
        self.assertEqual((child.wait(timeout=60), said), (1, b""))


class RunToLookAtTests(unittest.TestCase):
    """`status` and `cleanup` report on a run without starting one."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.current = None
        for patch in (
            mock.patch.object(runs, "run_directories",
                              lambda: sorted(p for p in self.root.iterdir() if p.is_dir())),
            mock.patch.object(runs, "read_current", lambda: self.current),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_at(self, name, device_ip):
        RunState.open(self.root / name).set_input("device_ip", device_ip)
        return runs.run_name(self.root / name)

    def test_given_an_address_it_is_that_devices_latest_run(self):
        # Seen: status --device-ip told of the current run, another device's.
        self.run_at("1-first", "10.0.0.51")
        latest = self.run_at("2-again", "10.0.0.51")
        self.current = self.run_at("3-other", "10.0.0.60")
        self.assertEqual(cli.run_to_look_at("10.0.0.51"), latest)

    def test_an_address_with_no_run_has_none(self):
        self.current = self.run_at("1-other", "10.0.0.60")
        self.assertIsNone(cli.run_to_look_at("10.0.0.51"))

    def test_without_an_address_it_is_the_current_run_else_the_latest(self):
        first = self.run_at("1-first", "10.0.0.51")
        latest = self.run_at("2-other", "10.0.0.60")
        self.assertEqual(cli.run_to_look_at(None), latest)
        self.current = first
        self.assertEqual(cli.run_to_look_at(None), first)


class RunChoiceTests(unittest.TestCase):
    """An unfinished conversion is offered back, never resumed or dropped silently."""

    def setUp(self):
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remembered = []
        for patch in (
            mock.patch.object(runs, "run_directories",
                              lambda: sorted(p for p in self.root.iterdir() if p.is_dir())),
            mock.patch.object(runs, "read_current", lambda: None),
            mock.patch.object(runs, "remember_current", self.remembered.append),
            mock.patch.object(output, "say"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_dir(self, name, done):
        state = RunState.open(self.root / name)
        state.set_input("device_ip", "10.0.0.54")
        for step in done:
            state.mark(step, DONE)
        return name

    def choose(self, answer):
        from unittest import mock
        with mock.patch.object(prompts, "ask_choice", return_value=answer) as menu:
            name = cli.resolve_run_name(None, False, None, ask=True)
        return name, menu

    def test_with_nothing_unfinished_a_new_run_starts_without_asking(self):
        self.run_dir("done", [step for step, _, _ in routes.STEPS])
        name, menu = self.choose(0)
        menu.assert_not_called()
        self.assertNotEqual(name, "done")
        self.assertEqual(self.remembered, [name])   # the new run is the current one

    def test_the_current_run_is_offered_first_else_the_latest(self):
        from unittest import mock
        for name, age in (("current", 200), ("older", 300), ("newer", 100)):
            self.run_dir(name, ["A0", "A5", "A1", "A2", "C2"])
            os.utime(self.root / name, (time.time() - age,) * 2)
        with mock.patch.object(runs, "read_current", return_value="current"):
            self.assertEqual(self.choose(0)[0], "current")
        self.assertEqual(self.choose(0)[0], "newer")

    def test_a_run_is_described_by_its_address_and_mac(self):
        self.run_dir("half", ["A0"])
        RunState.open(self.root / "half").set_input("device_mac", "12:34:56:ab:cd:ef")
        self.assertTrue(cli.describe_run(self.root / "half").startswith(
            "the device at 10.0.0.54, MAC 12:34:56:ab:cd:ef ("))

    def test_continuing_picks_up_the_unfinished_run(self):
        self.run_dir("half", ["A0", "A5", "A1", "A2", "C2"])
        cli.RUN_CHOICE.clear()
        name, menu = self.choose(0)
        self.assertEqual(name, "half")
        self.assertEqual(self.remembered, ["half"])
        self.assertIn("Chosen at the start: continue", " ".join(cli.RUN_CHOICE))   # for its log
        cli.RUN_CHOICE.clear()
        options = menu.call_args.args[1]
        self.assertIn("carries on at stage 7, Backing up Sonoff firmware", options[0])
        self.assertIn("past the point of no return", options[0])

    def test_starting_a_new_device_leaves_the_unfinished_run_alone(self):
        self.leaving_tasmota("half")
        name, menu = self.choose(1)
        self.assertNotEqual(name, "half")
        self.assertNotIn("past the point of no return", menu.call_args.args[1][0])
        self.assertTrue((self.root / "half").is_dir())

    def test_the_answer_reaches_the_new_runs_log(self):
        # The menu is gone from the screen once answered and comes before the
        # log opens: a part-converted device left behind must still show in it.
        from unittest import mock
        self.run_dir("past", ["A0", "A5", "A1", "A2", "C2"])
        cli.RUN_CHOICE.clear()
        self.choose(1)
        with mock.patch.object(output, "say") as say:
            cli.log_run_choice()
        logged = "\n".join(call.args[0] for call in say.call_args_list)
        self.assertIn("a new conversion", logged)
        self.assertIn("carries on at stage 7, Backing up Sonoff firmware", logged)
        self.assertIn("past the point of no return", logged)
        self.assertEqual(cli.RUN_CHOICE, [])

    def leaving_tasmota(self, name):
        """A run that wrote to its device, short of the point of no return:
        one from Tasmota to ESPHome, past L1."""
        self.device_run(name, "12:34:56:ab:cd:ef", ["A0", "A3", "A5", "L1"], source="tasmota", firmware="esphome")

    def device_run(self, name, mac, done, **inputs):
        self.run_dir(name, done)
        state = RunState.open(self.root / name)
        for key, value in {"device_mac": mac, **inputs}.items():
            state.set_input(key, value)

    def test_a_run_that_wrote_nothing_to_its_device_is_not_offered(self):
        # Stopped at a question, say, after "Convert another device": it
        # loses nothing by starting afresh.
        for done in ([], ["A0"], ["A0", "A5", "A1", "A2"]):
            with self.subTest(done=done):
                self.run_dir("quiet", done)
                name, menu = self.choose(0)
                menu.assert_not_called()
                self.assertNotEqual(name, "quiet")

    def test_a_run_a_later_run_of_the_same_device_wrote_over_is_not_offered(self):
        # Seen: a run stopped as it left Tasmota, carried on after a later run
        # had put ESPHome on - asking for the Wi-Fi to leave a Tasmota long gone.
        self.device_run("1-stale", "12:34:56:ab:cd:ef", ["A0", "A5", "A1", "A2", "C2"])
        self.device_run("2-later", "12:34:56:ab:cd:ef", ["A0", "A5", "L1"], source="tasmota", firmware="esphome")
        self.device_run("3-other", "12:34:56:ab:cd:01", ["A0", "A5", "A1", "A2", "C2"])
        _, menu = self.choose(0)
        self.assertEqual(len(menu.call_args.args[1]), 4)   # 2-later, 3-other, a new one, remove one
        self.assertNotIn("1-stale", " ".join(menu.call_args.args[1]))

    def test_a_later_run_that_wrote_nothing_leaves_it_offered(self):
        self.device_run("1-stale", "12:34:56:ab:cd:ef", ["A0", "A5", "A1", "A2", "C2"])
        self.device_run("2-later", "12:34:56:ab:cd:ef", ["A0"])
        _, menu = self.choose(0)
        # The first offered, with a new run and removing one; the later, which
        # wrote nothing, not.
        self.assertEqual(len(menu.call_args.args[1]), 3)
        self.assertIn("carries on at stage 7", menu.call_args.args[1][0])

    def test_not_asked_for_one_device_only_its_run_is_carried_on(self):
        from unittest import mock
        self.run_dir("half", ["A0"])
        with mock.patch.object(prompts, "ask_choice") as menu:
            self.assertNotEqual(cli.resolve_run_name(None, False, "10.0.0.99"), "half")
            self.assertEqual(cli.resolve_run_name(None, False, "10.0.0.54"), "half")
        menu.assert_not_called()

    def test_other_commands_continue_without_asking(self):
        from unittest import mock
        self.run_dir("half", ["A0"])
        cli.RUN_CHOICE.clear()
        with mock.patch.object(prompts, "ask_choice") as menu:
            self.assertEqual(cli.resolve_run_name(None, False, None), "half")
        menu.assert_not_called()
        self.assertEqual(self.remembered, ["half"])
        self.assertIn("the most recent unfinished run", " ".join(cli.RUN_CHOICE))   # for its log
        cli.RUN_CHOICE.clear()


class StageNumberTests(unittest.TestCase):
    """Where an unfinished run carries on, as the run-again menu says it
    (describe_run, from the run's record), is the stage its line showed as
    it ran (routes.begin_stage) - on every route, at every stage, chosen or
    not.  Seen: "Stopped at stage 10" for a run that carried on at 9."""

    KIND = {"espressif": "sonoff-s60", "beken": "sonoff-s61"}

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")

    def carries_on_at(self, name, inputs, done):
        state = RunState.open(self.root / name)
        for key, value in {"device_ip": "192.0.2.50", **inputs}.items():
            state.set_input(key, value)
        for step in done:
            state.mark(step, DONE)
        said = cli.describe_run(self.root / name)
        return said.split("carries on at ", 1)[1].split(")", 1)[0].split(", past", 1)[0]

    def as_shown(self, firmware, source, family, step):
        """The stage line `step` begins with, as the run draws it."""
        begun = []
        routes.apply_target(firmware, source, family)
        with mock.patch.object(output, "stage_begin", side_effect=lambda *line: begun.append(line)), \
                mock.patch.object(output, "set_stage_width"):
            routes.begin_stage(step)
        return "stage {}, {}".format(*begun[0])

    def test_every_stage_of_every_route(self):
        from tools.autoflash.routes import espressif
        checked = 0
        for name, family in routes.FAMILIES.items():
            for source in family.SOURCES:
                for firmware in family.FIRMWARES:
                    try:
                        routes.steps_for(firmware, source, family)
                    except ValueError:
                        continue  # no such route
                    ways = [(source, {})]
                    if (source, firmware) == ("tasmota", "esphome"):
                        ways = [(source, {routes.LEAVES_DIRECT: False}),
                                (espressif.DIRECT_FROM_TASMOTA, {routes.LEAVES_DIRECT: True})]
                    for route_source, extra in ways:
                        steps = [step for step, _, _ in routes.steps_for(firmware, route_source, family)]
                        for index, step in enumerate(steps):
                            inputs = {"device_kind": self.KIND[name], "source": source, "firmware": firmware, **extra}
                            with self.subTest(route=f"{name} {route_source}>{firmware}", step=step):
                                self.assertEqual(
                                    self.carries_on_at(f"{name}-{route_source}-{firmware}-{step}", inputs,
                                                       steps[:index]),
                                    self.as_shown(firmware, route_source, family, step))
                                checked += 1
        self.assertGreater(checked, 100)

    def test_a_run_stopped_before_its_firmware_was_chosen(self):
        # From Sonoff's firmware, found in eWeLink; from a converted device, the
        # prerequisites read: both carry on at "Choosing the firmware".
        for name, family in routes.FAMILIES.items():
            for source, done in (("sonoff", ["A0", "A1"]), ("esphome", ["A0"])):
                with self.subTest(family=name, source=source):
                    said = self.carries_on_at(f"{name}-{source}", {"device_kind": self.KIND[name], "source": source},
                                              done)
                    self.assertEqual(said, self.as_shown(None, source, family, "A3"))
                    self.assertTrue(said.endswith("Choosing the firmware"), said)


class StepCommandTests(unittest.TestCase):
    """`step` does one step of the run's route (cli.cmd_step)."""

    def setUp(self):
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")

    def test_a_dry_run_does_only_a_step_that_reads(self):
        # As a run's dry run stops before the first step that writes
        # (runner.run_steps): `step C2 --dry-run` sent the installer.
        from types import SimpleNamespace
        for name, family in routes.FAMILIES.items():
            routes.apply_target("esphome", "sonoff", family)
            for step, _, _ in routes.STEPS:
                with self.subTest(family=name, step=step), mock.patch.object(cli.join, "wait_while_device_busy"), \
                        mock.patch.object(output, "say") as said, \
                        mock.patch.object(cli.runner, "execute_with_retries", return_value=0) as executed:
                    self.assertEqual(cli.cmd_step(SimpleNamespace(dry_run=True), step), 0)
                    self.assertEqual(executed.called, step in family.READ_ONLY_STEPS)
                    if not executed.called:
                        self.assertIn(f"[dry-run] not doing {step}", said.call_args.args[0])


class RemoveRunTests(RunChoiceTests):
    """Unfinished runs can be removed from the menu, with care for half-converted devices."""

    def menu(self, answers, confirm=True):
        from unittest import mock
        with mock.patch.object(prompts, "ask_choice", side_effect=answers) as menu, \
             mock.patch.object(prompts, "ask_yes_no", return_value=confirm) as self.confirm, \
             mock.patch.object(prompts, "section") as section:
            name = cli.resolve_run_name(None, False, None, ask=True)
        return name, menu, section

    def test_removing_the_only_unfinished_run_then_starts_a_new_one(self):
        self.leaving_tasmota("half")
        name, _, section = self.menu([2])          # 1 continue, 2 new, 3 remove
        self.assertFalse((self.root / "half").exists())
        self.assertNotEqual(name, "half")
        self.assertEqual(self.remembered, [name])
        section.assert_not_called()                # not past the point of no return

    def test_a_half_converted_device_is_warned_about_before_removal(self):
        self.run_dir("past", ["A0", "A5", "A1", "A2", "C2"])
        _, _, section = self.menu([2])
        self.assertIn("part-converted", section.call_args.args[0])

    def test_declining_keeps_the_run_and_shows_the_menu_again(self):
        self.run_dir("half", ["A0", "A5", "A1", "A2", "C2"])
        name, menu, _ = self.menu([2, 0], confirm=False)
        self.assertTrue((self.root / "half").exists())
        self.assertEqual(name, "half")
        self.assertEqual(menu.call_count, 2)
        self.assertIs(self.confirm.call_args.kwargs["default"], False)   # Enter keeps it

    def test_with_several_runs_it_asks_which_to_remove(self):
        self.run_dir("first", ["A0", "A5", "A1", "A2", "C2"])
        self.run_dir("second", ["A0", "A5", "A1", "A2", "C2"])
        # 1-2 continue, 3 new, 4 remove; then pick the second; then start new
        name, menu, _ = self.menu([3, 1, 1])
        remaining = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(menu.call_args_list[1].args[0], "Which run should be removed")
        self.assertNotIn(name, ("first", "second"))

    def remove(self, closes, current=False):
        """remove_run on a run that left two firewall rules, `closes` saying
        whether each could be removed: whether it was, what was told."""
        from unittest import mock
        rules = [hostsys.FirewallRule("ufw", "10.0.0.54", [8088]),
                 hostsys.FirewallRule("ufw", "10.0.0.54", [8089])]
        state = RunState.open(self.root / "half")
        state.set_input("firewall_rules", [rule.to_json() for rule in rules])
        pointer = self.root / ".autoflash-current"
        pointer.write_text("half\n", encoding="utf-8")
        with mock.patch.object(prompts, "ask_yes_no", return_value=True), \
             mock.patch.object(runs, "read_current", return_value="half" if current else "other"), \
             mock.patch.object(runs, "run_name", return_value="half"), \
             mock.patch.object(runs, "CURRENT_POINTER", pointer), \
             mock.patch.object(hostsys, "close_firewall", side_effect=closes) as close, \
             mock.patch.object(output, "tell") as told:
            self.assertTrue(cli.remove_run(self.root / "half"))
        self.assertFalse((self.root / "half").exists())
        self.assertEqual(close.call_args_list, [mock.call(rule) for rule in rules])
        return "\n".join(call.args[0] for call in told.call_args_list), pointer.exists()

    def test_the_firewall_rules_a_run_left_are_removed_with_it(self):
        told, _ = self.remove([True, True])
        self.assertEqual(told, "")

    def test_a_rule_that_cannot_be_removed_is_left_to_remove_by_hand(self):
        told, _ = self.remove([True, False])
        self.assertIn("Remove it yourself:", told)
        self.assertIn("8089", told)
        self.assertNotIn("8088", told)

    def test_removing_the_current_run_forgets_it(self):
        self.assertFalse(self.remove([True, True], current=True)[1])
        self.assertTrue(self.remove([True, True], current=False)[1])


class SudoPasswordTests(unittest.TestCase):
    def setUp(self):
        import tools.autoflash.cli as autoflash
        self.autoflash = autoflash
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("AUTOFLASH_SUDO_PASSWORD", "AUTOFLASH_EMULATOR_SUDO_PASSWORD"):
            os.environ.pop(key, None)

    def test_the_emulators_password_is_only_for_an_unattended_run(self):
        os.environ["AUTOFLASH_EMULATOR_SUDO_PASSWORD"] = "emulator"
        with mock.patch.object(hostsys, "check_sudo_password", return_value=None), \
                mock.patch.object(prompts, "ask_admin_password", return_value="typed"):
            with mock.patch.object(prompts, "UNATTENDED", False):
                self.assertEqual(cli.admin_password(), "typed")
            with mock.patch.object(prompts, "UNATTENDED", True):
                self.assertEqual(cli.admin_password(), "emulator")

    def test_a_given_one_sudo_accepts_is_used(self):
        os.environ["AUTOFLASH_SUDO_PASSWORD"] = "right"
        with mock.patch.object(hostsys, "check_sudo_password", return_value=None), \
                mock.patch.object(prompts, "ask_admin_password") as asked:
            self.assertEqual(cli.admin_password(), "right")
        asked.assert_not_called()

    def test_unattended_without_one_says_what_to_give(self):
        with mock.patch.object(prompts, "UNATTENDED", True), \
                self.assertRaisesRegex(OSError, "AUTOFLASH_SUDO_PASSWORD"):
            cli.admin_password()


class ReadoutCommandTests(unittest.TestCase):
    """readout: one device, the one chosen, or with --all every device in the
    account without asking - as a script or an unattended run needs."""

    def read(self, **args):
        from types import SimpleNamespace
        from tools.autoflash import readout
        devices = [{"deviceid": "1000abcdef"}, {"deviceid": "1000fedcba"}]
        self.happened = []
        under_way = []

        def begin(number, name):
            under_way.append(True)
            self.happened.append(f"Stage {number} - {name}")

        def end(outcome):
            if under_way:   # as output: an end with no stage under way draws nothing
                under_way.clear()
                self.happened.append(outcome)

        def account_devices():
            self.happened.append("signed in")
            return devices
        ctx = SimpleNamespace(args=SimpleNamespace(**{"device_ip": None, "all": False, **args}),
                              account_devices=account_devices)
        told = mock.patch.object(cli.output, "tell", side_effect=lambda text="": self.happened.append(f"told: {text}"))
        with mock.patch.object(cli.output, "say"), told, \
                mock.patch.object(cli.output, "stage_begin", begin), mock.patch.object(cli.output, "stage_end", end), \
                mock.patch.object(readout, "run_all", return_value="all") as every, \
                mock.patch.object(readout, "read_out_chosen", return_value="chosen") as chosen, \
                mock.patch.object(readout, "end", side_effect=lambda ctx, said:
                                  self.happened.append(f"report, after: {said}")):
            self.assertEqual(cli.cmd_readout(ctx, None), 0)
        return every, chosen, devices

    def test_the_sign_in_is_the_readout_s_first_stage(self):
        # Seen: a readout chosen after "What next" left the window blank for
        # the sign-in's few seconds, as if nothing were happening.
        self.read()
        at = self.happened.index("Stage 1 - Querying eWeLink for devices")
        self.assertEqual(self.happened[at:at + 3], ["Stage 1 - Querying eWeLink for devices", "signed in", "complete"])

    def test_what_to_expect_is_said_above_the_first_stage(self):
        # As a conversion says it above its own.
        self.read()
        at = self.happened.index("Stage 1 - Querying eWeLink for devices")
        said = " ".join(item.removeprefix("told: ") for item in self.happened[:at])
        self.assertIn("Usually under 10 minutes", said)
        self.assertIn("until the last stage completes", said)

    def test_it_ends_with_the_report(self):
        self.read()
        self.assertEqual(self.happened[-1], "report, after: chosen")   # what is left to do, then the report

    def failed(self, began):
        from types import SimpleNamespace
        from tools.autoflash import readout
        from tools.autoflash.errors import Abort
        ctx = SimpleNamespace(args=SimpleNamespace(verbose=False, send=None), dir=Path("/nonexistent/run"),
                              readouts=[readout.Became("'Hall'", "BASICR4", "", "1.1.0", True, readout.FAILED)]
                              if began else [])
        with mock.patch.object(cli.output, "start"), mock.patch.object(cli.output, "finish"), \
                mock.patch.object(cli.output, "tell") as told, \
                mock.patch.object(cli.firewall, "release_firewall"), mock.patch.object(cli.runs, "forget_run_secrets"), \
                mock.patch.object(cli, "cmd_readout", side_effect=Abort("the device stopped answering")), \
                mock.patch.object(readout, "end") as ended:
            self.assertEqual(cli.read_out(ctx), 1)
        told.assert_any_call("FAILED: the device stopped answering")
        return ended

    def test_one_that_failed_ends_with_its_report_and_the_way_to_open_an_issue(self):
        # As a conversion that failed does: seen, an S61's readout ending at
        # "FAILED" with nothing under it.
        self.failed(began=True).assert_called_once_with(mock.ANY, "", failed=True)

    def test_one_that_failed_before_reaching_a_device_has_no_report(self):
        self.failed(began=False).assert_not_called()

    def test_all_reads_every_device_unasked(self):
        every, chosen, devices = self.read(all=True)
        self.assertEqual(every.call_args.args[1], devices)
        chosen.assert_not_called()

    def test_without_it_the_owner_chooses(self):
        every, chosen, _ = self.read()
        every.assert_not_called()
        chosen.assert_called_once()

    def test_it_is_an_option_of_the_command(self):
        import subprocess
        shown = subprocess.run([sys.executable, "-m", "tools.autoflash", "--help"], capture_output=True, text=True,
                               cwd=Path(cli.__file__).resolve().parents[2], timeout=60).stdout
        self.assertIn("--all", shown)


class EmailCommandTests(unittest.TestCase):
    def test_it_changes_the_address_kept_and_starts_no_run(self):
        with mock.patch("sys.argv", ["cli.py", "email"]), mock.patch.object(cli.signal, "signal"), \
                mock.patch.object(interrupt, "install"), mock.patch.object(hostsys, "password_asker"), \
                mock.patch.object(prompts, "UNATTENDED"), mock.patch.object(installer, "SIMULATED_BACKUP_FAILURES"), \
                mock.patch.object(sonoff_firmware, "SIMULATE_NONE"), mock.patch.object(identity, "SIMULATE_NONE"), \
                mock.patch.object(beken_images, "SPOIL_STEP"), mock.patch.object(beken_images, "SPOIL_TIMES"), \
                mock.patch.object(espressif_images, "SPOIL_STEP"), mock.patch.object(espressif_images, "SPOIL_TIMES"), \
                mock.patch.object(beken_images, "UNKNOWN_TABLE"), \
                mock.patch.object(cli.signup, "change", return_value=0) as change, \
                mock.patch.object(credentials, "load") as load, mock.patch.object(cli, "Context") as context:
            self.assertEqual(cli.main(), 0)
        change.assert_called_once_with()
        load.assert_not_called()
        context.assert_not_called()

    def test_it_is_a_command(self):
        import subprocess
        shown = subprocess.run([sys.executable, "-m", "tools.autoflash", "--help"], capture_output=True, text=True,
                               cwd=Path(cli.__file__).resolve().parents[2], timeout=60).stdout
        self.assertIn("email", shown)


if __name__ == "__main__":
    unittest.main()
