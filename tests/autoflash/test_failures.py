"""failures.classify: every way the tool says a run stopped has a class."""

from __future__ import annotations

import ast
import errno
import unittest
from pathlib import Path

from tools.autoflash import failures, runner
from tools.autoflash.routes import arrival

ROOT = Path(__file__).resolve().parents[2]
# What a step raises to stop the run, as the runner reports it. Not an
# OSError: one the network does not raise is "something unexpected went
# wrong" (runner.unexpected), recorded as a crash - each the tool raises
# itself is caught short of the runner (CAUGHT).
STOPPING = {"Abort", "NothingWritten", "StoppedPartWay", "DeviceError", "PhaseError", "DeviceNeverConnected"}
# Each OSError the tool raises with words of its own, by its file and how
# its words begin, and what catches it before the runner or a readout would
# take it for an error nobody foresaw. One not here that a step can reach is
# a stop to act on: an Abort (StoppedPartWay where a send stopped part way).
CAUGHT = {
    ("openbeken.py", "short read of the device's flash"): "arrived._back_up; readout_beken.read_whole_flash",
    ("readout.py", "not from a test run"): "readout.light_post's callers (OSError, ValueError)",
    ("readout.py", "the website answered"): "readout.send's callers (OSError, ValueError)",
    ("hostsys.py", "the download was cut off"): "public_copies.fetch, sonoff_firmware's sources",
    ("hostsys.py", "GetAdaptersAddresses failed"): "hostsys.lan_networks",
    ("hostsys.py", "sudo could not be run"): "firewall.ensure_firewall, as the rule not added",
    ("hostsys.py", "sudo refused"): "firewall.ensure_firewall, as the rule not added (or sudo's own words)",
    ("hostsys.py", "could not add the firewall rule"): "firewall.ensure_firewall",
    ("cli.py", "sudo needs this computer's password"): "firewall.ensure_firewall, as the rule not added",
    ("prompts.py", "this computer's password was not accepted"): "firewall.ensure_firewall, as the rule not added",
    ("installer.py", "simulated failure"): "installer's backup read (--simulate-backup-failure)",
}


