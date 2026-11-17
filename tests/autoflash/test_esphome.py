"""ESPHome on a converted device, as the tool asks it: its page and title, its
signal, and its native API, plain and encrypted."""
import unittest
import time
from unittest import mock

from tools.autoflash import esphome


class ESPHomePageTests(unittest.TestCase):
    """ESPHome's page, as the device serves it with web_server's local: true."""

    def serve(self, body, headers):
        import http.server
        import threading

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}"

    def test_a_gzipped_page_is_recognised(self):
        import gzip
        page = b"<!doctype html><html><body><esp-app></esp-app></body></html>"
        host = self.serve(gzip.compress(page), {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        self.assertTrue(esphome.is_esphome(host))

    def test_a_page_damaged_on_its_way_is_no_answer_not_a_crash(self):
        # Seen in the fault suite: one byte changed in the gzipped page, and
        # decompressing it raised zlib.error - "something unexpected went
        # wrong" - where a dropped connection is simply asked again.
        import gzip
        page = bytes(range(256)) * 64 + b"<esp-app></esp-app>"
        damaged = bytearray(gzip.compress(page))
        damaged[len(damaged) // 2] ^= 0x5A
        host = self.serve(bytes(damaged), {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        self.assertFalse(esphome.is_esphome(host))

    def test_a_tasmota_page_is_not(self):
        host = self.serve(b"<html><body>Tasmota</body></html>", {"Content-Type": "text/html"})
        self.assertFalse(esphome.is_esphome(host))


class ESPHomeTitleTests(unittest.TestCase):
    """The name an ESPHome's page shows, which says the kind of device: read
    from its event stream, which a real ESPHome never closes."""

    def serve(self, events, then_hold=True):
        import http.server
        import threading
        release = threading.Event()
        self.addCleanup(release.set)

        class Events(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(events)
                self.wfile.flush()
                if then_hold:
                    release.wait(30)   # the stream stays open, as ESPHome's does

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Events)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}"

    def test_the_title_comes_from_the_first_ping_without_the_stream_ending(self):
        host = self.serve(b'retry: 30000\nid: 1\nevent: ping\ndata: {"title":"esphome abcdef",'
                          b'"comment":"Sonoff-S61","ota":true,"log":true,"lang":"en","uptime":12}\n\n')
        started = time.monotonic()
        self.assertEqual(esphome.title(host, timeout=10), ("esphome abcdef", "Sonoff-S61"))
        self.assertLess(time.monotonic() - started, 5)

    def test_the_title_is_read_from_the_ping_not_an_event_before_it(self):
        host = self.serve(b'event: state\ndata: {"id":"switch-relay","state":"ON"}\n\n'
                          b'event: ping\ndata: {"title":"esphome abcdef","comment":""}\n\n')
        self.assertEqual(esphome.title(host, timeout=10), ("esphome abcdef", None))   # no comment: none

    def test_a_stream_with_no_title_says_none(self):
        host = self.serve(b"event: state\ndata: {}\n\n" * 25, then_hold=False)
        self.assertEqual(esphome.title(host, timeout=5), (None, None))

    def test_nothing_listening_says_none(self):
        import socket
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
        self.assertEqual(esphome.title(f"127.0.0.1:{port}", timeout=2), (None, None))


class SignalSensorTests(unittest.TestCase):
    """An ESPHome built with the Wi-Fi signal sensor (as ours is) gives the
    device's signal; one without it gives nothing, and the run goes on."""

    def serve(self, pages):
        import http.server
        import threading
        asked = []

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                body = pages.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}", asked

    def test_the_sensor_gives_the_signal_in_whole_dbm(self):
        host, asked = self.serve({"/sensor/WiFi%20Signal":
                                  b'{"id":"sensor-wifi_signal","value":-58.4,"state":"-58 dBm"}'})
        self.assertEqual(esphome.sensor_signal(host), -58)
        self.assertEqual(asked, ["/sensor/WiFi%20Signal"])

    def test_no_sensor_or_no_reading_yet_gives_nothing(self):
        for pages in ({}, {"/sensor/WiFi%20Signal": b'{"value":NaN,"state":"NA"}'},
                      {"/sensor/WiFi%20Signal": b'{"state":"NA"}'}):
            host, _ = self.serve(pages)
            self.assertIsNone(esphome.sensor_signal(host), pages)

    def test_without_the_sensor_the_api_is_asked(self):
        with mock.patch.object(esphome, "sensor_signal", return_value=None), \
                mock.patch.object(esphome, "api_signal", return_value=-66) as api:
            self.assertEqual(esphome.signal("192.0.2.50"), -66)
        api.assert_called_once_with("192.0.2.50", key=None)
        with mock.patch.object(esphome, "sensor_signal", return_value=-58), \
                mock.patch.object(esphome, "api_signal") as api:
            self.assertEqual(esphome.signal("192.0.2.50"), -58)
        api.assert_not_called()


class MeterTests(unittest.TestCase):
    """The readings the end-of-run check takes: one meter's Voltage and Power,
    or a two-channel one's Power 1, its relay's (the Dual R3's)."""

    serve = SignalSensorTests.serve

    def test_a_meter_with_one_channel(self):
        host, _ = self.serve({"/sensor/Voltage": b'{"value":230.1}', "/sensor/Power": b'{"value":99.9}'})
        self.assertEqual(esphome.meter(host), {"Voltage": 230.1, "Power": 99.9})

    def test_a_meter_with_two_takes_the_first_channels_power(self):
        host, asked = self.serve({"/sensor/Voltage": b'{"value":230}', "/sensor/Power%201": b'{"value":100}',
                                  "/sensor/Power%202": b'{"value":0}'})
        self.assertEqual(esphome.meter(host), {"Voltage": 230.0, "Power": 100.0})
        self.assertNotIn("/sensor/Power%202", asked)


class ApiSignalTests(unittest.TestCase):
    """ESPHome's native API, spoken plainly: its configuration dump gives the
    signal, as an ESPHome without the signal sensor (ours, 1.0.0) prints it."""

    def serve(self, answer):
        """A fake ESPHome API that reads the client's request, then sends `answer`."""
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(answer)
                connection.recv(4096)  # its Disconnect, or the close

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    @staticmethod
    def log(text):
        from tools.autoflash.esphome import _frame, _varint
        return _frame(29, b"\x08\x04" + b"\x1a" + _varint(len(text)) + text)

    def test_the_configuration_dump_gives_the_signal(self):
        from tools.autoflash.esphome import _frame
        hello = _frame(2, b"\x08\x01\x10\x10\x1a\x082026.9.1")
        dump = (self.log(b"\x1b[0;35m[C][logger:250]: Logger:\x1b[0m")
                + self.log("[C][wifi:1297]:   SSID: 'Home'\n  Signal strength: -61 dB ▂▄▆_".encode()))
        port = self.serve(hello + _frame(7) + dump)
        self.assertEqual(esphome.api_signal("127.0.0.1", port), -61)

    def test_the_dump_names_the_network_with_the_signal(self):
        dump = self.log("[C][wifi:1297]:   SSID: 'Home-2G'\n  BSSID: 02:00:00:C4:0A:56\n"
                        "  Signal strength: -61 dB ▂▄▆_".encode())
        port = self.serve(dump)
        self.assertEqual(esphome.api_wifi("127.0.0.1", port), {"signal": -61, "ssid": "Home-2G"})

    def test_an_encrypted_api_gives_nothing(self):
        port = self.serve(b"\x01\x00\x08Bad indicator")
        self.assertIsNone(esphome.api_signal("127.0.0.1", port))

    def test_an_api_that_never_says_gives_nothing_in_time(self):
        port = self.serve(self.log(b"[C][logger:250]: Logger:"))
        self.assertIsNone(esphome.api_signal("127.0.0.1", port, timeout=1.0))

    def test_no_api_at_all_gives_nothing(self):
        import socket
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
            # Held, not listening, while asked: refused, and no other test can take it.
            self.assertIsNone(esphome.api_signal("127.0.0.1", port, timeout=2.0))


class WifiActionTests(unittest.TestCase):
    """Our ESPHome gives the Wi-Fi it is on through its Wi-Fi action: listed,
    then called, its answer the details as JSON."""

    def serve(self, *steps):
        """A fake ESPHome API: for each (expected, answer), it waits until what it
        has been sent holds `expected`, then sends `answer`."""
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)

        def run():
            connection, _ = server.accept()
            received = b""
            with connection:
                for expected, answer in steps:
                    while expected not in received:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        received += chunk
                    connection.sendall(answer)
                connection.recv(4096)

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    @staticmethod
    def listed(*actions):
        import struct
        from tools.autoflash.esphome import _frame, _varint
        out = b""
        for name, key in actions:
            out += _frame(41, b"\x0a" + _varint(len(name)) + name + b"\x15" + struct.pack("<I", key))
        return out + _frame(19)

    @staticmethod
    def answer(success, data=b""):
        from tools.autoflash.esphome import _frame, _varint
        return _frame(131, b"\x08\x01" + (b"\x10\x01" if success else b"")
                      + (b"\x22" + _varint(len(data)) + data if data else b""))

    LIST = b"\x00\x00\x0b"  # the request to list entities

    @staticmethod
    def call(key):
        import struct
        return b"\x0d" + struct.pack("<I", key)  # the action named by its key

    def test_the_action_gives_the_name_and_password(self):
        # Answered only once it is called by the key it was listed with - by
        # its name, or the one it first had.
        for name in (b"autoflash_wifi", b"s60_wifi"):
            port = self.serve((self.LIST, self.listed((b"other", 7), (name, 0xCAFEF00D))),
                              (self.call(0xCAFEF00D), self.answer(True, b'{"ssid":"Home-2G","password":"pw"}')))
            self.assertEqual(esphome.api_wifi_details("127.0.0.1", port, timeout=3.0), ("Home-2G", "pw"), name)

    def test_an_esphome_without_the_action_gives_nothing(self):
        port = self.serve((self.LIST, self.listed((b"other", 7))))
        self.assertIsNone(esphome.api_wifi_details("127.0.0.1", port, timeout=2.0))

    def test_a_failed_call_gives_nothing(self):
        port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))), (self.call(1), self.answer(False)))
        self.assertIsNone(esphome.api_wifi_details("127.0.0.1", port, timeout=2.0))

    def test_an_answer_not_of_json_gives_nothing(self):
        from tools.autoflash.esphome import _frame
        for data in (b"not json", b'["Home-2G"]', b'{"ssid": 7}'):
            port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))), (self.call(1), self.answer(True, data)))
            self.assertIsNone(esphome.api_wifi_details("127.0.0.1", port, timeout=2.0), data)
        port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))),
                          (self.call(1), _frame(131, b"\x08\x01\x10\x01\x20\x07")))  # its data a number
        self.assertIsNone(esphome.api_wifi_details("127.0.0.1", port, timeout=2.0))

    def test_an_answer_to_another_call_is_passed_over(self):
        from tools.autoflash.esphome import _frame, _varint
        other = b'{"ssid":"Elsewhere","password":"x"}'
        port = self.serve((self.LIST, self.listed((b"s60_wifi", 1))),
                          (self.call(1), _frame(131, b"\x08\x02\x10\x01\x22" + _varint(len(other)) + other)
                           + self.answer(True, b'{"ssid":"Home-2G","password":"pw"}')))
        self.assertEqual(esphome.api_wifi_details("127.0.0.1", port, timeout=3.0), ("Home-2G", "pw"))

    def test_an_encrypted_api_says_so(self):
        port = self.serve((b"", b"\x01\x00\x08Bad indicator"))
        with self.assertRaises(esphome.ApiLocked) as locked:
            esphome.api_wifi_details("127.0.0.1", port, timeout=2.0)
        self.assertFalse(locked.exception.wrong)  # no key given: none to be wrong


