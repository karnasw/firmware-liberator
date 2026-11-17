"""proof_of_work: the work the API asks, as the Worker checks it."""

from __future__ import annotations

import contextlib
import hashlib
import io
import unittest
from unittest import mock

from tools.autoflash import proof_of_work

# Made by solve and checked by website/test/proof-of-work.test.mjs, so the
# tool and the Worker are held to the same work.
REPORT_ID = "0123456789abcdef"
VECTORS = [(proof_of_work.report_subject(REPORT_ID), "1794000000:4905"),
           (proof_of_work.body_subject(b"readout"), "1794000000:2293")]


class SolveTests(unittest.TestCase):
    def test_the_shared_vectors(self) -> None:
        for subject, expected in VECTORS:
            self.assertEqual(proof_of_work.solve(subject, 12, now=1794000000), expected, subject)

    def test_it_is_the_work_for_its_subject(self) -> None:
        subject = proof_of_work.report_subject("f" * 32)
        stamp, nonce = proof_of_work.solve(subject, 12, now=1794000000).split(":")
        digest = hashlib.sha256(f"{stamp}:{subject}:{nonce}".encode()).digest()
        self.assertEqual(stamp, "1794000000")
        self.assertLess(int.from_bytes(digest, "big"), 1 << (256 - 12))

    def test_a_report_and_a_readout_never_share_a_subject(self) -> None:
        report = proof_of_work.report_subject(REPORT_ID)
        self.assertNotRegex(report, r"^[0-9a-f]{64}$")
        self.assertRegex(proof_of_work.body_subject(b""), r"^[0-9a-f]{64}$")

    def test_long_work_says_it_is_still_going(self) -> None:
        ticks = []
        work = proof_of_work.solve("subject", 21, now=1794000000, tick=lambda: ticks.append(1))
        tries = int(work.split(":")[1])
        self.assertGreater(len(ticks), 0)
        self.assertEqual(len(ticks), tries // 0x40000)  # every quarter of a million tries, no more


class MainTests(unittest.TestCase):
    def test_it_prints_the_work_for_its_subject(self):
        subject = "report-" + "0" * 32
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(proof_of_work.os, "nice", create=True) as nice, \
                mock.patch.object(proof_of_work.time, "time", return_value=1794000000):
            self.assertEqual(proof_of_work.main([subject, "8"]), 0)
        self.assertEqual(out.getvalue().strip(), proof_of_work.solve(subject, 8, now=1794000000))
        nice.assert_called_once_with(19)  # the lowest priority, so the run beside it never waits

    def test_it_reads_its_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(proof_of_work.os, "nice", create=True), \
                mock.patch.object(proof_of_work.sys, "argv", ["proof_of_work.py", "subject", "4"]):
            self.assertEqual(proof_of_work.main(), 0)
        self.assertRegex(out.getvalue().strip(), r"^\d+:\d+$")

    def test_anything_else_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(proof_of_work.main([]), 2)
            self.assertEqual(proof_of_work.main(["subject", "many"]), 2)


if __name__ == "__main__":
    unittest.main()
