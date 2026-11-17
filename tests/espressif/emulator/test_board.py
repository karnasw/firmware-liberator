import io
import json
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tests.espressif.emulator.test_redact import after_an_earlier_release, under_an_earlier_release
from tools.autoflash import calibration, kinds
from tools.espressif.emulator import board

S60, POW_ELITE, DUALR3, POWCT = (board.find_kind(key) for key in
                                 ("sonoff-s60", "sonoff-pow-elite", "sonoff-dualr3", "sonoff-powct"))


def word(frame: bytes, at: int) -> int:
    return int.from_bytes(frame[at:at + 3], "big")


def esphome_reads(frame: bytes) -> dict[str, float]:
    """A CSE7766 frame as ESPHome's cse7766 reads it (esphome/components/cse7766)."""
    assert frame[0] in (0x55, 0xAA) or frame[0] & 0xF0 == 0xF0, frame[0]
    assert frame[1] == 0x5A and frame[23] == sum(frame[2:23]) & 0xFF
    assert not (frame[0] & 0xF0 == 0xF0 and frame[0] & 0xD), "a frame ESPHome drops"
    adj, readings = frame[20], {}
    if adj & 0x40:
        readings["voltage"] = word(frame, 2) / word(frame, 5)
    if frame[0] & 0xF0 == 0xF0 and frame[0] & 2:
        readings["power"] = 0.0
    elif adj & 0x10:
        readings["power"] = word(frame, 14) / word(frame, 17)
    if adj & 0x20:
        calculated = readings.get("power", 0.0) / readings.get("voltage", 1.0)
        readings["current"] = word(frame, 8) / word(frame, 11) if calculated > 0.05 else 0.0
    return readings


