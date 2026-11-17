import http.server
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from tools.autoflash import setup_network


class FakeDevice(http.server.ThreadingHTTPServer):
    """Answers like the bridge or like Tasmota's setup mode, as configured."""

    daemon_threads = True

    def __init__(self, behaviour: str):
        super().__init__(("127.0.0.1", 0), FakeDeviceHandler)
        self.behaviour = behaviour
        self.uploaded = b""
        self.wifi: dict[str, str] = {}
        self.page_views = 0  # each one is a Wi-Fi scan on a real device
        self.settings: tuple[dict, bytes] | None = None
        self.root_views = 0
        self.headers: list = []  # each POST's
        self.asked: list[tuple[float, str]] = []  # when each Wi-Fi page was asked, and how

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"


class FakeDeviceHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        device: FakeDevice = self.server
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            device.root_views += 1
            self.reply("<title>Autoflash bridge</title>" if device.behaviour == "bridge"
                       else "<title>Tasmota</title>")
            return
        if url.path == "/wi":
            device.asked.append((time.monotonic(), url.query))
            query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            if "save" in query:
                device.wifi = {"ssid": query["s1"][0], "password": query["p1"][0]}
                self.reply("<h3>Trying to connect device to network</h3>")
                return
            device.page_views += 1
            if device.page_views == 1 or device.behaviour == "tasmota-silent":
                self.reply("<h3>Trying to connect device to network<br>Home</h3>")
            elif device.behaviour == "tasmota-rejects":
                self.reply("<h3>Connect failed to Home<br>Please, check your credentials</h3>")
            else:
                self.reply("Successful WiFi Connection<br>Redirecting to new device's IP "
                           "address<br><a href='http://192.0.2.77'>192.0.2.77</a>")
            return
        self.reply("not found", 404)

    def do_POST(self):
        device: FakeDevice = self.server
        url = urllib.parse.urlsplit(self.path)
        device.headers.append(self.headers)
        if url.path == "/tasmota-settings":
            if device.behaviour == "bridge-v4":
                self.reply("Nothing matches the given URI", 404)
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            device.settings = (dict(urllib.parse.parse_qsl(url.query)), body)
            if device.behaviour == "bridge-not-joined":
                self.reply("The bridge has not joined a network. Nothing was saved.", 409)
            else:
                self.reply("Tasmota settings saved with the home network.")
            return
        if self.headers.get("Expect"):
            self.reply("Expect is not supported", 417)
            return
        length = int(self.headers["Content-Length"])
        device.uploaded = self.rfile.read(length)
        if device.behaviour == "bridge-refuses":
            self.reply("The image is not for this chip.\n")
            return
        self.reply("Upload verified. Rebooting into the new application...\n")


