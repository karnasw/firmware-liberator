"""report_schema: the file the Worker holds reports to is current, its lists
only grow, and its validator agrees with the Worker's."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest

from tools.repo import report_schema

FIXTURES = json.loads((report_schema.ROOT / "website/test/fixtures/reports.json").read_text(encoding="utf-8"))


def changed(item: dict, base: str = "valid") -> dict:
    """The first of FIXTURES[base] with one change at a dotted path (as
    website/test/report-schema.test.mjs makes it)."""
    report = copy.deepcopy(FIXTURES[base][0])

    def at(path: str) -> tuple[dict, str]:
        *keys, last = path.split(".")
        node = report
        for key in keys:
            node = node[key]
        return node, last
    for path, value in item.get("set", {}).items():
        node, key = at(path)
        node[key] = value
    if "drop" in item:
        node, key = at(item["drop"])
        del node[key]
    return report


class ReportSchemaTests(unittest.TestCase):
    def test_the_file_is_current(self):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            code = report_schema.main(["check"])
        self.assertEqual(code, 0, said.getvalue())

    def test_a_list_of_words_only_grows(self):
        old = {"properties": {"result": {"type": "string", "enum": ["converted", "retired"]}}}
        new = {"properties": {"result": {"type": "string", "enum": ["converted", "failed"]}}}
        self.assertEqual(report_schema.grown(new, old)["properties"]["result"]["enum"],
                         ["converted", "failed", "retired"])
        self.assertEqual(report_schema.grown(new, {}), new)

    def test_it_agrees_with_the_workers_on_every_example(self):
        schema = report_schema.current()
        for report in FIXTURES["valid"]:
            self.assertEqual(report_schema.validate(schema, report), [], report["id"])
        for item in FIXTURES["invalid"]:
            self.assertNotEqual(report_schema.validate(schema, changed(item)), [], item["why"])

    def test_a_readouts_reports_are_held_to_its_own_and_agree_with_the_workers(self):
        schema = report_schema.current()
        for report in FIXTURES["readoutValid"]:
            self.assertEqual(report_schema.validate(schema["readout"], report), [], report["id"])
            self.assertNotEqual(report_schema.validate(schema, report), [], "a readout's is no run's")
        for item in FIXTURES["readoutInvalid"]:
            self.assertNotEqual(report_schema.validate(schema["readout"], changed(item, "readoutValid")), [],
                                item["why"])

    def test_a_readouts_report_as_the_tool_makes_it_fits(self):
        from tools.autoflash import readout, report_data
        made = readout.Became("'Kitchen' (BASICR4)", "BASICR4", "SN-ESP32C3-BASICR4-01", "1.1.0", True,
                              readout.ACCEPTED, "r-1", mac="12:34:56:ab:cd:ef")
        for outcome in report_data.READOUT_RESULTS:
            made.outcome, made.error = outcome, "other" if outcome == report_data.FAILED else None
            sent = {**report_data.readout_payload("0" * 16, made, 75.0), "proof": "1794000000:198"}
            self.assertEqual(report_schema.validate(report_schema.current()["readout"], sent), [], outcome)

    def test_the_failure_patterns_are_the_tools_in_words_both_languages_read_alike(self):
        from tools.autoflash import failures
        written = report_schema.current()["failurePatterns"]
        self.assertEqual(written, [[name, pattern.pattern] for name, pattern in failures.PATTERNS])
        for _, pattern in written:
            # Groups, lookarounds, flags and the like differ between Python and
            # JavaScript, or are missing in one: none is used.
            self.assertNotIn("(?", pattern)
            self.assertNotRegex(pattern, r"\\[AZzGQE]|\[:")

    def test_a_value_of_the_wrong_kind_is_turned_away(self):
        schema = report_schema.current()
        for value in (None, [], "report", 1, True):
            self.assertNotEqual(report_schema.validate(schema, value), [], repr(value))


if __name__ == "__main__":
    unittest.main()
