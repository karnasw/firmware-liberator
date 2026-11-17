"""report_upload: a run's report kept, its work done beside the run, and sent."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import hostsys, proof_of_work, report_upload as upload

TODAY = dt.date(2026, 11, 10)


class Work:
    """A proof's process, as subprocess gives one, already done."""

    def __init__(self, made="1794270511:1805", code=0, late=False):
        self.made, self.returncode, self.late = made, code, late

    def communicate(self, timeout=None):
        if self.late:
            raise subprocess.TimeoutExpired("proof", timeout)
        return self.made, ""

    def poll(self):
        return self.returncode


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.queue = Path(folder.name) / "captures" / "reports"  # neither there yet, as in a fresh copy
        for patch in (mock.patch.object(upload, "QUEUE", self.queue),
                      mock.patch.object(upload, "NOTICE", self.queue / "notice-shown"),
                      mock.patch.dict(os.environ, {"AUTOFLASH_NO_REPORT": ""}),
                      mock.patch.object(upload, "in_background", lambda work: work())):
            patch.start()
            self.addCleanup(patch.stop)
        for name in hostsys.EMULATOR_MARKS:
            os.environ.pop(name, None)

    def item(self, report_id, made="2026-11-09", proof="1:2"):
        upload.keep({"report": {"id": report_id, "format": 1}, "proof": proof, "made": made})

    def ids(self):
        return sorted(path.stem for path in self.queue.glob("*.json"))


class EnabledTests(Case):
    def test_on_unless_told_otherwise(self):
        self.assertTrue(upload.enabled(SimpleNamespace(args=SimpleNamespace())))
        for flag in ("no_report", "dry_run", "simulate_backup_failure", "simulate_bad_image", "simulate_unknown_table"):
            self.assertFalse(upload.enabled(SimpleNamespace(args=SimpleNamespace(**{flag: True}))), flag)
        for value in ("1", "yes", "TRUE", "on"):
            with mock.patch.dict(os.environ, {"AUTOFLASH_NO_REPORT": value}):
                self.assertFalse(upload.enabled(SimpleNamespace(args=SimpleNamespace())), value)
        # Either chip family's: the Beken one's computer has only its device's power switch.
        for name, value in (("AUTOFLASH_EMULATOR_URL", "http://127.0.0.1:8100"),
                            ("AUTOFLASH_EMULATOR_POWER", "/device/power")):
            with mock.patch.dict(os.environ, {name: value}):
                self.assertFalse(upload.enabled(SimpleNamespace(args=SimpleNamespace())), name)

    def test_the_tests_themselves_send_none(self):
        # tests/no_network.py, which every test package installs, says so.
        from tests import no_network
        self.assertIn('os.environ["AUTOFLASH_NO_REPORT"] = "1"', Path(no_network.__file__).read_text(encoding="utf-8"))


class ApiTests(unittest.TestCase):
    """What the API takes (website/api/reports.js), as the tool sends it."""

    def constant(self, name):
        source = (Path(__file__).resolve().parents[2] / "website" / "api" / "reports.js").read_text(encoding="utf-8")
        return int(re.search(rf"export const {name} = (\d+);", source).group(1))

    def test_its_work_is_as_hard_as_the_api_asks(self):
        self.assertEqual(upload.PROOF_BITS, self.constant("REPORT_PROOF_BITS"))

    def test_every_report_kept_goes_in_one_sending(self):
        self.assertLessEqual(upload.KEEP, self.constant("MAX_REPORTS"))