class CalibrationActionTests(unittest.TestCase):
    """Our ESPHome takes the device's factory calibration through its
    calibration action, called with the 18 numbers as a list of floats and
    no answer wanted."""

    serve, LIST = WifiActionTests.serve, WifiActionTests.LIST
    listed, answer, call = (staticmethod(WifiActionTests.listed), staticmethod(WifiActionTests.answer),
                            staticmethod(WifiActionTests.call))
    NUMBERS = [0.0, 1010000.0, 0.0] * 6

    def test_the_numbers_go_as_one_list_of_floats(self):
        import socket
        import struct
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        received = bytearray()

        def run():
            connection, _ = server.accept()
            with connection:
                while self.LIST not in received:
                    received.extend(connection.recv(4096))
                connection.sendall(self.listed((b"autoflash_wifi", 1), (b"autoflash_calibration", 0xCAFEF00D)))
                while chunk := connection.recv(4096):
                    received.extend(chunk)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(esphome.api_give_calibration("127.0.0.1", self.NUMBERS, server.getsockname()[1],
                                                        timeout=3.0))
        thread.join(3)
        argument = b"".join(b"\x45" + struct.pack("<f", n) for n in self.NUMBERS)
        self.assertIn(self.call(0xCAFEF00D) + b"\x12" + bytes([len(argument)]) + argument, bytes(received))

    def test_an_esphome_without_the_action_gives_nothing(self):
        # 1.3.0 and before.
        port = self.serve((self.LIST, self.listed((b"autoflash_wifi", 1))))
        self.assertFalse(esphome.api_give_calibration("127.0.0.1", self.NUMBERS, port, timeout=2.0))


