"""A phase on the device (tools/autoflash/phases.py): its program loaded and
fetched, waited on only while the device may still be working on it, its
failures said, and the server's lines made readable.

PhaseRunner starts the plan server (serve_safeboot_migration.py) where it is
in the repository: a script moved (the 2026-10 reorganisation took the safeboot
tooling to tools/espressif/safeboot/) stops every conversion at D5 otherwise."""

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import output, phases
from tools.autoflash.phases import ARM_RE, LOADER_RE, PhaseError, PhaseRunner, short_loader

ROOT = Path(__file__).resolve().parents[2]


class PlanServerCommandTests(unittest.TestCase):
    def test_the_plan_server_script_is_where_the_command_says(self):
        runner = PhaseRunner(repo_root=ROOT, manifest=ROOT / "m.json", evidence_dir=ROOT / "live",
                             listen_ip="192.0.2.141", device=types.SimpleNamespace(host="192.0.2.50"),
                             log=lambda _: None)
        for phase in ("stage", "commit", "restore"):
            with self.subTest(phase=phase):
                command = runner._command(phase, [])
                script = Path(command[command.index("-c") + 2])   # python -c RUN_SCRIPT <script> ...
                self.assertTrue(script.is_file(), script)
                self.assertEqual(script.name, "serve_safeboot_migration.py")


class LoaderTests(unittest.TestCase):
    URL = "http://192.0.2.141:8089/berry/commit.be"

    def test_short_loader_is_equivalent_and_much_shorter(self):
        printed = (
            "def autoflash_urlbeload(url) var wc=webclient() wc.begin(url) var st=wc.GET() "
            "if st!=200 wc.close() raise 'connection_error',format('status:%i',st) end "
            f"var code=wc.get_string() return compile(code)() end autoflash_commit=autoflash_urlbeload('{self.URL}')"
        )
        compact = short_loader(self.URL, assign_to="autoflash_commit")
        self.assertLess(len(compact), len(printed) * 2 / 3)
        for fragment in ("webclient()", self.URL, "compile(", "autoflash_commit="):
            self.assertIn(fragment, compact)

    def test_short_loader_tries_the_fetch_again_until_it_is_answered(self):
        # Under packet loss one connection timed out and the device ran nothing
        # (lossy-phase, HTP: Connection timeout): each try a new connection,
        # the first answered taken; ten of 2 s each, inside FETCH_DEADLINE.
        compact = short_loader(self.URL)
        self.assertRegex(compact, r"var c,i=0 while i<10 c=webclient\(\) c\.begin\('[^']+'\) "
                                  r"if c\.GET\(\)==200 break end i\+=1 end ")

    def test_a_phase_not_armed_is_loaded_unrun_and_nothing_of_the_last_is_left_to_start(self):
        compact = short_loader(self.URL)
        self.assertTrue(compact.startswith("autoflash_run=nil "))
        self.assertTrue(compact.endswith(" autoflash_run=compile(c.get_string())"))
        self.assertTrue(short_loader(self.URL, assign_to="autoflash_commit").endswith(
            " autoflash_commit=compile(c.get_string())()"))

    def test_loader_url_and_arm_token_are_parsed_from_server_output(self):
        printed = f"return autoflash_urlbeload('{self.URL}')"
        self.assertEqual(LOADER_RE.search(printed).group("url"), self.URL)
        found = ARM_RE.search('autoflash_commit("d2_fjQTqh4KzfHDFSefx")')
        self.assertEqual(found.group("name"), "autoflash_commit")
        self.assertEqual(found.group("token"), "d2_fjQTqh4KzfHDFSefx")

    def started(self, *outcomes):
        """How many times the start was sent, the device answering each time."""
        from unittest import mock
        device = mock.Mock(berry=mock.Mock(side_effect=list(outcomes)), answers=mock.Mock(return_value=True))
        runner = PhaseRunner(repo_root=Path("."), manifest=Path("m.json"), evidence_dir=Path("live"),
                             listen_ip="192.0.2.141", device=device, log=lambda _: None)
        run_now = lambda target, daemon: mock.Mock(start=target)
        with mock.patch("tools.autoflash.phases.threading.Thread", side_effect=run_now):
            runner._start('autoflash_restore("token")')
        self.runner = runner
        return device.berry.call_count

    def test_a_start_that_never_reached_the_device_is_sent_again(self):
        # Seen in the fault suite: a Wi-Fi blip as G1's start went, and the
        # device never began - the step failed, to go through on the next try.
        import urllib.error
        dropped = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual(self.started(dropped, None), 2)
        self.assertEqual(self.started(dropped, dropped, dropped, None), 3)

    def test_once_sent_again_the_silence_is_judged_again(self):
        import urllib.error
        self.started(urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), None)
        self.assertFalse(self.runner._resending)
        self.assertFalse(self.runner._reply_lost)

    def test_a_start_that_never_reached_the_device_in_any_try_is_known_to_have_no_reply(self):
        # So the fetch check sends it again, once the device answers.
        import urllib.error
        dropped = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        self.started(dropped, dropped, dropped)
        self.assertTrue(self.runner._reply_lost)

    def test_a_start_that_may_have_arrived_is_never_sent_twice(self):
        import io
        import socket
        import urllib.error
        for outcome in (socket.timeout("timed out"), ConnectionResetError(104, "reset"),
                        urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO())):
            self.assertEqual(self.started(outcome, None), 1, outcome)

    def test_armed_phase_refuses_to_run_without_a_confirmation_callback(self):
        runner = PhaseRunner(
            repo_root=Path("."),
            manifest=Path("m.json"),
            evidence_dir=Path("live"),
            listen_ip="192.0.2.141",
            device=None,  # never reached: the guard fires first
            log=lambda _: None,
        )
        for phase in ("commit", "restore"):
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(PhaseError, "explicit confirmation"):
                    runner.run(phase)


