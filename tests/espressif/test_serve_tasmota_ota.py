import http.client
import threading
import time
import unittest

from tools.espressif.serve_tasmota_ota import FirmwareServer, parse_single_range
from tools.autoflash.routes.espressif import installer


class FirmwareServerTests(unittest.TestCase):
    def setUp(self):
        self.firmware = bytes(range(100))
        self.server = FirmwareServer(("127.0.0.1", 0), self.firmware, "127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path="/user1.bin", byte_range=None):
        headers = {} if byte_range is None else {"Range": byte_range}
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def test_shutdown_is_not_held_up_by_an_open_keep_alive_connection(self):
        # The device leaves its connection open after the last range. Shutdown
        # used to wait on it forever, after every byte had been delivered.
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        connection.request("GET", "/user1.bin", headers={"Range": "bytes=0-99"})
        response = connection.getresponse()
        self.assertEqual(response.read(), self.firmware)
        try:
            stopper = threading.Thread(target=self.server.shutdown, daemon=True)
            stopper.start()
            stopper.join(timeout=3)
            self.assertFalse(stopper.is_alive(), "shutdown blocked on an idle connection")
        finally:
            connection.close()

    def test_serves_only_exact_firmware_paths(self):
        status, _, body = self.request("/wrong.bin")
        self.assertEqual((status, body), (404, b""))
        status, _, body = self.request("/user1.bin?ignored=query")
        self.assertEqual((status, body), (200, self.firmware))

    def test_serves_the_names_it_is_given(self):
        # OpenBeken fetches by the URL it is given, firmware.rbl (routes/beken/onward.py).
        named = FirmwareServer(("127.0.0.1", 0), self.firmware, "127.0.0.1", names=("firmware.rbl",))
        thread = threading.Thread(target=named.serve_forever, daemon=True)
        thread.start()
        try:
            for path, status in (("/firmware.rbl", 200), ("/user1.bin", 404)):
                connection = http.client.HTTPConnection("127.0.0.1", named.server_address[1], timeout=2)
                connection.request("GET", path)
                self.assertEqual(connection.getresponse().status, status, path)
                connection.close()
        finally:
            named.shutdown()
            named.server_close()
            thread.join(timeout=2)

    def test_a_download_cut_off_says_how_far_it_got(self):
        import socket
        big = FirmwareServer(("127.0.0.1", 0), bytes(16 * 1024 * 1024), "127.0.0.1")
        thread = threading.Thread(target=big.serve_forever, daemon=True)
        thread.start()
        try:
            client = socket.create_connection(("127.0.0.1", big.server_address[1]), timeout=2)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            client.sendall(b"GET /user1.bin HTTP/1.1\r\nHost: x\r\n\r\n")
            got = 0
            while got < 200 * 1024:
                got += len(client.recv(65536))
            client.close()
            deadline = time.monotonic() + 5
            while big.unique_bytes_served() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreater(big.unique_bytes_served(), 0)
            self.assertLess(big.unique_bytes_served(), 16 * 1024 * 1024)
        finally:
            big.shutdown()
            big.server_close()
            thread.join(timeout=2)

    def test_rejects_wrong_source(self):
        self.server.device_ip = "192.0.2.55"
        status, _, body = self.request()
        self.assertEqual((status, body), (403, b""))
        self.assertEqual(self.server.bytes_served, 0)

    def test_valid_range_and_unique_coverage(self):
        status, headers, body = self.request(byte_range="bytes=0-23")
        self.assertEqual(status, 206)
        self.assertIn(("Content-Range", "bytes 0-23/100"), headers)
        self.assertEqual(body, self.firmware[:24])
        self.request(byte_range="bytes=20-99")
        # The handler counts after writing, so its thread may finish after the client has read.
        deadline = time.monotonic() + 2
        while self.server.bytes_served < 104 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.bytes_served, 104)
        self.assertEqual(self.server.unique_bytes_served(), 100)

    def test_invalid_ranges_return_416_without_bytes(self):
        for value in ("items=0-1", "bytes=-10", "bytes=10-9", "bytes=0-100", "bytes=0-1,3-4"):
            with self.subTest(value=value):
                status, headers, body = self.request(byte_range=value)
                self.assertEqual((status, body), (416, b""))
                self.assertIn(("Content-Range", "bytes */100"), headers)
        self.assertEqual(self.server.bytes_served, 0)

    def test_range_parser_accepts_open_end(self):
        self.assertEqual(parse_single_range("bytes=24-", 100), (24, 99))