class SetupNetworkTests(unittest.TestCase):
    def start(self, behaviour: str) -> FakeDevice:
        device = FakeDevice(behaviour)
        thread = threading.Thread(target=device.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (device.shutdown(), device.server_close()))
        return device

    def test_joining_is_noticed_by_the_page_marker(self):
        device = self.start("bridge")
        self.assertTrue(setup_network.wait_for_page(
            setup_network.BRIDGE_PAGE_MARKERS[0], host=device.host, timeout=5, poll=0.1))
        self.assertIs(setup_network.wait_for_page(
            setup_network.BRIDGE_PAGE_MARKERS[0], host="127.0.0.1:9", timeout=0.3, poll=0.1), False)

    def test_a_page_without_the_marker_is_asked_again_only_each_poll(self):
        # Each look at a device's page on its own network is a Wi-Fi scan.
        device = self.start("tasmota")
        ticks = []
        self.assertIs(setup_network.wait_for_page(
            setup_network.BRIDGE_PAGE_MARKERS[0], host=device.host, timeout=0.5, poll=0.2,
            tick=ticks.append), False)
        self.assertLessEqual(device.root_views, 4)
        self.assertEqual(len(ticks), device.root_views)

    def test_the_bridge_is_known_by_its_page_under_either_title(self):
        # 4.4.4's, and 4.4.3's before it: a device a run of an older checkout left
        # on its installer still shows the old one.
        for title in ("Autoflash bridge", "S60 OTA Bridge"):
            self.assertTrue(setup_network.is_bridge_page(f"<title>{title}</title><h1>{title}</h1>"))
        self.assertFalse(setup_network.is_bridge_page("<title>Tasmota</title>"))
        self.assertFalse(setup_network.is_bridge_page(None))

    def test_the_bridge_receives_the_exact_image(self):
        device = self.start("bridge")
        reply = setup_network.upload_to_bridge(bytes(range(256)) * 300, host=device.host, timeout=10)
        self.assertIn("Upload verified", reply)
        self.assertEqual(device.uploaded, bytes(range(256)) * 300)
        # As the documented curl command sends it.
        (headers,) = device.headers
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertIsNone(headers["Accept-Encoding"])
        self.assertIsNone(headers["Expect"])

    def test_an_image_the_bridge_does_not_verify_is_refused(self):
        device = self.start("bridge-refuses")  # a 200, but not its word that it took it
        with self.assertRaises(setup_network.SetupError) as refused:
            setup_network.upload_to_bridge(b"\xe9" * 1024, host=device.host, timeout=10)
        # Without the bridge's full stop, as the run's own sentence follows
        # ("... selected.. The device still has the installer", it once said).
        self.assertEqual(str(refused.exception), "the bridge refused the image: The image is not for this chip")

    def test_the_bridge_is_on_port_80_and_each_connection_is_closed(self):
        connection = mock.MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b"Upload verified. Tasmota settings saved."
        with mock.patch.object(setup_network.http.client, "HTTPConnection", return_value=connection) as made:
            setup_network.upload_to_bridge(b"\xe9" * 16, host="192.0.2.50")
            setup_network.send_tasmota_settings(b"T", host="192.0.2.50", layout={})
        self.assertEqual([call.args[:2] for call in made.call_args_list], [("192.0.2.50", 80)] * 2)
        self.assertEqual(connection.close.call_count, 2)

    def test_an_upload_the_bridge_stops_taking_is_given_up(self):
        # The network gone part way: noticed after the stall limit, not left
        # hanging, so the run can try again (through the bridge's own network).
        import socket
        import threading
        import time
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        held = []
        threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
        started = time.monotonic()
        with self.assertRaisesRegex(setup_network.SetupError, "did not complete"):
            setup_network.upload_to_bridge(b"\xe9" * 8_000_000, host=f"127.0.0.1:{server.getsockname()[1]}",
                                           timeout=0.5)
        self.assertLess(time.monotonic() - started, 10)
        for connection, _ in held:
            connection.close()
        self.assertLessEqual(setup_network.UPLOAD_STALL, 120)

    def test_wifi_details_are_sent_and_the_new_address_is_read(self):
        device = self.start("tasmota")
        outcome = setup_network.send_wifi(
            "Home & Garden", "p@ss word", host=device.host, settle=0.05, poll=0.05)
        self.assertEqual(outcome, ("saved", "192.0.2.77"))
        self.assertEqual(device.wifi, {"ssid": "Home & Garden", "password": "p@ss word"})
        # A look while still testing, then the verdict - no rapid polling,
        # which on a real device scans and knocks the computer off its network.
        self.assertEqual(device.page_views, 2)

    def test_the_page_is_left_alone_while_tasmota_tests_the_details(self):
        # Asked meanwhile, it would scan, and break the test of the Wi-Fi.
        device = self.start("tasmota")
        setup_network.send_wifi("Home", "pw", host=device.host, settle=0.3, poll=0.05)
        (saved, _), (verdict, _) = device.asked[:2]
        self.assertGreaterEqual(verdict - saved, 0.3)

    def test_a_failed_connection_is_reported_not_saved(self):
        device = self.start("tasmota-rejects")
        outcome = setup_network.send_wifi(
            "Home", "wrong", host=device.host, settle=0.05, poll=0.05)
        self.assertEqual(outcome, ("rejected", None))

    def test_no_verdict_within_the_time_is_unknown(self):
        device = self.start("tasmota-silent")  # keeps answering "trying to connect"
        outcome = setup_network.send_wifi(
            "Home", "pw", host=device.host, settle=0.0, poll=0.1, timeout=0.3)
        self.assertEqual(outcome, ("unknown", None))
        self.assertLessEqual(device.page_views, 5)  # asked again each poll, not as fast as it answers

    def test_no_setup_page_is_an_error(self):
        with self.assertRaises(setup_network.SetupError):
            setup_network.send_wifi("Home", "pw", host="127.0.0.1:9", settle=0.0, poll=0.1)

    def test_the_template_and_its_layout_reach_the_bridge(self):
        device = self.start("bridge")
        layout = {"pool": "23", "pool_size": "699", "ssid": "4", "password": "6", "crc": "4092"}
        reply = setup_network.send_tasmota_settings(b"T" * 4096, host=device.host, layout=layout)
        self.assertIn("saved", reply)
        self.assertEqual(device.settings, (layout, b"T" * 4096))
        self.assertEqual(device.headers[0]["Content-Type"], "application/octet-stream")

    def test_a_bridge_that_cannot_save_says_why(self):
        for behaviour, words in (("bridge-v4", "older than v5"), ("bridge-not-joined", "not joined")):
            device = self.start(behaviour)
            with self.assertRaisesRegex(setup_network.SetupError, words):
                setup_network.send_tasmota_settings(b"T" * 4096, host=device.host, layout={})

    def test_joining_is_detected_without_requesting_a_page(self):
        device = self.start("tasmota")
        hostname, port = device.host.split(":")
        self.assertTrue(setup_network.port_open(hostname, int(port)))
        self.assertEqual(device.page_views, 0)
        self.assertIs(setup_network.port_open("127.0.0.1", 9, timeout=0.3), False)
        with mock.patch.object(setup_network.socket, "create_connection") as connect:
            setup_network.port_open("192.0.2.50")
        self.assertEqual(connect.call_args.args[0], ("192.0.2.50", 80))


if __name__ == "__main__":
    unittest.main()