class EncryptedApiTests(unittest.TestCase):
    """An ESPHome whose API has an encryption key, spoken to as ESPHome's
    api_frame_helper_noise.cpp answers: the hello, the handshake, then each
    message sealed in a frame of its own.  Without its key, or with another,
    it says so; given a key, an ESPHome whose API is plain is spoken to
    plainly."""

    KEY = bytes(range(32))
    WIFI = b'{"ssid":"Home-2G","password":"pw"}'
    DUMP = "[C][wifi:1297]:   SSID: 'Home-2G'\n  Signal strength: -61 dB".encode()

    def serve(self, *, plain_first=False):
        """The fake, for two connections at most: encrypted with KEY - or, with
        `plain_first`, a plain API, which answers the encrypted hello "Bad
        indicator byte" and closes, and then speaks plainly."""
        import socket
        import threading
        from tools.autoflash import noise
        from tools.autoflash.esphome import _frame, _read_varint, _varint
        import struct
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        self.addCleanup(server.close)
        listed = (b"\x0a" + _varint(8) + b"s60_wifi" + b"\x15" + struct.pack("<I", 0xCAFEF00D))
        answered = b"\x08\x01\x10\x01\x22" + _varint(len(self.WIFI)) + self.WIFI
        log = b"\x08\x04\x1a" + _varint(len(self.DUMP)) + self.DUMP
        # What each request is answered with: its kind, then (kind, payload)s.
        answers = {1: [(2, b"\x08\x01\x10\x10")], 11: [(41, listed), (19, b"")],
                   42: [(131, answered)], 28: [(29, log)]}

        def exactly(connection, size):
            data = b""
            while len(data) < size:
                piece = connection.recv(size - len(data))
                if not piece:
                    raise ConnectionError
                data += piece
            return data

        def plainly(connection):
            read = lambda size: exactly(connection, size)  # noqa: E731
            while True:
                assert read(1) == b"\x00"
                size, kind = _read_varint(read), _read_varint(read)
                read(size)
                for answer, payload in answers.get(kind, []):
                    connection.sendall(_frame(answer, payload))

        def sealed(connection):
            read = lambda size: exactly(connection, size)  # noqa: E731
            try:
                hello = noise.read_frame(read)
            except noise.NoiseError:  # a plain client
                connection.sendall(noise.frame(b"\x01Bad indicator byte"))
                return
            assert hello == b""
            first = noise.read_frame(read)
            connection.sendall(noise.frame(b"\x01sonoff-s60\x0012:34:56:AB:CD:EF\x00"))
            device = noise.Handshake(self.KEY, b"NoiseAPIInit\x00\x00", initiator=False)
            try:
                device.read_message(first[1:])
            except noise.NoiseError:
                connection.sendall(noise.frame(b"\x01" + noise.WRONG_KEY_REASON.encode()))
                return
            connection.sendall(noise.frame(b"\x00" + device.write_message()))
            sending, receiving = device.split()
            while True:
                message = receiving.decrypt(noise.read_frame(read))
                for answer, payload in answers.get(int.from_bytes(message[:2], "big"), []):
                    connection.sendall(noise.frame(sending.encrypt(
                        answer.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload)))

        def run():
            for turn in range(2):
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                with connection:
                    try:
                        if plain_first and turn == 0:
                            connection.recv(4096)
                            connection.sendall(b"\x00Bad indicator byte")
                        elif plain_first:
                            plainly(connection)
                        else:
                            sealed(connection)
                    except (ConnectionError, OSError, AssertionError):
                        pass

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1]

    def test_with_its_key_the_action_gives_the_details(self):
        port = self.serve()
        self.assertEqual(esphome.api_wifi_details("127.0.0.1", port, timeout=3.0, key=self.KEY),
                         ("Home-2G", "pw"))

    def test_with_its_key_the_dump_gives_the_signal_and_network(self):
        port = self.serve()
        self.assertEqual(esphome.api_wifi("127.0.0.1", port, timeout=3.0, key=self.KEY),
                         {"signal": -61, "ssid": "Home-2G"})

    def test_without_its_key_or_with_another_it_says_so(self):
        port = self.serve()
        with self.assertRaises(esphome.ApiLocked) as locked:
            esphome.api_wifi_details("127.0.0.1", port, timeout=3.0)
        self.assertFalse(locked.exception.wrong)
        port = self.serve()
        with self.assertRaises(esphome.ApiLocked) as locked:
            esphome.api_wifi_details("127.0.0.1", port, timeout=3.0, key=bytes(32))
        self.assertTrue(locked.exception.wrong)
        self.assertIsNone(esphome.api_signal("127.0.0.1", self.serve(), timeout=3.0))  # just none

    def test_locked_even_when_it_hangs_up_before_the_question(self):
        # A device quicker than the tool: it answers the plain hello and hangs up
        # before the next write, which is refused - its answer is still read.
        import time
        send = esphome._Api.send

        def later(api, *messages):
            if messages[0][0] != esphome._HELLO:
                time.sleep(0.3)
            send(api, *messages)

        with mock.patch.object(esphome._Api, "send", later), \
             self.assertRaises(esphome.ApiLocked):
            esphome.api_wifi_details("127.0.0.1", self.serve(), timeout=3.0)

    def test_a_key_given_for_a_plain_api_is_not_used(self):
        port = self.serve(plain_first=True)
        self.assertEqual(esphome.api_wifi_details("127.0.0.1", port, timeout=3.0, key=self.KEY),
                         ("Home-2G", "pw"))

    def test_access_says_what_it_takes(self):
        self.assertEqual(esphome.api_access("127.0.0.1", self.KEY, self.serve()), esphome.API_OPEN)
        self.assertEqual(esphome.api_access("127.0.0.1", None, self.serve()), esphome.API_KEY)
        self.assertEqual(esphome.api_access("127.0.0.1", bytes(32), self.serve()), esphome.API_KEY)
        self.assertEqual(esphome.api_access("127.0.0.1", self.KEY, self.serve(plain_first=True)),
                         esphome.API_PLAIN)