class DocumentedTests(unittest.TestCase):
    """docs/what-the-tool-sends.md, which the notice links to, says what the tool does."""

    def test_the_notice_links_to_the_page_that_says_what_is_sent(self):
        page = upload.ROOT / "docs" / "what-the-tool-sends.md"
        self.assertTrue(page.exists())
        self.assertEqual(upload.WHAT_IS_SENT, "https://firmwareliberator.com/docs/what-the-tool-sends")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(upload, "NOTICE", Path(tmp) / "notice"), \
                mock.patch.object(upload, "QUEUE", Path(tmp) / "queue"), mock.patch.object(upload.output, "tell"), \
                mock.patch.object(upload, "enabled", return_value=True), mock.patch.object(upload, "in_background"), \
                mock.patch.object(upload.output, "show_link") as linked:
            upload.announce(SimpleNamespace(args=SimpleNamespace()))
        linked.assert_called_once_with(upload.WHAT_IS_SENT, "What is sent")

    def test_the_page_says_how_to_stop_it_and_where_unsent_ones_wait_as_the_tool_does(self):
        text = (upload.ROOT / "docs" / "what-the-tool-sends.md").read_text(encoding="utf-8")
        for said in ("--no-report", "AUTOFLASH_NO_REPORT=1", f"{upload.QUEUE.relative_to(upload.ROOT)}/",
                     f"{upload.KEEP_DAYS} days", "https://firmwareliberator.com/stats", "CC BY 4.0"):
            self.assertIn(said, text)

    def test_the_readme_links_to_it(self):
        readme = (upload.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("(docs/what-the-tool-sends.md)", readme)


class BeginTests(Case):
    def ctx(self, **args):
        return SimpleNamespace(args=SimpleNamespace(**args))

    def test_a_run_begins_its_report_and_its_work(self):
        ctx = self.ctx()
        with mock.patch.object(upload, "start_work", return_value=Work()) as start, contextlib.redirect_stdout(io.StringIO()):
            upload.begin(ctx)
        self.assertRegex(ctx.report_id, r"^[0-9a-f]{16}$")
        start.assert_called_once_with(ctx.report_id)

    def test_the_notice_is_said_once_on_a_computer(self):
        said = []
        with mock.patch.object(upload, "start_work", return_value=Work()), \
                mock.patch.object(upload.output, "tell", said.append):
            upload.begin(self.ctx())
            upload.begin(self.ctx())
        self.assertEqual(said, [upload.NOTICE_TEXT])
        self.assertIn("--no-report", upload.NOTICE_TEXT)

    def test_the_notice_is_said_once_where_reports_were_kept_before_it(self):
        self.item("a" * 32)
        said = []
        with mock.patch.object(upload, "start_work", return_value=Work()), \
                mock.patch.object(upload, "post", side_effect=OSError("down")), \
                mock.patch.object(upload.output, "tell", said.append):
            upload.begin(self.ctx())
            upload.begin(self.ctx())
        self.assertEqual(said, [upload.NOTICE_TEXT])

    def test_what_a_report_counts_of_one_run_starts_again_with_each(self):
        # Several devices in one sitting ("what next"): one run's counts are not the next one's.
        from tools.autoflash import interrupt
        self.addCleanup(interrupt.reset_carried_on)
        upload.hostsys.asked_for_admin = upload.hostsys.security_note_shown = True
        interrupt.note_carried_on()
        with mock.patch.object(upload, "start_work") as start:
            upload.begin(self.ctx(no_report=True))   # so even with reports off
        start.assert_not_called()
        self.assertEqual((upload.hostsys.asked_for_admin, upload.hostsys.security_note_shown, interrupt.carried_on()),
                         (False, False, 0))

    def test_switched_off_nothing_begins(self):
        ctx = self.ctx(no_report=True)
        with mock.patch.object(upload, "start_work") as start:
            upload.begin(ctx)
        start.assert_not_called()
        self.assertFalse(hasattr(ctx, "report_id"))
        self.assertFalse(self.queue.exists(), "not even the notice")

    def test_any_kept_from_before_are_sent_as_it_begins(self):
        self.item("a" * 32)
        with mock.patch.object(upload, "start_work", return_value=Work()), \
                mock.patch.object(upload, "post", return_value={"accepted": ["a" * 32]}) as post, \
                contextlib.redirect_stdout(io.StringIO()):
            upload.begin(self.ctx())
        self.assertEqual([report["id"] for report in post.call_args.args[0]], ["a" * 32])
        self.assertEqual(self.ids(), [])


class SendTests(Case):
    def ctx(self):
        return SimpleNamespace(args=SimpleNamespace(), report_id="b" * 32, report_work=Work("5:6"))

    def test_the_report_is_kept_before_it_is_sent_with_its_work(self):
        seen = []

        def post(reports, url=None):
            seen.append((self.ids(), reports))
            return {"accepted": [reports[0]["id"]]}
        with mock.patch.object(upload.report_data, "payload", return_value={"id": "b" * 32, "format": 1}), \
                mock.patch.object(upload, "post", post):
            upload.send(self.ctx(), "converted", 300, False, {})
        self.assertEqual(seen, [(["b" * 32], [{"id": "b" * 32, "format": 1, "proof": "5:6"}])])
        self.assertEqual(self.ids(), [], "forgotten once taken")

    def test_one_report_a_run(self):
        ctx = self.ctx()
        with mock.patch.object(upload.report_data, "payload", return_value={"id": "b" * 32}) as payload, \
                mock.patch.object(upload, "post", return_value={"accepted": ["b" * 32]}):
            upload.send(ctx, "converted", 300, False)
            upload.send(ctx, "failed", 300, False)
        self.assertEqual(payload.call_count, 1)
        self.assertEqual(ctx.last_report_id, "b" * 32, "its id kept, for the screen's report and an issue")

    def test_a_report_that_cannot_be_made_never_spoils_the_runs_end(self):
        logged = []
        with mock.patch.object(upload.report_data, "payload", side_effect=KeyError("device_kind")), \
                mock.patch.object(upload.output, "log_only", logged.append):
            self.assertIsNone(upload.send(self.ctx(), "converted", 300, False))
        self.assertIn("could not be kept", logged[0])

    def test_a_run_not_begun_sends_nothing(self):
        with mock.patch.object(upload, "post") as post:
            self.assertIsNone(upload.send(SimpleNamespace(args=SimpleNamespace()), "converted", 300, False))
        post.assert_not_called()


class OutcomeTests(Case):
    """Each run's outcome, a line in its folder whether its report is sent or
    not: what the emulators' fault suites read of why a run stopped."""

    def test_each_run_adds_its_outcome_a_run_carried_on_in_the_same_folder_too(self):
        folder = self.queue.parent / "devices" / "12-34-56-ab-cd-ef" / "runs" / "20261114-180016"
        folder.mkdir(parents=True)
        ctx = SimpleNamespace(args=SimpleNamespace(), state=SimpleNamespace(path=folder / "autoflash-state.json"))
        endings = iter([("failed", {"step": "K2", "error": "network-join", "text": "words", "crash_site": None}),
                        ("converted", None)])
        with mock.patch.object(upload.report_data, "outcome", side_effect=lambda ctx, ending: next(endings)), \
                mock.patch.object(upload, "post") as post:
            upload.send(ctx, "failed", 300, False)
            upload.send(ctx, "converted", 300, True)
        post.assert_not_called()   # no report this run: its outcome is kept all the same
        lines = [json.loads(line) for line in (folder / upload.OUTCOMES).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(line["result"], line["stopped"]) for line in lines],
                         [("failed", {"step": "K2", "error": "network-join"}), ("converted", None)],
                         "the class, never the words, which stay in run.log")
        self.assertLessEqual(lines[0]["at"], lines[1]["at"])

    def test_a_run_that_converted_keeps_its_checks_as_it_judged_them(self):
        from tools.autoflash import report
        folder = self.queue.parent / "devices" / "12-34-56-ab-cd-ef" / "runs" / "20261115-100000"
        folder.mkdir(parents=True)
        ctx = SimpleNamespace(args=SimpleNamespace(), state=SimpleNamespace(path=folder / "autoflash-state.json"))
        with mock.patch.object(upload.report_data, "outcome", return_value=("converted", None)), \
                mock.patch.object(upload, "post"):
            upload.send(ctx, "converted", 300, False, {"Relay": report.METER_CONFIRMS, "Power meter": "on: 230 V"})
            upload.send(ctx, "converted", 300, False, {"Relay": report.RELAY_SWITCHES})
        lines = [json.loads(line) for line in (folder / upload.OUTCOMES).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["checks"] for line in lines],
                         [{"answer": "passed", "relay": report.METER_CONFIRMS, "meter_confirms": True},
                          {"answer": "passed", "relay": report.RELAY_SWITCHES, "meter_confirms": None}],
                         "a device with no meter has nothing for it to confirm")

    def test_an_outcome_that_cannot_be_kept_never_spoils_the_runs_end(self):
        logged = []
        ctx = SimpleNamespace(args=SimpleNamespace(), state=SimpleNamespace(path=Path("/nonexistent/x/state.json")))
        with mock.patch.object(upload.report_data, "outcome", return_value=("converted", None)), \
                mock.patch.object(upload.output, "log_only", logged.append):
            upload.send(ctx, "converted", 300, False)
        self.assertIn("outcome could not be kept", logged[0])