def literals(node: ast.AST) -> list[str]:
    """Each message the code may write there, each value it fills in as {}:
    both of an if-else's."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return ["".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [left + right for left in literals(node.left) or ["{}"] for right in literals(node.right) or ["{}"]]
    if isinstance(node, ast.IfExp):
        return literals(node.body) + literals(node.orelse)
    return []


def raised() -> list[tuple[str, str]]:
    """(where, message) for each message a stopping error is raised with."""
    found = []
    for path in sorted((ROOT / "tools/autoflash").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            for message in literals(node.exc.args[0]) if name in STOPPING else []:
                found.append((f"{path.relative_to(ROOT)}:{node.lineno}", message))
    return found


class ClassifyTests(unittest.TestCase):
    def test_every_message_raised_has_a_class(self) -> None:
        messages = raised()
        self.assertGreater(len(messages), 100)   # the walk found them
        # One that starts with what it fills in ("{}: {}", "{}; its console
        # says: {}") passes on another's words first, and is classed by them
        # when the run stops.
        own = [(where, message) for where, message in messages if not message.startswith("{}")]
        unclassed = [f"{where}: {message[:80]}" for where, message in own
                     if failures.classify(message) == "other"]
        self.assertEqual(unclassed, [], "add a pattern to failures.PATTERNS")

    def test_a_stop_that_says_its_class_has_words_of_that_class(self) -> None:
        """Its class is sent as it is, and the API holds back a report whose
        words read as none of the tool's messages: so its words must read as
        its class, which is what this checks - the right class, not just one."""
        from tools.autoflash import errors
        own = {name: cls.failure for name, cls in vars(errors).items()
               if isinstance(cls, type) and issubclass(cls, errors.Abort) and cls.failure}
        said = []
        for path in sorted((ROOT / "tools/autoflash").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args):
                    continue
                func = node.exc.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                given = next((kw.value.value for kw in node.exc.keywords if kw.arg == "failure"
                              and isinstance(kw.value, ast.Constant)), own.get(name))
                if given:
                    said += [(f"{path.relative_to(ROOT)}:{node.lineno}", given, message)
                             for message in literals(node.exc.args[0]) if not message.startswith("{}")]
        self.assertGreaterEqual(len(said), 5)   # the walk found them
        self.assertEqual([f"{where}: {given}, not {failures.classify(message)}: {message[:60]}"
                          for where, given, message in said if failures.classify(message) != given], [])
        self.assertEqual(failures.class_of(errors.StoppedPartWay("words of no class")), "upload-cut")
        self.assertEqual(failures.class_of(errors.Abort("nothing answers at 192.0.2.50")), "device-not-found")

    def test_every_oserror_the_tool_raises_itself_is_caught_short_of_the_runner(self) -> None:
        found = set()
        for path in sorted((ROOT / "tools/autoflash").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args
                        and getattr(node.exc.func, "id", "") == "OSError"):
                    # Its words, wherever among its arguments: OSError(errno, "words") too.
                    said = next((words for arg in node.exc.args if (words := literals(arg))), ["{}"])
                    for message in said:
                        begins = next((start for where, start in CAUGHT if where == path.name
                                       and message.startswith(start)), message[:40])
                        found.add((path.name, begins))
        self.assertEqual(sorted(found - set(CAUGHT)), [],
                         "raise a stop to act on (an Abort), or say in CAUGHT what catches it")
        self.assertEqual(sorted(set(CAUGHT) - found), [], "in CAUGHT, but raised no more")

    def test_each_pattern_names_a_class(self) -> None:
        self.assertLessEqual({name for name, _ in failures.PATTERNS}, set(failures.CLASSES))
        self.assertEqual(failures.classify(""), "other")
        self.assertEqual(failures.classify("words no pattern knows"), "other")

    def test_the_runners_own_words(self) -> None:
        self.assertEqual(failures.classify(runner.connection_lost(ConnectionResetError("reset"))), "device-lost")
        self.assertEqual(failures.classify(runner.unexpected(KeyError("x"))), "crash")
        full = OSError(errno.ENOSPC, "No space left on device")
        self.assertEqual(failures.classify(runner.unexpected(full)), "no-room")
        self.assertEqual(failures.classify(
            "the device turned it down, after it was sent 3 times. Switch the device off and on, "
            "then run the same command again"), "not-taken")

    def test_messages_that_could_be_taken_for_another(self) -> None:
        # Each where an earlier, broader pattern would otherwise win.
        cases = {
            "not confirmed - nothing has been written to the device": "declined",
            "the device already runs Sonoff's firmware: there is nothing to convert": "nothing-to-do",
            "the device is a BASICR4, which this tool does not convert yet. Nothing was written": "unsupported-device",
            "the device did not fetch the D1 instructions from this computer within 90s; nothing was written":
                "computer-network",
            "eWeLink could not be reached, and a device on Sonoff's firmware is found and converted through it. "
            "Nothing was written: check this computer's internet connection": "ewelink-unreachable",
            "nothing answers at 192.0.2.50: check the device is switched on and on your Wi-Fi": "device-not-found",
            "the device at 192.0.2.50 did not answer within 30s": "device-no-answer",
            "the public dump fetched is not the one this tool knows (its hash differs): refusing it": "integrity",
            "the published tasmota image is missing: 404": "download",
            "Tasmota 15.0.1 is on the device, but its template is gone: it lost its settings.": "settings-lost",
            "the device's memory could not be read (timeout). Switch it off and on": "unexpected-layout",
            "this device cannot go to OpenBeken: a S60TPG takes Tasmota or ESPHome": "bad-option",
            # Passed to arrival.until_back, which stops the run with them: no raise to find.
            arrival.NOT_BACK: "network-join",
            arrival.ON_WIFI_ONLY: "ewelink-unreachable",
        }
        for message, expected in cases.items():
            self.assertEqual(failures.classify(message), expected, message)


if __name__ == "__main__":
    unittest.main()