class OfflineTests(unittest.TestCase):
    """A device offline in eWeLink - after a send cut off part way, say - is
    waited for, not refused at once."""

    def test_one_that_comes_back_is_sent_to(self):
        from unittest import mock
        from tools.ewelink import ota_push as sender
        looks = iter([{"online": False}, {"online": True}])
        with mock.patch("builtins.print"):
            device = sender.until_online({"online": False}, lambda: next(looks), sleep=lambda _: None)
        self.assertTrue(device["online"])

    def test_one_that_stays_away_is_given_up_on(self):
        from unittest import mock
        from tools.ewelink import ota_push as sender
        looks = []

        def look():
            looks.append(1)
            return {"online": False}

        with mock.patch("builtins.print"):
            device = sender.until_online({"online": False}, look, sleep=lambda _: None)
        self.assertFalse(device["online"])
        self.assertEqual(len(looks), sender.OFFLINE_WAIT // 10)


class FailureTests(unittest.TestCase):
    """A failed send says whether the upgrade command went out."""

    def said(self, sent):
        import io
        from unittest import mock
        import tools.espressif.serve_tasmota_ota as sender
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            sender.report_failure(OSError("EOF occurred in violation of protocol"), sent)
        return err.getvalue()

    def test_before_the_command_it_says_nothing_reached_the_device(self):
        import tools.espressif.serve_tasmota_ota as sender
        said = self.said(sent=False)
        self.assertIn("ERROR: EOF occurred", said)
        self.assertIn(sender.NOT_SENT, said)

    def test_after_it_only_the_error(self):
        import tools.espressif.serve_tasmota_ota as sender
        self.assertNotIn(sender.NOT_SENT, self.said(sent=True))

    def test_the_run_looks_for_what_it_says(self):
        import tools.espressif.serve_tasmota_ota as sender
        self.assertTrue(sender.NOT_SENT.startswith(installer.BRIDGE_NOT_SENT))


class AnnouncedVersionTests(unittest.TestCase):
    def test_the_bridge_is_sent_as_the_announced_version(self):
        # Its own version (4.4.4) stays in the published file; the device's
        # updater is told the fixed one, in the command and the wrapper.
        from tools.autoflash.runs import ROOT
        from tools.espressif.analyze_vendor_ota import analyze
        from tools.espressif.build_vendor_ota import ANNOUNCED_VERSION
        sent, from_advert = self.send()
        # The account's id for the device, as the run gives it: no discovery advert needed.
        self.assertEqual(sent["device_id"], "1000000001")
        from_advert.assert_not_called()
        # The run's sign-in, which it handed over: one of its own would end it.
        self.assertEqual(sent["account"].held, ("us", {"at": "run-token", "user": {"apikey": "a"}}))
        self.assertEqual(sent["version"], ANNOUNCED_VERSION)
        self.assertEqual(analyze(sent["firmware"])["wrapper"]["header_version"], ANNOUNCED_VERSION)
        self.assertEqual(sent["firmware"][100:], (ROOT / installer.BRIDGE_OTA).read_bytes()[100:])
        self.assertIsNone(sent["event"])   # no run's events file given

    def test_what_the_send_does_goes_into_the_runs_events(self):
        import tempfile
        from pathlib import Path
        from tools.autoflash import output
        with tempfile.TemporaryDirectory() as directory:
            sent, _ = self.send("--events", str(Path(directory) / output.EVENTS))
            sent["event"]("resent", attempt=2)
            self.assertEqual([(e["event"], e["attempt"]) for e in output.events(Path(directory))], [("resent", 2)])

    def send(self, *more):
        """main() run as installer.step_bridge runs it, `more` added: what it
        gave deliver(), and the discovery-advert lookup."""
        import sys
        from pathlib import Path
        from unittest import mock
        import tools.espressif.serve_tasmota_ota as sender
        from tools.autoflash.runs import ROOT
        from tools.ewelink.ota_push import Delivery
        bridge = ROOT / installer.BRIDGE_OTA
        exists = Path.exists
        argv = ["serve_tasmota_ota.py", "--listen-ip", "192.0.2.2", "--device-ip", "192.0.2.3",
                "--firmware", str(bridge), "--email", "someone@example.com",
                "--expected-current-version", "1.2.0", "--device-id", "1000000001", sender.SAFETY_FLAG, *more]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(Path, "exists", autospec=True,
                               side_effect=lambda path: path.name != "RECOVERY_LOCK" and exists(path)), \
             mock.patch.dict("os.environ", {"EWELINK_PASSWORD": "pw", "EWELINK_SESSION":
                             '{"region": "us", "at": "run-token", "user": {"apikey": "a"}}'}), \
             mock.patch.object(sender, "target_id_from_mdns") as from_advert, \
             mock.patch.object(sender, "deliver", return_value=Delivery(sent=True, complete=True)) as deliver, \
             mock.patch("builtins.print"):
            self.assertEqual(sender.main(), 0)
        return deliver.call_args.kwargs, from_advert


class ReadoutTests(unittest.TestCase):
    """--readout-model: the bridge alone, wrapped as any model, to a device of a
    kind not known yet; nothing else, and a known kind only under its own name."""
    NAME = "FWSW-E32NEW-NEW-ESP32C3"

    def send(self, app: bytes, device: dict, model: str = NAME, given: str | None = None):
        import sys
        import tempfile
        from pathlib import Path
        from unittest import mock
        import tools.espressif.serve_tasmota_ota as sender
        from tools.espressif import build_vendor_ota
        from tools.ewelink.ota_push import Delivery
        checked = []

        def deliver(**kwargs):
            try:
                kwargs["check_device"](device, device.get("extra", {}).get("model", ""))
            except Exception as exc:  # noqa: BLE001 - what the check said
                checked.append(str(exc))
                return Delivery(sent=False, complete=False, error=str(exc))
            checked.append("passed")
            return Delivery(sent=True, complete=True)

        exists = Path.exists
        with tempfile.TemporaryDirectory() as tmp:
            wrapped = Path(tmp) / "bridge.ota"
            wrapped.write_bytes(build_vendor_ota.build_wrapper(app, version="4.4.5", model=model))
            argv = ["serve_tasmota_ota.py", "--listen-ip", "192.0.2.2", "--device-ip", "192.0.2.3",
                    "--firmware", str(wrapped), "--email", "someone@example.com",
                    "--expected-current-version", "1.0.0", "--mdns-capture", "mdns.json", sender.SAFETY_FLAG,
                    "--readout-model", given or model]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(Path, "exists", autospec=True,
                                   side_effect=lambda path: path.name != "RECOVERY_LOCK" and exists(path)), \
                 mock.patch.dict("os.environ", {"EWELINK_PASSWORD": "pw"}), \
                 mock.patch.object(sender, "target_id_from_mdns", return_value="1000000000"), \
                 mock.patch.object(sender, "deliver", side_effect=deliver), \
                 mock.patch("builtins.print"):
                return sender.main(), checked

    UNKNOWN = {"productModel": "NEWTHING", "extra": {"model": "SN-ESP32C3-NEW-01"}, "params": {"fwVersion": "1.0.0"}}

    def test_the_bridge_goes_to_a_device_not_known_yet(self):
        from tools.espressif import chips
        self.assertEqual(self.send(chips.ESP32C3.bridge_app(), self.UNKNOWN), (0, ["passed"]))

    def test_the_classic_esp32_bridge_too(self):
        from tools.espressif import chips
        self.assertEqual(self.send(chips.ESP32.bridge_app(), self.UNKNOWN, model="FWSW-E32NEW-NEW-ESP32D0WDV3"),
                         (0, ["passed"]))

    def test_anything_but_the_bridge_is_refused_before_the_cloud(self):
        from tools.espressif import chips
        app = chips.ESP32.file("recovery").read_bytes()
        self.assertEqual(self.send(app, self.UNKNOWN, model="FWSW-E32NEW-NEW-ESP32D0WDV3"), (2, []))

    def test_wrapped_as_another_name_than_given_is_refused(self):
        from tools.espressif import chips
        self.assertEqual(self.send(chips.ESP32C3.bridge_app(), self.UNKNOWN, model="FWSW-OTHER-ESP32C3", given=self.NAME),
                         (2, []))

    def test_a_known_kind_is_sent_it_only_as_its_own_name(self):
        from tools.autoflash import kinds
        from tools.espressif import chips
        s60 = {"productModel": "S60TPG", "extra": {"model": kinds.SONOFF_S60.stock_model},
               "params": {"fwVersion": "1.0.0"}}
        code, checked = self.send(chips.ESP32C3.bridge_app(), s60)
        self.assertEqual(code, 1)
        self.assertIn("not the Sonoff S60", checked[0])


class DescribeCloudTests(unittest.TestCase):
    def test_offline_and_online(self):
        from tools.ewelink.ota_push import describe_cloud
        self.assertIn("went offline", describe_cloud({"action": "sysmsg", "params": {"online": False}}, "1"))
        self.assertIn("back online", describe_cloud({"action": "sysmsg", "params": {"online": True}}, "1"))

    def test_upgrade_reply(self):
        from tools.ewelink.ota_push import describe_cloud
        self.assertEqual(describe_cloud({"error": 0, "sequence": "7"}, "7"), "Cloud: upgrade command accepted")
        self.assertIn("error 406", describe_cloud({"error": 406, "sequence": "7"}, "7"))

    def test_update_shows_state_but_never_identifiers(self):
        from tools.ewelink.ota_push import describe_cloud
        line = describe_cloud({"action": "update", "deviceid": "1001234abc", "apikey": "secret",
                               "params": {"fwVersion": "1.2.0", "ssid": "Home", "switch": "on"}}, "1")
        self.assertIn('"fwVersion": "1.2.0"', line)
        self.assertNotIn("1001234abc", line)
        self.assertNotIn("secret", line)
        self.assertNotIn("Home", line)

    def test_empty_messages_stay_quiet(self):
        from tools.ewelink.ota_push import describe_cloud
        self.assertIsNone(describe_cloud({"action": "update", "params": {}}, "1"))
        self.assertIsNone(describe_cloud({"error": 0, "sequence": "other"}, "1"))


if __name__ == "__main__":
    unittest.main()
