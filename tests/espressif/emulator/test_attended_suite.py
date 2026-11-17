"""The attended runs (tools/espressif/emulator/attended_suite.py): each
scenario's answers still answer questions the tool asks, and a run is judged
from its recording as it should be - here with recordings made up, as a
terminal would have them."""
import ast
import io
import json
import re
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.espressif.emulator import attended_suite

ROOT = Path(__file__).resolve().parents[3]
HINT = "\x1b[2m(q to quit)\x1b[0m"


def recording(*parts: str) -> tuple[bytes, list[int]]:
    """screen.raw of made-up parts, and where each "|" between them was:
    where an answer was typed."""
    raw, marks = b"", []
    for part in parts:
        if part == "|":
            marks.append(len(raw))
        else:
            raw += part.replace("\n", "\r\n").encode()
    return raw, marks


class JudgeTests(unittest.TestCase):
    SCENARIO = {"answers": [{"when": "Which firmware", "type": "1\r"}],
                "expect": {"exit": 0, "result": "Converted"}}

    def judge(self, raw, marks, code=0, scenario=None, answers=None, result="Converted", clipboard=None):
        scenario = scenario or self.SCENARIO
        answers = answers or [{"when": item["when"], "type": item["type"], "typed_at": marks[i:i + 1]}
                              for i, item in enumerate(scenario["answers"])]
        return attended_suite.judge(scenario, raw, answers, code, result=result, clipboard=clipboard)

    def test_a_run_as_it_should_be_passes(self):
        raw, marks = recording(" Stage 1 - Choosing the firmware - Underway\n\n     1. Tasmota\n",
                               f" Which firmware would you like {HINT}: ", "|", "1\n",
                               "\x1b[1F\x1b[2K" * 4, " Stage 1 - Choosing the firmware - Complete\n\n",
                               " Conversion complete!\n")
        self.assertEqual(self.judge(raw, marks), [])

    def test_a_question_without_the_hint_or_shown_twice_is_a_question_fault(self):
        raw, marks = recording(" Which firmware would you like: ", "|", "1\n")
        self.assertIn("FAIL-QUESTION: no (q to quit) at 'Which firmware would you like:'",
                      self.judge(raw, marks))
        raw, marks = recording(f" Which firmware would you like {HINT}: 9\n",
                               f" Which firmware would you like {HINT}: ", "|", "1\n")
        self.assertTrue(any("shown twice" in verdict for verdict in self.judge(raw, marks)))

    def test_a_row_the_terminal_wrapped_is_a_question_fault(self):
        raw, marks = recording(" " + "x" * 130 + "\n", f" Which firmware would you like {HINT}: ", "|")
        self.assertIn("FAIL-QUESTION: a row the terminal wrapped itself", self.judge(raw, marks))

    def test_a_question_left_waiting_unanswered_and_an_answer_never_typed(self):
        raw, _ = recording(f" Which firmware would you like {HINT}: 1\n", f" Type PROCEED to go ahead {HINT}: ")
        verdicts = self.judge(raw, [], code=124)
        self.assertIn("FAIL-UNANSWERED: 'Type PROCEED to go ahead (q to quit):'", verdicts)
        self.assertIn("FAIL-UNUSED: 'Which firmware'", verdicts)
        self.assertIn("FAIL-RESULT: exit 124, not 0", verdicts)

    def test_an_optional_answer_need_not_be_typed(self):
        scenario = {"answers": [{"when": "Use it anyway", "type": "y\r", "optional": True}], "expect": {}}
        self.assertEqual(self.judge(b" done\r\n", [], scenario=scenario), [])

    def test_the_report_must_say_what_was_expected(self):
        raw, _ = recording(" Sonoff's firmware could not be put back, so the device is back on ESPHome.\n")
        scenario = {"answers": [], "expect": {"result": "Converted"}}
        self.assertEqual(self.judge(raw, [], scenario=scenario, result="Put back on ESPHome"),
                         ["FAIL-RESULT: Result 'Put back on ESPHome', not 'Converted'"])
        self.assertEqual(self.judge(raw, [], scenario=scenario, result=None),   # a run that made no report
                         ["FAIL-RESULT: Result 'none', not 'Converted'"])

    def test_a_key_typed_anywhere_answers_no_question(self):
        scenario = {"answers": [{"when": "Stage 4", "type": "\x03", "anywhere": True}], "expect": {}}
        raw, marks = recording(" Stage 4 - Leaving Tasmota - Underway\n", "|")
        answers = [{"when": "Stage 4", "type": "\x03", "typed_at": marks, "anywhere": True}]
        self.assertEqual(self.judge(raw, marks, scenario=scenario, answers=answers), [])

    def test_an_answer_that_switches_the_device_or_signals_the_run_is_judged_too(self):
        scenario = {"answers": [{"when": "Stage 8", "power": "off", "anywhere": True},
                                {"when": "Which firmware", "signal": "TERM", "optional": True}], "expect": {}}
        raw, marks = recording(" Stage 8 - Installing interim firmware - Underway\n", "|")
        answers = [{"when": "Stage 8", "type": "", "typed_at": marks, "anywhere": True, "power": "off"},
                   {"when": "Which firmware", "type": "", "typed_at": [], "signal": "TERM"}]
        self.assertEqual(self.judge(raw, marks, scenario=scenario, answers=answers), [])
        answers[0]["typed_at"] = []
        self.assertEqual(self.judge(raw, marks, scenario=scenario, answers=answers), ["FAIL-UNUSED: 'Stage 8'"])

    def test_an_answer_that_failed_to_act_is_an_answer_fault(self):
        scenario = {"answers": [{"when": "setup network", "wifi": "join", "anywhere": True}], "expect": {}}
        raw, marks = recording(" Join the device's setup network\n", "|")
        answers = [{"when": "setup network", "type": "", "typed_at": marks, "anywhere": True, "wifi": "join",
                    "failed": ["wifi join: No network of the device's own is on the air."]}]
        self.assertEqual(self.judge(raw, marks, scenario=scenario, answers=answers),
                         ["FAIL-ANSWER: 'setup network': wifi join: No network of the device's own is on the air."])

    def test_how_the_run_ended_is_judged_with_what_it_copied(self):
        # unattended_run.end_problems, on the screen it ended on.
        scenario = {"answers": [], "expect": {}}
        report = (" FAILED: the device did not answer\n\n     Liberator: 3.0.0\n     OS:        Ubuntu\n\n"
                  " If it should not have stopped, please open an issue here:\n"
                  "     https://firmwareliberator.com/issues\n     Report ID: 0123456789abcdef\n"
                  "     This report is on your clipboard.\n\n This run's log:\n     run.log\n")
        raw, _ = recording(report)
        copied = "Liberator: 3.0.0\nReport ID: 0123456789abcdef\n"
        self.assertEqual(self.judge(raw, [], scenario=scenario, clipboard=copied), [])
        self.assertEqual(self.judge(raw, [], scenario=scenario, clipboard=""),
                         ["FAIL-SCREEN: said its report is on the clipboard, and nothing was put there"])

    def test_a_line_the_scenario_wants_shown_must_be_on_the_final_screen(self):
        scenario = {"answers": [], "expect": {"shows": [r"^ *Not needed: +1$"]}}
        raw, _ = recording(" Nothing was read out.\n")
        self.assertEqual(self.judge(raw, [], scenario=scenario),
                         ["FAIL-SCREEN: not shown: '^ *Not needed: +1$'"])

    def test_a_line_the_scenario_never_wants_is_found_at_an_answer_or_at_the_end(self):
        scenario = {"answers": [{"when": "Stage 3", "type": "zq", "anywhere": True}], "expect": {"never": ["zq"]}}
        for shown, verdicts in ((" Stage 3 - Choosing the firmware - Underway zq\n", ["FAIL-SCREEN: shown: 'zq'"]),
                                (" Stage 3 - Choosing the firmware - Underway\n", [])):
            with self.subTest(shown=shown):
                # Echoed at one answer, then taken off by the end: found all the same.
                raw, marks = recording(shown, "|", "\x1b[1F\x1b[2K Stage 3 - Choosing the firmware - Complete\n")
                answers = [{"when": "Stage 3", "type": "zq", "typed_at": marks, "anywhere": True}]
                self.assertEqual(self.judge(raw, marks, scenario=scenario, answers=answers), verdicts)

    def test_a_row_the_terminal_wrapped_after_the_last_answer_is_a_screen_fault(self):
        scenario = {"answers": [], "expect": {}}
        raw, _ = recording(" " + "x" * 130 + "\n")
        self.assertEqual(self.judge(raw, [], scenario=scenario),
                         ["FAIL-SCREEN: a row the terminal wrapped itself, after the last answer"])


