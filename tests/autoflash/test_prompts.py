"""What the tool asks (tools/autoflash/prompts.py): a secret shown only as it
is typed, keys typed ahead thrown away, a way to quit at every question, the
answers of an unattended run, and the device's setup network looked for."""
import io
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.autoflash.test_names import SetupNetworkTestCase
from tools.autoflash import names, output, prompts


class TypedAheadTests(unittest.TestCase):
    """A key pressed while nothing was asking - during the look round the
    network, with nothing on screen - is not the next question's answer (seen
    in the emulator: an Enter taken as an empty answer to "Which device")."""

    def test_dropped_on_a_terminal(self):
        with mock.patch.object(prompts.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(prompts.sys.stdin, "fileno", return_value=0), \
             mock.patch.object(prompts.hostsys, "IS_WINDOWS", False), \
             mock.patch("termios.tcflush") as flush:
            prompts.discard_typed_ahead()
        import termios
        flush.assert_called_once_with(0, termios.TCIFLUSH)

    def test_piped_answers_are_left_alone(self):
        with mock.patch.object(prompts.sys.stdin, "isatty", return_value=False), \
             mock.patch("termios.tcflush") as flush:
            prompts.discard_typed_ahead()
        flush.assert_not_called()

    def test_every_question_drops_it_first(self):
        order = []
        with mock.patch.object(prompts, "discard_typed_ahead", side_effect=lambda: order.append("dropped")), \
             mock.patch("builtins.input", side_effect=lambda _: order.append("asked") or "2"), \
             mock.patch.object(prompts, "UNATTENDED", False), mock.patch.object(prompts, "_emit"):
            self.assertEqual(prompts.ask_choice("Which device is this run for", ["a", "b"]), 1)
        self.assertEqual(order, ["dropped", "asked"])


class SecretPromptTests(unittest.TestCase):
    """Only the character just typed is ever readable, and never after Enter."""

    def test_only_the_last_character_is_shown(self):
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        for char in "pass":
            self.assertTrue(line.feed(char))
        self.assertEqual(line.display(reveal_last=True), "***s")
        self.assertEqual(line.display(reveal_last=False), "****")
        self.assertEqual(line.value, "pass")

    def test_backspace_and_ctrl_u_edit_the_value(self):
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        for char in "pasz\x7fs":
            line.feed(char)
        self.assertEqual(line.value, "pass")
        line.feed("\x15")
        self.assertEqual((line.value, line.display(True)), ("", ""))

    def test_control_characters_are_not_stored(self):
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        for char in "a\tb\x01c":
            line.feed(char)
        self.assertEqual(line.value, "abc")

    def test_only_a_character_added_is_revealed(self):
        # An edit or a control key shows no character: revealing one then
        # would show the one before it.
        from tools.autoflash.prompts import SecretLine
        line = SecretLine()
        line.feed("a")
        line.feed("b")
        self.assertEqual([line.feed(char) for char in ("\x7f", "\x01", "\x15")], [False, False, False])

    def test_typing_ahead_is_dropped_before_a_password_is_asked(self):
        order = []
        with mock.patch.object(prompts, "discard_typed_ahead", side_effect=lambda: order.append("dropped")), \
             mock.patch.object(prompts, "_ask_secret", side_effect=lambda *_: order.append("asked") or "secret"):
            self.assertEqual(prompts.ask_secret("Password: "), "secret")
        self.assertEqual(order, ["dropped", "asked"])

    def test_ctrl_c_at_a_password_stops_as_ctrl_c(self):
        with mock.patch.object(prompts, "UNATTENDED", False), \
             mock.patch.object(prompts, "_read_secret", side_effect=KeyboardInterrupt), \
             mock.patch.object(prompts.output, "after_secret") as shown, \
             mock.patch.multiple(prompts.output._s, after_blank=False, after_stage=False, waited=False), \
             mock.patch("sys.stdout", io.StringIO()), self.assertRaises(KeyboardInterrupt):
            prompts._ask_secret("Password: ")
        self.assertNotIn("*", shown.call_args.args[0])

    @unittest.skipIf(sys.platform == "win32", "needs a POSIX pseudo-terminal")
    def test_a_real_terminal_never_keeps_a_readable_character(self):
        import pty
        import subprocess
        import sys

        master, slave = pty.openpty()
        code = (
            "from tools.autoflash.prompts import ask_secret\n"
            "value = ask_secret('Password: ')\n"
            "print('LEN', len(value), value == 'Secret-9x')\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
            cwd=Path(__file__).resolve().parents[2],
        )
        os.close(slave)
        output = b""

        def drain(seconds, until=None):
            nonlocal output
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not (until and until in output):
                import select
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output += os.read(master, 4096)
                    except OSError:
                        return

        # Typed only once the prompt is up: a busy machine can take more than
        # a second to start the child, and keys typed before then are lost.
        drain(15.0, until=b"> ")
        for char in "Secret-9x":
            os.write(master, char.encode())
            drain(0.05)
        os.write(master, b"\r")
        drain(1.0)
        process.wait(timeout=5)
        os.close(master)

        text = output.decode(errors="replace")
        self.assertIn("LEN 9 True", text)
        # The prompt, with its quit hint, is too long to share the input row in
        # an 80-column terminal, so it sits on its own line above "> ".
        self.assertIn("Password (q to quit):", re.sub(r"\x1b\[[0-9;]*m", "", text))   # in grey
        # What is left on the input row after its final redraw: asterisks only.
        input_rows = [row for row in text.split("\n") if "> " in row]
        final = input_rows[-1].rstrip("\r").split("\r")[-1].replace("\x1b[K", "")
        self.assertEqual(final.strip(), "> *********")
        # While typing, each character appeared alone at the end, never with the rest.
        self.assertIn("> ********x", text)
        self.assertNotIn("Secret", text)


class QuitOptionTests(unittest.TestCase):
    """Every kind of prompt offers a way to quit, and quitting stops cleanly."""

    def answer(self, function, *args, reply, **kwargs):
        from unittest import mock
        with mock.patch("builtins.input", return_value=reply), \
             mock.patch.object(prompts, "_emit"):
            return function(*args, **kwargs)

    def test_a_menu_numbers_only_its_options_and_q_quits(self):
        # q quits at every question, said in grey after it: never a number.
        from unittest import mock
        shown, asked = [], []
        with mock.patch("builtins.input", side_effect=lambda prompt: asked.append(prompt) or "q"), \
             mock.patch.object(prompts, "_emit", side_effect=shown.extend):
            with self.assertRaises(prompts.QuitRun):
                prompts.ask_choice("Which", ["One", "Two"])
        self.assertEqual(shown, ["1. One", "2. Two"])
        self.assertIn("(q to quit)", asked[0])

    def test_a_number_past_the_options_is_asked_again(self):
        from unittest import mock
        replies = iter(["3", "2"])
        with mock.patch("builtins.input", side_effect=lambda prompt: next(replies)), \
             mock.patch.object(prompts, "_emit"), mock.patch.object(prompts, "_retry") as retried:
            self.assertEqual(prompts.ask_choice("Which", ["One", "Two"]), 1)
        self.assertIn("between 1 and 2", retried.call_args.args[1][0])

    def test_a_menu_choice_still_works(self):
        self.assertEqual(self.answer(prompts.ask_choice, "Which", ["One", "Two"], reply="2"), 1)

    def test_q_quits_yes_no_and_typed_questions(self):
        for function, args in ((prompts.ask_yes_no, ("Go on",)), (prompts.ask_text, ("Name",))):
            with self.subTest(function=function.__name__):
                with self.assertRaises(prompts.QuitRun):
                    self.answer(function, *args, reply="q")

    def test_a_lone_q_at_a_password_prompt_quits(self):
        from unittest import mock
        with mock.patch.object(prompts, "_read_secret", return_value="q"):
            with self.assertRaises(prompts.QuitRun):
                prompts.ask_secret("Password: ")
        with mock.patch.object(prompts, "_read_secret", return_value="quietly-long-password"):
            self.assertEqual(prompts.ask_secret("Password: "), "quietly-long-password")

    def test_one_device_of_several_is_passed_by_with_s_and_q_still_quits(self):
        from unittest import mock
        asked = []
        with mock.patch("builtins.input", side_effect=lambda prompt: asked.append(prompt) or "s"), \
             mock.patch.object(prompts, "discard_typed_ahead"):
            self.assertFalse(prompts.require_word(prompts.CONSENT_WORD, skip=True))
        self.assertIn("(s to skip this device)", asked[0])
        self.assertIn("(q to quit)", asked[0])
        with mock.patch("builtins.input", return_value="q"), mock.patch.object(prompts, "discard_typed_ahead"):
            with self.assertRaises(prompts.QuitRun):
                prompts.require_word(prompts.CONSENT_WORD, skip=True)

    def test_s_is_no_answer_where_nothing_can_be_skipped(self):
        from unittest import mock
        replies = iter(["s", "q"])
        with mock.patch("builtins.input", side_effect=lambda prompt: next(replies)), \
             mock.patch.object(prompts, "discard_typed_ahead"):
            self.assertFalse(prompts.require_word(prompts.CONSENT_WORD))
        self.assertEqual(next(replies, "both read"), "both read")

    def test_quitting_a_standalone_tool_is_a_clean_exit(self):
        self.assertTrue(issubclass(prompts.QuitRun, SystemExit))
        stopped = prompts.QuitRun("stopped at an operator prompt")
        self.assertEqual((stopped.code, stopped.reason), ("Stopped.", "stopped at an operator prompt"))

    def test_enter_takes_the_default_and_anything_typed_replaces_it(self):
        replies = iter(["", "other", "", "q"])
        with mock.patch("builtins.input", side_effect=lambda _: next(replies)), \
             mock.patch.object(prompts, "_emit"), mock.patch.object(prompts, "_retry"):
            self.assertEqual(prompts.ask_text("Name", default="kept"), "kept")
            self.assertEqual(prompts.ask_text("Name", default="kept"), "other")
            with self.assertRaises(prompts.QuitRun):   # no default: Enter is no answer
                prompts.ask_text("Name")

    def test_a_section_is_shown_unless_it_is_quiet(self):
        with mock.patch.object(output, "show") as shown, mock.patch.object(output, "detail") as logged:
            prompts.section("Shown", ["a line"])
            prompts.section("Logged", ["a line"], quiet=True)
        self.assertIn(mock.call("-- Shown"), shown.call_args_list)
        self.assertIn(mock.call("-- Logged"), logged.call_args_list)
        self.assertNotIn(mock.call("-- Logged"), shown.call_args_list)


class SearchTests(SetupNetworkTestCase):
    def find(self, visible, ctx, *later):
        scans = iter([visible, *later])
        with mock.patch.object(prompts, "scan_wifi_ssids", side_effect=lambda **_: next(scans, visible)), \
             mock.patch.object(prompts.time, "sleep"):
            return prompts.find_ssid(names.setup_networks(ctx))

    def test_the_devices_network_is_found_by_either_name(self):
        self.assertEqual(self.find(["Home", "tasmota-ABCDEF-1234"], self.ctx()), "tasmota-ABCDEF-1234")
        self.assertEqual(self.find(["Home", "ESP_ABCDF0"], self.ctx()), "ESP_ABCDF0")

    def test_its_real_name_over_the_one_it_had_for_a_moment(self):
        # Tasmota turns its setup network on, then names it: for that moment
        # it is ESP-IDF's ESP_ABCDF0, which a computer lists for a while after,
        # perhaps first. Asked to join it, a person would find it gone.
        self.assertEqual(self.find(["ESP_ABCDF0", "Home", "tasmota-ABCDEF-1234"], self.ctx()),
                         "tasmota-ABCDEF-1234")

    def test_caught_in_that_moment_it_is_looked_at_again(self):
        self.assertEqual(self.find(["Home", "ESP_ABCDF0"], self.ctx(), ["Home", "tasmota-abcdef"]),
                         "tasmota-abcdef")

    def test_the_name_asked_for_first_wins_over_scan_order(self):
        self.assertEqual(self.find(["tasmota-ABCDEF-1234", "tasmota-abcdef"], self.ctx()), "tasmota-abcdef")

    def test_a_neighbours_tasmota_or_esp_is_not(self):
        self.assertIsNone(self.find(["tasmota-112233-0001", "ESP_112234", "ESP_ABCDEF"], self.ctx()))

    def test_the_installers_network_is_not_taken_for_tasmotas(self):
        # Seen in the fault suite: sent through the installer's own network,
        # Tasmota went on, and the installer's network - still listed for a
        # moment as it restarted - was taken for Tasmota's, never to be joined.
        # A Tasmota the run named has the Wi-Fi, and raises no network by itself.
        self.assertIsNone(self.find(["Home", "autoflash-recovery-CDEF"], self.ctx()))
        # It is the installer's, and found as that.
        with mock.patch.object(prompts, "scan_wifi_ssids", return_value=["Home", "autoflash-recovery-CDEF"]):
            self.assertEqual(prompts.find_ssid(names.bridge_networks(self.ctx())), "autoflash-recovery-CDEF")

    def test_only_tasmotas_own_name_is_kept_as_the_devices(self):
        ctx = self.ctx()
        names.note_setup_network(ctx, "ESP_ABCDF0")  # not what its router shows
        names.note_setup_network(ctx, "autoflash-recovery-CDEF")  # only until it is set up
        self.assertIsNone(ctx.state.input("device_name"))
        names.note_setup_network(ctx, "tasmota-ABCDEF-1234")
        self.assertEqual(ctx.state.input("device_name"), "tasmota-ABCDEF-1234")


class UnattendedPromptTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(prompts, "UNATTENDED", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.screen = io.StringIO()
        stdout = mock.patch("sys.stdout", self.screen)
        stdout.start()
        self.addCleanup(stdout.stop)
        self.addCleanup(output.finish)

    def test_no_key_is_waited_for(self):
        with mock.patch("builtins.input", side_effect=AssertionError("read the keyboard")):
            prompts.wait_for_key()

    def test_no_key_is_waited_for_unattended_on_a_terminal_nor_with_either_end_piped(self):
        for unattended, stdin, stdout in ((True, True, True), (False, True, False), (False, False, True)):
            with self.subTest(unattended=unattended, stdin=stdin, stdout=stdout), \
                    mock.patch.object(prompts, "UNATTENDED", unattended), \
                    mock.patch.object(prompts.sys.stdin, "isatty", return_value=stdin), \
                    mock.patch.object(self.screen, "isatty", return_value=stdout, create=True), \
                    mock.patch.object(prompts, "discard_typed_ahead", side_effect=AssertionError("waited")):
                prompts._wait_for_key()

    def test_a_yes_no_question_takes_its_cautious_default(self):
        self.assertFalse(prompts.ask_yes_no("Carry on anyway", default=False))
        self.assertTrue(prompts.ask_yes_no("Enter it now", default=True))
        self.assertIn("Carry on anyway (y/N): n   (unattended)", self.screen.getvalue())

    def test_any_other_question_stops_the_run_and_names_it(self):
        for ask in (lambda: prompts.ask_choice("Which device", ["one", "two"]),
                    lambda: prompts.ask_yes_no("No default"),
                    lambda: prompts.ask_text("eWeLink account email"),
                    lambda: prompts.ask_secret("Wi-Fi password: ")):
            with self.assertRaises(prompts.NoOneToAnswer):  # a QuitRun: the run stops cleanly
                ask()
        self.assertIn("eWeLink account email", self.screen.getvalue())

    def test_proceed_is_taken_as_typed(self):
        with mock.patch("builtins.input", side_effect=AssertionError("read the keyboard")):
            self.assertTrue(prompts.require_word("proceed"))
        self.assertIn("PROCEED   (unattended)", self.screen.getvalue())


@unittest.skipUnless(hasattr(os, "fork"), "a pseudo-terminal needs Linux or macOS")
class CountdownKeyTests(unittest.TestCase):
    """The countdown after a Ctrl-C carries on by itself, or at once with
    Enter; q stops, as at every question - taken as it is pressed, on a real
    terminal.  Its first redraw once readied the terminal for a typed answer,
    so a key waited for an Enter, whose row was left blank."""

    def ask(self, typed):
        import codecs
        import textwrap
        from tools.repo import unattended_run
        root = Path(__file__).resolve().parents[2]
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(root)!r})
            from tools.autoflash import output, prompts
            output.start(None, verbose=False)
            output.transient_begin()
            carry_on = prompts.countdown_carry_on("Carrying on to the next stage", seconds=20)
            output.transient_end()
            output.show(f"carry on {{carry_on}}")
            output.finish()
        """)
        code, written = unattended_run.record([sys.executable, "-c", script], (80, 24), live=False, timeout=30,
                                              answers=[unattended_run.Answer(r"press Enter now", typed)])
        screen = unattended_run.Screen(80, 24)
        screen.feed(codecs.decode(written, "utf-8", errors="replace"))
        self.written = written
        return code, screen.render(colour=False).splitlines()

    def test_q_stops_at_once(self):
        started = time.monotonic()
        code, rows = self.ask("q")
        self.assertEqual((code, rows[-1].strip()), (0, "carry on False"))
        self.assertLess(time.monotonic() - started, 10)   # not the countdown's 20 s

    def test_a_key_whose_code_has_a_q_does_not_stop(self):
        # F2's code in many terminals is Esc O Q: its Q was taken as q.
        code, rows = self.ask("\x1bOQ\x1b[A\r")
        self.assertEqual((code, rows[-1].strip()), (0, "carry on True"))

    def test_enter_carries_on_at_once_leaving_no_row(self):
        for typed, said in (("\r", "carry on True"), ("q\r", "carry on False")):
            with self.subTest(typed=typed):
                code, rows = self.ask(typed)
                self.assertEqual(rows[-1].strip(), said)
                self.assertNotEqual(rows[-2:-1], [""], rows)   # no blank row left above it

    def test_its_seconds_count_down_in_an_80_column_window(self):
        # Seen: wrapped onto two rows there, only the hint's was redrawn, and
        # "in 20 s" stood still above it.
        from tools.autoflash import runner
        import inspect
        # The longest it says (ask_after_ctrl_c's); before a question, "Carrying on".
        self.assertIn('], "Carrying on to the next stage")', inspect.getsource(runner))
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((80, 24))), \
                mock.patch.object(output, "colour_ok", return_value=True), \
                mock.patch("sys.stdout", io.StringIO()):
            shown = output.question_row("Carrying on to the next stage in 20 s, or press Enter now",
                                        prompts.hint_to_quit())
        self.assertIn("in 20 s", shown)

    def test_how_to_quit_is_said_as_at_every_question(self):
        self.ask("\r")
        self.assertIn(b"\x1b[2m(q to quit)\x1b[0m", self.written)   # in grey, never a number


class FakeConsole:
    """Windows' console as the tool meets it, with a person at its keyboard:
    msvcrt's reads a key at a time - a key with no character as "\xe0" or
    "\x00", then its code - and input()'s own line editing, as far as the
    keys here go: the up arrow and F3 bring the last line typed back, Esc
    clears the line.  Its own writes go to the screen kept here; each answer
    is typed a moment after its question shows, as unattended_run's are."""

    def __init__(self, answers):
        import threading
        self.answers = list(answers)   # (pattern, keys), in order
        self.keys: list[str] = []
        self.history: list[str] = []
        self.written = ""
        self.since = 0   # where the screen was when the last answer was typed
        self.ready = threading.Condition()
        self.person = threading.Thread(target=self._type, daemon=True)

    def press(self, keys):
        with self.ready:
            self.keys.extend(keys)
            self.ready.notify_all()

    def _type(self):
        for pattern, keys in self.answers:
            with self.ready:
                self.ready.wait_for(lambda: re.search(pattern, output.ANSI_RE.sub("", self.written[self.since:])),
                                    timeout=10)
                self.since = len(self.written)
            time.sleep(0.1)
            self.press(keys)

    # sys.stdout
    def write(self, text):
        with self.ready:
            self.written += text
            self.ready.notify_all()
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    # msvcrt
    def kbhit(self):
        with self.ready:
            return bool(self.keys)

    def getwch(self):
        with self.ready:
            if not self.ready.wait_for(lambda: self.keys, timeout=10):
                raise AssertionError(f"waited for a key nobody typed: {self.written[-200:]!r}")
            return self.keys.pop(0)

    # input(), the console's own line editing
    def input(self, prompt=""):
        self.write(prompt)
        line = ""
        while (char := self.getwch()) != "\r":
            if char in ("\x00", "\xe0"):
                if self.getwch() in ("H", "=") and self.history:   # up arrow, F3
                    line = self.history[-1]
            elif char == "\x1b":
                line = ""
            elif char == "\x08":
                line = line[:-1]
            else:
                line += char
        self.history.append(line)
        self.write(line + "\n")
        return line