class FetchCheckTests(unittest.TestCase):
    """A phase stops within seconds when the device never fetches its program."""

    class Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    class Reader:
        def join(self, _):
            pass

    def runner(self, answers=(True,)):
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner.log = mock.Mock()
        runner._device_answers = mock.Mock(side_effect=list(answers))
        runner._start = mock.Mock()
        return runner

    def test_a_fetch_lets_the_phase_continue(self):
        process = self.Process()
        self.runner()._require_fetch(process, ['  [10.0.0.5] "GET /berry/preflight.be HTTP/1.1" 200 -'],
                                     self.Reader(), "preflight")
        self.assertFalse(process.terminated)

    def test_no_fetch_stops_the_server_and_says_so(self):
        from unittest import mock
        from tools.autoflash import phases
        process, runner = self.Process(), self.runner()
        with mock.patch.object(phases, "FETCH_DEADLINE", 1.0):
            with self.assertRaises(phases.DeviceNeverConnected):
                runner._require_fetch(process, [], self.Reader(), "preflight")
        self.assertTrue(process.terminated)
        runner._start.assert_not_called()  # it was answered: the device ran it, and could not reach us

    def test_a_start_lost_on_the_way_is_sent_again_not_failed(self):
        # Seen in the fault suite: L1's start sent as a Wi-Fi blip began, lost
        # in it, and the step failed as a firewall - the device answering.
        from unittest import mock
        from tools.autoflash import phases
        captured, runner = [], self.runner()
        runner._unanswered, runner._start_code = True, ("loader",)

        def fetched(_):
            runner._unanswered = False
            captured.append('  [10.0.0.5] "GET /berry/plan.be HTTP/1.1" 200 -')
        runner._start.side_effect = fetched
        with mock.patch.object(phases, "FETCH_DEADLINE", 1.0):
            runner._require_fetch(self.Process(), captured, self.Reader(), "plan")
        runner._start.assert_called_once_with("loader")

    def test_a_start_cut_off_on_the_way_is_sent_again_though_taken_for_sent(self):
        # Seen in the fault suite: the blip cut L1's start off, its request
        # ending as if sent - so with no reply to wait for.
        from unittest import mock
        from tools.autoflash import phases
        captured, runner = [], self.runner()
        runner._reply_lost, runner._start_code = True, ("loader",)
        runner._start.side_effect = lambda _: captured.append('  [10.0.0.5] "GET /berry/plan.be HTTP/1.1" 200 -')
        with mock.patch.object(phases, "FETCH_DEADLINE", 1.0):
            runner._require_fetch(self.Process(), captured, self.Reader(), "plan")
        runner._start.assert_called_once_with("loader")

    def test_a_start_s_request_cut_off_is_known_to_have_no_reply_coming(self):
        import http.client
        import threading
        from unittest import mock
        from tools.autoflash import phases
        for failure, lost in ((http.client.RemoteDisconnected("closed"), True),
                              (ConnectionResetError("reset"), True), (None, False)):
            runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
            done = threading.Event()

            def berry(code, timeout, failure=failure):
                try:
                    if failure:
                        raise failure
                    return {"Br": "nil"}
                finally:
                    done.set()
            runner.device = mock.Mock(berry=berry)
            runner._start("loader")
            done.wait(5)
            deadline = time.monotonic() + 5
            while runner._unanswered and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(runner._reply_lost, lost, failure)

    def test_a_start_lost_every_time_fails_in_the_end(self):
        from unittest import mock
        from tools.autoflash import phases
        runner = self.runner(answers=[True] * 10)
        runner._unanswered, runner._start_code = True, ("loader",)
        with mock.patch.object(phases, "FETCH_DEADLINE", 1.0):
            with self.assertRaises(phases.DeviceNeverConnected):
                runner._require_fetch(self.Process(), [], self.Reader(), "plan")
        self.assertEqual(runner._start.call_count, phases.START_TRIES - 1)

    def test_no_verdict_while_a_start_that_never_arrived_waits_to_go_again(self):
        from unittest import mock
        from tools.autoflash import phases
        captured, runner, joins = [], self.runner(), []
        runner._resending = True

        class Reader:
            def join(self, _):
                joins.append(1)
                if len(joins) == 10:  # back, and sent again: the deadline anew
                    runner._resending, runner._started_at = False, 2.0
                if len(joins) == 13:
                    captured.append('  [10.0.0.5] "GET /berry/plan.be HTTP/1.1" 200 -')
        with mock.patch.object(phases, "FETCH_DEADLINE", 2.0):
            runner._require_fetch(self.Process(), captured, Reader(), "plan")
        runner._device_answers.assert_not_called()


