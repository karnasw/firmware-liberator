import http.server
import io
import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import kinds
from tools.repo import emulator_stats

ROOT = Path(__file__).resolve().parents[2]
ADMIN = (ROOT / "website/admin/emulator.js").read_text(encoding="utf-8")
# The fields runs and batches both carry, which the admin view spreads into each.
COMMON = re.findall(r"^  (\w+):", re.search(r"const COMMON = \{\n(.*?)\n\};", ADMIN, re.S).group(1), re.M)


def run_args(**more):
    return {"family": "espressif", "suite": "fault", "device": "sonoff-s60", "scenario": "sonoff>tasmota:D4:deviceoff-1",
            "verdict": "PASS", "cloud": "stub", "route": "sonoff>tasmota", "step": "D4", "fault": "deviceoff",
            "at": 40, "tries": 1, "seconds": 300, "workers": 6, **more}


class Admin(http.server.BaseHTTPRequestHandler):
    """The admin view's upload, as emulator.js answers it: what `answer`
    says, every run taken but those `refuse` names."""
    answer = 200
    refuse: set = set()
    seen: list = []
    batches: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = {"/v1/emulator/runs": "runs", "/v1/emulator/batches": "batches"}[self.path]
        if key == "runs":
            type(self).seen.append((self.headers, body))
        else:
            type(self).batches.append(body)
        if self.answer != 200:
            self.send_response(self.answer)
            self.end_headers()
            return
        ids = [emulator_stats._id(key, item) for item in body[key]]
        said = json.dumps({"taken": [one for one in ids if one not in self.refuse],
                           "refused": [{"id": one, "why": "made up"} for one in ids if one in self.refuse]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(said.encode())

    def log_message(self, *args):
        pass


class EmulatorStatsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Admin)   # a port of its own
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Admin.answer, Admin.refuse, Admin.seen, Admin.batches = 200, set(), [], []
        self.env_file = self.folder / "home" / "emulator.env"
        self.env_file.parent.mkdir()
        self.env_file.write_text("EMULATOR_STATS_CLIENT_ID=cid.access\nEMULATOR_STATS_CLIENT_SECRET=made-up\n",
                                 encoding="utf-8")
        for patch in (mock.patch.object(emulator_stats, "CREDENTIALS", self.env_file), mock.patch.dict("os.environ", {
                "AUTOFLASH_EMU_STATS_URL": f"http://127.0.0.1:{server.server_address[1]}/v1/emulator",
                "EMULATOR_STATS_CLIENT_ID": "", "EMULATOR_STATS_CLIENT_SECRET": "", "AUTOFLASH_EMU_NO_STATS": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def settled(self):
        """Once every background send is done: the runs a test kept, sent or not."""
        with emulator_stats._sending:
            pass
        deadline = time.monotonic() + 10
        while emulator_stats._waiting and time.monotonic() < deadline:
            time.sleep(0.01)
        with emulator_stats._sending:
            pass

    def kept(self):
        path = self.folder / emulator_stats.UNSENT
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_a_run_has_every_field_the_admin_view_takes_and_no_other(self):
        fields = re.search(r"const RUN_FIELDS = \{\n(.*?)\n\};", ADMIN, re.S).group(1)
        self.assertEqual(sorted(emulator_stats.run_row(**run_args())),
                         sorted(re.findall(r"^  (\w+):", fields, re.M) + list(COMMON)))

    def test_as_many_a_request_as_the_admin_view_takes(self):
        self.assertIn(f"export const MAX_RUNS = {emulator_stats.CHUNK};", ADMIN)

    def test_a_run_is_said_as_the_admin_view_takes_it(self):
        row = emulator_stats.run_row(**run_args(scenario="caf\u00e9 \x00" + "x" * 300, fault=60, at=150, tries="x",
                                                verdict="FAIL-SCREEN: shown: 'x'"))
        self.assertRegex(row["id"], r"^[0-9a-f]{32}$")
        self.assertRegex(row["finished"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(row["batch"], emulator_stats.BATCH)
        self.assertEqual((row["scenario"][:6], len(row["scenario"])), ("caf? ?", 200))
        self.assertEqual((row["fault"], row["at"], row["tries"], row["verdict"]), ("60", None, None, "FAIL-SCREEN"))
        self.assertIsInstance(row["dirty"], bool)
        self.assertEqual(emulator_stats.verdict_of("pass"), "PASS")
        self.assertEqual(emulator_stats.verdict_of("???"), "ERROR")

    def test_a_run_says_why_its_runs_stopped_as_each_run_classed_it(self):
        outcomes = [{"at": 1.0, "result": "failed", "stopped": {"step": "K2", "error": "network-join"}},
                    {"at": 2.0, "result": "cancelled", "stopped": {"step": "K2", "error": "ctrl-c"}},
                    {"at": 3.0, "result": "crashed", "stopped": {"step": "K2", "error": "crash"}},
                    {"at": 4.0, "result": "converted", "stopped": None}]
        self.assertEqual(emulator_stats.stopped_by(outcomes), ["network-join", "crash"],
                         "a run the person stopped did not stop of itself")
        self.assertEqual(emulator_stats.stopped_by(None), [])
        self.assertEqual(emulator_stats.stopped_by([{"result": "failed", "stopped": {"error": "Not A Class"}}]), [])
        self.assertEqual(len(emulator_stats.stopped_by(outcomes[:1] * 40)), emulator_stats.STOPPED_MOST)
        self.assertIn(f"export const STOPPED_MOST = {emulator_stats.STOPPED_MOST};", ADMIN)
        row = emulator_stats.run_row(**run_args(outcomes=outcomes, arg=120, starved=True,
                                                error="FileNotFoundError(2, 'No such file: /home/x/y')"))
        self.assertEqual((row["stopped_by"], row["arg"], row["starved"]), (["network-join", "crash"], "120", True))
        self.assertEqual(row["error"], "FileNotFoundError", "the class alone, never the message's paths")
        self.assertIsNone(emulator_stats.error_of("not an exception"))

    def test_a_batch_says_which_list_it_ran(self):
        every, quick = ["a", "b", "c"], ["a", "c"]
        self.assertEqual(emulator_stats.tier(every, every, quick), "full")
        self.assertEqual(emulator_stats.tier(["c", "a"], every, quick), "quick")
        self.assertEqual(emulator_stats.tier(["a"], every, quick), "subset")
        fields = re.search(r"const BATCH_FIELDS = \{\n(.*?)\n\};", ADMIN, re.S).group(1)
        self.assertIn('tier: (v) => ' + " || ".join(f'v === "{name}"' for name in emulator_stats.TIERS), fields)

    def test_a_run_names_its_familys_emulator_image_as_image_sh_tags_it(self):
        for family in ("espressif", "beken"):
            tag = subprocess.run([str(ROOT / "tools" / family / "emulator" / "image.sh"), "tag"], capture_output=True,
                                 text=True, check=True).stdout.strip()
            self.assertEqual(emulator_stats.run_row(**run_args(family=family))["image"], tag)
        self.assertIsNone(emulator_stats.image("no-such-family"))

    def test_a_fault_run_carries_its_faults_argument_and_its_runs_results(self):
        with mock.patch.object(emulator_stats, "record") as kept:
            emulator_stats.fault_run(self.folder, family="beken", device="sonoff-s61",
                                     scenario={"id": "a>b:K2:outage-120", "route": "a>b", "step": "K2",
                                               "fault": "outage", "arg": 120, "at": 40},
                                     verdict="PASS", tries=2, seconds=600, cloud="stub", workers=7, rerun=False,
                                     outcomes=[{"result": "converted"}], error=None, starved=False)
        self.assertEqual({k: kept.call_args.kwargs[k] for k in ("fault", "arg", "outcomes")},
                         {"fault": "outage", "arg": 120, "outcomes": [{"result": "converted"}]})

    def test_each_device_has_the_firmware_the_checkout_installs_on_it(self):
        for kind in kinds.EVERY:
            firmware = emulator_stats.firmware(kind.key)
            self.assertTrue(firmware, kind.key)
            self.assertLessEqual(len(firmware), 16, "the admin view takes at most 16")
        self.assertIn("tasmota", emulator_stats.firmware("sonoff-s60"))
        self.assertIn("openbeken", emulator_stats.firmware("sonoff-s61"))

    def test_each_run_is_sent_as_it_is_kept_with_the_token(self):
        for verdict in ("PASS", "FAIL"):
            emulator_stats.record(self.folder, **run_args(verdict=verdict))
            self.settled()
        self.assertEqual(self.kept(), [])
        self.assertEqual([[run["verdict"] for run in body["runs"]] for _, body in Admin.seen], [["PASS"], ["FAIL"]])
        headers = Admin.seen[0][0]
        self.assertEqual((headers.get("CF-Access-Client-Id"), headers.get("CF-Access-Client-Secret")),
                         ("cid.access", "made-up"))
        self.assertEqual(emulator_stats.send(self.folder), "Emulator stats: none to send")

    def test_runs_kept_while_a_send_is_under_way_are_sent_after_it(self):
        with emulator_stats._sending:   # a send under way
            for _ in range(3):
                emulator_stats.record(self.folder, **run_args())
            self.assertEqual(len(self.kept()), 3)
        self.assertEqual(emulator_stats.send(self.folder), "Emulator stats: 3 sent")
        self.settled()
        self.assertEqual(self.kept(), [])
        self.assertEqual(sum(len(body["runs"]) for _, body in Admin.seen), 3, "each sent once")

    def test_runs_left_over_are_sent_as_the_batch_ends(self):
        Admin.answer = 503
        for _ in range(2):
            emulator_stats.record(self.folder, **run_args())
            self.settled()
        self.assertEqual(len(self.kept()), 2)
        Admin.answer = 200
        self.assertEqual(emulator_stats.send(self.folder), "Emulator stats: 2 sent")
        self.assertEqual(self.kept(), [])

    def test_a_run_refused_is_said_and_not_kept_for_ever(self):
        with emulator_stats._sending:
            emulator_stats.record(self.folder, **run_args())
            emulator_stats.record(self.folder, **run_args())
            Admin.refuse = {self.kept()[0]["id"]}
        self.assertIn("1 sent, 1 refused", emulator_stats.send(self.folder))
        self.assertEqual(self.kept(), [])

    def test_runs_the_admin_view_did_not_take_are_kept_for_the_next_batch(self):
        Admin.answer = 503
        emulator_stats.record(self.folder, **run_args())
        self.settled()
        self.assertIn("1 kept to send later (the admin view answered 503)", emulator_stats.send(self.folder))
        self.assertEqual(len(self.kept()), 1)
        Admin.answer = 200
        self.assertEqual(emulator_stats.send(self.folder), "Emulator stats: 1 sent")

    def test_more_than_a_request_takes_are_sent_as_several(self):
        with emulator_stats._sending:
            for _ in range(emulator_stats.CHUNK + 1):
                emulator_stats.record(self.folder, **run_args())
        self.assertIn(f"{emulator_stats.CHUNK + 1} sent", emulator_stats.send(self.folder))
        self.assertEqual([len(body["runs"]) for _, body in Admin.seen], [emulator_stats.CHUNK, 1])

    def test_without_the_token_nothing_is_sent_and_it_says_where_it_comes_from(self):
        self.env_file.unlink()
        emulator_stats.record(self.folder, **run_args())
        self.settled()
        said = emulator_stats.send(self.folder)
        self.assertIn("1 kept, not sent - no EMULATOR_STATS_CLIENT_ID", said)
        self.assertIn("cloudflare_settings.py writes it", said)
        self.assertEqual((Admin.seen, len(self.kept())), ([], 1))

    def test_switched_off_nothing_is_kept_nor_sent(self):
        # As every test has it (tests/no_network.py), so a suite's batch run in a test sends nothing.
        with emulator_stats._sending:   # kept, its send left for later
            emulator_stats.record(self.folder, **run_args())
        with mock.patch.dict("os.environ", {"AUTOFLASH_EMU_NO_STATS": "1"}):
            emulator_stats.record(self.folder, **run_args())
            self.assertEqual(emulator_stats.send(self.folder), "Emulator stats: off (AUTOFLASH_EMU_NO_STATS)")
        self.assertEqual((len(self.kept()), Admin.seen), (1, []))
        emulator_stats._waiting.discard(self.folder)

    def test_a_batch_says_what_it_set_out_to_run_and_is_sent_before_its_runs(self):
        with emulator_stats._sending:
            emulator_stats.began(self.folder, family="beken", suite="fault", device="sonoff-s61", total=40, planned=12,
                                 cloud="stub", workers=2)
            emulator_stats.record(self.folder, **run_args(rerun=True))
        self.assertEqual(emulator_stats.send(self.folder), "Emulator stats: 1 sent, 1 batches")
        [body] = Admin.batches
        [sent] = body["batches"]
        self.assertEqual({key: sent[key] for key in ("batch", "family", "device", "suite", "total", "planned")},
                         {"batch": emulator_stats.BATCH, "family": "beken", "device": "sonoff-s61", "suite": "fault",
                          "total": 40, "planned": 12})
        self.assertEqual(sorted(sent), sorted(re.findall(r"^  (\w+):", re.search(
            r"const BATCH_FIELDS = \{\n(.*?)\n\};", ADMIN, re.S).group(1), re.M) + list(COMMON)))
        self.assertIs(Admin.seen[0][1]["runs"][0]["rerun"], True)
        self.assertFalse((self.folder / emulator_stats.UNSENT_BATCHES).exists())

    def test_a_batch_begun_without_credentials_says_so_once_and_keeps_its_runs(self):
        self.env_file.unlink()
        self.addCleanup(setattr, emulator_stats, "_told_unsent", emulator_stats._told_unsent)
        emulator_stats._told_unsent = False
        err = io.StringIO()
        with emulator_stats._sending, mock.patch("sys.stderr", err):
            for device in ("sonoff-s61", "sonoff-s60tpf"):   # a suite begins a batch for each device
                emulator_stats.began(self.folder, family="beken", suite="fault", device=device, total=40,
                                     planned=12, cloud="stub", workers=2)
        said = err.getvalue()
        self.assertEqual(said.count("Emulator stats: not sent - this computer has no emulator-stats token"), 1, said)
        self.assertIn(f"({self.env_file} missing). Runs are kept in {self.folder}.", said)
        self.assertIn("  To add it: python3 tools/repo/cloudflare_settings.py rotate emulator-stats (a new secret", said)
        self.assertNotIn("push", said)
        self.assertIn(f"Then here: python3 tools/repo/emulator_stats.py send {self.folder}", said)
        self.assertTrue((self.folder / emulator_stats.UNSENT_BATCHES).exists())
        emulator_stats._waiting.discard(self.folder)

    def test_push_with_no_token_here_says_how_to_have_it_again(self):
        self.env_file.unlink()
        [said] = emulator_stats.push(["host-a"])
        self.assertIn("cloudflare_settings.py rotate emulator-stats", said)

    def test_a_batch_begun_with_credentials_says_nothing_of_them(self):
        self.addCleanup(setattr, emulator_stats, "_told_unsent", emulator_stats._told_unsent)
        emulator_stats._told_unsent = False
        err = io.StringIO()
        with emulator_stats._sending, mock.patch("sys.stderr", err):
            emulator_stats.began(self.folder, family="beken", suite="fault", device="sonoff-s61", total=40,
                                 planned=12, cloud="stub", workers=2)
        self.assertEqual(err.getvalue(), "")
        emulator_stats._waiting.discard(self.folder)

    def test_a_check_run_is_said_as_every_familys_is(self):
        with emulator_stats._sending:
            emulator_stats.check_run(self.folder, family="espressif", device="sonoff-s60", route="sonoff>tasmota",
                                     passed=False, seconds=90, cloud="stub")
        [row] = self.kept()
        self.assertEqual((row["suite"], row["scenario"], row["route"], row["verdict"], row["step"]),
                         ("check", "check:sonoff>tasmota", "sonoff>tasmota", "FAIL", None))
        emulator_stats._waiting.discard(self.folder)

    def test_the_credentials_are_written_for_their_owner_alone(self):
        with mock.patch.object(emulator_stats, "CREDENTIALS", self.folder / "new" / "emulator.env"):
            path = emulator_stats.write_credentials("cid2.access", "made-up-2")
            self.assertEqual(emulator_stats.client(), ("cid2.access", "made-up-2"))
        self.assertEqual((path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777), (0o600, 0o700))

    def test_push_sends_the_credentials_on_ssh_s_input_never_its_command_line(self):
        ran = []

        def ssh(command, **kwargs):
            ran.append((command, kwargs["input"]))
            return subprocess.CompletedProcess(command, 255 if command[-2] == "down" else 0, "", "no route")
        with mock.patch.object(emulator_stats.subprocess, "run", side_effect=ssh):
            self.assertEqual(emulator_stats.push(["up", "down"]), ["down: ssh ended 255: no route"])
        self.assertEqual([command[-2] for command, _ in ran], ["up", "down"])
        for command, given in ran:
            self.assertEqual(given, self.env_file.read_text(encoding="utf-8"))
            self.assertNotIn("made-up", " ".join(command))
            self.assertIn("umask 077", command[-1])
            self.assertIn('"$HOME/.config/firmware-liberator/emulator.env"', command[-1])

    def test_push_names_its_machines_from_the_environment_or_env_infra(self):
        infra = self.folder / ".env.infra"
        infra.write_text("EMULATOR_STATS_HOSTS=one two\n", encoding="utf-8")
        with mock.patch.object(emulator_stats, "INFRA_FILE", infra), mock.patch.dict("os.environ", {"EMULATOR_STATS_HOSTS": ""}):
            self.assertEqual(emulator_stats.hosts(), ["one", "two"])
            with mock.patch.dict("os.environ", {"EMULATOR_STATS_HOSTS": "three"}):
                self.assertEqual(emulator_stats.hosts(), ["three"])

    def test_the_environment_wins_over_the_file(self):
        with mock.patch.dict("os.environ", {"EMULATOR_STATS_CLIENT_ID": "other.access"}):
            self.assertEqual(emulator_stats.client(), ("other.access", "made-up"))


class EveryFamilyTests(unittest.TestCase):
    """Every chip family's suites send their runs alike: each fault suite, a
    family's to come too, the attended suite for each family it runs, and the
    final-firmware check on each family's way of running it."""

    def test_every_fault_suite_says_its_batch_records_each_run_and_sends_as_its_batch_ends(self):
        suites = sorted(ROOT.glob("tools/*/emulator/fault_suite.py"))
        self.assertGreaterEqual(len(suites), 2)
        for suite in suites:
            source = suite.read_text(encoding="utf-8")
            for call in ("emulator_stats.began(OUT,", "emulator_stats.fault_run(OUT,", "rerun=sc[\"id\"] in ",
                         "print(emulator_stats.send(OUT), flush=True)"):
                self.assertIn(call, source, f"{suite}: {call}")

    def test_every_familys_final_firmware_check_records_each_conversion(self):
        from tools.repo import emulator_check
        where = {"espressif": ["tools/repo/emulator_check.py", "tools/espressif/emulator/public_devices.py"],
                 "beken": ["tools/beken/emulator/fault_suite.py"]}
        self.assertEqual(set(where), set(emulator_check.EMULATORS), "a new family's check records its runs too")
        for files in where.values():
            for name in files:
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("emulator_stats.check_run(", source, name)
                self.assertIn("emulator_stats.began(", source, name)
                self.assertIn("emulator_stats.send(", source, name)

    def test_the_attended_suite_records_each_family_it_runs_as_that_familys_device(self):
        from tools.espressif.emulator import attended_suite
        source = (ROOT / "tools/espressif/emulator/attended_suite.py").read_text(encoding="utf-8")
        self.assertIn("emulator_stats.attended_run(", source)
        self.assertIn("emulator_stats.began(RESULTS,", source)
        self.assertIn("print(emulator_stats.send(RESULTS), flush=True)", source)
        for family in attended_suite.SCENARIOS:
            self.assertEqual(attended_suite.device_of(family, None).family, family)


if __name__ == "__main__":
    unittest.main()
