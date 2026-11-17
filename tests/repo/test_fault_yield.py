import json
import tempfile
import unittest
from pathlib import Path

from tools.repo import fault_yield


def results(folder: Path, *rows: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows) + "not json\n",
                                          encoding="utf-8")


class FoundTests(unittest.TestCase):
    def test_a_fault_is_read_off_its_scenario_id(self):
        self.assertEqual(fault_yield.fault_of("sonoff>esphome:K2:outage-120"), ("outage", "120", ""))
        self.assertEqual(fault_yield.fault_of("tasmota>sonoff:D4@90:crash-1"), ("crash", "1", "@90"))
        self.assertEqual(fault_yield.fault_of("sonoff>readout:read:outage-30"), ("outage", "30", ""))
        self.assertEqual(fault_yield.fault_of("sonoff>tasmota:C3:legacy-v1.2.0+G1off"),
                         ("legacy", "v1.2.0+G1off", ""))
        self.assertIsNone(fault_yield.fault_of("no fault here"))

    def test_each_length_says_how_many_ran_it_and_which_ever_failed(self):
        lines = fault_yield.found([
            {"id": "a>b:S1:outage-120", "verdict": "PASS"},
            {"id": "a>b:S2:outage-120", "verdict": "FAIL-SCREEN"},
            {"id": "a>b:S2:outage-120", "verdict": "PASS"},          # fixed since: it still found it
            {"id": "a>b:S1:outage-5", "result": "PASS"},             # the Beken suite's rows
            {"id": "a>b:S3:outage-5", "result": "NOT-FIRED"},        # never landed: not counted
            {"id": "a>b:S1@10:crash-1", "result": "ERROR"},          # the suite's own error
        ])
        self.assertEqual([line.split()[:5] for line in lines],
                         [["outage-5", "1", "run", "0", "failed"],
                          ["outage-120", "2", "run", "1", "failed", ]])
        self.assertIn("e.g. a>b:S2:outage-120 FAIL-SCREEN", lines[1])

    def test_a_row_copied_twice_from_one_machine_counts_once(self):
        with tempfile.TemporaryDirectory() as directory:
            row = {"id": "a>b:S1:outage-5", "verdict": "FAIL", "finished": "2026-11-08 10:00:00", "host": "h",
                   "instance": 1}
            results(Path(directory, "one", "suite"), row)
            results(Path(directory, "two", "suite"), row, dict(row, instance=2))
            self.assertEqual(len(fault_yield.rows(Path(directory, "one"), Path(directory, "two"))), 2)


if __name__ == "__main__":
    unittest.main()
