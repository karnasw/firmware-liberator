import re
import unittest
from pathlib import Path

from tools.repo import newer_python

ROOT = Path(__file__).resolve().parents[2]


def found(source):
    return [(line, what) for line, what, _ in newer_python.found_in(source)]


class FoundTests(unittest.TestCase):
    def test_what_a_newer_python_added_is_found_with_its_line(self):
        source = ("import tomllib\n"
                  "from typing import Self\n"
                  "self.enterContext(manager)\n"
                  "when = datetime.UTC\n"
                  "raise ExceptionGroup('x', [])\n")
        self.assertEqual(found(source), [(1, "the module tomllib"), (2, "typing.Self"), (3, "enterContext"),
                                         (4, "datetime.UTC"), (5, "ExceptionGroup")])

    def test_what_the_oldest_has_is_not(self):
        source = ("import tomli\nfrom typing import Optional\nself.addCleanup(patch.stop)\n"
                  "when = datetime.timezone.utc\nvalue = text.removeprefix('x')\n")
        self.assertEqual(found(source), [])

    def test_a_line_saying_it_is_used_only_where_it_is_there_is_left(self):
        source = f"if sys.version_info >= (3, 11):\n    import tomllib  # {newer_python.ALLOWED}: guarded\n"
        self.assertEqual(found(source), [])

    def test_main_names_each_file_and_line_and_fails(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "a.py")
            path.write_text("self.enterContext(x)\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(newer_python.main([str(path)]), 1)
        self.assertEqual(out.getvalue(), f"{path}:1: enterContext came in Python 3.11; the tool runs on 3.10 "
                                         "and newer (tools/repo/newer_python.py)\n")


class OldestTests(unittest.TestCase):
    """Everything that says which Python is the oldest says the one OLDEST is."""

    oldest = ".".join(map(str, newer_python.OLDEST))

    def test_the_tool_s_own_checks_ask_for_it(self):
        major, minor = newer_python.OLDEST
        self.assertIn(f"sys.version_info >= ({major}, {minor})", (ROOT / "start.sh").read_text(encoding="utf-8"))
        self.assertIn(f"sys.version_info >= ({major}, {minor})",
                      (ROOT / "tools/windows-python.ps1").read_text(encoding="utf-8"))

    def test_ci_tests_on_it_and_ruff_reads_for_it(self):
        workflow = (ROOT / ".github/workflows/emulator-check.yml").read_text(encoding="utf-8")
        self.assertIn(f'python-version: "{self.oldest}"', workflow)
        self.assertRegex((ROOT / ".ruff.toml").read_text(encoding="utf-8"),
                         rf'(?m)^target-version = "py{re.escape(self.oldest.replace(".", ""))}"')


if __name__ == "__main__":
    unittest.main()