class PhaseFailureTests(unittest.TestCase):
    """A phase that does not pass says why: the device's words, or the timeout's."""

    def test_the_devices_reason_is_given(self):
        from tools.autoflash.phases import failure_reason
        captured = "STAGE RESULT: FAIL\nTHE DEVICE SAYS: migration_refused: GET /chunk/safeboot/2 returned HTTP -1\n"
        self.assertEqual(failure_reason(captured),
                         "the device says: migration_refused: GET /chunk/safeboot/2 returned HTTP -1")

    def test_a_timeout_is_given(self):
        from tools.autoflash.phases import failure_reason
        self.assertIn("sent nothing for 6 minutes",
                      failure_reason("TIMEOUT: the device has sent nothing for 6 minutes, and gave no reason."))

    def test_nothing_known_is_none(self):
        from tools.autoflash.phases import failure_reason
        self.assertIsNone(failure_reason("  [10.0.0.63] \"GET /berry/stage.be HTTP/1.1\" 200 -"))

    def test_the_step_abort_carries_the_reason(self):
        error = phases.phase_failed("stage", 4, "THE DEVICE SAYS: flash is not exactly 4 MiB")
        self.assertEqual(str(error), "stage did not pass: the device says: flash is not exactly 4 MiB")
        # The server's own marker now, and the old one a log of an earlier version holds.
        error = phases.phase_failed("stage", 4, "THE DEVICE SAYS: flash is not exactly 4 MiB")
        self.assertEqual(str(error), "stage did not pass: the device says: flash is not exactly 4 MiB")
        self.assertEqual(str(phases.phase_failed("stage", 3, "")), "stage did not pass (exit 3)")

    def console(self, lines):
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        runner = PhaseRunner.__new__(PhaseRunner)
        shown = []
        runner.log = shown.append
        runner.device = mock.Mock(console_log=mock.Mock(return_value=lines))
        return runner._show_device_console(), shown

    def test_the_devices_console_is_read_for_its_own_words(self):
        # As the emulated device logged it, cut off from the computer mid-stage.
        said, shown = self.console([
            "13:56:01.000 WIF: Connected",
            "13:56:03.251 AUTOFLASH STAGE: 65536/811824 bytes",
            "13:57:11.702 HTP: Connection timeout after -1 ms",
            '13:57:11.702 RSL: RESULT = {"Br":"[migration_refused] POST /capture/staged/22 '
            'returned HTTP -1 after 21 attempts"}',
        ])
        self.assertEqual(said, "POST /capture/staged/22 returned HTTP -1 after 21 attempts")
        self.assertEqual(len(shown), 2)
        self.assertTrue(all(line.startswith("    The device's console: ") for line in shown))

    def test_another_berry_error_keeps_its_kind(self):
        said, _ = self.console(['RSL: RESULT = {"Br":"[index_error] list index out of range"}'])
        self.assertEqual(said, "index_error: list index out of range")

    def test_an_earlier_attempts_error_is_not_this_ones_reason(self):
        said, shown = self.console([
            '13:29:34.062 RSL: RESULT = {"Br":"[migration_refused] POST /capture/staged/2 '
            'returned HTTP -1 after 107 attempts"}',
            "13:31:03.251 AUTOFLASH STAGE: 65536/811824 bytes",
        ])
        self.assertIsNone(said)
        self.assertEqual(len(shown), 2)  # still shown, for whoever reads the log

    def test_a_device_stuck_after_a_phase_is_to_be_switched_off_and_on(self):
        # Tasmota waits for ever on a download cut off part way, answering
        # nothing; switching it off and on is the way out, and safe here.
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        for answers, advised in ((False, True), (True, False)):
            runner = PhaseRunner.__new__(PhaseRunner)
            shown = []
            runner.log = shown.append
            runner._device_answers = mock.Mock(return_value=answers)
            runner._advise_if_stuck()
            self.assertEqual(any("switch it off" in line for line in shown), advised, answers)

    def test_a_device_that_cannot_answer_adds_nothing(self):
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        runner = PhaseRunner.__new__(PhaseRunner)
        shown = []
        runner.log = shown.append
        runner.device = mock.Mock(console_log=mock.Mock(side_effect=OSError("unreachable")))
        runner._show_device_console()
        self.assertEqual(shown, [])