class ApiConversationTests(unittest.TestCase):
    """Each question put to ESPHome's API, as the device hears it: hello and
    connect first, its pings answered, and a goodbye at the end."""

    def serve(self, request, answer):
        """A fake ESPHome API that waits for `request`, pings, sends `answer`,
        then keeps all it is sent until the client hangs up."""
        import socket
        import threading
        from tools.autoflash.esphome import _frame
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        heard = bytearray()

        def run():
            connection, _ = server.accept()
            with connection:
                connection.settimeout(5)
                sent = False
                while True:
                    try:
                        chunk = connection.recv(4096)
                    except OSError:
                        return
                    if not chunk:
                        return
                    heard.extend(chunk)
                    if not sent and request in heard:
                        connection.sendall(_frame(7) + answer)
                        sent = True

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return server.getsockname()[1], thread, heard

    @staticmethod
    def kinds(heard):
        from tools.autoflash.esphome import _read_varint
        data, at, kinds = bytes(heard), 0, []

        def read(size):
            nonlocal at
            at += size
            return data[at - size:at]
        while at < len(data):
            assert read(1) == b"\x00"
            size, kind = _read_varint(read), _read_varint(read)
            read(size)
            kinds.append(kind)
        return kinds

    def test_each_question_says_hello_answers_pings_and_says_goodbye(self):
        from tools.autoflash.esphome import _frame
        hello_answer = _frame(2, b"\x08\x01\x10\x0a")
        log = ApiSignalTests.log(b"[C][wifi:1297]:   SSID: 'Home'\n  Signal strength: -61 dB")
        info = DeviceInfoTests.info()
        listed = WifiActionTests.listed((b"autoflash_wifi", 1))
        answer = WifiActionTests.answer(True, b'{"ssid":"Home-2G","password":"pw"}')
        for name, ask, request, reply, kinds in (
                ("access", esphome.api_access, b"\x00\x00\x03", hello_answer, []),
                ("signal", esphome.api_wifi, b"\x00\x04\x1c", log, [28]),
                ("info", esphome.api_device_info, b"\x00\x00\x09", info, [9]),
                ("action", esphome.api_wifi_details, b"\x00\x00\x0b", listed + answer, [11, 42])):
            with self.subTest(name):
                port, thread, heard = self.serve(request, reply)
                self.assertIsNotNone(ask("127.0.0.1", port=port, timeout=3.0))
                thread.join(5)
                self.assertEqual(self.kinds(heard), [1, 3] + kinds[:1] + [8] + kinds[1:] + [5])

    def test_it_is_asked_on_esphomes_api_port(self):
        with mock.patch.object(esphome.socket, "create_connection", side_effect=OSError("refused")) as dialled:
            self.assertIsNone(esphome.api_wifi("192.0.2.50"))
        self.assertEqual(dialled.call_args.args[0], ("192.0.2.50", 6053))

    def test_numbers_and_messages_are_read_as_protobuf_writes_them(self):
        from tools.autoflash.esphome import _fields, _varint
        self.assertEqual(_varint(300), b"\xac\x02")
        self.assertEqual(_fields(b"\x08\xac\x02\x12\x02ab"), {1: 300, 2: b"ab"})
        with self.assertRaisesRegex(ValueError, "cut short"):
            _fields(b"\x12\x05ab")


