"""The upgrade command sent again when a device has lost it."""

import io
import socket
import struct
import threading
import time
import unittest
import urllib.request
from unittest import mock

from tools.ewelink import ota_push
from tools.ewelink.ota_push import Resending

BACK = {"action": "sysmsg", "params": {"online": True}}
AWAY = {"action": "sysmsg", "params": {"online": False}}


class ResendingTests(unittest.TestCase):
    def test_a_device_back_that_asks_for_nothing_has_lost_it(self):
        # Seen on the real cloud: offline as it fetched, a power cut, back online
        # and nothing more asked for, with a run waiting out its two minutes.
        watch = Resending(100.0)
        watch.heard(AWAY, 101.0)
        watch.heard(BACK, 130.0)
        self.assertFalse(watch.lost(125.0, 130.0 + ota_push.RESEND_SETTLE - 1))
        self.assertTrue(watch.lost(125.0, 130.0 + ota_push.RESEND_SETTLE))

    def test_a_device_back_and_fetching_is_left_alone(self):
        # It may say it is online with its download under way.
        watch = Resending(100.0)
        watch.heard(BACK, 101.0)
        self.assertFalse(watch.lost(102.0, 101.0 + ota_push.RESEND_SETTLE))
        self.assertFalse(watch.lost(102.0, 102.0 + ota_push.RESEND_QUIET - 1))

    def test_a_device_away_is_waited_for_however_long(self):
        watch = Resending(100.0)
        watch.heard(AWAY, 101.0)
        self.assertFalse(watch.lost(0.0, 1000.0))

    def test_a_device_that_fetched_part_is_not_sent_it_again_until_it_is_back(self):
        # Seen on the S61: cut off part way, the device said nothing of going
        # offline; sent again after 45 s quiet, it was still starting, a send wasted.
        watch = Resending(100.0)
        self.assertFalse(watch.lost(110.0, 110.0 + ota_push.RESEND_QUIET + 60))
        watch.heard(BACK, 200.0)
        self.assertTrue(watch.lost(110.0, 200.0 + ota_push.RESEND_SETTLE))

    def test_a_device_online_all_along_that_never_asks_has_lost_it(self):
        # A command gone down a connection eWeLink had not yet let go of.
        watch = Resending(100.0)
        self.assertFalse(watch.lost(0.0, 100.0 + ota_push.RESEND_QUIET - 1))
        self.assertTrue(watch.lost(0.0, 100.0 + ota_push.RESEND_QUIET))


class OfflineWaitTests(unittest.TestCase):
    def test_a_device_away_its_five_minutes_after_a_send_cut_off_is_waited_for(self):
        # Seen on the real cloud: offline about 300 s after a stopped send, then
        # back once it restarted by itself; waited for 180 s, the run gave up first.
        looks = iter([{"online": False}] * 30 + [{"online": True}])   # back after 310 s
        device = ota_push.until_online({"online": False}, lambda: next(looks), sleep=lambda _: None,
                                       say=lambda _: None)
        self.assertTrue(device["online"])


class LastAskedTests(unittest.TestCase):
    def test_a_request_counts_as_the_device_asking(self):
        server = ota_push.FirmwareServer(("127.0.0.1", 0), b"x" * 64, "127.0.0.1", say=lambda _: None, live=False)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.assertEqual(server.last_asked, 0.0)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/user1.bin", timeout=5) as answer:
            answer.read()
        self.assertGreater(server.last_asked, 0.0)


class DroppedRequestTests(unittest.TestCase):
    def test_a_request_the_device_drops_is_one_line_not_a_traceback_on_the_screen(self):
        # Seen in the Beken fault suite: the device's Wi-Fi gone a moment as it
        # downloads, its connection reset part way through a request.
        said = []
        server = ota_push.FirmwareServer(("127.0.0.1", 0), b"x" * 64, "127.0.0.1", say=said.append, live=False)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        screen = io.StringIO()
        with mock.patch("sys.stderr", screen):
            client = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
            client.sendall(b"GET /user1.bin HTTP/1.1\r\nRange: bytes=0-")
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # a reset, not a close
            client.close()
            for _ in range(50):
                if said:
                    break
                time.sleep(0.05)
        self.assertEqual(screen.getvalue(), "")
        self.assertEqual(len(said), 1)
        self.assertRegex(said[0], r"^  \[127\.0\.0\.1\] request dropped: ConnectionResetError")