class PhaseWaitTests(unittest.TestCase):
    """A phase is waited on only while the device may still be working on it."""

    class Server:
        def __init__(self, exits_after=None):
            self.calls = 0
            self.exits_after = exits_after

        def wait(self, timeout):
            import subprocess
            self.calls += 1
            if self.exits_after is not None and self.calls >= self.exits_after:
                return 0
            raise subprocess.TimeoutExpired("server", timeout)

        def poll(self):
            return None

    def wait(self, server, answers, heard_ago=60.0, exec_timeout=540.0):
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner._device_answers = mock.Mock(side_effect=answers)
        clock = iter(range(1000, 100_000, 2))
        with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)):
            return runner._wait("stage", server, exec_timeout, lambda: 1000 - heard_ago), runner

    def test_a_device_that_answers_after_going_quiet_has_stopped(self):
        from tools.autoflash.phases import PhaseError
        with self.assertRaisesRegex(PhaseError, "stopped the stage without reaching this computer"):
            self.wait(self.Server(), answers=[True])

    def test_a_busy_device_is_waited_on(self):
        # Not answering: still at it (or offline, riding out a router restart).
        code, runner = self.wait(self.Server(exits_after=40), answers=[False] * 100)
        self.assertEqual(code, 0)
        self.assertGreater(runner._device_answers.call_count, 1)

    def test_a_device_still_sending_is_not_disturbed(self):
        code, runner = self.wait(self.Server(exits_after=20), answers=[True] * 100, heard_ago=-10_000)
        self.assertEqual(code, 0)
        runner._device_answers.assert_not_called()

    def test_no_verdict_while_a_start_that_never_arrived_waits_to_go_again(self):
        # Seen in the fault suite: a blip took the device off its Wi-Fi for 18 s
        # as the start went; this check failed the step just before it went again.
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner._resending = True
        runner._device_answers = mock.Mock(return_value=True)
        clock = iter(range(1000, 100_000, 2))
        with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(runner._wait("restore", self.Server(exits_after=30), 540.0, lambda: 0), 0)
        runner._device_answers.assert_not_called()

    def test_a_start_lost_on_the_way_is_sent_again_not_failed(self):
        # Seen in the fault suite: L1's start sent as a Wi-Fi blip began. The
        # device answered 40 s on, never having run it, and the step failed.
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        shown = []
        runner.log = shown.append
        runner._device_answers = mock.Mock(side_effect=[True] + [False] * 100)  # then busy running it
        runner._start_code = ("autoflash_run()",)
        runner._unanswered = True
        sent = []
        runner._start = sent.append
        clock = iter(range(1000, 100_000, 2))
        with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(runner._wait("stage", self.Server(exits_after=15), 540.0, lambda: 0), 0)
        self.assertEqual(sent, ["autoflash_run()"])
        self.assertIn("    The start did not reach the device; sending it again.", shown)

    def test_a_start_cut_off_on_the_way_is_sent_again_though_taken_for_sent(self):
        # Seen in the fault suite: L1's start cut off by a Wi-Fi blip, its
        # request ending as if sent. The device asked for nothing after it.
        from unittest import mock
        from tools.autoflash import phases
        for asked, again in ((900, ["autoflash_run()"]), (None, ["autoflash_run()"]), (1100, [])):
            runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
            runner.log = lambda line: None
            runner._device_answers = mock.Mock(side_effect=[True] + [False] * 100)
            runner._start_code, runner._started_at = ("autoflash_run()",), 1000
            sent = []
            runner._start = sent.append
            clock = iter(range(1200, 100_000, 2))
            with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)):
                if again:
                    runner._wait("stage", self.Server(exits_after=15), 540.0, lambda: 0, lambda: asked)
                else:  # it asked after the start, so ran it: stopped, as before
                    with self.assertRaisesRegex(phases.PhaseError, "without reaching this computer"):
                        runner._wait("stage", self.Server(), 540.0, lambda: 0, lambda: asked)
            self.assertEqual(sent, again, asked)

    def test_a_start_lost_every_time_fails_in_the_end(self):
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner.log = lambda line: None
        runner._device_answers = mock.Mock(return_value=True)
        runner._start_code = ("autoflash_run()",)
        runner._unanswered = True
        runner._start = mock.Mock()
        clock = iter(range(1000, 100_000, 2))
        with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)), \
                self.assertRaisesRegex(phases.PhaseError, "without reaching this computer"):
            runner._wait("stage", self.Server(), 540.0, lambda: 0)
        self.assertEqual(runner._start.call_count, phases.START_TRIES - 1)

    def test_the_last_start_s_reply_ends_its_wait_not_an_earlier_one_s(self):
        import threading
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        first, second = threading.Event(), threading.Event()
        replies = iter([first, second])
        runner.device = mock.Mock(berry=lambda code, timeout: next(replies).wait(5))
        runner._start("autoflash_run()")
        runner._start("autoflash_run()")
        first.set()
        for _ in range(50):
            threading.Event().wait(0.01)
        self.assertTrue(runner._unanswered)
        second.set()
        for _ in range(500):
            if not runner._unanswered:
                break
            threading.Event().wait(0.01)
        self.assertFalse(runner._unanswered)

    def test_quiet_is_counted_from_the_start_when_it_went_later(self):
        from unittest import mock
        from tools.autoflash import phases
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner._started_at = 1_000_000.0  # sent again just now: not quiet yet
        runner._device_answers = mock.Mock(return_value=True)
        clock = iter(range(1000, 1060, 2))
        with mock.patch.object(phases.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(runner._wait("restore", self.Server(exits_after=25), 540.0, lambda: 0), 0)
        runner._device_answers.assert_not_called()

    def test_the_backstop_still_ends_it(self):
        from tools.autoflash.phases import PhaseError
        with self.assertRaisesRegex(PhaseError, "did not complete within 1 minutes"):
            self.wait(self.Server(), answers=[False] * 1000, exec_timeout=60.0)

    def test_starting_does_not_wait_for_a_reply(self):
        # The commit restarts the device: its reply never comes, and the result
        # is already in - this used to cost a minute per conversion.
        import threading
        from unittest import mock
        from tools.autoflash.phases import PhaseRunner
        runner = PhaseRunner.__new__(PhaseRunner)
        release = threading.Event()
        runner.device = mock.Mock(berry=mock.Mock(side_effect=lambda *a, **k: release.wait(5)))
        runner.log = mock.Mock()
        runner._start('autoflash_commit("x")')
        release.set()
        runner.device.berry.assert_called_once()


    def started_again(self, phase, arm, lost):
        """The commands a device is sent when its start goes again: by `lost`,
        given the runner and its start, standing in for what notices."""
        import time as real_time
        from unittest import mock
        from tools.autoflash import phases
        url = "http://192.0.2.1:8088/berry/stage.be"
        runner = phases.PhaseRunner.__new__(phases.PhaseRunner)
        runner.log = lambda line: None
        sent = []
        runner.device = mock.Mock()
        runner._require_fetch = mock.Mock()
        runner._device_back = mock.Mock(return_value=True)
        runner.device.berry.side_effect = lambda code, timeout: sent.append(code) or lost(runner, code)

        def wait(*args):
            deadline = real_time.monotonic() + 5
            while len(sent) < 4 and real_time.monotonic() < deadline:
                real_time.sleep(0.01)
            return 0
        runner._wait = wait
        runner._drive(phase, mock.Mock(), [], mock.Mock(), lambda phase: True, 540.0, lambda: url, lambda: arm)
        return url, sent

    def test_a_start_sent_again_fetches_its_instructions_again_first(self):
        # Seen in the fault suite: K1's device switched off just after it fetched
        # its instructions. Restarted, it had lost them, and answered the start,
        # sent again as it came back, with "'autoflash_run' undeclared".
        import urllib.error
        for phase, arm, start in (("stage", None, "autoflash_run()"),
                                  ("restore", ("autoflash_restore", "t0k3n"), 'autoflash_restore("t0k3n")')):
            with self.subTest(phase):
                gone = []

                def lost(runner, code):
                    if code == start and not gone:   # the device off as the start went
                        gone.append(code)
                        raise urllib.error.URLError("no route to host")
                url, sent = self.started_again(phase, arm, lost)
                self.assertIn(url, sent[0])
                self.assertEqual(sent[1], start)       # off as it went
                self.assertIn(url, sent[2])            # back: fetched again,
                self.assertEqual(sent[3], start)       # then started, a command of its own
                self.assertNotIn(start, sent[2])       # never in one: the device runs out of stack


class PhaseRunTests(unittest.TestCase):
    """PhaseRunner.run against a stand-in for serve_safeboot_migration.py: a
    script printing what the server prints, and exiting as it would."""

    URL = "http://10.0.0.5:8089/berry/{phase}.be"
    FETCH = '10.0.0.54 - - "GET /berry/{phase}.be HTTP/1.1" 200 -'
    CHUNK = '10.0.0.54 - - "GET /chunk/1 HTTP/1.1" 200 -'
    SERVER = """
import sys, time
received = sys.stdin.buffer.read() if {piped} else b""
open({stdin_file!r}, "w").write(str(len(received)))
for line in {lines!r}:
    if line.startswith("sleep "):
        time.sleep(float(line[6:]))
    else:
        print(line, flush=True)
sys.exit({code})
"""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.logged = []
        self.device = mock.Mock(host="10.0.0.54", console_log=mock.Mock(return_value=[]),
                                answers=mock.Mock(return_value=True))
        mock.patch.dict(output._private, {}, clear=True).start()
        mock.patch.object(output, "raw_progress").start()
        self.addCleanup(mock.patch.stopall)

    def run_phase(self, phase, lines, code=0, confirm=None, plan=None, recovery=None):
        server = self.root / "server.py"
        server.write_text(self.SERVER.format(piped=bool(plan or recovery), stdin_file=str(self.root / "stdin"),
                                             lines=[line.format(phase=phase, url=self.URL.format(phase=phase))
                                                    for line in lines], code=code), encoding="utf-8")
        runner = PhaseRunner(repo_root=self.root, manifest=self.root / "m.json", evidence_dir=self.root / "live",
                             listen_ip="10.0.0.5", device=self.device, log=self.logged.append, recovery=recovery)
        with mock.patch.object(PhaseRunner, "_command", return_value=[sys.executable, "-u", str(server)]):
            result = runner.run(phase, confirm=confirm, plan=plan)
        return result, runner

    def loader(self):
        return "autoflash_urlbeload('{url}')"

    def test_a_phase_runs_and_its_log_keeps_nothing_private(self):
        output.private("Home-2G-made-up", "wifi-name")
        plan = mock.Mock(bundle=mock.Mock(return_value=b"x" * 1234), title="the plan", blocks=lambda: 1)
        plan.name = "move"
        (code, said), runner = self.run_phase(
            "plan", [self.loader(), "sleep 0.6", self.FETCH, "sleep 0.6", self.CHUNK, "joined Home-2G-made-up"],
            plan=plan)
        self.assertEqual(code, 0)
        self.assertIn("joined Home-2G-made-up", said)
        self.assertEqual((self.root / "stdin").read_text(encoding="utf-8"), "1234")  # the plan, piped
        log = (self.root / "live" / "plan-move-server.log").read_text(encoding="utf-8")
        self.assertIn("joined <wifi-name>", log)
        self.assertNotIn("Home-2G-made-up", log)
        self.assertIsNotNone(runner.busy_seconds)  # the device's last request came after the start
        sent = [c.args[0] for c in self.device.berry.call_args_list]
        self.assertIn("compile(", sent[0])
        # Started by a short command of its own, not from the loader's line:
        # the plan, run from there, overflowed the device's stack.
        self.assertEqual(sent[1:], [f"{phases.START_NAME}()"])

    def test_a_server_with_no_loader_stops_the_phase_with_what_it_said(self):
        with self.assertRaisesRegex(PhaseError, "did not print a loader URL:\nport in use"):
            self.run_phase("stage", ["port in use"], code=1)
        self.device.berry.assert_not_called()

    def test_an_armed_phase_not_confirmed_is_never_started(self):
        confirm = mock.Mock(return_value=False)
        with self.assertRaisesRegex(PhaseError, "not confirmed; nothing was armed"):
            self.run_phase("commit", [self.loader(), 'autoflash_commit("t0ken")', self.FETCH, "sleep 30"],
                           confirm=confirm)
        confirm.assert_called_once_with("commit")
        sent = [c.args[0] for c in self.device.berry.call_args_list]
        self.assertEqual(len(sent), 1)                # the instructions went, ...
        self.assertIn("autoflash_commit=", sent[0])   # ... the start never did

    def test_an_armed_phase_confirmed_is_started_with_its_token(self):
        (code, _), _ = self.run_phase("commit", [self.loader(), 'autoflash_commit("t0ken")', self.FETCH, "sleep 0.5"],
                                      confirm=lambda phase: True)
        self.assertEqual(code, 0)
        sent = [c.args[0] for c in self.device.berry.call_args_list]
        self.assertIn("autoflash_commit=", sent[0])
        self.assertEqual(sent[1:], ['autoflash_commit("t0ken")'])

    def test_an_armed_phase_with_no_arm_command_is_never_started(self):
        with self.assertRaisesRegex(PhaseError, "did not print an arm command"):
            self.run_phase("restore", [self.loader()], confirm=lambda phase: True)
        self.device.berry_fire.assert_not_called()
        self.device.berry.assert_not_called()

    def test_a_device_that_never_fetches_its_instructions_stops_the_phase_at_once(self):
        # The server would go on to fail by itself, saying less.
        for phase, lines, confirm in (
                ("stage", [self.loader(), "sleep 4"], None),
                ("commit", [self.loader(), 'autoflash_commit("t0ken")', "sleep 4"], lambda phase: True)):
            with self.subTest(phase), mock.patch.object(phases, "FETCH_DEADLINE", 1.0):
                with self.assertRaises(phases.DeviceNeverConnected):
                    self.run_phase(phase, lines, code=1, confirm=confirm)
                self.assertEqual(len(self.device.berry.call_args_list), 1)   # never told to start
                self.device.berry.reset_mock()

    def test_a_phase_stopped_part_way_stops_its_server(self):
        import subprocess
        started, real = [], subprocess.Popen

        def popen(*args, **kwargs):
            started.append(real(*args, **kwargs))
            self.addCleanup(started[-1].kill)
            return started[-1]
        with mock.patch.object(phases.subprocess, "Popen", side_effect=popen), self.assertRaises(PhaseError):
            self.run_phase("commit", [self.loader(), 'autoflash_commit("t0ken")', self.FETCH, "sleep 30"],
                           confirm=lambda phase: False)
        self.assertIsNotNone(started[0].wait(timeout=5))

    def test_each_run_starts_afresh_keeping_the_last_ones_log(self):
        self.run_phase("stage", [self.loader(), "sleep 0.6", self.FETCH, "sleep 0.6", self.CHUNK])
        (code, _), runner = self.run_phase("stage", [self.loader(), "sleep 0.6", self.FETCH, "sleep 0.6", self.CHUNK])
        self.assertIsNotNone(runner.busy_seconds)
        self.assertTrue((self.root / "live" / "stage-server.1.log").is_file())
        # A run that never started has kept the device busy for no time at all, not the last one's.
        with mock.patch.object(PhaseRunner, "_command", return_value=[sys.executable, "-c", "print('port in use')"]):
            with self.assertRaises(PhaseError):
                runner.run("stage")
        self.assertIsNone(runner.busy_seconds)

    def test_a_server_failing_says_the_devices_reason_once(self):
        self.device.console_log.return_value = ['RSL: RESULT = {"Br":"[migration_refused] its console\'s reason"}']
        (code, said), _ = self.run_phase("stage", [self.loader(), self.FETCH], code=1)
        self.assertTrue(said.endswith("THE DEVICE SAYS: its console's reason"), said)
        # Where the server has said why, it is not said again.
        (code, said), _ = self.run_phase("stage", [self.loader(), self.FETCH, "THE DEVICE SAYS: the server's reason"],
                                         code=1)
        self.assertTrue(said.endswith("THE DEVICE SAYS: the server's reason"), said)

    def test_a_failed_phase_carries_the_devices_own_reason(self):
        self.device.console_log.return_value = [
            'RSL: RESULT = {"Br":"[migration_refused] the staged image did not verify"}']
        (code, said), _ = self.run_phase("stage", [self.loader(), self.FETCH], code=3, recovery=b"image")
        self.assertEqual(code, 3)
        self.assertEqual((self.root / "stdin").read_text(encoding="utf-8"), "5")  # the recovery image, piped
        self.assertTrue(said.endswith("THE DEVICE SAYS: the staged image did not verify"), said)
        self.assertTrue(any("stage-server.log" in line for line in self.logged), self.logged)


class ServerLogTests(unittest.TestCase):
    def test_each_try_keeps_its_own_server_log_the_latest_where_the_evidence_reads_it(self):
        # Seen on the emulator: K1 failed, was tried again and passed, and the
        # failed try's log, written over, could not say why.
        from tools.autoflash import phases
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "commit-server.log"
            for attempt in ("first", "second", "third"):
                phases.keep_earlier_try(log)
                log.write_text(attempt, encoding="utf-8")
            self.assertEqual(log.read_text(encoding="utf-8"), "third")
            self.assertEqual([(Path(directory) / f"commit-server.{n}.log").read_text(encoding="utf-8")
                              for n in (1, 2)], ["first", "second"])


class PhaseDisplayTests(unittest.TestCase):
    """Hundreds of server request lines become a few readable ones."""

    def run_lines(self, phase, blocks, lines, title=None):
        from tools.autoflash.phases import PhaseDisplay
        written, logged = [], []
        display = PhaseDisplay(phase, blocks, write=written.append, log=logged.append, title=title)
        for line in lines:
            display.feed(line)
        display.close()
        return "".join(written), logged

    def test_staging_is_one_progress_line_and_the_result(self):
        lines = ['  server| Phase: stage', '  [10.0.0.5] "GET /berry/stage.be HTTP/1.1" 200 -']
        for n in range(198):
            lines += [f'  [10.0.0.5] "GET /chunk/safeboot/{n} HTTP/1.1" 200 -',
                      f'  [10.0.0.5] "POST /capture/staged/{n} HTTP/1.1" 200 -']
        lines += ['  [10.0.0.5] "POST /report/stage HTTP/1.1" 200 -', "", "STAGE RESULT: PASS",
                  "Evidence: /tmp/x/stage-report.json"]
        written, logged = self.run_lines("stage", 198, lines)
        self.assertEqual(logged, ["    The device fetched its instructions.", "    STAGE RESULT: PASS"])
        self.assertIn("block 198/198", written)
        self.assertNotIn("memory layout", written)   # its captures are not a preflight's
        self.assertNotIn("\n", written.rstrip("\n"), "progress stays on one line")

    def test_installing_esphome_counts_its_blocks(self):
        lines = ['  [10.0.0.5] "GET /berry/plan.be HTTP/1.1" 200 -']
        for n in range(226):
            lines += [f'  [10.0.0.5] "GET /chunk/esphome/{n} HTTP/1.1" 200 -',
                      f'  [10.0.0.5] "POST /capture/esphome/{n} HTTP/1.1" 200 -']
        written, _ = self.run_lines("plan", 226, lines, title="ESPHome")
        self.assertIn("Copying ESPHome: block 226/226", written)
        self.assertNotIn("startup firmware", written)

    def test_the_preflight_counts_what_it_reads_and_a_line_after_ends_the_progress(self):
        from tools.autoflash.phases import PhaseDisplay
        written, logged = [], []
        display = PhaseDisplay("preflight", None, write=written.append, log=logged.append)
        for n in range(2):
            display.feed(f'  [10.0.0.5] "POST /capture/flash/{n} HTTP/1.1" 200 -')
        display.feed("PREFLIGHT RESULT: PASS")
        display.close()
        self.assertEqual(written, ["\r    Reading the device's memory layout: 1 blocks   ",
                                   "\r    Reading the device's memory layout: 2 blocks   ", "\n"])
        self.assertEqual(logged, ["    PREFLIGHT RESULT: PASS"])

    def test_how_many_blocks_a_phase_copies(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "m.json"
            manifest.write_text(json.dumps({"artifacts": {"safeboot": {"size": 4096 * 3 + 1},
                                                          "recovery_safeboot": {"size": 4096 * 2}}}),
                                encoding="utf-8")
            runner = PhaseRunner(repo_root=Path(directory), manifest=manifest, evidence_dir=Path(directory),
                                 listen_ip="192.0.2.141", device=None, log=lambda _: None)
            self.assertEqual(runner._blocks("stage"), 2)      # the recovery image's
            self.assertEqual(runner._blocks("restore"), 4)    # a part block is a block
            self.assertIsNone(runner._blocks("commit"))
            self.assertEqual(runner._blocks("plan", mock.Mock(blocks=lambda: 57)), 57)

    def test_refusals_are_always_shown(self):
        _, logged = self.run_lines("commit", None, ["  REFUSED /chunk/safeboot/9: outside the artifact",
                                                    "REFUSING: could not bind server"])
        self.assertEqual(len(logged), 2)


if __name__ == "__main__":
    unittest.main()
