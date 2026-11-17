import json
import tempfile
import unittest
from pathlib import Path

from tools.repo import scenario_times


def results(folder: Path, *rows: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "results.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class TimesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_a_row_without_its_time_takes_the_gap_since_its_instances_last(self):
        # The Espressif suite's rows: each instance's worker runs one scenario after another.
        results(self.root / "suite",
                {"id": "a", "instance": 1, "finished": "2026-11-08 10:00:00"},
                {"id": "b", "instance": 2, "finished": "2026-11-08 10:01:00"},
                {"id": "c", "instance": 1, "finished": "2026-11-08 10:05:00"},
                {"id": "d", "instance": 2, "finished": "2026-11-08 10:31:00"})
        self.assertEqual(scenario_times.times(self.root), {"c": 300.0, "d": 1800.0})

    def test_a_row_saying_its_own_time_is_taken_at_its_word(self):
        results(self.root / "suite", {"id": "a", "at": "2026-11-08T10:00:00", "seconds": 412})
        self.assertEqual(scenario_times.times(self.root), {"a": 412.0})

    def test_the_latest_run_of_a_scenario_counts(self):
        results(self.root / "one", {"id": "a", "at": "2026-11-07T10:00:00", "seconds": 100})
        results(self.root / "two", {"id": "a", "at": "2026-11-08T10:00:00", "seconds": 200})
        self.assertEqual(scenario_times.times(self.root), {"a": 200.0})

    def test_a_pause_between_batches_is_not_a_scenario(self):
        results(self.root / "suite",
                {"id": "a", "instance": 1, "finished": "2026-11-08 01:00:00"},
                {"id": "b", "instance": 1, "finished": "2026-11-08 09:00:00"})
        self.assertEqual(scenario_times.times(self.root), {})

    def test_copies_kept_from_other_machines_are_read_where_each_family_keeps_its_own(self):
        results(self.root / "kept" / "second" / "suite-stub", {"id": "a", "at": "2026-11-08T10:00:00", "seconds": 100})
        results(self.root / "kept" / "second" / "beken" / "suite", {"id": "b", "at": "2026-11-08T10:00:00", "seconds": 200})
        self.assertEqual(scenario_times.kept(self.root), [self.root / "kept" / "second"])
        self.assertEqual(scenario_times.times(*scenario_times.kept(self.root)), {"a": 100.0})
        self.assertEqual(scenario_times.times(*scenario_times.kept(self.root, "beken")), {"b": 200.0})
        self.assertEqual(scenario_times.kept(self.root / "none"), [])

    def test_each_family_reads_its_own_suites_only(self):
        # The Beken suite's are in beken/, a level down from the Espressif suite's.
        results(self.root / "suite", {"id": "a", "at": "2026-11-08T10:00:00", "seconds": 100})
        results(self.root / "beken/suite", {"id": "b", "at": "2026-11-08T10:00:00", "seconds": 100})
        self.assertEqual(scenario_times.times(self.root), {"a": 100.0})
        self.assertEqual(scenario_times.times(self.root / "beken"), {"b": 100.0})


class OrderTests(unittest.TestCase):
    def test_the_longest_start_first(self):
        found = scenario_times.longest_first([{"id": "short"}, {"id": "long"}, {"id": "middle"}],
                                             {"short": 60, "long": 1800, "middle": 300})
        self.assertEqual([sc["id"] for sc in found], ["long", "middle", "short"])

    def test_one_never_run_is_put_as_the_typical_one_and_its_faults_length(self):
        took = {"a": 100, "b": 200, "c": 300}
        found = scenario_times.longest_first([{"id": "a"}, {"id": "c"}, {"id": "new", "arg": 180}], took)
        self.assertEqual([sc["id"] for sc in found], ["new", "c", "a"])   # 200 + 180
        found = scenario_times.longest_first([{"id": "a"}, {"id": "c"}, {"id": "new", "arg": "once"}], took)
        self.assertEqual([sc["id"] for sc in found], ["c", "new", "a"])   # 200

    def test_those_as_long_keep_their_order(self):
        found = scenario_times.longest_first([{"id": "x"}, {"id": "y"}], {})
        self.assertEqual([sc["id"] for sc in found], ["x", "y"])


class ShareTests(unittest.TestCase):
    def test_machines_are_given_work_to_end_together(self):
        took = {"a": 600, "b": 500, "c": 400, "d": 300, "e": 200, "f": 100}
        found = scenario_times.shares([{"id": i} for i in took], took, [1, 1])
        self.assertEqual([[sc["id"] for sc in share] for share, _ in found], [["a", "d", "e"], ["b", "c", "f"]])
        self.assertEqual([seconds for _, seconds in found], [1100, 1000])

    def test_a_machine_running_more_emulators_is_given_more(self):
        took = {i: 100 for i in "abcdefgh"}
        found = scenario_times.shares([{"id": i} for i in took], took, [3, 1])
        self.assertEqual([len(share) for share, _ in found], [6, 2])
        self.assertEqual([seconds for _, seconds in found], [200, 200])

    def test_with_nothing_timed_yet_each_machine_is_given_as_many(self):
        found = scenario_times.shares([{"id": i} for i in "abcd"], {}, [1, 1])
        self.assertEqual([len(share) for share, _ in found], [2, 2])

    def test_each_machine_runs_one_emulator_or_more(self):
        with self.assertRaises(ValueError):
            scenario_times.shares([{"id": "a"}], {}, [2, 0])

    def test_the_shares_are_written_beside_the_ids_and_said(self):
        with tempfile.TemporaryDirectory() as directory:
            ids = Path(directory) / "all.txt"
            ids.write_text("a any\nb cloud\nc any\n", encoding="utf-8")
            said = scenario_times.write_shares(str(ids), [1, 1], [{"id": i} for i in "abc"],
                                               {"a": 3600, "b": 1800, "c": 1800})
            self.assertEqual((Path(f"{ids}.1").read_text(encoding="utf-8"),
                              Path(f"{ids}.2").read_text(encoding="utf-8")), ("a\n", "b\nc\n"))
        self.assertEqual(said[0], f"{ids}.1: 1 scenarios on 1 emulators, about 1.0 h")
        with tempfile.TemporaryDirectory() as directory:
            ids = Path(directory) / "all.txt"
            ids.write_text("z\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not scenarios of this suite: z"):
                scenario_times.write_shares(str(ids), [1], [{"id": "a"}], {})

    def test_times_come_from_more_than_one_machines_results(self):
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            results(Path(one) / "suite", {"id": "a", "at": "2026-11-08T10:00:00", "seconds": 100})
            results(Path(two) / "suite", {"id": "b", "at": "2026-11-08T10:00:00", "seconds": 200})
            self.assertEqual(scenario_times.times(Path(one), Path(two)), {"a": 100.0, "b": 200.0})


if __name__ == "__main__":
    unittest.main()
