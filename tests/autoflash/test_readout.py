"""A readout, what every chip family's shares: what it sends (the files a
read gives, and what the device said it is), where to and when not, which way
a device is read, and a run ending on it without a conversion."""
import contextlib
import hashlib
import http.server
import io
import lzma
import json
import os
import tarfile
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import account, errors, hostsys, openbeken, output, records, prompts, readout, readout_beken
from tools.autoflash import readout_espressif, report, report_data, runner, runs
from tools.autoflash.state import RunState
from tools.espressif import chips
from tools.ewelink import firmware_name, offered_firmware

# The website asked, where start_run says it wants every readout.
ASK_WANTED = readout.ask_wanted
MANIFEST = {"format": 1, "model": "NEWTHING", "stock_model": "SN-ESP32C3-NEW-01", "stock_version": "1.0.0"}
DEVICE = {"deviceid": "1000abcdef", "name": "Hall light", "productModel": "NEWTHING",
          "extra": {"model": "SN-ESP32C3-NEW-01", "uiid": 77}, "params": {"fwVersion": "1.0.0"}}


def ended(ctx) -> list[tuple[str, str, str]]:
    """How each device's readout in the sitting ended, as its report gives it:
    (name, outcome, detail)."""
    return [(item.name, item.outcome, item.detail) for item in readout.readouts(ctx)]


HALL, PORCH = "'Hall light' (NEWTHING)", "'Porch' (NEWTHING)"


def record_stages(test: unittest.TestCase) -> list[str]:
    """The readout's stage lines as `test` draws them: "N Name" as each
    begins and its outcome as it ends, as output draws them - an end with no
    stage under way draws nothing. Numbered from 1, as a readout on its own."""
    stages: list[str] = []
    under_way = []

    def begin(number, name):
        under_way.append(True)
        stages.append(f"{number} {name}")

    def end(outcome):
        if under_way:
            under_way.clear()
            stages.append(outcome)
    for patch in (mock.patch.object(output, "stage_begin", begin), mock.patch.object(output, "stage_end", end),
                  mock.patch.object(readout, "_first_stage", 1)):
        patch.start()
        test.addCleanup(patch.stop)
    return stages


def record(product: str, model: str) -> dict:
    """The account's record of a device eWeLink knows as `product`, its firmware as `model`."""
    return {**DEVICE, "productModel": product, "extra": {**DEVICE["extra"], "model": model}}


def members(data: bytes) -> list[str]:
    """The readout's files as they are stored, in order."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        return [member.name for member in archive.getmembers()]


def unpacked(data: bytes) -> dict[str, bytes]:
    """The readout's files, each .xz one decompressed, by its name without .xz."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive.getmembers()}
    return {name.removesuffix(".xz"): lzma.decompress(body) if name.endswith(".xz") else body
            for name, body in files.items()}