class WiringTests(unittest.TestCase):
    def test_each_meter_from_its_template(self):
        self.assertEqual(board.Wiring.of(S60), board.Wiring("cse7766", 7, None, (board.Channel(4),)))
        self.assertEqual(board.Wiring.of(POW_ELITE), board.Wiring("cse7766", 16, None, (board.Channel(13),)))
        # ESPHome's configs agree: the Dual R3's UART tx GPIO25, rx GPIO26, relays GPIO27 and GPIO14.
        self.assertEqual(board.Wiring.of(DUALR3),
                         board.Wiring("cse7761", 26, 25, (board.Channel(27), board.Channel(14))))

    def test_the_powct_clamp_is_not_switched_by_its_relay(self):
        wiring = board.Wiring.of(POWCT)
        self.assertEqual((wiring.chip, wiring.output_pin, wiring.input_pin), ("cse7761", 25, 26))
        self.assertTrue(wiring.current_transformer)
        self.assertEqual(wiring.channels[0].load(0), board.LOAD)
        self.assertEqual(wiring.channels[1].load(0xFFFFFFFF).watts, 0)

    def test_no_meter_where_the_kind_has_none(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                self.assertEqual(board.Wiring.of(kind) is not None, kind.has_meter and kind.chip in board.SOCS)


class Cse7766Tests(unittest.TestCase):
    def setUp(self):
        self.meter = board.Cse7766(board.Wiring.of(S60))

    def test_a_load_while_the_relay_is_on(self):
        reads = esphome_reads(self.meter.frame(1 << 4, 0.0))
        self.assertAlmostEqual(reads["voltage"], 230, delta=0.5)
        self.assertAlmostEqual(reads["power"], 100, delta=0.1)
        self.assertAlmostEqual(reads["current"], 100 / 230, delta=0.01)

    def test_none_while_it_is_off(self):
        frame = self.meter.frame(1 << 5, 0.0)   # another pin high, not the relay's
        self.assertEqual(frame[0], 0xF2)   # power cycle out of range
        reads = esphome_reads(frame)
        self.assertEqual((reads["power"], reads["current"]), (0.0, 0.0))
        self.assertAlmostEqual(reads["voltage"], 230, delta=0.5)

    def test_tasmota_takes_the_power_as_its_cycle_changes(self):
        # xnrg_02_cse7766.ino counts a power reading only once its cycle differs
        # from the first: so too in a firmware that reads only every other frame.
        cycles = [word(self.meter.frame(1 << 4, n * 0.055), 17) for n in range(40)]
        self.assertGreater(len(set(cycles[::2])), 1)
        self.assertGreater(len(set(cycles[1::2])), 1)

    def test_its_coefficients_are_none_of_tasmotas_defaults(self):
        frame = self.meter.frame(0, 0.0)
        chip = {"VoltageCal": word(frame, 2) // 100, "CurrentCal": word(frame, 8), "PowerCal": word(frame, 14) // 1000}
        for name, default in calibration.TASMOTA_DEFAULTS.items():
            self.assertNotEqual(chip[name], default, name)

    def test_its_pulses_count_the_energy(self):
        for n in range(101):
            frame = self.meter.frame(1 << 4, n * 0.1)
        wh = int.from_bytes(frame[21:23], "big") * board.Cse7766.POWER_COEFFICIENT / 1e6 / 3600
        self.assertAlmostEqual(wh, 100 * 10 / 3600, delta=0.002)


class Cse7761Tests(unittest.TestCase):
    """The exchange Tasmota's xnrg_19_cse7761.ino and ESPHome's cse7761 have with the chip."""

    def setUp(self):
        self.meter = board.Cse7761(board.Wiring.of(DUALR3))
        self.outputs = 0

    def write(self, register: int, value: int | None = None) -> bytes:
        frame = bytes([0xA5, register])
        if value is not None:
            frame += value.to_bytes(1 if value < 0xFF else 2, "big")
            frame += bytes([~sum(frame) & 0xFF])
        return self.meter.feed(frame, self.outputs, time.monotonic())

    def read(self, register: int, size: int) -> int:
        answer = self.write(register)
        self.assertEqual(len(answer), size + 1)
        self.assertEqual(answer[-1], ~(0xA5 + register + sum(answer[:-1])) & 0xFF)
        return int.from_bytes(answer[:-1], "big")

    def start(self, syscon: int) -> list[int]:
        self.write(0xEA, 0x96)
        self.assertEqual(self.read(0x00, 2), 0x0A04)
        coefficients = [self.read(0x70 + n, 2) for n in range(8)]
        self.assertEqual(self.read(0x6F, 2), ~(0xFFFF + sum(coefficients)) & 0xFFFF)
        self.write(0xEA, 0xE5)
        self.assertTrue(self.read(0x43, 1) & 0x10)
        self.write(0x80, syscon)
        self.write(0xEA, 0xDC)
        self.assertFalse(self.read(0x43, 1) & 0x10)
        return coefficients

    def test_the_firmwares_start_up(self):
        self.start(0xFF04)
        self.assertEqual(self.read(0x00, 2), 0xFF04)
        self.write(0x80, 0x1234)   # writes closed: not taken
        self.assertEqual(self.read(0x00, 2), 0xFF04)

    def test_each_channel_follows_its_relay(self):
        coefficients = self.start(0xFF04)
        volts = self.read(0x26, 3) / (0x400000 * 100 // coefficients[2])
        self.assertAlmostEqual(volts, 230, delta=0.1)
        self.assertLess(self.read(0x24, 3), 1600)   # under the firmwares' no-load threshold
        self.outputs = 1 << 27
        amps = self.read(0x24, 3) / ((0x800000 * 100 // coefficients[0]) * 10)
        watts = self.read(0x2C, 4) / (0x80000000 // coefficients[3])
        self.assertAlmostEqual(amps, 100 / 230, delta=0.001)
        self.assertAlmostEqual(watts, 100, delta=0.01)
        self.assertEqual(self.read(0x25, 3), 0)
        self.outputs = 1 << 14
        self.assertEqual(self.read(0x2C, 4), 0)
        self.assertGreater(self.read(0x2D, 4), 0)

    def test_a_frame_with_a_wrong_checksum_is_ignored(self):
        self.start(0xFF04)
        self.write(0xEA, 0xE5)
        self.meter.feed(bytes([0xA5, 0x80, 0x12, 0x34, 0x00]), 0, 0.0)
        self.assertEqual(self.read(0x00, 2), 0xFF04)

    def test_the_powct_clamp_reads_at_its_own_scale_whatever_the_relay(self):
        self.meter = board.Cse7761(board.Wiring.of(POWCT))
        coefficients = self.start(0xFE00)
        amps = self.read(0x24, 3) / ((0x800000 * 100 // (coefficients[0] * 5)) * 10)
        self.assertAlmostEqual(amps, 100 / 230, delta=0.001)

    def test_a_gain_the_board_is_not_calibrated_at_reads_wrong(self):
        self.meter = board.Cse7761(board.Wiring.of(POWCT))
        coefficients = self.start(0xFF04)   # the Dual R3's 16x on a clamp's input
        amps = self.read(0x24, 3) / ((0x800000 * 100 // (coefficients[0] * 5)) * 10)
        self.assertAlmostEqual(amps, 16 * 100 / 230, delta=0.01)


class RoutingTests(unittest.TestCase):
    @staticmethod
    def registers(values: dict[int, int]):
        return lambda address: values.get(address, 0)

    def test_an_esp32c3_uart_through_the_matrix(self):
        # As Tasmota and Sonoff's firmware leave an S60: U1RXD from GPIO7.
        soc = board.SOCS["esp32c3"]
        read = self.registers({soc.in_sel + 4 * 9: 0x47})
        self.assertEqual(board.routing(soc, board.Wiring.of(S60), read), board.Routing(frozenset(), frozenset({1})))

    def test_an_esp32_uart_through_its_iomux_pin(self):
        # U2RXD's IOMUX pin is GPIO16, the POW Elite's meter: no matrix bit.
        soc = board.SOCS["esp32"]
        self.assertEqual(board.routing(soc, board.Wiring.of(POW_ELITE), self.registers({})).tells, {2})

    def test_both_ways_for_a_meter_that_is_asked(self):
        soc = board.SOCS["esp32"]
        read = self.registers({soc.in_sel + 4 * 14: 0x80 | 26, soc.out_sel + 4 * 25: 14})
        self.assertEqual(board.routing(soc, board.Wiring.of(DUALR3), read),
                         board.Routing(frozenset({0}), frozenset({0})))

    def test_the_relay_is_on_only_while_its_output_is_enabled(self):
        soc = board.SOCS["esp32c3"]
        self.assertEqual(board.relay_outputs(soc, self.registers({soc.gpio_out: 0x30, soc.gpio_enable: 0x20})), 0x20)


class SendTests(unittest.TestCase):
    def test_what_a_uart_has_not_taken_is_not_queued(self):
        """A firmware that reads late gets the meter's frames from now, not
        the ones it missed."""
        with tempfile.TemporaryDirectory() as tmp:
            the_board = board.Board("esp32c3", S60, Path(tmp), None)
            for server in (*the_board.listeners, the_board.monitor):
                self.addCleanup(server.close)
            conn, qemu_end = socket.socketpair()
            self.addCleanup(conn.close)
            self.addCleanup(qemu_end.close)
            the_board.uarts[1] = conn
            for n in range(100):
                the_board.send(frozenset({1}), bytes([n]) * 24)
            qemu_end.setblocking(False)
            held = qemu_end.recv(100000)
            self.assertLessEqual(len(held), board.Board.BACKLOG + 24)
            the_board.send(frozenset({1}), b"now")
            self.assertEqual(qemu_end.recv(100), b"now")

    def test_a_uart_that_is_never_read_stops_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            the_board = board.Board("esp32", POW_ELITE, Path(tmp), None)
            for server in (*the_board.listeners, the_board.monitor):
                self.addCleanup(server.close)
            unread, qemu_end = socket.socketpair()
            read, reader = socket.socketpair()
            self.addCleanup(lambda: [conn.close() for conn in (unread, qemu_end, read, reader)])
            the_board.uarts[2], the_board.uarts[0] = unread, read
            started = time.monotonic()
            for _ in range(20000):   # some 480 KB, more than a socket holds
                the_board.send(frozenset({2, 0}), bytes(24))
                reader.recv(4096)
            self.assertLess(time.monotonic() - started, 10)
            self.assertIs(the_board.uarts[2], unread)


class HeardTests(unittest.TestCase):
    def test_the_first_command_on_a_uart_routed_since_the_last_look_is_answered(self):
        """ESPHome resets a CSE7761 and reads it as it sets the UART's pins: the
        board, last looking before that, reads the routing again."""
        soc = board.SOCS["esp32"]
        registers = {soc.in_sel + 4 * 14: 0x80 | 25, soc.out_sel + 4 * 26: 14}   # the POWCT on UART0

        class Monitor:
            read = staticmethod(lambda address: registers.get(address, 0))

        with tempfile.TemporaryDirectory() as tmp:
            the_board = board.Board("esp32", POWCT, Path(tmp), io.BytesIO())
            for server in (*the_board.listeners, the_board.monitor):
                self.addCleanup(server.close)
            conn, qemu_end = socket.socketpair()
            self.addCleanup(conn.close)
            self.addCleanup(qemu_end.close)
            the_board.uarts[0], the_board.qmp = conn, Monitor()
            the_board.heard(0, bytes([0xA5, 0x00]))
            qemu_end.settimeout(1)
            self.assertEqual(qemu_end.recv(10), bytes([0x0A, 0x04, ~(0xA5 + 0x0A + 0x04) & 0xFF]))
            self.assertEqual(the_board.console.getvalue(), bytes([0xA5, 0x00]))


# A stand-in for QEMU: it connects to the board's sockets as -chardev and -mon
# say, answers QMP's xp from REGISTERS, prints what UART0 sends, and writes what
# its UART1 hears to the file named by its first argument.
FAKE_QEMU = textwrap.dedent("""
    import json, socket, sys, time
    out, args = sys.argv[1], sys.argv[2:]
    REGISTERS = json.loads(args[args.index("--registers") + 1])
    paths = [a.split("path=")[1] for a in args if a.startswith("socket,id=")]
    uarts = [socket.socket(socket.AF_UNIX) for _ in paths[:-1]]
    for conn, path in zip(uarts, paths):
        conn.connect(path)
    qmp = socket.socket(socket.AF_UNIX); qmp.connect(paths[-1])
    f = qmp.makefile("rwb")
    f.write(b'{"QMP": {}}\\n'); f.flush()
    uarts[0].sendall(b"boot line\\n")
    heard, end = b"", time.monotonic() + 3
    uarts[1].settimeout(0.05)
    qmp.settimeout(0.05)
    while time.monotonic() < end and len(heard) < 48:
        try:
            line = f.readline()
            if line:
                cmd = json.loads(line)
                text = cmd.get("arguments", {}).get("command-line", "")
                ret = "%s: %#010x" % (text.split()[-1], REGISTERS.get(str(int(text.split()[-1], 16)), 0)) if text else {}
                f.write(json.dumps({"return": ret}).encode() + b"\\n"); f.flush()
        except (socket.timeout, OSError):
            pass
        try:
            heard += uarts[1].recv(100)
        except socket.timeout:
            pass
    open(out, "wb").write(heard)
""")


class BoardTests(unittest.TestCase):
    def test_qemu_through_the_board(self):
        """The console reaches stdout, and an S60's UART1, routed from GPIO7,
        hears CSE7766 frames that read the load while GPIO4 is on."""
        soc = board.SOCS["esp32c3"]
        registers = {soc.in_sel + 4 * 9: 0x47, soc.gpio_out: 1 << 4, soc.gpio_enable: 1 << 4}
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "qemu.py").write_text(FAKE_QEMU, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", board.__file__, "--chip", "esp32c3", "--kind", "sonoff-s60", "--run", tmp,
                 "--", sys.executable, "-I", str(run / "qemu.py"), str(run / "heard"),
                 "--registers", json.dumps({str(k): v for k, v in registers.items()})],
                capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"boot line\n")
            heard = (run / "heard").read_bytes()
            self.assertEqual(sorted(path.name for path in run.iterdir()), ["heard", "qemu.py"])
        start = heard.index(b"\x55\x5a")
        self.assertAlmostEqual(esphome_reads(heard[start:start + 24])["power"], 100, delta=0.1)

    def test_a_pow_elite_holds_gpio23_low(self):
        # Sonoff's firmware reads it to tell a POWR316D from a POWR320D: high,
        # as every input idled, it drove the 320D's latching relay in the
        # emulator, never GPIO13, and read no load.
        with tempfile.TemporaryDirectory() as tmp:
            args = board.Board("esp32", board.find_kind("sonoff-pow-elite"), Path(tmp), None).qemu_args()
        self.assertIn("driver=esp32.gpio,property=gpio_in,value=0xff7fffff", args)
        self.assertIn("driver=esp32.gpio,property=gpio_in1,value=0xff", args)

    def test_other_boards_leave_the_inputs_as_device_supervisor_sets_them(self):
        for chip, kind in (("esp32c3", "sonoff-s60"), ("esp32", "sonoff-th-elite")):
            with tempfile.TemporaryDirectory() as tmp:
                args = board.Board(chip, board.find_kind(kind), Path(tmp), None).qemu_args()
            self.assertFalse([arg for arg in args if "gpio_in" in arg], kind)

    def test_a_pin_above_31_is_held_in_the_second_word(self):
        self.assertEqual(board.input_levels(board.SOCS["esp32"], (33,)),
                         ["-global", "driver=esp32.gpio,property=gpio_in,value=0xffffffff",
                          "-global", "driver=esp32.gpio,property=gpio_in1,value=0xfd"])


class EarlierReleaseTests(unittest.TestCase):
    def test_it_starts_under_an_earlier_releases_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            script = under_an_earlier_release("board.py", temp)
            done = subprocess.run([sys.executable, "-I", "-B", str(script), "--help"], capture_output=True,
                                  text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_it_starts_after_an_earlier_releases_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            script = after_an_earlier_release("board.py", temp)
            done = subprocess.run([sys.executable, "-I", "-B", str(script), "--help"], capture_output=True,
                                  text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