class RunTests(unittest.TestCase):
    def test_a_readout_scenario_runs_a_readout(self):
        command = attended_suite.wrapper(61, {"command": "readout"}, "--", "--new", mac="12:34:56:ab:cd:ef")
        self.assertIn("--readout", command[:command.index("--")])
        self.assertNotIn("--readout", attended_suite.wrapper(61, {}, mac="12:34:56:ab:cd:ef"))

    def test_the_api_has_enough_of_the_devices_product_where_the_scenario_says(self):
        from unittest import mock
        with mock.patch("tools.autoflash.records.read_device", return_value={"product_model": "s60tpg"}):
            environment = attended_suite.run_environment({"api_enough": True}, "12:34:56:ab:cd:ef")
            self.assertEqual(environment["AUTOFLASH_API_ENOUGH"], "S60TPG")
            with mock.patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("AUTOFLASH_API_ENOUGH", None)
                self.assertNotIn("AUTOFLASH_API_ENOUGH", attended_suite.run_environment({}, "12:34:56:ab:cd:ef"))

    def test_the_stub_lists_the_device_under_its_product_on_a_checkout_with_no_runs(self):
        # Seen on a fresh checkout: the stub listed no product, and the readout
        # of readout-then-not-needed was refused by the API (400) for no model.
        from unittest import mock
        with mock.patch("tools.autoflash.records.read_device", return_value={}), \
                mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("AUTOFLASH_EMU_STUB_PRODUCT_MODEL", None)
            environment = attended_suite.run_environment({}, "12:34:56:ab:cd:ef")
        self.assertEqual(environment["AUTOFLASH_EMU_STUB_PRODUCT_MODEL"], "S60TPG")