def a_ctx(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(state=RunState.open(Path(tmp)), dir=Path(tmp), listen_ip=lambda: "192.0.2.141",
                           email=lambda: "me@example.com", ewelink_env=lambda: {}, helper_env=lambda: {},
                           ewelink_region=lambda: "eu")


# The anonymised files a read gives offer(): the scrubbing itself is in the
# chip family's run path and tested in tests/espressif/test_scrub.py.
FILES = {"flash.bin": b"flash bytes", "efuse.bin": b"\0" * 124, "info.txt": b"bridge 4.4.5\n",
         "report.json": b"{}"}


class StoppedByCtrlCTests(unittest.TestCase):
    """What a readout stopped by Ctrl-C ends by saying, by where it was: a Beken
    device's reader stands in place of Sonoff's firmware, so switching it off
    and on would only start the reader again."""

    def said(self, reached):
        state = SimpleNamespace(input=lambda key: reached if key == readout.STATE_KEY else None)
        return readout.stopped_by_ctrl_c(SimpleNamespace(state=state))

    def test_a_beken_device_on_its_reader_is_put_back_by_the_readout_run_again(self):
        said = self.said(readout.REPLACED)
        self.assertIn("Run the readout again", said)
        self.assertNotIn("switch it off and on", said)

    def test_anywhere_else_switching_it_off_and_on_starts_sonoff_again(self):
        for reached in (None, "started", "sent", "read"):
            with self.subTest(reached):
                self.assertIn("switch it off and on to go back to Sonoff", self.said(reached))


class PackageTests(unittest.TestCase):
    def test_the_prepared_files_go_in_as_they_are(self):
        files = unpacked(readout.package(MANIFEST, FILES))
        self.assertEqual(set(files), {"manifest.json", "flash.bin", "efuse.bin", "info.txt", "report.json"})
        self.assertEqual(files["flash.bin"], b"flash bytes")

    def test_the_manifest_comes_first_as_it_is_the_device_s_firmware_in_xz_and_the_offered_as_it_is(self):
        # Uncompressed, the website can check an offered file against its SHA-256.
        offered = [offered_firmware.Offered("user1.bin", "1.1.0", b"\xe9offered app")]
        archive = readout.package(MANIFEST, FILES, offered)
        names = members(archive)
        self.assertEqual(names[0], "manifest.json")
        self.assertIn("flash.bin.xz", names)
        self.assertIn("offered/user1.bin", names)
        with tarfile.open(fileobj=io.BytesIO(archive)) as opened:
            data = opened.extractfile("offered/user1.bin").read()
        self.assertEqual(hashlib.sha256(data).hexdigest(), offered[0].sha256)
        self.assertIn("report.json", names)

    def test_no_contact_goes_in_it(self):
        self.assertNotIn("contact", json.loads(unpacked(readout.package(MANIFEST, None))["manifest.json"]))

    def test_not_read_only_what_it_said_goes(self):
        self.assertEqual(set(unpacked(readout.package(MANIFEST, None))), {"manifest.json"})

    def test_the_firmware_ewelink_offers_goes_too_as_it_is(self):
        offered = [offered_firmware.Offered("user1.bin", "1.1.0", b"\xe9offered app")]
        files = unpacked(readout.package(MANIFEST, FILES, offered))
        self.assertEqual(files["offered/user1.bin"], b"\xe9offered app")
        self.assertIn("flash.bin", files)
        self.assertEqual(unpacked(readout.package(MANIFEST, None, offered))["offered/user1.bin"], b"\xe9offered app")


class DeviceAtTests(unittest.TestCase):
    """--device-ip picks the account device at that address: by its hardware
    address, whatever its family; by discovery, for a record that gives none;
    or the one device there."""
    S61 = {"deviceid": "1000abcdef", "params": {"staMac": "12:34:56:AB:CD:EF"}}
    OTHER = {"deviceid": "2000bbbbbb", "params": {"staMac": "aa:bb:cc:dd:ee:ff"}}

    def device_at(self, address, devices, answering=None, mac=None):
        with mock.patch.object(readout.discover_ewelink, "discover", return_value={"packets": []}), \
                mock.patch.object(readout, "answering_ids", return_value=answering or {}), \
                mock.patch.object(readout.hostsys, "mac_of", return_value=mac):
            return readout.device_at(address, devices)

    def test_by_its_hardware_address_whatever_answers_discovery(self):
        self.assertIs(self.device_at("192.0.2.60", [self.OTHER, self.S61], mac="12:34:56:ab:cd:ef",
                                     answering={"2000bbbbbb": "192.0.2.60"}), self.S61)

    def test_by_discovery_where_no_hardware_address_tells(self):
        self.assertIs(self.device_at("192.0.2.60", [self.OTHER, self.S61],
                                     answering={"2000bbbbbb": "192.0.2.60"}), self.OTHER)

    def test_the_one_device_in_the_account_when_nothing_else_tells(self):
        self.assertIs(self.device_at("192.0.2.60", [self.S61]), self.S61)

    def test_it_stops_when_it_cannot_tell_among_several(self):
        with self.assertRaisesRegex(readout.Abort, "could not tell which device"):
            self.device_at("192.0.2.60", [self.OTHER, self.S61])


class WhereTests(unittest.TestCase):
    def test_the_website_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(prompts, "UNATTENDED", False):
            self.assertEqual(readout.upload_url(), readout.UPLOAD_URL)

    def test_never_from_the_emulator_or_unattended(self):
        # Either chip family's: the Beken one's computer has only its device's power switch.
        for name, value in (("AUTOFLASH_EMULATOR_URL", "http://127.0.0.1:1"),
                            ("AUTOFLASH_EMULATOR_POWER", "/device/power")):
            with mock.patch.dict(os.environ, {name: value}, clear=True):
                self.assertIsNone(readout.upload_url(), name)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(prompts, "UNATTENDED", True):
            self.assertIsNone(readout.upload_url())

    def test_a_test_server_when_given(self):
        with mock.patch.dict(os.environ, {"AUTOFLASH_READOUT_URL": "http://127.0.0.1:9/readout",
                                          "AUTOFLASH_EMULATOR_URL": "x"}):
            self.assertEqual(readout.upload_url(), "http://127.0.0.1:9/readout")


class ProofTests(unittest.TestCase):
    def test_it_is_the_work_the_worker_checks(self):
        import hashlib
        data = b"\x1f\x8breadout"
        stamp, nonce = readout.proof(data, now=1794000000, bits=12).split(":")
        digest = hashlib.sha256(f"{stamp}:{hashlib.sha256(data).hexdigest()}:{nonce}".encode()).digest()
        self.assertEqual(stamp, "1794000000")
        self.assertLess(int.from_bytes(digest, "big"), 1 << (256 - 12))


class SendTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(readout, "PROOF_BITS", 8)  # the real work takes seconds
        patch.start()
        self.addCleanup(patch.stop)

    def test_posted_as_a_tar_and_its_reference_returned(self):
        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's name
                got["type"] = self.headers["Content-Type"]
                got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.dumps({"id": "r-2026-0001"}).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            reference = readout.send(b"\x1f\x8bdata", f"http://127.0.0.1:{server.server_address[1]}/readout")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((reference, got["type"], got["body"]), ("r-2026-0001", "application/x-tar", b"\x1f\x8bdata"))


class StillWantedTests(unittest.TestCase):
    """Asked before a readout is made: whether the website still wants one."""

    def ask(self, status=200, answer=None, url="set"):
        got = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's name
                got["path"], got["type"] = self.path, self.headers["Content-Type"]
                got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
                got["proof"] = self.headers["X-Proof"]
                body = json.dumps(answer if answer is not None else {"wanted": False}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        given = f"http://127.0.0.1:{server.server_address[1]}/readout" if url == "set" else url
        try:
            with mock.patch.object(readout, "upload_url", return_value=given), mock.patch.object(output, "log_only"):
                wanted = readout.ask_wanted("BASICR4", "SN-BASICR4-01", "1.1.0", "whole", ["a" * 64, "b" * 64],
                                              report_data.device_hash("12:34:56:ab:cd:ef"))
        finally:
            server.shutdown()
            server.server_close()
        return wanted, got

    def test_it_asks_beside_where_readouts_go_with_lighter_work_of_its_own(self):
        import hashlib
        wanted, got = self.ask(answer={"wanted": False, "why": "held", "held": ["a" * 64, "c" * 64]})
        self.assertEqual(wanted, readout.Wanted(False, "held", frozenset({"a" * 64})))   # c was not asked of
        self.assertEqual((got["path"], got["type"]), ("/wanted", "application/json"))
        asked = json.loads(got["body"])
        # A random field of its own, so two alike questions never share a proof.
        self.assertRegex(asked.pop("nonce"), r"^[0-9a-f]{16}$")
        self.assertEqual(asked, {"format": 1, "model": "BASICR4", "stock_model": "SN-BASICR4-01",
                                 "stock_version": "1.1.0", "stage": "whole", "firmware": ["a" * 64, "b" * 64],
                                 "device_hash": report_data.device_hash("12:34:56:ab:cd:ef")})
        stamp, nonce = got["proof"].split(":")
        work = hashlib.sha256(f"{stamp}:{hashlib.sha256(got['body']).hexdigest()}:{nonce}".encode()).digest()
        self.assertLess(int.from_bytes(work, "big"), 1 << (256 - readout.LIGHT_PROOF_BITS))
        self.assertLess(readout.LIGHT_PROOF_BITS, readout.PROOF_BITS)

    def test_wanted_when_the_website_says_so_or_cannot_say(self):
        self.assertTrue(self.ask(answer={"wanted": True})[0].wanted)
        self.assertTrue(self.ask(status=404, answer={"error": "an earlier website"})[0].wanted)
        self.assertTrue(self.ask(status=429, answer={"error": "too many"})[0].wanted)
        self.assertTrue(self.ask(url="http://127.0.0.1:9/readout")[0].wanted)   # nothing there

    def test_never_asked_from_a_test_run(self):
        wanted, got = self.ask(url=None)
        self.assertTrue(wanted.wanted)
        self.assertEqual(got, {})


class RateLimitTests(unittest.TestCase):
    """The website takes two readouts a minute from one address: a readout of
    every device reaches that, as one in the emulator did (a 429, kept unsent)."""

    def post(self, answers):
        got = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's name
                self.rfile.read(int(self.headers["Content-Length"]))
                got.append(self.headers["X-Proof"])
                status, body = answers[len(got) - 1]
                body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(readout, "PROOF_BITS", 8), mock.patch.object(output, "say"), \
                    mock.patch.object(readout.time, "sleep") as slept:
                try:
                    said = readout.send(b"\x1f\x8bdata", f"http://127.0.0.1:{server.server_address[1]}/readout")
                except OSError as exc:
                    said = exc
        finally:
            server.shutdown()
            server.server_close()
        return said, got, [call.args[0] for call in slept.call_args_list]

    def test_too_many_at_once_is_sent_again_shortly_with_the_same_work(self):
        said, proofs, waits = self.post([(429, {"error": "too many"}), (201, {"id": "r-1"})])
        self.assertEqual(said, "r-1")
        self.assertEqual(len(set(proofs)), 1)
        self.assertEqual(waits, [15.0])

    def test_it_gives_up_after_a_minute_and_a_half_of_tries(self):
        said, proofs, waits = self.post([(429, {"error": "too many"})] * 7)
        self.assertIsInstance(said, OSError)
        self.assertIn("429", str(said))
        self.assertEqual((len(proofs), waits), (7, [15.0] * 6))

    def test_anything_else_turned_away_is_not_sent_again(self):
        said, proofs, waits = self.post([(503, {"error": "enough for today"})])
        self.assertIsInstance(said, OSError)
        self.assertEqual((len(proofs), waits), (1, []))


class LocateTests(unittest.TestCase):
    """A device's address with none given: by the hardware address its record
    gives, whatever its family - by discovery only where it gives none."""
    BEKEN = {"deviceid": "1000abcdef", "productModel": "NEWBK", "extra": {"model": "SN-BK7238-NEW-01"},
             "params": {"staMac": "12:34:56:AB:CD:EF"}}

    def setUp(self):
        for patch in (mock.patch.object(output, "say"), mock.patch.object(output, "tell"),
                      mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"),
                      mock.patch.object(hostsys, "lan_networks", return_value={"192.0.2.10": 24})):
            patch.start()
            self.addCleanup(patch.stop)
        discover = mock.patch.object(readout.discover_ewelink, "discover", return_value={"packets": []})
        self.discover = discover.start()
        self.addCleanup(discover.stop)
        self.clock = 0.0

        def sleep(seconds):
            self.clock += seconds
        for patch in (mock.patch.object(readout.time, "monotonic", side_effect=lambda: self.clock),
                      mock.patch.object(readout.time, "sleep", side_effect=sleep)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_beken_device_missed_by_one_sweep_is_looked_for_again_before_asking(self):
        # Seen: an emulated S61 beside another emulator, missed by one sweep,
        # its address then asked for, which an unattended run cannot answer.
        with mock.patch.object(hostsys, "find_by_mac", side_effect=[None, None, "192.0.2.77"]) as found, \
                mock.patch.object(prompts, "ask_ipv4") as asked:
            self.assertEqual(readout.locate(None, self.BEKEN), "192.0.2.77")
        self.assertEqual(found.call_count, 3)
        self.assertEqual(self.clock, 2 * readout.LOOK_AGAIN)
        asked.assert_not_called()

    def test_it_asks_once_it_has_looked_for_long_enough(self):
        with mock.patch.object(hostsys, "find_by_mac", return_value=None) as found, \
                mock.patch.object(prompts, "ask_ipv4", return_value="192.0.2.77") as asked:
            self.assertEqual(readout.locate(None, self.BEKEN), "192.0.2.77")
        self.assertEqual(found.call_count, int(readout.LOOK_FOR / readout.LOOK_AGAIN) + 1)
        asked.assert_called_once()

    def test_a_beken_device_is_found_by_its_hardware_address_unasked(self):
        with mock.patch.object(hostsys, "find_by_mac", return_value="192.0.2.77") as found, \
                mock.patch.object(prompts, "ask_ipv4") as asked:
            self.assertEqual(readout.locate(None, self.BEKEN), "192.0.2.77")
        self.assertEqual(found.call_args.args, ("12:34:56:ab:cd:ef", ["192.0.2.10"]))
        asked.assert_not_called()
        self.discover.assert_not_called()

    def test_a_beken_address_typed_is_checked_by_its_hardware_address_not_discovery(self):
        with mock.patch.object(hostsys, "find_by_mac", return_value=None), \
                mock.patch.object(prompts, "ask_ipv4", return_value="192.0.2.77") as asked:
            self.assertEqual(readout.locate(None, self.BEKEN), "192.0.2.77")
        self.assertNotIn("LAN Control", " ".join(asked.call_args.kwargs["help_lines"]))
        check = asked.call_args.kwargs["check"]
        with mock.patch.object(hostsys, "mac_of", return_value="12:34:56:ab:cd:ef"):
            self.assertEqual(check("192.0.2.77"), [])
        with mock.patch.object(hostsys, "mac_of", return_value="aa:bb:cc:dd:ee:ff"):
            self.assertIn("another device", check("192.0.2.1")[0])
        self.discover.assert_not_called()

    def test_a_beken_device_is_known_by_its_product_model_alone(self):
        # An account record with no firmware model in it: the product says the chip family.
        from tools.autoflash import kinds
        self.assertTrue(readout.is_beken({"productModel": kinds.SONOFF_S61.example_product_model}))
        self.assertFalse(readout.is_beken(DEVICE))

    def test_an_espressif_device_is_found_by_its_hardware_address_with_no_lan_control(self):
        device = {**DEVICE, "params": {"staMac": "12:34:56:AB:CD:EF"}}
        with mock.patch.object(hostsys, "find_by_mac", return_value="192.0.2.77") as found, \
                mock.patch.object(prompts, "ask_ipv4") as asked:
            self.assertEqual(readout.locate(None, device), "192.0.2.77")
        self.assertEqual(found.call_args.args[0], "12:34:56:ab:cd:ef")
        asked.assert_not_called()
        self.discover.assert_not_called()

    def test_one_whose_record_gives_no_hardware_address_is_found_by_discovery(self):
        self.discover.return_value = {"packets": ["x"]}
        with mock.patch.object(readout, "answering_ids", return_value={"1000abcdef": "192.0.2.88"}), \
                mock.patch.object(hostsys, "find_by_mac") as by_mac:
            self.assertEqual(readout.locate(None, DEVICE), "192.0.2.88")
        by_mac.assert_not_called()


def start_run(test: unittest.TestCase) -> None:
    """`test` set up for readout.run on DEVICE, at 192.0.2.77, asking eWeLink
    nothing: its context (test.ctx), and the filing of its run (test.filed)."""
    for name in ("say", "tell", "log_only", "transient_begin", "transient_end", "progress", "progress_clear"):
        patch = mock.patch.object(output, name)
        patch.start()
        test.addCleanup(patch.stop)
    for patch in (mock.patch.object(prompts, "section"), mock.patch.object(prompts, "warning_box")):
        patch.start()
        test.addCleanup(patch.stop)
    test.stages = record_stages(test)
    test.tmp = tempfile.TemporaryDirectory()
    test.addCleanup(test.tmp.cleanup)
    test.ctx = a_ctx(test.tmp.name)
    asked = account.Asked("1.0.0", "SN-NEW-01", "NEWTHING", [], {}, "1000abcdef")
    filed = mock.patch.object(runs, "file_device")
    test.filed = filed.start()
    test.addCleanup(filed.stop)
    for patch in (mock.patch.object(readout, "locate", return_value="192.0.2.77"),
                  # A first stage's look for its MAC alone: found nowhere, unless a test says.
                  mock.patch.object(readout, "answering_at", return_value=None),
                  # On Sonoff's firmware, not a readout's reading program left on it.
                  mock.patch.object(readout_espressif.installer, "bridge_answers", return_value=False),
                  mock.patch.object(openbeken, "is_readout", return_value=False),
                  mock.patch.object(account, "already_converting", return_value=None),
                  mock.patch.object(hostsys, "mac_of", return_value="12:34:56:ab:cd:ef"),
                  mock.patch.object(account, "ask_account", return_value=asked),
                  mock.patch.object(records, "remember_device"),
                  # Wanted, as the website would say: never asked of it from a test.
                  mock.patch.object(readout, "ask_wanted", return_value=readout.Wanted(True)),
                  mock.patch.object(readout, "notify", return_value=True),
                  mock.patch.object(offered_firmware, "fetch",
                                    return_value=([offered_firmware.Offered("user1.bin", "1.1.0", b"\xe9")], []))):
        patch.start()
        test.addCleanup(patch.stop)


def s60_asked() -> account.Asked:
    """What an S60 on Sonoff's firmware says, through eWeLink: a kind read in full."""
    from tools.autoflash import kinds
    return account.Asked("1.2.0", kinds.SONOFF_S60.stock_model, "S60TPG", [], {}, "1000abcdef")


S60_ASKED = s60_asked()


def learned(name: str, chip: str):
    """eWeLink's firmware name for the device learned as `name`, on `chip`."""
    return mock.patch.object(firmware_name, "learn", return_value=(firmware_name.Learned(name, chip, "eWeLink"), []))


class RunTests(unittest.TestCase):
    def setUp(self):
        start_run(self)

    def test_a_chip_with_no_bridge_is_sent_nothing_and_what_it_said_is_offered(self):
        with learned("FWSW-01-NEW-BK7238", "BK7238"), \
                mock.patch.object(hostsys, "run_tool") as sent, \
                mock.patch.object(readout, "offer", return_value="done") as offered:
            self.assertEqual(readout.run(self.ctx, DEVICE), "done")
        sent.assert_not_called()
        manifest, read, updates = offered.call_args.args[1:]
        self.assertEqual((manifest["firmware_name"], manifest["read"], read), ("FWSW-01-NEW-BK7238", False, None))
        self.assertEqual([item.name for item in updates], ["user1.bin"])
        self.assertEqual(manifest["offered"][0]["version"], "1.1.0")
        self.assertNotIn("1000abcdef", json.dumps(manifest))

    def test_a_device_on_another_firmware_has_its_first_stage_read_out_and_nothing_sent_to_it(self):
        # Still in the eWeLink account, converted - by this tool or by hand: its
        # record and the firmware eWeLink offers it are still worth having.
        from tools.autoflash import kinds
        for product, kind, chip, firmware in (("S60TPG", kinds.SONOFF_S60, "ESP32C3", "esphome"),
                                              ("S61STPF", kinds.SONOFF_S61, "BK7238", "openbeken")):
            converted = account.NotOnSonoff(firmware, f"192.0.2.77 is running {firmware}, not Sonoff's firmware")
            # As eWeLink knows it: a kind read in full on Sonoff's firmware.
            asked = account.Asked("1.2.0", kind.stock_model, product, [], {}, "1000abcdef")
            for batch in (None, readout.Batch()):
                with self.subTest(kind.key, batch=batch), learned(kind.wrapper_model, chip), \
                        mock.patch.object(account, "ask_account", return_value=asked) as ask, \
                        mock.patch.object(account, "already_converting", side_effect=converted) as checked, \
                        mock.patch.object(runs, "hold_device"), mock.patch.object(readout, "ask_wanted",
                                                                                  return_value=readout.Wanted(True)) as wanted, \
                        mock.patch.object(readout_espressif, "read_and_anonymise") as espressif, \
                        mock.patch.object(readout_beken, "read_and_anonymise") as beken, \
                        mock.patch.object(readout, "offer", return_value="") as offered:
                    self.assertEqual(readout.run(self.ctx, record(product, kind.stock_model), batch=batch), "")
                checked.assert_called_once_with("192.0.2.77")
                espressif.assert_not_called()
                beken.assert_not_called()
                self.assertEqual(ask.call_args.args[1:], ("1000abcdef",))
                self.assertEqual(wanted.call_args.args[3], "first")
                manifest, files = offered.call_args.args[1:3]
                self.assertEqual((manifest["read"], manifest["runs"], files), (False, firmware, None))
                self.assertEqual([item.name for item in offered.call_args.args[3]], ["user1.bin"])

    def test_each_wait_is_a_stage_of_its_own(self):
        # Seen in attended emulator runs: with no stage line, the window stayed
        # blank while the readout looked for the device and asked it what it was.
        with learned("FWSW-01-NEW-BK7238", "BK7238"), mock.patch.object(readout, "offer", return_value="done"):
            readout.run(self.ctx, DEVICE)
        self.assertEqual(self.stages, [
            "2 Locating the device on your network", "complete",   # for its MAC: eWeLink's record has none
            "3 Probing the device", "complete",
            "4 Fetching the eWeLink firmware", "complete",
            "5 Reading the device's memory", "skipped",           # no reading program for its chip
            "6 Restoring Sonoff's firmware", "skipped",
            "7 Saving the readout", "complete"])

    def test_a_first_stage_device_eWeLink_gives_no_mac_for_is_looked_for_never_asked_for(self):
        # Its report names it by its MAC, so a readout and a later conversion
        # of the device are known as one.
        with learned("FWSW-01-NEW-BK7238", "BK7238"), mock.patch.object(readout, "offer", return_value="done"), \
                mock.patch.object(readout, "answering_at", return_value="192.0.2.88") as looked, \
                mock.patch.object(prompts, "ask_ipv4") as asked:
            readout.run(self.ctx, DEVICE)
        looked.assert_called_once_with(DEVICE)
        asked.assert_not_called()
        self.assertEqual(hostsys.mac_of.call_args.args, ("192.0.2.88",))
        self.assertEqual(readout.readouts(self.ctx)[-1].mac, "12:34:56:ab:cd:ef")
        self.assertIsNone(self.ctx.state.input("device_ip"), "its address is not the readout's")

    def test_one_found_nowhere_is_read_out_all_the_same_with_no_mac(self):
        with learned("FWSW-01-NEW-BK7238", "BK7238"), mock.patch.object(readout, "offer", return_value="done"), \
                mock.patch.object(prompts, "ask_ipv4") as asked:
            self.assertEqual(readout.run(self.ctx, DEVICE), "done")
        asked.assert_not_called()
        self.assertIsNone(readout.readouts(self.ctx)[-1].mac)

    def test_one_whose_mac_eWeLink_gives_is_not_looked_for(self):
        given = {**DEVICE, "params": {**DEVICE["params"], "staMac": "12:34:56:AB:CD:01"}}
        with learned("FWSW-01-NEW-BK7238", "BK7238"), \
                mock.patch.object(readout, "offer", return_value="done"), \
                mock.patch.object(readout, "answering_at") as looked:
            readout.run(self.ctx, given)
            looked.assert_not_called()
            self.assertEqual(self.stages[:2], ["2 Locating the device on your network", "skipped"])
        self.assertEqual(readout.readouts(self.ctx)[0].mac, "12:34:56:ab:cd:01")

    def test_one_read_in_full_restores_its_firmware_in_a_stage_of_its_own(self):
        from tools.autoflash import kinds
        s60 = record("S60TPG", kinds.SONOFF_S60.stock_model)

        def read(*_args, **_kwargs):
            readout.restoring()   # as each chip's readout says, between the two
            return {}, True
        with mock.patch.object(firmware_name, "learn", return_value=(None, ["no offer"])), \
                mock.patch.object(account, "ask_account", return_value=S60_ASKED), \
                mock.patch.object(runs, "hold_device"), \
                mock.patch.object(readout_espressif, "read_and_anonymise", side_effect=read), \
                mock.patch.object(readout, "offer", return_value="done"):
            readout.run(self.ctx, s60)
        self.assertEqual(self.stages[6:], ["5 Reading the device's memory", "complete",
                                           "6 Restoring Sonoff's firmware", "complete",
                                           "7 Saving the readout", "complete"])
        self.assertEqual(self.stages[:2], ["2 Locating the device on your network", "complete"])

    def test_the_stage_under_way_stops_with_the_readout(self):
        from tools.autoflash import kinds
        s60 = record("S60TPG", kinds.SONOFF_S60.stock_model)
        with mock.patch.object(firmware_name, "learn", return_value=(None, ["no offer"])), \
                mock.patch.object(account, "ask_account", return_value=S60_ASKED), \
                mock.patch.object(runs, "hold_device"):
            for batch, raised, ended in ((None, errors.NothingWritten("not confirmed"), "stopped"),
                                         (readout.Batch(), errors.NothingWritten("skipped as asked"), "skipped"),
                                         (None, errors.Abort("the read did not come through"), "stopped")):
                with self.subTest(raised=raised), mock.patch.object(readout_espressif, "read_and_anonymise",
                                                                    side_effect=raised):
                    self.stages.clear()
                    with self.assertRaises(type(raised)):
                        readout.run(self.ctx, s60, batch=batch)
                    self.assertEqual(self.stages[-2:], ["5 Reading the device's memory", ended])

    def test_a_run_s_readout_ends_the_run_s_stage_and_carries_on_from_it(self):
        # Seen in an attended emulator run: the run's "Discovering the device"
        # stayed under way through the whole readout it turned into.
        output.stage_begin(2, "Discovering the device")
        width = mock.patch.object(output, "set_stage_width")
        laid_out = width.start()
        self.addCleanup(width.stop)
        with mock.patch.object(readout, "read_out_chosen",
                               side_effect=lambda *_: readout.begin("locate") or "read"), \
                mock.patch.object(readout, "end") as ended_with, self.assertRaises(errors.Finished) as finished:
            readout.from_run(self.ctx, [DEVICE])
        self.assertEqual(self.stages, ["2 Discovering the device", "complete",
                                       "3 Locating the device on your network"])
        # It ends as a readout on its own does, its report shown: the run says no more.
        self.assertEqual(ended_with.call_args.args[1], "read")
        self.assertEqual(str(finished.exception), "")
        self.assertEqual(laid_out.call_args.args[0], max(len(name) for _, name in readout.STAGES))

    def test_each_readout_sends_one_report_however_it_ends(self):
        # One a readout, sent as it ends, so a sitting of several sends one
        # each, all of one kind.
        from tools.autoflash import kinds, report_upload
        s60 = record("S60TPG", kinds.SONOFF_S60.stock_model)
        # (raised, batch, its outcome, whether its error is classed)
        cases = ((None, None, readout.ACCEPTED, False),
                 (errors.Abort("the device stopped answering"), None, readout.FAILED, True),
                 (prompts.QuitRun("q"), None, readout.CANCELLED, False),
                 (errors.NothingWritten("skipped as asked"), readout.Batch(), readout.SKIPPED, False),
                 (RuntimeError("a bug"), None, readout.CRASHED, False))
        for raised, batch, outcome, classed in cases:
            sent = []
            with self.subTest(outcome=outcome), \
                    mock.patch.object(report_upload, "begin_readout", side_effect=lambda ctx: sent.append("begun")), \
                    mock.patch.object(report_upload, "send_readout",
                                      side_effect=lambda ctx, became, seconds: sent.append(
                                          (became.outcome, became.error, became.mac))), \
                    mock.patch.object(firmware_name, "learn", return_value=(None, ["no offer"])), \
                    mock.patch.object(account, "ask_account", return_value=S60_ASKED), \
                    mock.patch.object(runs, "hold_device"), \
                    mock.patch.object(readout_espressif, "read_and_anonymise",
                                      side_effect=raised, return_value=({}, True)), \
                    mock.patch.object(readout, "send", return_value="r-1"), \
                    mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"):
                try:
                    readout.run(self.ctx, s60, batch=batch)
                except BaseException:   # noqa: BLE001 - each as the readout ends it
                    pass
                classified = readout.failures.classify("the device stopped answering") if classed else None
                self.assertEqual(sent, ["begun", (outcome, classified, "12:34:56:ab:cd:ef")])

    def test_a_device_on_another_firmware_passes_its_reads_by(self):
        from tools.autoflash import kinds
        converted = account.NotOnSonoff("esphome", "192.0.2.77 is running ESPHome, not Sonoff's firmware")
        with learned(kinds.SONOFF_S60.wrapper_model, "ESP32C3"), \
                mock.patch.object(account, "ask_account", return_value=S60_ASKED), \
                mock.patch.object(account, "already_converting", side_effect=converted), \
                mock.patch.object(readout_espressif, "read_and_anonymise", return_value=({}, True)), \
                mock.patch.object(runs, "hold_device"), mock.patch.object(readout, "offer", return_value=""):
            readout.run(self.ctx, record("S60TPG", kinds.SONOFF_S60.stock_model))
        self.assertEqual(self.stages, [
            "2 Locating the device on your network", "complete", "3 Probing the device", "complete",
            "4 Fetching the eWeLink firmware", "complete",
            "5 Reading the device's memory", "skipped", "6 Restoring Sonoff's firmware", "skipped",
            "7 Saving the readout", "complete"])

    def test_a_kind_the_tool_knows_takes_its_own_name_when_ewelink_gives_none(self):
        from tools.autoflash import kinds
        s60 = account.Asked("1.2.0", kinds.SONOFF_S60.stock_model, "S60TPG", [], {}, "1000abcdef")
        with mock.patch.object(account, "ask_account", return_value=s60), \
                mock.patch.object(firmware_name, "learn", return_value=(None, ["no offer"])), \
                mock.patch.object(readout_espressif, "read_out", side_effect=errors.NothingWritten("stop")) as read:
            with self.assertRaises(errors.NothingWritten):
                readout.run(self.ctx, record("S60TPG", kinds.SONOFF_S60.stock_model))
        self.assertEqual(read.call_args.args[1:3], (chips.ESP32C3, kinds.SONOFF_S60.wrapper_model))
        self.assertTrue(read.call_args.kwargs["known"])

    def asked_as(self, product: str, model: str):
        asked = account.Asked("1.0.0", model, product, [], {}, "1000abcdef")
        return mock.patch.multiple(account, ask_account=mock.Mock(return_value=asked))

    def read(self, product: str, model: str, name: str, chip: str):
        """What a readout of this device sent it (the reading program, or None),
        and what it offered to send."""
        with self.asked_as(product, model), learned(name, chip), \
                mock.patch.object(readout_espressif, "read_and_anonymise", return_value=({}, True)) as espressif, \
                mock.patch.object(readout_beken, "read_and_anonymise", return_value=({}, True)) as beken, \
                mock.patch.object(readout, "offer", return_value="done") as offered:
            readout.run(self.ctx, record(product, model))
        sent = espressif if espressif.called else beken if beken.called else None
        return sent, offered.call_args.args[1]

    def test_a_device_the_tool_has_not_seen_is_sent_nothing(self):
        # Even with a chip a bridge runs on: it could be laid out so the bridge
        # leaves it unable to start, as the BASICR4 would have been.
        sent, manifest = self.read("NEWTHING", "SN-NEW-01", "FWSW-E32-NEW-ESP32C3", "ESP32C3")
        self.assertIsNone(sent)
        self.assertFalse(manifest["read"])

    def test_what_answers_at_a_devices_address_is_checked_before_ewelink_is_asked(self):
        # account.already_converting, as a conversion's A2 does - but not on a
        # reading program a readout stopped part way left there, whose readout
        # carries on: the bridge (which answers no discovery, as Sonoff's
        # firmware would), or a Beken device's readout OpenBeken.
        from tools.autoflash import kinds
        for product, kind, chip, left_on in (("BASICR4", kinds.SONOFF_BASICR4, "ESP32C3",
                                              (readout_espressif.installer, "bridge_answers")),
                                             ("S61STPF", kinds.SONOFF_S61, "BK7238", (openbeken, "is_readout"))):
            asked = account.Asked("1.0.0", kind.stock_model, product, [], {}, "1000abcdef")
            for reading in (False, True):
                with self.subTest(kind.key, reading=reading), learned(kind.wrapper_model, chip), \
                        mock.patch.object(*left_on, return_value=reading), \
                        mock.patch.object(account, "already_converting") as checked, \
                        mock.patch.object(account, "ask_account", return_value=asked) as ask, \
                        mock.patch.object(readout_espressif, "read_and_anonymise", return_value=({}, True)), \
                        mock.patch.object(readout_beken, "read_and_anonymise", return_value=({}, True)), \
                        mock.patch.object(readout, "offer", return_value="done"):
                    readout.run(self.ctx, record(product, kind.stock_model))
                self.assertEqual(checked.call_args_list, [] if reading else [mock.call("192.0.2.77")])
                self.assertEqual(ask.call_args.args[1:], ("1000abcdef",))

    def test_a_kind_wanting_a_readout_is_read_in_full(self):
        from tools.autoflash import kinds
        basicr4 = kinds.SONOFF_BASICR4
        self.assertIs(basicr4.status, kinds.Status.READOUT_WANTED)
        sent, _ = self.read("BASICR4", basicr4.stock_model, basicr4.wrapper_model, "ESP32C3")
        self.assertIsNotNone(sent)

    def test_a_listed_product_under_another_firmware_name_is_sent_nothing(self):
        # A variant sharing the product model - a Matter version, say - is not the kind.
        from tools.autoflash import kinds
        sent, _ = self.read("S60TPG", kinds.SONOFF_S60.stock_model, "FWSW-E32S60M-S60M-ESP32C3", "ESP32C3")
        self.assertIsNone(sent)

    def test_a_kind_not_supported_yet_is_sent_nothing(self):
        from tools.autoflash import kinds
        nspanel = kinds.SONOFF_NSPANEL
        self.assertIs(nspanel.status, kinds.Status.NOT_SUPPORTED)
        sent, _ = self.read("NSPANEL", nspanel.stock_model, nspanel.wrapper_model, "ESP32D0WD")
        self.assertIsNone(sent)

    def test_what_it_keeps_of_the_device_and_learns_its_name_from(self):
        from tools.autoflash import kinds
        basicr4 = kinds.SONOFF_BASICR4
        offers = {"offers": [{"version": "1.1.0", "binList": []}]}
        asked = account.Asked("1.0.0", basicr4.stock_model, "BASICR4", [], offers, "1000abcdef")
        with mock.patch.multiple(account, ask_account=mock.Mock(return_value=asked)), \
                learned(basicr4.wrapper_model, "ESP32C3"), \
                mock.patch.object(readout_espressif, "read_and_anonymise", return_value=({}, True)), \
                mock.patch.object(readout, "offer", return_value="done"):
            readout.run(self.ctx, record("BASICR4", basicr4.stock_model))
            self.assertEqual(firmware_name.learn.call_args.args[0], offers["offers"])
        self.assertEqual({key: self.ctx.state.input(key) for key in readout.DEVICE_KEYS[:5]}, {
            "device_ip": "192.0.2.77", "device_mac": "12:34:56:ab:cd:ef", "stock_version": "1.0.0",
            "product_model": "BASICR4", readout.STATE_KEY: "started"})
        records.remember_device.assert_called_once_with(
            "12:34:56:ab:cd:ef", ewelink_name="Hall light", ewelink_id="1000abcdef", model="BASICR4")

    def test_its_run_is_filed_under_the_device_as_a_conversions_is(self):
        device = {**DEVICE, "params": {**DEVICE["params"], "staMac": "12:34:56:AB:CD:EF"}}
        with learned("FWSW-01-NEW-BK7238", "BK7238"), mock.patch.object(readout, "offer", return_value="done"):
            readout.run(self.ctx, device)
        self.assertEqual(self.filed.call_args.args[1], "12:34:56:ab:cd:ef")
        self.assertEqual(self.ctx.state.input("device_mac"), "12:34:56:ab:cd:ef")


class NotWantedTests(unittest.TestCase):
    """A readout that would add nothing, or one the day has no room for:
    nothing more is done - but the owner may still ask to be told."""

    def setUp(self):
        start_run(self)
        readout.ask_wanted.return_value = readout.Wanted(False, "held")
        for patch in (mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"),
                      mock.patch.object(prompts, "UNATTENDED", False)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_basicr4(self, **extra):
        from tools.autoflash import kinds
        basicr4 = kinds.SONOFF_BASICR4
        asked = account.Asked("1.1.0", basicr4.stock_model, "BASICR4", [], {}, "1000abcdef")
        with learned(basicr4.wrapper_model, "ESP32C3"), \
                mock.patch.multiple(account, ask_account=mock.Mock(return_value=asked)), \
                mock.patch.object(readout_espressif, "read_and_anonymise") as read, \
                mock.patch.object(readout, "offer") as offered:
            said = readout.run(self.ctx, {**record("BASICR4", basicr4.stock_model), **extra})
        read.assert_not_called()
        offered.assert_not_called()
        offered_firmware.fetch.assert_not_called()
        return said

    def test_nothing_is_fetched_sent_to_it_or_sent_in_and_it_asks_as_a_whole_readout(self):
        said = self.run_basicr4(params={"fwVersion": "1.1.0", "staMac": "12:34:56:AB:CD:EF"})
        self.assertEqual((said, ended(self.ctx)[-1][1]), ("", readout.UNNEEDED))   # the report says so
        product, model, version, stage, _, device = readout.ask_wanted.call_args.args
        self.assertEqual((product, version, stage), ("BASICR4", "1.1.0", "whole"))
        # The device, as its report names it, so the website counts devices, not questions.
        self.assertEqual(device, report_data.device_hash("12:34:56:ab:cd:ef"))

    def test_why_is_shown_and_nothing_asked(self):
        # On the screen, not the log alone; the email question waits for the
        # tool's exit (signup).
        with mock.patch.object(prompts, "ask_choice") as asked:
            self.assertEqual(self.run_basicr4(), "")
        asked.assert_not_called()
        readout.notify.assert_not_called()
        self.assertIn("    Not needed: the website has what it needs of this device already.",
                      [call.args[0] for call in output.tell.call_args_list])

    def test_in_a_batch_it_says_so_and_the_rest_go_on(self):
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"):
            said = readout.run_all(self.ctx, [DEVICE, {**DEVICE, "deviceid": "1000fedcba", "name": "Porch"}])
        self.assertEqual((said, ended(self.ctx)), ("", [(HALL, readout.UNNEEDED, ""), (PORCH, readout.UNNEEDED, "")]))

    def test_a_sender_at_its_daily_limit_ends_a_batch_there_too(self):
        readout.ask_wanted.return_value = readout.Wanted(False, "limit")
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"):
            said = readout.run_all(self.ctx, [DEVICE, {**DEVICE, "deviceid": "1000fedcba", "name": "Porch"}])
        self.assertEqual(readout.ask_wanted.call_count, 1)
        self.assertEqual((said, ended(self.ctx)), ("", [(HALL, readout.WEBSITE_FULL, readout.LIMIT_TODAY),
                                                         (PORCH, readout.WEBSITE_FULL, readout.LIMIT_TODAY)]))

    def test_a_full_day_ends_a_batch_there(self):
        readout.ask_wanted.return_value = readout.Wanted(False, "full")
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"):
            said = readout.run_all(self.ctx, [DEVICE, {**DEVICE, "deviceid": "1000fedcba", "name": "Porch"}])
        self.assertEqual(readout.ask_wanted.call_count, 1)
        self.assertEqual((said, ended(self.ctx)), ("", [(HALL, readout.WEBSITE_FULL, readout.FULL_TODAY),
                                                         (PORCH, readout.WEBSITE_FULL, readout.FULL_TODAY)]))

    def test_a_full_store_ends_a_batch_there_and_says_a_day_will_not_do(self):
        readout.ask_wanted.return_value = readout.Wanted(False, "store-full")
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"):
            said = readout.run_all(self.ctx, [DEVICE, {**DEVICE, "deviceid": "1000fedcba", "name": "Porch"}])
        self.assertEqual(readout.ask_wanted.call_count, 1)
        self.assertEqual((said, ended(self.ctx)), ("", [(HALL, readout.WEBSITE_FULL, readout.STORE_FULL),
                                                         (PORCH, readout.WEBSITE_FULL, readout.STORE_FULL)]))
        self.assertNotIn("tomorrow", readout.STORE_FULL)


class HeldFirmwareTests(unittest.TestCase):
    """The files eWeLink offers that the website holds already: left out."""

    OFFERS = {"offers": [{"version": "1.1.0", "binList": [
        {"name": "user1.bin", "downloadUrl": "https://example.com/user1.bin", "digest": "A" * 64},
        {"name": "user2.bin", "downloadUrl": "https://example.com/user2.bin", "digest": "b" * 64}]}]}

    def setUp(self):
        start_run(self)
        asked = account.Asked("1.0.0", "SN-NEW-01", "NEWTHING", [], self.OFFERS, "1000abcdef")
        for patch in (mock.patch.object(account, "ask_account", return_value=asked),
                      mock.patch.object(account, "ask_account", return_value=asked)):
            patch.start()
            self.addCleanup(patch.stop)
        readout.ask_wanted.return_value = readout.Wanted(True, "", frozenset({"a" * 64}))

    def test_the_digests_go_with_the_question_and_a_held_file_is_not_fetched(self):
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"), mock.patch.object(readout, "offer", return_value="done") as offered:
            readout.run(self.ctx, DEVICE)
        self.assertEqual(readout.ask_wanted.call_args.args[4], ["a" * 64, "b" * 64])
        self.assertEqual(offered_firmware.fetch.call_args.kwargs["skip"], frozenset({"a" * 64}))
        manifest = offered.call_args.args[1]
        self.assertIn({"file": None, "version": "1.1.0", "sha256": "a" * 64, "held": True}, manifest["offered"])

    def test_at_most_eight_digests_go_with_the_question(self):
        offers = [{"version": "1.1.0", "binList": [{"digest": f"{n:x}" * 64} for n in range(10)]}]
        self.assertEqual(readout.offered_digests(offers), [f"{n:x}" * 64 for n in range(8)])
        self.assertEqual(len(readout.offered_digests(offers, versions=True)), 10)

    def test_a_beken_device_read_in_full_still_fetches_it_to_put_sonoff_back_but_does_not_send_it(self):
        from tools.autoflash import kinds
        s61 = kinds.SONOFF_S61
        held = offered_firmware.Offered("user1.bin", "1.1.0", b"held")
        readout.ask_wanted.return_value = readout.Wanted(True, "", frozenset({held.sha256}))
        offered_firmware.fetch.return_value = ([held], [])
        asked = account.Asked("1.0.2", s61.stock_model, "S61STPF", [], self.OFFERS, "1000abcdef")
        with mock.patch.multiple(account, ask_account=mock.Mock(return_value=asked)):
            with learned(s61.wrapper_model, "BK7238"), \
                    mock.patch.object(readout_beken, "stock_from_offered", return_value=mock.Mock()), \
                    mock.patch.object(readout_beken, "read_and_anonymise", return_value=({}, True)), \
                    mock.patch.object(readout, "offer", return_value="done") as offered:
                readout.run(self.ctx, record("S61STPF", s61.stock_model))
        self.assertEqual(offered_firmware.fetch.call_args.kwargs["skip"], frozenset())
        self.assertEqual(offered.call_args.args[3], [])
        # Not sent, so named by its SHA-256 alone: no file the website would look for.
        self.assertEqual(offered.call_args.args[1]["offered"],
                         [{"file": None, "version": "1.1.0", "sha256": held.sha256, "held": True}])


class NotifyTests(unittest.TestCase):
    """An owner's address, to be emailed as more devices are supported."""

    def test_one_request_naming_each_device_and_readout_once(self):
        with mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(readout, "light_post", return_value={}) as posted:
            self.assertTrue(readout.notify("me@example.com", [("NSPanel", "E32-MSW-NX"), ("NSPanel", "E32-MSW-NX"),
                                                              ("NEWTHING", ""), ("", "x")],
                                           ["2026-11-07-0123456789ab", "2026-11-07-0123456789ab"]))
            path, value = posted.call_args.args
            readout.notify("me@example.com", [], update=True)
        self.assertEqual((path, value["email"]), ("/notify", "me@example.com"))
        self.assertEqual(value["devices"], [{"model": "NSPanel", "stock_model": "E32-MSW-NX"},
                                            {"model": "NEWTHING", "stock_model": "-"}])
        self.assertEqual(value["readouts"], ["2026-11-07-0123456789ab"])
        self.assertIs(value["update"], False)   # a sign-up: confirmed by email if it is not yet
        self.assertIs(posted.call_args.args[1]["update"], True)

    def test_with_no_device_seen_it_still_asks(self):
        with mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(readout, "light_post", return_value={}) as posted:
            self.assertTrue(readout.notify("me@example.com", []))
        self.assertEqual((posted.call_args.args[1]["devices"], posted.call_args.args[1]["readouts"]), ([], []))

    def test_never_from_a_test_run_and_never_raises(self):
        with mock.patch.object(readout, "upload_url", return_value=None):
            self.assertFalse(readout.notify("me@example.com", [("NSPanel", "x")]))
        with mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(readout, "light_post", side_effect=OSError("refused")), \
                mock.patch.object(output, "log_only"):
            self.assertFalse(readout.notify("me@example.com", [("NSPanel", "x")]))

    def test_at_most_twenty_devices_and_readouts_in_one_sign_up(self):
        with mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(readout, "light_post", return_value={}) as posted:
            readout.notify("me@example.com", [(f"P{n}", "m") for n in range(30)],
                           [f"2026-11-07-{n:012x}" for n in range(30)])
        self.assertEqual(len(posted.call_args.args[1]["devices"]), readout.NOTIFY_MOST)
        self.assertEqual(len(posted.call_args.args[1]["readouts"]), readout.NOTIFY_MOST)

    def test_a_readout_sent_again_is_remembered_for_the_sign_up(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(readout, "send", side_effect=["2026-11-07-0123456789ab", readout.ENOUGH]), \
                mock.patch.object(readout.signup, "SITTING", readout.signup.Sitting()):
            kept = Path(tmp) / readout.NAME
            kept.write_bytes(b"a readout")
            readout.send_saved(kept)
            readout.send_saved(kept)   # one the website has enough of: no reference
            self.assertEqual(list(readout.signup.SITTING.readouts), ["2026-11-07-0123456789ab"])

    def test_a_readout_taken_is_remembered_for_the_sign_up(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(output, "say"), \
                mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(readout, "send", return_value="2026-11-07-0123456789ab"), \
                mock.patch.object(readout.signup, "SITTING", readout.signup.Sitting()):
            readout.offer(a_ctx(tmp), MANIFEST, None)
            self.assertEqual(list(readout.signup.SITTING.readouts), ["2026-11-07-0123456789ab"])


class ProofProgressTests(unittest.TestCase):
    def test_each_wait_turns_the_stages_spinner(self):
        # Outside a stage nothing else moves: an owner left looking at a still
        # screen for a minute may take it to have stopped.
        shown = []

        @contextlib.contextmanager
        def working(text):
            shown.append(text)
            yield

        answers = iter([urllib.error.HTTPError("u", 429, "too many", {}, io.BytesIO(b"{}")), None])

        def opened(request, timeout):
            failed = next(answers)
            if failed:
                raise failed
            return io.BytesIO(b'{"id": "r-1"}')
        with mock.patch.object(output, "working", working), mock.patch.object(readout, "PROOF_BITS", 8), \
                mock.patch.object(readout.hostsys, "urlopen", side_effect=opened), \
                mock.patch.object(readout.time, "sleep"):
            self.assertEqual(readout.send(b"data", "http://127.0.0.1:9/readout"), "r-1")
        self.assertEqual([text.split(":")[0] for text in shown], ["Preparing to send - a few seconds to a minute or two...",
                                                                 "The website takes two readouts a minute"])


class FirstStageTests(unittest.TestCase):
    """A device the tool has not seen: everything from eWeLink, nothing asked of it."""

    def setUp(self):
        start_run(self)

    def test_it_needs_no_address_and_is_never_asked_for_one(self):
        # So a device answering no discovery - a Matter one, say - is read out
        # too. Its MAC alone may be looked for, for its report (RunTests).
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"), mock.patch.object(readout, "offer", return_value="done"):
            readout.run(self.ctx, DEVICE)
        readout.locate.assert_not_called()
        self.assertEqual(account.ask_account.call_args.args[1], "1000abcdef")

    def test_with_nothing_from_its_cloud_the_accounts_record_is_sent(self):
        failed = mock.Mock(side_effect=errors.Abort("no OTA model"))
        with mock.patch.object(account, "ask_account", failed), \
                mock.patch.object(firmware_name, "learn", return_value=(None, ["no offer"])), \
                mock.patch.object(readout, "offer", return_value="done") as offered:
            self.assertEqual(readout.run(self.ctx, DEVICE), "done")
        manifest = offered.call_args.args[1]
        self.assertEqual((manifest["model"], manifest["stock_model"], manifest["stock_version"], manifest["read"]),
                         ("NEWTHING", "SN-ESP32C3-NEW-01", "1.0.0", False))


class ChooseTests(unittest.TestCase):
    def choose(self, devices, answer):
        with mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"), \
                mock.patch.object(prompts, "ask_choice", return_value=answer) as asked:
            chosen = readout.choose(SimpleNamespace(), devices)
        return asked.call_args.args[1], chosen

    def test_every_device_in_the_account_is_offered_first_those_it_converts_too(self):
        # A device the tool converts may run firmware the website has no readout
        # of: new firmware is what a conversion meets unready.
        from tools.autoflash import kinds
        s60 = record("S60TPG", kinds.SONOFF_S60.stock_model)
        other = {**DEVICE, "deviceid": "1000fedcba", "name": "Porch"}
        options, chosen = self.choose([s60, DEVICE, other], 0)
        self.assertEqual(options[0], "Every device in the account (3)")
        self.assertIsNone(chosen)
        self.assertEqual(self.choose([s60, DEVICE, other], 2)[1], DEVICE)
        self.assertEqual(self.choose([s60, DEVICE, other], 1)[1], s60)
        self.assertEqual(len(self.choose([s60, DEVICE], 0)[0]), 3)

    def test_a_kind_is_known_for_a_full_readout_by_its_firmware_model_too(self):
        from tools.autoflash import kinds
        self.assertTrue(readout.reads_out(record("NEWTHING", kinds.SONOFF_S60.stock_model)))
        self.assertFalse(readout.reads_out(DEVICE))

    def test_every_one_is_every_device_in_the_account(self):
        from tools.autoflash import kinds
        s60 = record("S60TPG", kinds.SONOFF_S60.stock_model)
        with mock.patch.object(readout, "choose", return_value=None), \
                mock.patch.object(readout, "run_all", return_value="done") as every:
            self.assertEqual(readout.read_out_chosen(SimpleNamespace(), [s60, DEVICE]), "done")
        self.assertEqual(every.call_args.args[1], [s60, DEVICE])

    def test_a_device_it_converts_is_read_only_if_its_firmware_is_new_to_the_website(self):
        from tools.autoflash import kinds
        start_run(self)
        s60 = kinds.SONOFF_S60
        asked = account.Asked("1.2.0", s60.stock_model, "S60TPG", [], {}, "1000abcdef")
        readout.ask_wanted.return_value = readout.Wanted(False, "held")
        with mock.patch.multiple(account, ask_account=mock.Mock(return_value=asked)), \
                learned(s60.wrapper_model, "ESP32C3"), \
                mock.patch.object(readout_espressif, "read_and_anonymise") as read, \
                mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"), \
                mock.patch.object(runs, "hold_device") as held:
            said = readout.run_all(self.ctx, [record("S60TPG", s60.stock_model)])
        held.assert_called_once()   # held as one written to may be, and nothing left behind
        self.assertEqual(readout.ask_wanted.call_args.args[:4], ("S60TPG", s60.stock_model, "1.2.0", "whole"))
        read.assert_not_called()
        self.assertEqual((said, ended(self.ctx)[-1][1]), ("", readout.UNNEEDED))

class RunAllTests(unittest.TestCase):
    """Every device the tool does not convert, read out in one go, each sent on its own."""

    def setUp(self):
        start_run(self)
        from tools.autoflash import kinds
        self.basicr4 = {**record("BASICR4", kinds.SONOFF_BASICR4.stock_model), "deviceid": "1000000004",
                        "name": "Shed"}
        self.other = {**DEVICE, "deviceid": "1000fedcba", "name": "Porch"}
        for patch in (mock.patch.object(prompts, "UNATTENDED", False),
                      mock.patch.object(readout, "upload_url", return_value="http://127.0.0.1:9/readout"),
                      mock.patch.object(runs, "hold_device")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_all(self, devices, send=None):
        """run_all through readout.run, each device's readout sent by `send`
        (default: a reference each); what it ended by saying, and what was sent."""
        sent = mock.Mock(side_effect=send or (lambda data, url: f"r-{len(sent.call_args_list)}"))
        with learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"), mock.patch.object(readout, "send", sent), \
                mock.patch.object(readout_espressif, "read_and_anonymise", return_value=(FILES, True)) as read:
            said = readout.run_all(self.ctx, devices)
        self.read = read
        return said, [json.loads(unpacked(call.args[0])["manifest.json"]) for call in sent.call_args_list]

    def test_each_is_sent_on_its_own_and_nothing_asked(self):
        with mock.patch.object(prompts, "ask_choice") as asked:
            said, manifests = self.run_all([DEVICE, self.other])
        asked.assert_not_called()
        self.assertEqual(len(manifests), 2)
        self.assertFalse([manifest for manifest in manifests if "contact" in manifest])
        self.assertEqual((said, ended(self.ctx)), ("", [(HALL, readout.ACCEPTED, "r-1"), (PORCH, readout.ACCEPTED, "r-2")]))

    def test_the_first_stage_ones_go_before_a_full_readout(self):
        from tools.autoflash import kinds
        def asked(ctx, deviceid, ip=None):
            if deviceid != "1000000004":   # DEVICE, a kind the tool has not seen
                return account.Asked("1.0.0", "SN-NEW-01", "NEWTHING", [], {}, deviceid)
            return account.Asked("1.1.0", kinds.SONOFF_BASICR4.stock_model, "BASICR4", [], {}, deviceid)
        with mock.patch.object(account, "ask_account", side_effect=asked), \
                learned(kinds.SONOFF_BASICR4.wrapper_model, "ESP32C3"), \
                mock.patch.object(readout, "send", side_effect=["r-1", "r-2"]) as sent, \
                mock.patch.object(readout_espressif, "read_and_anonymise", return_value=(FILES, True)):
            readout.run_all(self.ctx, [self.basicr4, DEVICE])
        self.assertEqual([json.loads(unpacked(call.args[0])["manifest.json"]).get("read", True)
                          for call in sent.call_args_list], [False, True])

    def test_one_the_website_has_enough_of_is_said_so_and_the_rest_go_on(self):
        said, manifests = self.run_all([DEVICE, self.other], send=[readout.ENOUGH, "r-2"])
        self.assertEqual(len(manifests), 2)
        self.assertEqual(ended(self.ctx)[0], (HALL, readout.UNNEEDED, ""))

    def test_one_not_sent_is_kept_under_its_id_and_the_rest_go_on(self):
        said, manifests = self.run_all([DEVICE, self.other], send=[OSError("no route"), "r-2"])
        self.assertEqual(len(manifests), 2)
        self.assertTrue((Path(self.tmp.name) / "readout-1000abcdef.tar").exists())
        self.assertIn("readout --send", said)

    def test_a_full_readout_passed_by_at_its_question_lets_the_rest_go_on(self):
        # The first stage's go first: DEVICE's, then the BASICR4's full one.
        with mock.patch.object(readout, "run", side_effect=["sent", errors.NothingWritten("not confirmed")]):
            said = readout.run_all(self.ctx, [self.basicr4, DEVICE])
        self.assertEqual(ended(self.ctx), [("'Shed' (BASICR4)", readout.NOT_READ, "not confirmed")])
        self.assertEqual(said, "sent")   # the first's, run as stubbed

    def test_a_full_readout_failing_part_way_stops_the_rest(self):
        # It may be left on its reading program, and need seeing to first.
        second = {**self.basicr4, "deviceid": "1000000005", "name": "Barn"}
        with mock.patch.object(readout, "run", side_effect=["sent", errors.Abort("the bridge did not answer"),
                                                             "sent"]) as each:
            with self.assertRaises(errors.Abort) as stopped:
                readout.run_all(self.ctx, [self.basicr4, DEVICE, second])
        self.assertEqual(each.call_count, 2)
        self.assertIn("Not read out yet: 'Barn' (BASICR4).", str(stopped.exception))
        self.assertTrue(str(stopped.exception).startswith("'Shed' (BASICR4): the bridge did not answer"))

    def test_a_first_stage_failing_lets_the_rest_go_on(self):
        with mock.patch.object(readout, "run", side_effect=[errors.Abort("eWeLink is down"), "sent"]):
            said = readout.run_all(self.ctx, [DEVICE, self.other])
        self.assertEqual(ended(self.ctx), [(HALL, readout.NOT_READ, "eWeLink is down")])
        self.assertEqual(said, "sent")   # Porch's, run as stubbed

    def test_nothing_of_one_device_is_taken_for_the_next(self):
        seen = []

        def each(ctx, device, batch):
            seen.append(ctx.state.input("device_ip"))
            ctx.state.set_input("device_ip", "192.0.2.77")
            ctx.state.set_input(readout_espressif.BRIDGE_HOST, "192.0.2.77")
            return "sent"
        with mock.patch.object(readout, "run", side_effect=each):
            readout.run_all(self.ctx, [DEVICE, self.other])
        self.assertEqual(seen, [None, None])

    def test_the_run_stays_where_it_is_and_a_device_written_to_is_held(self):
        from tools.autoflash import kinds
        device = {**self.basicr4, "params": {**DEVICE["params"], "staMac": "12:34:56:AB:CD:EF"}}
        with mock.patch.object(account, "ask_account", return_value=account.Asked(
                "1.1.0", kinds.SONOFF_BASICR4.stock_model, "BASICR4", [], {}, "1000000004")), \
                learned(kinds.SONOFF_BASICR4.wrapper_model, "ESP32C3"), \
                mock.patch.object(readout, "send", return_value="r-1"), \
                mock.patch.object(readout_espressif, "read_and_anonymise", return_value=(FILES, True)):
            readout.run_all(self.ctx, [device])
        self.filed.assert_not_called()
        self.assertEqual(runs.hold_device.call_args.args[1], "12:34:56:ab:cd:ef")

    def test_a_full_readouts_question_may_pass_its_device_by(self):
        from tools.autoflash import kinds
        with mock.patch.object(account, "ask_account", return_value=account.Asked(
                "1.1.0", kinds.SONOFF_BASICR4.stock_model, "BASICR4", [], {}, "1000000004")), \
                learned(kinds.SONOFF_BASICR4.wrapper_model, "ESP32C3"), \
                mock.patch.object(readout_espressif, "read_and_anonymise",
                                  side_effect=errors.NothingWritten("skipped as asked, nothing was sent to it")) as read:
            said = readout.run_all(self.ctx, [self.basicr4])
        self.assertTrue(read.call_args.kwargs["skippable"])
        self.assertEqual((said, ended(self.ctx)),
                         ("", [("'Shed' (BASICR4)", readout.SKIPPED, "skipped as asked, nothing was sent to it")]))

    def test_q_at_a_full_readouts_question_stops_the_rest(self):
        second = {**self.basicr4, "deviceid": "1000000005", "name": "Barn"}
        with mock.patch.object(readout, "run", side_effect=["sent", prompts.QuitRun("q"), "sent"]) as each:
            said = readout.run_all(self.ctx, [self.basicr4, DEVICE, second])
        self.assertEqual(each.call_count, 2)
        self.assertEqual(said, "sent")   # Hall light's, run as stubbed
        self.assertEqual(ended(self.ctx), [("'Shed' (BASICR4)", readout.NOT_READ, "stopped as asked"),
                                           ("'Barn' (BASICR4)", readout.NOT_READ, "stopped as asked")])

    def test_each_device_sent_carries_work_of_its_own(self):
        # One proof is good for one readout's bytes alone, so a batch cannot
        # spread one proof over many devices: each POST is worked for afresh.
        import hashlib
        posted = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server's name
                body = self.rfile.read(int(self.headers["Content-Length"]))
                posted.append((self.path, hashlib.sha256(body).hexdigest(), self.headers["X-Proof"]))
                answer = json.dumps({"wanted": True} if self.path == "/wanted" else {"id": f"r-{len(posted)}"}).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def asked(ctx, deviceid):
            return account.Asked("1.0.0", "SN-NEW-01", f"NEW-{deviceid}", [], {}, deviceid)
        with mock.patch.object(readout, "PROOF_BITS", 8), learned("FWSW-E32-NEW-ESP32C3", "ESP32C3"), \
                mock.patch.object(readout, "ask_wanted", ASK_WANTED), \
                mock.patch.object(account, "ask_account", side_effect=asked), \
                mock.patch.object(readout, "upload_url",
                                  return_value=f"http://127.0.0.1:{server.server_address[1]}/readout"):
            readout.run_all(self.ctx, [DEVICE, self.other])
        # Each device asked first, then its readout sent: each with work of its own.
        self.assertEqual([path for path, _, _ in posted], ["/wanted", "/readout"] * 2)
        self.assertEqual(len({digest for _, digest, _ in posted}), 4)
        for path, digest, proof in posted:
            stamp, nonce = proof.split(":")
            work = hashlib.sha256(f"{stamp}:{digest}:{nonce}".encode()).digest()
            bits = readout.LIGHT_PROOF_BITS if path == "/wanted" else 8
            self.assertLess(int.from_bytes(work, "big"), 1 << (256 - bits))


class ReportTests(unittest.TestCase):
    """The end of a readout, as a conversion's: one report for every device
    read out in the sitting, then the links to support the project."""

    DONE = [readout.Became("'Kitchen' (BASICR4)", "BASICR4", "SN-ESP32C3-BASICR4-01", "1.1.0", True,
                           readout.ACCEPTED, "r-1"),
            readout.Became("'Hall light' (NEWTHING)", "NEWTHING", "SN-NEW-01", "2.0.0", False,
                           readout.KEPT, "a test run"),
            readout.Became("'Porch' (NEWTHING)", "NEWTHING", "SN-NEW-01", "2.0.0", None, readout.UNNEEDED)]

    def test_it_counts_the_devices_those_accepted_and_those_not_needed_and_gives_no_time(self):
        fields = dict(readout.report_fields(self.DONE))
        self.assertEqual((fields["Devices"], fields["Accepted"], fields["Not needed"]), ("3", "1", "1"))
        self.assertNotIn("Time", fields)
        # How it ended is said above the report, and each device's under it.
        self.assertNotIn("Result", fields)

    def test_those_kept_unsent_are_counted_on_the_right_only_where_there_are_any(self):
        lines = report.report_screen(readout.report_fields(self.DONE), columns=100, right=readout.REPORT_RIGHT,
                                     balance=False)
        at = {label: line.index(label) for line in lines for label in ("Devices:", "Not sent:") if label in line}
        self.assertEqual(at["Not sent:"], at["Devices:"], "\n".join(lines))   # in the right-hand column
        self.assertTrue(any(line.rstrip().endswith("Not sent:   1") for line in lines))
        self.assertNotIn("Not sent", dict(readout.report_fields([self.DONE[0], self.DONE[2]])))

    def test_one_device_is_named_by_its_model_and_version_alone_under_how_it_ended(self):
        self.assertEqual(readout.report_devices(self.DONE[:1]), [("Accepted", "BASICR4\u00a01.1.0")])
        self.assertEqual(readout.report_devices(self.DONE[2:]), [("Not needed", "NEWTHING\u00a02.0.0")])
        shown = repr(readout.report_fields(self.DONE[:1])) + repr(readout.report_devices(self.DONE[:1]))
        for left_out in ("Kitchen", "r-1", "memory"):   # its name in the account, its reference
            self.assertNotIn(left_out, shown)

    def test_several_are_listed_by_how_each_ended_each_model_and_version_once(self):
        done = [*self.DONE,
                readout.Became("'Garage' (BASICR4)", "BASICR4", "SN-ESP32C3-BASICR4-01", "1.1.0", True,
                               readout.ACCEPTED, "r-2"),
                readout.Became("'Shed' (BASICR4)", "BASICR4", "SN-ESP32C3-BASICR4-01", "1.2.0", True,
                               readout.ACCEPTED, "r-3")]
        self.assertEqual(dict(readout.report_fields(done))["Accepted"], "3")   # each device counted
        self.assertEqual(readout.report_devices(done), [("Accepted", "BASICR4\u00a01.1.0, BASICR4\u00a01.2.0"),
                                                        ("Not needed", "NEWTHING\u00a02.0.0"),
                                                        ("Not sent", "NEWTHING\u00a02.0.0")])

    def test_the_counts_are_on_the_right_and_the_lists_under_the_rest_wrapping_between_devices_alone(self):
        many = [readout.Became(f"'D{n}'", f"MODEL{n}", "", "1.0.0", True, readout.ACCEPTED) for n in range(12)]
        lines = report.report_screen(readout.report_fields(many), columns=80, right=readout.REPORT_RIGHT,
                                     below=readout.report_devices(many))
        self.assertTrue(any(line.rstrip().endswith("Accepted:   12") for line in lines), "\n".join(lines))
        at = next(index for index, line in enumerate(lines) if line.lstrip().startswith("Accepted:"))
        self.assertTrue(all("Devices:" not in line for line in lines[at:]), "under the columns")
        listed = " ".join(line.split(":", 1)[-1] for line in lines[at:])
        self.assertEqual(listed.split(), " ".join(f"MODEL{n} 1.0.0," for n in range(12)).rstrip(",").split())
        for line in lines[at:]:
            self.assertLessEqual(len(line), 80)
            self.assertRegex(line.rstrip(), r"(MODEL\d+ 1\.0\.0,?|Accepted:)$", "never inside a device's name")

    def shown_by_end(self, done, failed=False):
        shown = []
        with mock.patch.object(output, "tell", side_effect=lambda text="": shown.append(text)), \
                mock.patch.object(output, "show", side_effect=lambda text, *_: shown.append(text)), \
                mock.patch.object(output, "show_links"), mock.patch.object(output, "show_link"), \
                mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
            readout.end(SimpleNamespace(readouts=done), "", failed=failed)
        return shown

    def test_the_counts_stay_together_on_the_right_beside_the_rows_of_the_left(self):
        for done, counts in ((self.DONE, ["Devices:", "Accepted:", "needed:", "sent:"]),   # some kept unsent
                             ([self.DONE[0], self.DONE[2]], ["Devices:", "Accepted:", "needed:"])):
            with self.subTest(counts=len(counts)):
                shown = self.shown_by_end(done)
                self.assertFalse([line for line in shown if "Result:" in line], "said above it")
                rows = [next(line for line in shown if label in line) for label in ("Liberator:", "OS:", "Python:")]
                self.assertEqual([line.split()[-2] for line in rows], counts[:3], "\n".join(shown))
                labels = ("Devices:", "Accepted:", "Not needed:", "Not sent:")
                at = {line.index(label, 20) for line in shown for label in labels if label in line[20:]}
                self.assertEqual(len(at), 1, "\n".join(shown))   # in one column

    def test_one_that_failed_has_the_way_to_open_an_issue_where_the_links_to_support_the_project_go(self):
        failed = [readout.Became("'Hall'", "BASICR4", "", "1.1.0", True, readout.FAILED)]
        shown = self.shown_by_end(failed, failed=True)
        self.assertIn("If it should not have stopped, please open an issue here:", shown)
        self.assertNotIn("You've reached the bit where I hold out my hat!", shown)
        self.assertNotIn("Readout complete!", shown)
        self.assertNotIn("Nothing was read out.", shown, "its FAILED line says how it ended")
        self.assertEqual(next(line for line in shown if "BASICR4" in line).split(), ["Failed:", "BASICR4", "1.1.0"])

    def test_one_that_failed_puts_its_report_and_the_failed_ones_id_on_the_clipboard(self):
        failed = [readout.Became("'Kitchen'", "BASICR4", "", "1.1.0", True, readout.ACCEPTED, report_id="a" * 32),
                  readout.Became("'Hall'", "MINIR4", "", "1.2.0", True, readout.FAILED, report_id="b" * 32)]
        copied = []
        with mock.patch.object(report.hostsys, "copy_to_clipboard",
                               side_effect=lambda text: copied.append(text) or True):
            shown = self.shown_by_end(failed, failed=True)
        self.assertNotIn("Readout complete!", shown, "one read before it: its FAILED line still says how it ended")
        rows = [row.split() for row in copied[0].splitlines()]
        self.assertIn(["Failed:", "MINIR4", "1.2.0"], rows)
        self.assertEqual(rows[-1], ["Report", "ID:", "b" * 32], copied[0])   # the one that failed
        self.assertFalse(copied[0].startswith(" "), "its margin taken off")
        at = shown.index(f"    Report ID: {'b' * 32}")
        self.assertEqual(shown[at + 1], "    This report is on your clipboard.")

    def test_one_that_finished_leaves_the_clipboard_alone_and_shows_no_report_id(self):
        done = [readout.Became("'Kitchen'", "BASICR4", "", "1.1.0", True, readout.ACCEPTED, report_id="a" * 32)]
        with mock.patch.object(report.hostsys, "copy_to_clipboard") as copied:
            shown = self.shown_by_end(done)
        copied.assert_not_called()
        self.assertFalse([line for line in shown if "Report ID" in line or "clipboard" in line], shown)

    def test_one_that_read_nothing_out_says_so_above_its_report(self):
        shown = self.shown_by_end(self.DONE[2:])
        self.assertIn("Nothing was read out.", shown)
        self.assertNotIn("Readout complete!", shown)

    def test_it_ends_as_a_conversion_does(self):
        shown = []
        with mock.patch.object(output, "tell", side_effect=lambda text="": shown.append(text)), \
                mock.patch.object(output, "show", side_effect=lambda text, *_: shown.append(text)), \
                mock.patch.object(output, "show_links", side_effect=lambda links, **_: shown.append(links)), \
                mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
            readout.end(SimpleNamespace(readouts=self.DONE), "Switch the device off and on once more.")
        text = [item for item in shown if isinstance(item, str)]
        first = next(line for line in text if line.lstrip().startswith("Liberator:"))
        order = [text.index(line) for line in ("Readout complete!", "Switch the device off and on once more.",
                                               first, "You've reached the bit where I hold out my hat!")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("How it went:", text)
        self.assertTrue(any(line.lstrip().startswith("Accepted:") and "BASICR4 1.1.0" in line for line in text))
        self.assertTrue(any(line.lstrip().startswith("Not needed:") for line in text))
        self.assertEqual(shown[-1][0][0], report.SPONSORS_URL)
        self.assertFalse([line for line in text if "Readout saved in" in line])


class OfferTests(unittest.TestCase):
    def offer(self, url="http://127.0.0.1:9/readout"):
        """offer() to `url`: what was sent, what the run ends by saying, and
        whether a copy was left behind."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prompts, "section") as shown, \
                mock.patch.object(output, "say"), mock.patch.object(prompts, "UNATTENDED", False), \
                mock.patch.object(prompts, "ask_choice") as menu, mock.patch.object(prompts, "ask_text") as typed, \
                mock.patch.object(readout, "upload_url", return_value=url), \
                mock.patch.object(readout, "send", return_value="r-1") as sent:
            ctx = a_ctx(tmp)
            ctx.readout = self.became = readout.Became("'Hall light' (NEWTHING)")
            said = readout.offer(ctx, MANIFEST, None)
            kept = (Path(tmp) / readout.NAME).exists()
        self.asked = [question for question in (shown, menu, typed) if question.called]
        return (unpacked(sent.call_args.args[0]) if sent.called else None), said, kept

    def test_it_is_sent_unasked(self):
        # The docs say what a readout holds: a question here only stops it
        # part way, and the email question waits for the tool's exit.
        sent, said, kept = self.offer()
        self.assertEqual(self.asked, [])
        self.assertFalse(kept)  # sent: no copy is left behind
        self.assertEqual(json.loads(sent["manifest.json"])["model"], MANIFEST["model"])
        self.assertEqual((said, self.became.outcome, self.became.detail), ("", readout.ACCEPTED, "r-1"))

    def test_from_a_test_run_it_is_kept_unsent(self):
        sent, _, kept = self.offer(None)
        self.assertIsNone(sent)
        self.assertTrue(kept)
        self.assertEqual((self.became.outcome, self.became.detail), (readout.KEPT, "a test run"))

    def test_the_owner_is_never_asked_for_their_wifi_password(self):
        # Where the settings hold none, the copy goes as it is: a question
        # here only makes people hesitate to send it.
        flash = b"\xff" * 0x20000
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prompts, "ask_secret") as asked, \
                mock.patch.object(output, "say"), mock.patch.object(readout, "upload_url", return_value=None):
            readout.offer(a_ctx(tmp), MANIFEST, {"flash.bin": flash})
        asked.assert_not_called()


class OfferedAtA1Tests(unittest.TestCase):
    def test_an_account_of_devices_not_supported_offers_a_readout(self):
        with mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"), \
                mock.patch.object(prompts, "section") as shown, \
                mock.patch.object(prompts, "ask_choice", return_value=3) as asked, \
                mock.patch.object(readout, "from_run", side_effect=errors.Finished("read")) as read:
            with self.assertRaises(errors.Finished):
                account.find_account_device(SimpleNamespace(email=lambda: "me@example.com"), [DEVICE])
        self.assertIn("Read out my device, to help add support for it", asked.call_args.args[1])
        self.assertEqual(read.call_args.args[1], [DEVICE])
        self.assertIn("'Hall light' (NEWTHING)", " ".join(shown.call_args.args[1]))

    def test_an_empty_account_does_not(self):
        with mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"), \
                mock.patch.object(prompts, "section"), \
                mock.patch.object(prompts, "ask_choice", side_effect=prompts.QuitRun) as asked:
            with self.assertRaises(prompts.QuitRun):
                account.find_account_device(SimpleNamespace(email=lambda: "me@example.com"), [])
        self.assertEqual(len(asked.call_args.args[1]), 3)


class EndsTheRunTests(unittest.TestCase):
    def test_a_readout_at_a1_ends_the_run_with_nothing_converted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_ctx(tmp)
            with mock.patch.object(output, "stage_end") as ended, mock.patch.object(output, "tell") as told, \
                    mock.patch.object(runner.routes, "begin_stage"), mock.patch.object(output, "say"), \
                    mock.patch.object(runner.routes, "STEP_INDEX",
                                      {"A1": ("Pair", mock.Mock(side_effect=errors.Finished("Sent, thank you!")))}):
                self.assertEqual(runner.execute(ctx, "A1"), 0)
            self.assertTrue(ctx.finished)
            self.assertEqual(ctx.state.status("A1"), "failed")
        ended.assert_called_with("complete")
        self.assertIn("Sent, thank you!", [call.args[0] for call in told.call_args_list])


class OfferScreenTests(unittest.TestCase):
    """A readout that cannot be sent says why, and where it is kept."""

    def offer(self, url, sending=None):
        from tests.autoflash.test_unlock import drawn
        said = {}

        def body(look):
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prompts, "UNATTENDED", False), \
                    mock.patch.object(readout, "upload_url", return_value=url), \
                    mock.patch.object(readout, "send", side_effect=sending or (lambda data, url: "r-1")):
                try:
                    said["end"] = readout.offer(a_ctx(tmp), MANIFEST, None)
                except readout.Abort as exc:
                    said["end"] = str(exc)
        _, final = drawn(body)
        return final, said["end"]

    def test_left_when_sending_fails_to_say_what(self):
        _, said = self.offer("http://127.0.0.1:9/readout", sending=OSError("no route"))
        self.assertIn("could not be sent", said)


if __name__ == "__main__":
    unittest.main()