class WindowsConsoleKeyTests(unittest.TestCase):
    """Keys pressed by accident on Windows - the arrows, Home and End, Page
    Up and Down, Delete, F1, F3 and F7, Esc, Tab - while the tool works or
    asks: none changes an answer, brings an earlier one back or stops the
    run.  Windows' console was left to edit a typed answer itself, and its
    up arrow and F3 brought the last answer back, F7 a list of them, and Esc
    cleared what was typed; at the countdown, Page Down's code is a Q."""

    STRAY = ["\xe0H", "\xe0P", "\xe0K", "\xe0M", "\xe0G", "\xe0O", "\xe0I", "\xe0Q", "\xe0S",
             "\x00;", "\x00=", "\x00A", "\x1b", "\t"]

    def ask(self, question, answers, *, while_working=()):
        console = FakeConsole(answers)
        fake_msvcrt = mock.Mock(kbhit=console.kbhit, getwch=console.getwch)
        with mock.patch.dict(sys.modules, {"msvcrt": fake_msvcrt}), \
             mock.patch.object(prompts.hostsys, "IS_WINDOWS", True), \
             mock.patch.object(prompts, "UNATTENDED", False), \
             mock.patch.object(prompts.sys.stdin, "isatty", return_value=True), \
             mock.patch.dict(sys.modules, {"readline": None}), \
             mock.patch("builtins.input", console.input), \
             mock.patch.object(output, "colour_ok", return_value=False), \
             mock.patch("sys.stdout", console):
            console.press("".join(while_working))
            console.person.start()
            answer = question()
        return answer, console

    def test_keys_pressed_by_accident_at_every_question_change_nothing(self):
        stray = "".join(self.STRAY)
        questions = {
            "choice": (lambda: prompts.ask_choice("Which firmware would you like", ["Tasmota", "ESPHome"]),
                       [("would you like", stray + "2\r")], 1),
            "yes or no": (lambda: prompts.ask_yes_no("Use it anyway", default=False),
                          [("Use it anyway", stray + "y\r")], True),
            "text": (lambda: prompts.ask_text("eWeLink account email"),
                     [("account email", stray + "you@example.com\r")], "you@example.com"),
            "address": (lambda: prompts.ask_ipv4("The device's address"),
                        [("address", stray + "192.168.99.50\r")], "192.168.99.50"),
            "password": (lambda: prompts.ask_secret("Wi-Fi password for 'Home': "),
                         [("password", stray + "hunter22\r")], "hunter22"),
            "key": (lambda: prompts.wait_for_key(), [("Press any key", "\xe0Q")], None),
            "word": (lambda: prompts.require_word(prompts.CONSENT_WORD),
                     [("Type PROCEED", stray + "PROCEED\r")], True),
            "countdown": (lambda: prompts.countdown_carry_on("The run carries on", seconds=30),
                          [("press Enter now", stray + "\r")], True),
        }
        for name, (question, answers, expected) in questions.items():
            with self.subTest(question=name):
                started = time.monotonic()
                answer, console = self.ask(question, answers, while_working=self.STRAY)
                self.assertEqual(answer, expected, console.written)
                self.assertLess(time.monotonic() - started, 20)   # never the countdown's 30 s

    def test_esc_in_the_middle_of_an_answer_keeps_what_was_typed(self):
        answer, _ = self.ask(lambda: prompts.ask_text("eWeLink account email"),
                             [("account email", "you@exa\x1bmple.com\r")])
        self.assertEqual(answer, "you@example.com")

    def test_an_answer_is_not_recalled_by_the_next_question(self):
        # The consent word is typed, never brought back with the up arrow or F3.
        for key in ("\xe0H", "\x00="):
            with self.subTest(key=repr(key)):
                answer, _ = self.ask(
                    lambda: [prompts.ask_text("eWeLink account email"), prompts.require_word(prompts.CONSENT_WORD)],
                    [("account email", "PROCEED\r"), ("Type PROCEED", key + "\r"), ("Only the TYPED word", "q\r")])
                self.assertEqual(answer, ["PROCEED", False])

    def test_the_answer_is_edited_with_the_arrows_home_end_and_delete(self):
        for keys in ("you@exampl.com" + "\xe0K" * 4 + "e" + "\xe0H\xe0P\r",   # back four, then up and down
                     "xyou@example.comz\xe0G\xe0S\xe0O\x08\r",                # Home, Delete; End, Backspace
                     "you@example.co\x00;m\xe0I\r"):                            # F1 and Page Up dropped
            with self.subTest(keys=keys):
                answer, console = self.ask(lambda: prompts.ask_text("eWeLink account email"),
                                           [("account email", keys)])
                self.assertEqual(answer, "you@example.com")
                from tools.repo import unattended_run
                screen = unattended_run.Screen(80, 24)
                screen.feed(console.written.replace("\n", "\r\n"))
                rows = [row.strip() for row in screen.render(colour=False).splitlines() if row.strip()]
                self.assertTrue(rows[-1].endswith(": you@example.com"), rows)   # on the question's row, as typed

    def test_ctrl_c_at_a_question_stops_at_once(self):
        with self.assertRaises(KeyboardInterrupt):
            self.ask(lambda: prompts.ask_text("eWeLink account email"), [("account email", "you\x03")])