def question_texts() -> list[str]:
    """Every text the tool can show, as it is written in its code: each
    string, an f-string's run-time parts as "...". The questions are among
    them."""
    texts = []
    for folder in ("tools/autoflash", "tools/espressif/safeboot"):
        for path in (ROOT / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    texts.append(node.value)
                elif isinstance(node, ast.JoinedStr):
                    texts.append("".join(part.value if isinstance(part, ast.Constant) else "..."
                                         for part in node.values))
    return texts


def shows_as_a_stage(pattern: re.Pattern[str], texts: list[str]) -> bool:
    """Whether `pattern` finds a stage line, "Stage N - <name>  - Underway",
    with one of the tool's texts as the name."""
    return any(pattern.search(f"Stage {number} - {text}  - Underway")
               for text in texts if "\n" not in text and len(text) < 60 for number in range(1, 21))


class InstanceTests(unittest.TestCase):
    def test_a_run_uses_one_instance_per_worker_never_two_scenarios_at_once(self):
        found = {f"s60-{i}": ("espressif", {}) for i in range(7)} | {f"s61-{i}": ("beken", {}) for i in range(3)}
        busy, used, lock = set(), [], threading.Lock()

        def run_one(n, name, family, scenario, results, mac, workers):
            with lock:
                self.assertNotIn(n, busy, f"{name} on {n}, which another scenario is using")
                busy.add(n)
                used.append((family, n))
            time.sleep(0.02)
            with lock:
                busy.discard(n)
            return f"PASS {name}"

        with mock.patch.object(attended_suite, "run_one", run_one), redirect_stdout(io.StringIO()):
            lines = attended_suite.run_all(list(found), found, Path("results"), None, workers=3, first=81)
        self.assertEqual(len(lines), 10)
        self.assertEqual({n for family, n in used if family == "espressif"}, {81, 82, 83})
        self.assertEqual({n for family, n in used if family == "beken"}, {81})


class ScenarioTests(unittest.TestCase):
    def test_every_answer_still_answers_a_question_the_tool_asks(self):
        # A question reworded fails here, in a second, not as FAIL-UNANSWERED
        # after an hour in the emulator. Optional answers answer what may not
        # be asked.
        texts = question_texts()
        found = attended_suite.scenarios()
        self.assertGreater(len(found), 8)
        for name, (_, scenario) in found.items():
            for answer in scenario["answers"] + scenario.get("again", {}).get("answers", []):
                with self.subTest(scenario=name, when=answer["when"]):
                    self.assertLessEqual(set(answer), {"when", "type", "times", "optional", "anywhere", "power",
                                                       "signal", "router", "wifi", "after"})
                    if "after" in answer:
                        self.assertTrue(answer.get("anywhere"), "after: for an answer typed anywhere")
                    # Each does one thing: types, switches the device or the
                    # router, moves the computer's Wi-Fi, or signals the run.
                    self.assertEqual(sum(key in answer for key in ("type", "power", "signal", "router", "wifi")), 1,
                                     answer)
                    self.assertIn(answer.get("power", "off"), ("off", "on"))
                    self.assertIn(answer.get("signal", "TERM"), ("TERM", "HUP", "INT"))
                    self.assertRegex(answer.get("router", "off"), r"^(off|on( \S{8,63})?)$")
                    self.assertRegex(answer.get("wifi", "home"), r"^(home|join( \S+)?)$")
                    pattern = re.compile(answer["when"])
                    if answer.get("optional"):
                        continue
                    # A key typed, or the device switched, anywhere still waits
                    # for a line the run must show: a note, or a stage line as
                    # output draws it.
                    self.assertTrue(any(pattern.search(text) for text in texts)
                                    or answer.get("anywhere") and shows_as_a_stage(pattern, texts),
                                    f"{name}: nothing the tool shows says {answer['when']!r}")

    def test_every_scenario_says_what_it_shows_and_how_it_ends(self):
        for name, (_, scenario) in attended_suite.scenarios().items():
            with self.subTest(scenario=name):
                self.assertTrue(scenario.get("about"))
                self.assertIn("expect", scenario)
                self.assertLessEqual(set(scenario), {"about", "start", "prep", "args", "size", "minutes",
                                                     "answers", "expect", "command", "again", "api_enough"})
                for expect in (scenario["expect"], scenario.get("again", {}).get("expect", {})):
                    self.assertLessEqual(set(expect), {"exit", "result", "screen", "shows", "never"})
                    for wanted in expect.get("shows", []) + expect.get("never", []):
                        re.compile(wanted)
                for action in scenario.get("prep", []):
                    self.assertIn(action if isinstance(action, str) else action[0],
                                  ("sudo forgets", "tasmota password", "locked esphome", "convert"))
                self.assertEqual(json.loads(json.dumps(scenario)), scenario)


if __name__ == "__main__":
    unittest.main()
