"""A fault scenario's earlier attempts kept when it is run again."""

import tempfile
import unittest
from pathlib import Path

from tools.repo.earlier_attempts import keep_earlier


class KeepEarlierTests(unittest.TestCase):
    def test_each_attempt_is_kept_in_order_and_the_latest_where_it_was(self):
        # Seen: a batch's second go wrote over the first's log, and how the first
        # failed was gone by the time it was looked at.
        with tempfile.TemporaryDirectory() as tmp:
            log, evidence = Path(tmp) / "s.log", Path(tmp) / "s-evidence"
            for attempt in ("first", "second", "third"):
                keep_earlier(log)
                log.write_text(attempt, encoding="utf-8")
                keep_earlier(evidence)
                evidence.mkdir()
                (evidence / "run.log").write_text(attempt, encoding="utf-8")
            self.assertEqual([(Path(tmp) / f"s.log{n}").read_text(encoding="utf-8") for n in (".1", ".2", "")],
                             ["first", "second", "third"])
            self.assertEqual((Path(tmp) / "s-evidence.1" / "run.log").read_text(encoding="utf-8"), "first")

    def test_nothing_there_is_nothing_to_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(keep_earlier(Path(tmp) / "none"))


if __name__ == "__main__":
    unittest.main()