class FakeCloud:
    """The app's connection: `script` the messages it brings, one to a look."""
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def send_json(self, message):
        self.sent.append(message)

    def receive_json(self, timeout):
        if self.script:
            message = self.script.pop(0)
            if message is not None:
                return message
        raise socket.timeout

    def close(self):
        pass


class SendingAgainTests(unittest.TestCase):
    def send(self, script, firmware=b"x" * 64, abandoned=lambda: False, **limits):
        cloud = FakeCloud(script)
        said, self.events = [], []
        device = {"deviceid": "1000000000", "apikey": "a", "online": True, "params": {"fwVersion": "1.2.0"},
                  "extra": {"model": "M"}}
        settings = {"RESEND_SETTLE": 0.0, "RESEND_QUIET": 3600.0, "RESEND_WAIT": 0.3} | limits
        with mock.patch.object(ota_push, "get_devices", return_value=[device]), \
                mock.patch.object(ota_push, "select_device", return_value=device), \
                mock.patch.object(ota_push, "app_socket", return_value=cloud), \
                mock.patch.multiple(ota_push, **settings):
            delivered = ota_push.deliver(
                account=ota_push.Account(lambda: ("eu", {"at": "t"})), device_id="1000000000",
                firmware=firmware, version="9", listen_ip="127.0.0.1", listen_port=0, device_ip="127.0.0.1",
                wait=0.6, say=said.append, live=False,
                event=lambda name, **fields: self.events.append((name, fields)), abandoned=abandoned)
        return delivered, cloud, said

    def test_a_device_back_gets_the_command_again_up_to_the_limit(self):
        _, cloud, said = self.send([AWAY, BACK, None, AWAY, BACK, None, AWAY, BACK])
        self.assertEqual(len(cloud.sent), ota_push.COMMAND_SENDS)
        self.assertEqual({message["action"] for message in cloud.sent}, {"upgrade"})
        self.assertIn("upgrade command sent again (attempt 2 of 3).", " ".join(said))
        # As the fault suites follow it (autoflash output.EVENTS).
        self.assertEqual(self.events, [("phase", {"phase": "serving"}), ("resent", {"attempt": 2}),
                                       ("resent", {"attempt": 3})])

    def test_serving_stops_once_the_tool_that_started_it_has_gone(self):
        # Seen in the fault suite: the tool killed as it started its server, the
        # server went on alone, holding its port, with no one to answer to.
        delivered, cloud, said = self.send([AWAY, BACK, None, AWAY, BACK], abandoned=lambda: True)
        self.assertIn("The tool that started this has gone: stopped serving.", " ".join(said))
        self.assertEqual(len(cloud.sent), 1)   # never sent again
        self.assertEqual(delivered.bytes_served, 0)

    def test_a_device_that_stays_away_is_sent_it_once(self):
        _, cloud, _ = self.send([AWAY])
        self.assertEqual(len(cloud.sent), 1)

    def test_a_send_again_refused_does_not_end_the_wait(self):
        delivered, cloud, _ = self.send([AWAY, BACK, None, {"sequence": "any", "error": 503}])
        self.assertGreaterEqual(len(cloud.sent), 2)
        self.assertIsNone(delivered.error)

    def test_the_first_send_refused_ends_it(self):
        with mock.patch.object(ota_push.time, "time", return_value=1.0):
            delivered, cloud, _ = self.send([{"sequence": "1000", "error": 504}])
        self.assertIn("rejected", delivered.error)
        self.assertEqual(len(cloud.sent), 1)


if __name__ == "__main__":
    unittest.main()