class ReadoutTests(Case):
    """A readout's report: one for each device's readout, begun as it starts and
    sent as it ends, through the same queue as a run's."""

    def test_each_readout_begins_its_report_and_sends_it_once_as_it_ends(self):
        ctx = SimpleNamespace(args=SimpleNamespace())
        posted = []

        def post(reports, url=None):
            posted.append(reports)
            return {"accepted": [report["id"] for report in reports]}
        made = SimpleNamespace(name="'Kitchen' (BASICR4)")
        with mock.patch.object(upload, "start_work", return_value=Work("7:8")), \
                mock.patch.object(upload.report_data, "readout_payload",
                                  side_effect=lambda report_id, became, seconds: {"id": report_id, "kind": "readout"}) as payload, \
                mock.patch.object(upload, "post", post):
            upload.begin_readout(ctx)
            first = ctx.readout_report_id
            upload.send_readout(ctx, made, 60)
            upload.send_readout(ctx, made, 60)   # one a readout
            upload.begin_readout(ctx)
            upload.send_readout(ctx, made, 60)
        self.assertEqual(payload.call_count, 2)
        self.assertEqual(payload.call_args_list[0].args, (first, made, 60))
        self.assertEqual([[report["proof"] for report in reports] for reports in posted], [["7:8"], ["7:8"]])
        self.assertNotEqual(posted[0][0]["id"], posted[1][0]["id"])
        self.assertEqual(self.ids(), [], "forgotten once taken")
        self.assertEqual(made.report_id, posted[1][0]["id"], "its id on it, for the screen and an issue")

    def test_switched_off_none_is_begun_or_sent(self):
        ctx = SimpleNamespace(args=SimpleNamespace(no_report=True))
        with mock.patch.object(upload, "start_work") as work, mock.patch.object(upload, "post") as post:
            upload.begin_readout(ctx)
            self.assertIsNone(upload.send_readout(ctx, SimpleNamespace(), 60))
        work.assert_not_called()
        post.assert_not_called()

    def test_a_readout_says_the_notice_and_sends_those_kept_as_a_run_does(self):
        self.item("c" * 32)
        with mock.patch.object(upload, "post", return_value={"accepted": ["c" * 32]}) as post, \
                contextlib.redirect_stdout(io.StringIO()), mock.patch.object(upload.output, "tell") as told:
            upload.announce(SimpleNamespace(args=SimpleNamespace()))
        told.assert_called_once_with(upload.NOTICE_TEXT)
        post.assert_called_once()