class CtrlCFirstTests(unittest.TestCase):
    """A Ctrl-C pressed while nothing was asking is asked about before the
    next question, not after it.  Seen: pressed while the run looked for
    devices, "You pressed Ctrl-C" came only once the device was chosen and a
    key pressed, at the end of the first stage."""

    # Each kind of question, and an answer it takes the first time.
    KINDS = {
        "text": (lambda: prompts.ask_text("eWeLink account email"), "you@example.com"),
        "yes or no": (lambda: prompts.ask_yes_no("Use it anyway"), "y"),
        "choice": (lambda: prompts.ask_choice("Which device is this run for", ["a", "b"]), "1"),
        "word": (lambda: prompts.require_word(prompts.CONSENT_WORD), "proceed"),
        "key": (lambda: prompts.wait_for_key(), None),
        "password": (lambda: prompts.ask_secret("Wi-Fi password: "), "hunter22"),
    }

    def ask(self, kind, *, carry_on=True, unattended=False):
        from tools.autoflash import interrupt
        self.addCleanup(interrupt.clear)
        interrupt._state["first"] = 1.0   # pressed earlier, not yet asked about
        order = []
        question, answer = self.KINDS[kind]

        def asked(*_args, **_kwargs):
            order.append("question")
            return answer

        def about_ctrl_c(_question):
            order.append("ctrl-c")
            return carry_on
        with mock.patch.object(prompts.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(prompts.sys.stdout, "isatty", return_value=True), \
             mock.patch.object(prompts, "UNATTENDED", unattended), \
             mock.patch.object(prompts, "countdown_carry_on", side_effect=about_ctrl_c), \
             mock.patch.object(prompts, "discard_typed_ahead"), mock.patch.object(prompts, "_emit"), \
             mock.patch.object(prompts, "section"), mock.patch.object(prompts.output, "tell"), \
             mock.patch.object(prompts, "_read_answer", side_effect=asked), \
             mock.patch.object(prompts, "_wait_for_key", side_effect=asked), \
             mock.patch.object(prompts, "_ask_secret", side_effect=asked):
            try:
                question()
            except KeyboardInterrupt:
                order.append("stopped")
            except prompts.QuitRun:
                order.append("unattended")
        return order, interrupt.pending()

    def test_asked_about_before_every_kind_of_question(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(self.ask(kind), (["ctrl-c", "question"], False))

    def test_stopping_there_stops_before_the_question(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(self.ask(kind, carry_on=False), (["ctrl-c", "stopped"], False))

    def test_every_kind_of_question_asks_it_first(self):
        # A new kind of question asks about it too, or this fails. The
        # countdown is the question about it.
        import ast
        readers = {"_raw", "_read_answer", "_ask_secret", "_wait_for_key", "setcbreak", "getwch", "input"}
        not_questions = {"discard_typed_ahead", "countdown_carry_on"}
        tree = ast.parse(Path(prompts.__file__).read_text(encoding="utf-8"))
        missing = []
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef) or function.name.startswith("_") \
                    or function.name in not_questions:
                continue
            names = {node.id if isinstance(node, ast.Name) else node.attr for node in ast.walk(function)
                     if isinstance(node, (ast.Name, ast.Attribute))}
            if readers & names and "_ctrl_c_first" not in names:
                missing.append(function.name)
        self.assertEqual(missing, [])

    def test_an_unattended_run_leaves_it_to_the_end_of_the_stage(self):
        order, pending = self.ask("word", unattended=True)
        self.assertNotIn("ctrl-c", order)
        self.assertTrue(pending)


class QuestionScreenTests(unittest.TestCase):
    """Every kind of question, asked under a stage's line as a run asks it,
    on a real terminal, answered by keys typed as a person types them -
    wrongly first, where a wrong answer can be given.  As it is asked: how
    to quit said in grey, never a numbered option, and every row wrapped by
    the tool, none by the terminal; a password typed on a row of its own.
    Once answered: nothing of it left, the stage's own note still there.
    Each of these was first seen in an attended emulator run."""

    SIZE = (80, 40)
    NOTE = "A note the stage showed before the question."
    # (what is asked, keys typed as each pattern shows, what it returns)
    QUESTIONS = {
        "choice": ('prompts.ask_choice("Which firmware would you like", ["Tasmota", "ESPHome"], '
                   'help_lines=["Either can be changed later."])',
                   [("would you like", "3\r"), ("would you like", "2\r")], 1),
        "yes or no": ('prompts.ask_yes_no("Use it anyway", default=False)',
                      [("Use it anyway", "maybe\r"), ("Use it anyway", "y\r")], True),
        "text": ('prompts.ask_text("eWeLink account email", help_lines=["The account the device is in."], '
                 'example="you@example.com")',
                 [("account email", "\r"), ("account email", "you@example.com\r")], "you@example.com"),
        "address": ('prompts.ask_ipv4("The device\'s address")',
                    [("address", "eth0\r"), ("address", "192.168.99.50\r")], "192.168.99.50"),
        "password": ('prompts.ask_secret("Wi-Fi password for \'Home\': ")', [("password", "hunter22\r", r".*\n\s*>")],
                     "hunter22"),
        "key": ("prompts.wait_for_key()", [("Press any key", "x")], None),
        "word": ('prompts.require_word(prompts.CONSENT_WORD)',
                 [("Type PROCEED", "y\r"), ("Only the TYPED word", "proceed\r")], True),
        "word, one device of several": ('prompts.require_word(prompts.CONSENT_WORD, skip=True)',
                                        [("s to skip this device", "s\r")], False),
        # A question asked first once readied the terminal for typed answers,
        # and the countdown then took a key only with an Enter.
        "countdown": ('[prompts.ask_yes_no("Use it anyway", default=False), '
                      'prompts.countdown_carry_on("The run carries on", seconds=30)]',
                      [("Use it anyway", "y\r"), ("press Enter now", "\r")], [True, True]),
    }

    def ask(self, question, typed):
        import codecs
        import json
        import tempfile
        import textwrap
        from tools.repo import unattended_run
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "answer.json"
            script = textwrap.dedent(f"""
                import json, sys
                sys.path.insert(0, {str(root)!r})
                from tools.autoflash import output, prompts
                output.start(None, verbose=False)
                output.set_stage_width(30, 15)
                output.stage_begin(5, "Querying Sonoff firmware")
                output.tell("    {self.NOTE}")
                output.transient_begin()
                answer = {question}
                output.transient_end()
                open({str(result)!r}, "w").write(json.dumps(answer))
                output.stage_end("stopped")   # what it showed left, as a run stopped there leaves it
                output.finish()
            """)
            # Typed once the question is up - its quit hint drawn - not as soon
            # as what comes before it is: a wrong answer's reason, say.
            answers = [unattended_run.Answer(rf"(?s){when}.*\(q to quit\){after}", keys)
                       for when, keys, after in (item + ("",) * (3 - len(item)) for item in typed)]
            started = time.monotonic()
            code, written = unattended_run.record([sys.executable, "-c", script], self.SIZE, live=False,
                                                  timeout=30, answers=answers)
            took = time.monotonic() - started
            answer = json.loads(result.read_text(encoding="utf-8")) if result.exists() else "no answer"

        def screen(upto):
            drawn = Screen(*self.SIZE)
            drawn.feed(codecs.decode(written[:upto], "utf-8", errors="replace"))
            return drawn
        asked = [screen(at) for rule in answers for at in rule.typed_at]
        return code, answer, took, written, asked, screen(len(written))

    def test_every_question(self):
        from concurrent.futures import ThreadPoolExecutor
        from tools.repo import unattended_run
        import warnings
        # Side by side: each waits on its own terminal, most of the time. A
        # terminal forked from threads can deadlock a child that runs Python
        # before it starts its command; record's only sizes the terminal,
        # then starts it (unattended_run.record), so Python's warning is not for it.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", r"This process .* is multi-threaded, use of forkpty",
                                    DeprecationWarning)
            with ThreadPoolExecutor(len(self.QUESTIONS)) as pool:
                runs = {name: pool.submit(self.ask, question, typed)
                        for name, (question, typed, _) in self.QUESTIONS.items()}
        for name, (_, typed, expected) in self.QUESTIONS.items():
            with self.subTest(question=name):
                code, answer, took, written, asked, end = runs[name].result()
                self.assertEqual((code, answer), (0, expected), end.render(colour=False))
                self.assertEqual(len(asked), len(typed), "a question not asked as expected")
                self.assertLess(took, 20)   # never the countdown's 30 s
                for screen in asked:
                    rows = [row for row in screen.render(colour=False).splitlines() if row.strip()]
                    self.assertIn("(q to quit)", rows[-2] if name == "password" else rows[-1])
                    self.assertFalse([row for row in rows if re.match(r"\s*\d+\.\s+(Quit|Exit|Stop)", row)])
                    self.assertEqual(screen.wrapped, 0, "\n".join(rows))
                self.assertIn(b"\x1b[2m(q to quit)", written)   # in grey
                if name == "password":
                    rows = [row for row in asked[0].render(colour=False).splitlines() if row.strip()]
                    self.assertEqual((rows[-2].strip(), rows[-1].strip()), ("Wi-Fi password for 'Home' (q to quit):", ">"))
                rows = [row for row in end.render(colour=False).splitlines() if row.strip()]
                self.assertEqual(len(rows), 3, "\n".join(rows))
                self.assertTrue(unattended_run.STAGE_ROW.match(rows[0]), rows[0])
                self.assertEqual(rows[1].strip(), self.NOTE)
                self.assertTrue(rows[2].rstrip().endswith("- Stopped"), rows[2])


    # Keys pressed by accident at a question, before its answer: the arrows,
    # Home and End, F1, Tab, Page Up.
    STRAY = "\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1bOP\t\x1b[5~"
    # A Python with no readline (some builds have none): input() is the tool's own.
    NO_READLINE = 'sys.modules.__setitem__("readline", None) or '

    def test_keys_pressed_by_accident_at_every_question_change_nothing(self):
        # Seen: an arrow key's code at the password swallowed what was typed
        # straight after it, the password and its Enter too, and the run hung;
        # and where Python has no readline, arrows moved the cursor into the
        # rows above and stayed in the answer.
        from concurrent.futures import ThreadPoolExecutor
        import warnings
        cases = {}
        for name, (question, typed, expected) in self.QUESTIONS.items():
            stray = [(typed[0][0], self.STRAY + typed[0][1], *typed[0][2:]), *typed[1:]]
            cases[name] = (question, stray, expected)
            setup = "[" + self.NO_READLINE.rstrip(" or") + "] and " if question.startswith("[") else self.NO_READLINE
            cases[f"{name}, no readline"] = (setup + question, stray, expected)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", r"This process .* is multi-threaded, use of forkpty",
                                    DeprecationWarning)
            with ThreadPoolExecutor(len(cases)) as pool:
                runs = {name: pool.submit(self.ask, question, typed) for name, (question, typed, _) in cases.items()}
        for name, (_, _, expected) in cases.items():
            with self.subTest(question=name):
                code, answer, took, _, _, end = runs[name].result()
                self.assertEqual((code, answer), (0, expected), end.render(colour=False))
                self.assertLess(took, 20)
                rows = [row for row in end.render(colour=False).splitlines() if row.strip()]
                self.assertEqual(len(rows), 3, "\n".join(rows))   # the question taken off, nothing else left
                self.assertEqual(end.wrapped, 0, "\n".join(rows))

    def test_without_readline_the_answer_is_edited_as_readline_edits_it(self):
        for keys in ("you@exampl.com" + "\x1b[D" * 4 + "e" + "\x1b[A\x1b[B\r",   # back four, then up and down
                     "xyou@example.comz\x1b[H\x1b[3~\x1b[F\x7f\r",                # Home, Delete; End, Backspace
                     "you@example.co\x1bOPm\x1b[5~\r"):                             # F1 and Page Up dropped
            with self.subTest(keys=keys):
                code, answer, *_ = self.ask(self.NO_READLINE + 'prompts.ask_text("eWeLink account email")',
                                            [("account email", keys)])
                self.assertEqual((code, answer), (0, "you@example.com"))

    def test_without_readline_the_answer_shows_as_typed_on_its_row(self):
        import codecs
        import textwrap
        from tools.repo import unattended_run
        root = Path(__file__).resolve().parents[2]
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(root)!r})
            sys.modules["readline"] = None
            from tools.autoflash import prompts
            print("Above the question.")
            prompts._read_line("Email: ")
            print("After it.")
        """)
        code, written = unattended_run.record(
            [sys.executable, "-c", script], self.SIZE, live=False, timeout=30,
            answers=[unattended_run.Answer("Email", "you@exampl.com\x1b[D\x1b[D\x1b[D\x1b[De\x1b[A\r")])
        screen = Screen(*self.SIZE)
        screen.feed(codecs.decode(written, "utf-8", errors="replace"))
        rows = [row for row in screen.render(colour=False).splitlines() if row.strip()]
        self.assertEqual((code, rows), (0, ["Above the question.", "Email: you@example.com", "After it."]))

    def test_a_wifi_password_typed_differently_is_asked_afresh(self):
        # Seen in an attended emulator run: the two tries, and why, stayed
        # under each other as it was asked again.
        question = ("__import__('tools.espressif.safeboot.prepare_imprinted_bundle', "
                    "fromlist=['ask_password']).ask_password('Home')")
        code, answer, _, _, asked, end = self.ask(question, [
            ("password for 'Home'", "abc\r", r".*\n\s*>"), ("Same password again", "abd\r", r".*\n\s*>"),
            ("password for 'Home'", "\r", r".*\n\s*>"), ("Same password again", "\r", r".*\n\s*>")])
        self.assertEqual((code, answer), (0, ""), end.render(colour=False))
        rows = [row for row in asked[2].render(colour=False).splitlines() if row.strip()]
        self.assertEqual(sum("(q to quit)" in row for row in rows), 1, "\n".join(rows))
        self.assertEqual(rows[-3].strip(), "The two passwords did not match - try again.")

    def test_arrow_keys_edit_the_answer(self):
        # Seen in an attended emulator run: echoed by the terminal, the up
        # arrow moved the cursor into the rows above, and both arrows' codes
        # were taken as part of the answer.
        code, answer, _, _, _, end = self.ask('prompts.ask_text("eWeLink account email")', [
            ("account email", "you@exampl.com" + "\x1b[D" * 4 + "e" + "\x1b[A\x1b[B\r")])
        self.assertEqual((code, answer), (0, "you@example.com"), end.render(colour=False))
        rows = [row for row in end.render(colour=False).splitlines() if row.strip()]
        self.assertEqual(len(rows), 3, "\n".join(rows))
        self.assertEqual(rows[1].strip(), self.NOTE)

    def test_an_answer_is_not_recalled_by_the_next_question(self):
        # The consent word is typed, never brought back with the up arrow.
        code, answer, _, _, _, end = self.ask(
            '[prompts.ask_text("eWeLink account email"), prompts.require_word(prompts.CONSENT_WORD)]',
            [("account email", "proceed\r"), ("Type PROCEED", "\x1b[A\r"), ("Only the TYPED word", "q\r")])
        self.assertEqual((code, answer), (0, ["proceed", False]), end.render(colour=False))

    def test_every_kind_of_question_is_here(self):
        # A public function reading the keyboard itself is a kind of question:
        # asked above, or this fails. Those built on one (ask_ipv4 on
        # ask_text) are that one's.
        import ast
        readers = {"_raw", "_read_answer", "_ask_secret", "_wait_for_key", "setcbreak", "getwch", "input"}
        not_questions = {"discard_typed_ahead"}   # throws away keys typed early: asks nothing
        tree = ast.parse(Path(prompts.__file__).read_text(encoding="utf-8"))
        kinds = {function.name for function in tree.body
                 if isinstance(function, ast.FunctionDef) and not function.name.startswith("_")
                 and readers & {node.id if isinstance(node, ast.Name) else node.attr for node in ast.walk(function)
                                if isinstance(node, (ast.Name, ast.Attribute))}} - not_questions
        asked = " ".join(question for question, _, _ in self.QUESTIONS.values())
        self.assertGreater(len(kinds), 5)
        self.assertEqual(sorted(kind for kind in kinds if f"prompts.{kind}(" not in asked), [])


class Screen:
    """unattended_run.Screen, counting the rows the terminal had to wrap
    itself: a row the tool wrote past the edge, unwrapped."""

    def __new__(cls, width, height):
        from tools.repo import unattended_run

        class Counting(unattended_run.Screen):
            wrapped = 0

            def _put(self, char):
                self.wrapped += self.wrap_next
                super()._put(char)
        return Counting(width, height)


class AdminPasswordTests(unittest.TestCase):
    def test_a_wrong_password_is_replaced_not_piled_up(self):
        # As every question's wrong answer is: only why, and the question again.
        from tests.autoflash.test_unlock import drawn
        asked = iter(["wrong-one", "wrong-two", "right-one"])

        def body(look):
            def secret(prompt, reveal_seconds=1.0):
                look()
                value = next(asked)
                output.show(f"{prompt}{'*' * len(value)}")
                return value
            with mock.patch.object(prompts, "ask_secret", side_effect=secret), \
                 mock.patch.object(prompts.hostsys, "check_sudo_password",
                                   side_effect=lambda value: None if value == "right-one" else "refused"):
                prompts.ask_admin_password()
        screens, _ = drawn(body)
        third = screens[2]
        self.assertEqual(third.count("That password was not accepted"), 1, third)
        self.assertNotIn("*********", third)   # neither wrong try left


class AdminPasswordTriesTests(unittest.TestCase):
    """sudo stops after three wrong passwords, and some systems lock the
    account for a while after a few: three tries, no more."""

    def ask(self, accepted):
        asked = []
        with mock.patch("sys.stdout", io.StringIO()), \
             mock.patch.object(prompts, "ask_secret", side_effect=lambda _: asked.append(1) or f"try-{len(asked)}"), \
             mock.patch.object(prompts.hostsys, "check_sudo_password",
                               side_effect=lambda value: None if value == accepted else "refused"), \
             mock.patch.object(prompts, "_retry"):
            try:
                return prompts.ask_admin_password(), len(asked)
            except OSError:
                return None, len(asked)
            finally:
                output.finish()

    def test_the_password_sudo_accepts_is_given(self):
        self.assertEqual(self.ask("try-2"), ("try-2", 2))

    def test_three_refused_give_up(self):
        self.assertEqual(self.ask(None), (None, 3))


class SecretFieldTests(unittest.TestCase):
    def test_a_password_is_typed_on_its_own_row_however_short_its_question(self):
        # The same for every password, whatever its question's length.
        for prompt in ("PIN (q to quit): ", "This computer's password (for sudo) (q to quit): "):
            self.assertTrue(prompts._secret_field_below(prompt), prompt)


class CtrlCCarriedOnTests(unittest.TestCase):
    def test_a_ctrl_c_carried_on_from_is_counted_for_the_report_and_one_stopped_at_is_not(self):
        from tools.autoflash import interrupt
        interrupt.reset_carried_on()
        self.addCleanup(interrupt.reset_carried_on)
        for carry_on in (True, False, True):
            with mock.patch.object(prompts, "countdown_carry_on", return_value=carry_on), \
                    mock.patch.object(prompts, "section"), mock.patch.object(output, "transient_begin"), \
                    mock.patch.object(output, "transient_end"):
                prompts.ask_after_ctrl_c(["safe"], "Carrying on")
        self.assertEqual(interrupt.carried_on(), 2)


class PowerWarningTests(unittest.TestCase):
    """Every go-ahead before a write warns against cutting the device's power
    in the same words, which leave room for the later notes that ask for it
    to be switched off and on: "during the process" contradicted them."""

    def test_every_warning_is_the_one_line(self):
        self.assertIn("unless this tool tells you to", prompts.DO_NOT_CUT_POWER)
        root = Path(__file__).resolve().parents[2] / "tools" / "autoflash"
        written = [path.relative_to(root).as_posix() for path in root.rglob("*.py")
                   if "DO NOT CUT" in path.read_text(encoding="utf-8")]
        self.assertEqual(written, ["prompts.py"])


if __name__ == "__main__":
    unittest.main()