class DeviceInfoTests(unittest.TestCase):
    """What ESPHome says it is, asked through its API."""

    serve = WifiActionTests.serve

    @staticmethod
    def info(name=b"esphome-abcdef", version=b"2026.10.0", built=b"2026-10-26 17:26:39 +0000",
             project=b"liberator.sonoff-s60", release=b"1.4.0"):
        from tools.autoflash.esphome import _frame, _varint
        fields = b"".join(bytes([number << 3 | 2]) + _varint(len(value)) + value
                          for number, value in ((2, name), (4, version), (5, built), (8, project), (9, release)) if value)
        return _frame(10, b"\x08\x00" + fields)

    def test_it_says_its_project_and_its_version(self):
        port = self.serve((b"\x00\x00\x09", self.info()))
        self.assertEqual(esphome.api_device_info("127.0.0.1", port, timeout=3.0),
                         {"name": "esphome-abcdef", "esphome_version": "2026.10.0",
                          "built": "2026-10-26 17:26:39 +0000", "project_name": "liberator.sonoff-s60",
                          "project_version": "1.4.0", "encrypted": False})

    def test_one_built_as_no_project_says_none(self):
        port = self.serve((b"\x00\x00\x09", self.info(project=b"", release=b"")))
        info = esphome.api_device_info("127.0.0.1", port, timeout=3.0)
        self.assertEqual((info["project_name"], info["project_version"]), ("", ""))

    def test_an_encrypted_api_says_so(self):
        port = self.serve((b"", b"\x01\x00\x08Bad indicator"))
        with self.assertRaises(esphome.ApiLocked):
            esphome.api_device_info("127.0.0.1", port, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