class SendingTests(Case):
    def test_each_the_api_answered_for_is_forgotten_the_rest_kept(self):
        for name in ("accepted", "duplicate", "rejected", "later", "unanswered"):
            self.item(name.ljust(32, "0"))
        answer = {"accepted": ["never-kept".ljust(32, "0"), "accepted".ljust(32, "0")], "duplicate": ["duplicate".ljust(32, "0")],
                  "rejected": [{"id": "rejected".ljust(32, "0"), "why": "x"}, {"id": None, "why": "y"}],
                  "later": ["later".ljust(32, "0")]}
        with mock.patch.object(upload, "post", return_value=answer):
            upload.send_kept(TODAY)
        self.assertEqual(self.ids(), ["later".ljust(32, "0"), "unanswered".ljust(32, "0")])

    def test_a_network_or_api_error_keeps_them_all(self):
        self.item("c" * 32)
        for error in (OSError("no route"), ValueError("not JSON")):
            with mock.patch.object(upload, "post", side_effect=error):
                upload.send_kept(TODAY)
            self.assertEqual(self.ids(), ["c" * 32])

    def test_one_kept_without_its_work_has_it_done_then_and_kept_until_sent(self):
        self.item("d" * 32, proof=None)
        with mock.patch.object(upload, "start_work", return_value=Work("7:8")) as start, \
                mock.patch.object(upload, "post", side_effect=OSError("down")):
            upload.send_kept(TODAY)
        start.assert_called_once_with("d" * 32)
        self.assertEqual(json.loads((self.queue / f"{'d' * 32}.json").read_text())["proof"], "7:8")

    def test_one_whose_work_is_not_done_in_time_waits_for_another_go(self):
        self.item("e" * 32, proof=None)
        with mock.patch.object(upload, "start_work", return_value=Work(late=True)), \
                mock.patch.object(upload, "post") as post:
            upload.send_kept(TODAY)
        post.assert_not_called()
        self.assertEqual(self.ids(), ["e" * 32])

    def test_kept_for_thirty_days_and_ninety_nine_at_most(self):
        self.item("old".ljust(32, "0"), made="2026-10-09")
        for n in range(upload.KEEP + 2):
            self.item(f"{n:032x}")
        (self.queue / "broken.json").write_text("not json", encoding="utf-8")
        kept = upload.prune(TODAY)
        self.assertEqual(len(kept), upload.KEEP)
        self.assertNotIn("old".ljust(32, "0"), self.ids())
        self.assertNotIn("broken", self.ids())
        self.assertEqual(len(self.ids()), upload.KEEP)

    def test_one_thirty_days_old_is_kept_one_a_day_older_is_not(self):
        self.item("thirty".ljust(32, "0"), made=(TODAY - dt.timedelta(days=30)).isoformat())
        self.item("thirty-one".ljust(32, "0"), made=(TODAY - dt.timedelta(days=31)).isoformat())
        self.assertEqual([item["report"]["id"] for item in upload.prune(TODAY)], ["thirty".ljust(32, "0")])

    def test_sent_as_the_api_takes_them(self):
        answered = io.BytesIO(json.dumps({"accepted": []}).encode())
        with mock.patch.object(upload.hostsys, "urlopen", return_value=contextlib.nullcontext(answered)) as opened:
            self.assertEqual(upload.post([{"id": "f" * 32}], "http://127.0.0.1:9/reports"), {"accepted": []})
        request = opened.call_args.args[0]
        self.assertEqual((request.get_method(), request.full_url), ("POST", "http://127.0.0.1:9/reports"))
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"reports": [{"id": "f" * 32}]})


class WorkTests(Case):
    def test_its_work_is_done_at_the_lowest_priority_and_is_the_reports(self):
        with mock.patch.object(upload, "PROOF_BITS", 8):
            made = upload.proof_of(upload.start_work("0" * 32), timeout=60)
        stamp, nonce = made.split(":")
        digest = hashlib.sha256(f"{stamp}:{proof_of_work.report_subject('0' * 32)}:{nonce}".encode()).digest()
        self.assertLess(int.from_bytes(digest, "big"), 1 << (256 - 8), "the work the Worker checks")
        source = Path(proof_of_work.__file__).read_text(encoding="utf-8")
        self.assertIn("os.nice(19)", source)

    def test_on_windows_it_runs_below_normal_with_no_window(self):
        with mock.patch.object(upload.hostsys, "IS_WINDOWS", True), \
                mock.patch.object(upload.subprocess, "Popen") as popen, mock.patch.object(upload.atexit, "register"):
            upload.start_work("0" * 32)
        self.assertEqual(popen.call_args.kwargs["creationflags"], 0x4000 | 0x08000000)

    def test_its_process_is_ended_with_the_tool(self):
        with mock.patch.object(upload.subprocess, "Popen") as popen, \
                mock.patch.object(upload.atexit, "register") as register:
            process = upload.start_work("0" * 32)
        register.assert_called_once_with(upload.stop, process)
        popen.return_value.poll.return_value = None
        upload.stop(process)
        process.kill.assert_called_once_with()

    def test_work_that_failed_or_is_late_is_none(self):
        self.assertIsNone(upload.proof_of(Work(code=1)))
        self.assertIsNone(upload.proof_of(Work(late=True)))
        self.assertIsNone(upload.proof_of(Work(made="")))
        self.assertIsNone(upload.proof_of(None))


if __name__ == "__main__":
    unittest.main()
