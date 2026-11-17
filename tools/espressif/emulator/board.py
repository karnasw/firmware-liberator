#!/usr/bin/env python3
"""The board around an emulated Espressif device's chip: its serial port, and its
power meter, which measures a load while the relay that switches it is on.

device-supervisor starts QEMU through this (`board.py --chip esp32c3 --kind
sonoff-s60 -- qemu-system-riscv32 ...`): it adds QEMU's serial ports, each a
socket it serves, and a QMP monitor, through which it reads the chip's GPIO
registers. Run from the checkout's copy in the container (/work), as the
redactor and the cloud stub are, so changing it needs no new image. Python's
standard library only, as the container has nothing else.

What the chip's UARTs send and receive goes where the firmware has routed
them, as on the device's board:

- UART0 always reaches the console - the serial log - as its IOMUX pins keep
  their function whatever else it is routed to (ESPHome's driver notes the
  same of a real chip).
- A UART whose TX the GPIO matrix routes to the meter's input pin is heard by
  the meter; one whose RX takes the meter's output pin, through the matrix or
  that pin's IOMUX function, hears it. The meter's pins come from the kind's
  Tasmota template (tools/autoflash/kinds.py), which is how the device is wired.

The meter is the chip the template names: a CSE7766 (the S60, the POW Elite),
which sends a frame of readings about every 55 ms by itself, or a CSE7761 (the
Dual R3, the POWCT), which answers register reads. Each channel measures
LOAD while the relay the template gives it is on - the GPIO's output set and
enabled - and nothing while it is off; the mains voltage is measured on the
supply side, so it is there either way. The POWCT's clamp is not switched by
its relay: it reads LOAD whatever the relay does.

The baud rate and frame format a UART is set to are not checked: a firmware
reading the meter at the wrong speed would still get its frames.

Every input pin idles high (device-supervisor), but those a kind's board holds
low (HELD_LOW): a pin the firmware reads to know which board it is on.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import selectors
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    # Under an earlier release's tool (unattended-test.sh --code) the tree around
    # this folder is that release's: this checkout's modules are kept in it. A
    # later copy of the checkout empties that copy, leaving its folders.
    _kept = Path(__file__).resolve().parent / "checkout"
    sys.path.insert(0, str(_kept if (_kept / "tools/__init__.py").is_file() else Path(__file__).resolve().parents[3]))

from tools.autoflash import kinds  # noqa: E402


@dataclass(frozen=True)
class Load:
    """What is plugged in: a resistive load on the mains."""
    volts: float = 230.0
    watts: float = 100.0
    hertz: float = 50.0

    @property
    def amps(self) -> float:
        return self.watts / self.volts


LOAD = Load()

# Tasmota's GPIO codes (AGPIO(GPIO_x) = x * 32 + index), as the kinds' templates
# give each pin's use.
RELAY1 = 224                        # Relay1 .. Relay8: 224 .. 231
CSE7766_RX = 3104                   # the chip's TX, a UART RX of the SoC
CSE7761_TX, CSE7761_RX = 7296, 7328  # CSE7761_RX + 1 is the POWCT's
# The GPIO each slot of a template is for: an ESP32's skips the flash's pins.
TEMPLATE_GPIOS = {"esp32c3": tuple(range(22)), "esp32": (0, 1, 2, 3, 4, 5, 9, 10, *range(12, 40))}
# A current clamp, not a meter in the relay's line: it measures whatever its
# cable carries, whether or not the relay is on.
CLAMPS = {"sonoff-powct"}
# Input pins a kind's board holds low, where device-supervisor's every input idling
# high would read otherwise. Sonoff's POW Elite firmware reads GPIO23, its pull-up
# on, to tell a POWR316D (low) from a POWR320D (high), whose latching relay it
# would drive instead (devices/sonoff-pow-elite/README.md).
HELD_LOW = {"sonoff-pow-elite": (23,)}


@dataclass(frozen=True)
class Channel:
    """One of the meter's current inputs: the relay pin that switches its load,
    or none when nothing does; and whether anything is connected to it."""
    relay: int | None
    loaded: bool = True

    def load(self, outputs: int) -> Load:
        on = self.loaded and (self.relay is None or bool(outputs >> self.relay & 1))
        return LOAD if on else Load(LOAD.volts, 0.0, LOAD.hertz)


@dataclass(frozen=True)
class Wiring:
    """The meter as the device's template wires it."""
    chip: str                 # "cse7766" or "cse7761"
    output_pin: int           # the meter's TX: what the SoC receives on
    input_pin: int | None     # the meter's RX: what the SoC sends on
    channels: tuple[Channel, ...]
    current_transformer: bool = False

    @classmethod
    def of(cls, kind: kinds.Kind) -> Wiring | None:
        """The meter of `kind`, from its Tasmota template; None for one with none."""
        if not kind.has_meter or kind.chip not in TEMPLATE_GPIOS:
            return None
        pins: dict[int, int] = {}
        for gpio, code in zip(TEMPLATE_GPIOS[kind.chip], kind.tasmota_gpio):
            pins.setdefault(code, gpio)
        relays = [pins.get(RELAY1 + n) for n in range(8)]
        clamp = kind.key in CLAMPS
        if CSE7766_RX in pins:
            return cls("cse7766", pins[CSE7766_RX], None, (Channel(None if clamp else relays[0]),))
        rx = pins.get(CSE7761_RX, pins.get(CSE7761_RX + 1))
        if rx is not None and CSE7761_TX in pins:
            if clamp:
                channels = (Channel(None), Channel(None, loaded=False))
            else:
                channels = tuple(Channel(relay, relay is not None) for relay in relays[:2])
            return cls("cse7761", rx, pins[CSE7761_TX], channels, current_transformer=clamp)
        return None


# --- the meter chips ----------------------------------------------------------

class Cse7766:
    """Chipsea's CSE7766: a 24-byte frame, 4800 baud 8E1, sent continuously
    (one about every 55 ms): two header bytes, the voltage, current and power
    coefficients and cycles (24-bit, big-endian), the register-updated flags,
    the PF pulse count (16-bit) and a checksum of bytes 2 to 22. A reading is
    its coefficient over its cycle (microseconds per pulse). With no load the
    power cycle is out of range, which the header's bit 1 says."""

    PERIOD = 0.055
    # As Tasmota (xnrg_02_cse7766.ino) and the datasheet give a typical chip's:
    # none is one of Tasmota's defaults, which it would take again.
    VOLTAGE_COEFFICIENT = 191200
    CURRENT_COEFFICIENT = 16140
    POWER_COEFFICIENT = 5364000
    HEADER, HEADER_NO_POWER, CHECK = 0x55, 0xF2, 0x5A
    VOLTAGE_UPDATED, CURRENT_UPDATED, POWER_UPDATED = 0x40, 0x20, 0x10
    MAX_CYCLE = 0xFFFFFF

    def __init__(self, wiring: Wiring):
        self.channel = wiring.channels[0]
        self.pulses = 0.0
        self.noise = random.Random(0)
        self.last: float | None = None

    @staticmethod
    def word(value: int) -> bytes:
        return min(value, Cse7766.MAX_CYCLE).to_bytes(3, "big")

    def frame(self, outputs: int, now: float) -> bytes:
        load = self.channel.load(outputs)
        if self.last is not None:
            # Each PF pulse is POWER_COEFFICIENT / 1e6 watt-seconds, as ESPHome
            # and Tasmota count energy.
            self.pulses += load.watts * (now - self.last) / (self.POWER_COEFFICIENT / 1e6)
        self.last = now
        # A real cycle is never quite the same twice. Tasmota counts a power
        # reading only once its cycle differs from the first it saw since the
        # relay came on, so a cycle that alternated could match it for good in
        # a firmware that reads every other frame.
        jitter = self.noise.randint(0, 2)
        header = self.HEADER
        voltage_cycle = round(self.VOLTAGE_COEFFICIENT / load.volts)
        if load.watts > 0:
            current_cycle = round(self.CURRENT_COEFFICIENT / load.amps) + jitter
            power_cycle = round(self.POWER_COEFFICIENT / load.watts) + jitter
        else:
            header, current_cycle, power_cycle = self.HEADER_NO_POWER, self.MAX_CYCLE, self.MAX_CYCLE
        body = (self.word(self.VOLTAGE_COEFFICIENT) + self.word(voltage_cycle)
                + self.word(self.CURRENT_COEFFICIENT) + self.word(current_cycle)
                + self.word(self.POWER_COEFFICIENT) + self.word(power_cycle)
                + bytes([self.VOLTAGE_UPDATED | self.CURRENT_UPDATED | self.POWER_UPDATED])
                + (int(self.pulses) & 0xFFFF).to_bytes(2, "big"))
        return bytes([header, self.CHECK]) + body + bytes([sum(body) & 0xFF])


@dataclass
class Cse7761:
    """Chipsea's CSE7761: 38400 baud 8E1, the SoC asking. A read is 0xA5 and
    the register; the chip answers its value (big-endian, as wide as the
    register) and ~(0xA5 + register + the value's bytes). A write is 0xA5,
    the register | 0x80, the value and that checksum, and is taken only while
    writes are enabled; 0xA5 0xEA, a command and its checksum, resets the
    chip, enables or closes writes, or picks a channel. Readings scale as the
    datasheet's conversion coefficients say, which the chip keeps, with their
    checksum, at 0x6F to 0x77."""

    wiring: Wiring
    registers: dict[int, int] = field(default_factory=dict)
    writable: bool = False
    buffer: bytearray = field(default_factory=bytearray)
    energy: list[float] = field(default_factory=lambda: [0.0, 0.0])
    last: float | None = None

    SPECIAL, RESET, ENABLE_WRITE, CLOSE_WRITE = 0xEA, 0x96, 0xE5, 0xDC
    SYSCON, HFCONST, UFREQ, RMSIA, RMSIB, RMSU, POWERFACTOR = 0x00, 0x02, 0x23, 0x24, 0x25, 0x26, 0x27
    ENERGY_PA, ENERGY_PB, POWERPA, POWERPB, SYSSTATUS, COEFFCHKSUM = 0x28, 0x29, 0x2C, 0x2D, 0x43, 0x6F
    RMSIAC, RMSIBC, RMSUC, POWERPAC, POWERPBC, POWERSC, ENERGYAC, ENERGYBC = range(0x70, 0x78)
    WIDTHS = {RMSIA: 3, RMSIB: 3, RMSU: 3, POWERFACTOR: 3, ENERGY_PA: 3, ENERGY_PB: 3, 0x2F: 3,
              POWERPA: 4, POWERPB: 4, 0x44: 4, SYSSTATUS: 1, 0x7F: 3}
    DEFAULTS = {SYSCON: 0x0A04, 0x01: 0x0000, HFCONST: 0x1000, 0x13: 0x0001, 0x1D: 0x3210, 0x6E: 0xFFFF}
    # The chip's factory calibration: Tasmota's and ESPHome's reference values,
    # which take a chip's own when its checksum is right.
    COEFFICIENTS = {RMSIAC: 52241, RMSIBC: 52241, RMSUC: 42563, POWERPAC: 44513, POWERPBC: 44513,
                    POWERSC: 44513, ENERGYAC: 44513, ENERGYBC: 44513}
    CLOCK = 3579545  # Hz
    # The current inputs' gain each board is calibrated at (SYSCON's PGAIA
    # and PGAIB): a shunt at 16, a current transformer at 1.
    SHUNT_GAIN, TRANSFORMER_GAIN = 16, 1
    # A current transformer's input stands for five times the current a
    # shunt's does (Tasmota's and ESPHome's POWCT scale).
    TRANSFORMER_SCALE = 5

    def __post_init__(self):
        self.reset()

    def reset(self) -> None:
        self.registers = {**self.DEFAULTS, **self.COEFFICIENTS}
        self.registers[self.COEFFCHKSUM] = ~(0xFFFF + sum(self.COEFFICIENTS.values())) & 0xFFFF
        self.writable = False

    def width(self, register: int) -> int:
        return self.WIDTHS.get(register, 2)

    @staticmethod
    def gain(bits: int) -> int:
        return 16 if bits & 4 else 1 << (bits & 3)

    def readings(self, outputs: int, now: float) -> dict[int, int]:
        """The measurement registers, as the load is now."""
        scale = self.TRANSFORMER_SCALE if self.wiring.current_transformer else 1
        calibrated = self.TRANSFORMER_GAIN if self.wiring.current_transformer else self.SHUNT_GAIN
        syscon = self.registers[self.SYSCON]
        gains = (self.gain(syscon & 7), self.gain(syscon >> 6 & 7))
        coefficient = self.COEFFICIENTS
        volts_unit = 0x400000 * 100 // coefficient[self.RMSUC]
        amps_unit = (0x800000 * 100 // (coefficient[self.RMSIAC] * scale)) * 10
        watts_unit = 0x80000000 // (coefficient[self.POWERPAC] * scale)
        loads = [channel.load(outputs) for channel in self.wiring.channels]
        loads += [Load(LOAD.volts, 0.0)] * (2 - len(loads))
        if not syscon >> 10 & 1:   # ADC2ON: channel B off
            loads[1] = Load(LOAD.volts, 0.0)
        if self.last is not None:
            for n, load in enumerate(loads):
                self.energy[n] += load.watts * (now - self.last) / 3600   # Wh
        self.last = now
        hfconst = self.registers[self.HFCONST]
        values = {self.RMSU: round(LOAD.volts * volts_unit),
                  self.UFREQ: round(self.CLOCK / 8 / LOAD.hertz),
                  self.POWERFACTOR: 0x7FFFFF}
        for n, (load, gain) in enumerate(zip(loads, gains)):
            factor = gain / calibrated
            values[(self.RMSIA, self.RMSIB)[n]] = min(round(load.amps * amps_unit * factor), 0x7FFFFF)
            values[(self.POWERPA, self.POWERPB)[n]] = min(round(load.watts * watts_unit * factor), 0x7FFFFFFF)
            # Datasheet: Wh = pulses * EnergyXC * HFConst * 1000 / 2^41.
            per_pulse = coefficient[(self.ENERGYAC, self.ENERGYBC)[n]] * hfconst * 1000 / 2 ** 41
            values[(self.ENERGY_PA, self.ENERGY_PB)[n]] = int(self.energy[n] / per_pulse) & 0xFFFFFF
        return values

    def feed(self, data: bytes, outputs: int, now: float) -> bytes:
        """What the chip says to what it is sent."""
        self.buffer += data
        answer = bytearray()
        while self.buffer:
            if self.buffer[0] != 0xA5:
                del self.buffer[0]
                continue
            if len(self.buffer) < 2:
                break
            register = self.buffer[1]
            if register == self.SPECIAL or register & 0x80:
                length = 4 if register == self.SPECIAL else 3 + self.width(register & 0x7F)
                if len(self.buffer) < length:
                    break
                frame, self.buffer = bytes(self.buffer[:length]), self.buffer[length:]
                if frame[-1] != ~sum(frame[:-1]) & 0xFF:
                    continue
                value = int.from_bytes(frame[2:-1], "big")
                if register == self.SPECIAL:
                    if value == self.RESET:
                        self.reset()
                    elif value in (self.ENABLE_WRITE, self.CLOSE_WRITE):
                        self.writable = value == self.ENABLE_WRITE
                elif self.writable:
                    self.registers[register & 0x7F] = value
                continue
            del self.buffer[:2]
            if register == self.SYSSTATUS:
                value = 0x10 if self.writable else 0
            else:
                value = self.readings(outputs, now).get(register, self.registers.get(register, 0))
            body = value.to_bytes(self.width(register), "big")
            answer += body + bytes([~(0xA5 + register + sum(body)) & 0xFF])
        return bytes(answer)


# --- the SoC's registers, as QEMU's monitor reads them -------------------------

@dataclass(frozen=True)
class Soc:
    """Where an ESP chip keeps its GPIO outputs and its GPIO matrix's routing."""
    uarts: int
    gpio_out: int
    gpio_enable: int
    in_sel: int            # GPIO_FUNC0_IN_SEL_CFG_REG: one per input signal
    in_sel_pin: int        # its pin bits
    in_sel_matrix: int     # its bit routing the signal through the matrix, not the IOMUX
    out_sel: int           # GPIO_FUNC0_OUT_SEL_CFG_REG: one per pin
    out_sel_signal: int    # its signal bits
    uart_signals: tuple[int, ...]           # UnRXD_IN_IDX, which is UnTXD_OUT_IDX too
    iomux_rx: tuple[int | None, ...]        # each UART's RX pin through the IOMUX
    gpio_device: str                        # QEMU's GPIO device, whose properties set the input pins
    inputs: tuple[int, ...]                 # its gpio_in, gpio_in1 ...: 32 pins each, all high as device-supervisor sets them


SOCS = {
    "esp32c3": Soc(2, 0x60004004, 0x60004020, 0x60004154, 0x1F, 0x40, 0x60004554, 0xFF, (6, 9), (20, None),
                   "esp32c3.gpio", (0x3FFFFF,)),
    "esp32": Soc(3, 0x3FF44004, 0x3FF44020, 0x3FF44130, 0x3F, 0x80, 0x3FF44530, 0x1FF, (14, 17, 198), (3, 9, 16),
                 "esp32.gpio", (0xFFFFFFFF, 0xFF)),
}


def input_levels(soc: Soc, held_low: tuple[int, ...]) -> list[str]:
    """QEMU's arguments setting the input pins as the board holds them: high but
    for `held_low`. After device-supervisor's, which they take the place of."""
    args = []
    for n, high in enumerate(soc.inputs):
        level = high & ~sum(1 << (pin - 32 * n) for pin in held_low if pin // 32 == n)
        name = "gpio_in" if n == 0 else f"gpio_in{n}"
        args += ["-global", f"driver={soc.gpio_device},property={name},value={level:#x}"]
    return args


@dataclass(frozen=True)
class Routing:
    """Which UARTs the meter's pins are on now."""
    hears: frozenset[int] = frozenset()   # UARTs whose TX the meter hears
    tells: frozenset[int] = frozenset()   # UARTs that hear the meter


def routing(soc: Soc, wiring: Wiring | None, read) -> Routing:
    """The meter's UARTs, from the GPIO matrix's registers (`read(address)`)."""
    if wiring is None:
        return Routing()
    tells = set()
    for uart, signal_index in enumerate(soc.uart_signals):
        value = read(soc.in_sel + 4 * signal_index)
        pin = value & soc.in_sel_pin if value & soc.in_sel_matrix else soc.iomux_rx[uart]
        if pin == wiring.output_pin:
            tells.add(uart)
    hears = set()
    if wiring.input_pin is not None:
        selected = read(soc.out_sel + 4 * wiring.input_pin) & soc.out_sel_signal
        hears = {uart for uart, signal_index in enumerate(soc.uart_signals) if signal_index == selected}
    return Routing(frozenset(hears), frozenset(tells))


def relay_outputs(soc: Soc, read) -> int:
    """The GPIOs driven high: output set and enabled."""
    return read(soc.gpio_out) & read(soc.gpio_enable)


class Qmp:
    """QEMU's QMP monitor, on a connected socket: physical memory read through
    its human monitor's xp, which a device's registers answer as the CPU's
    reads would."""

    def __init__(self, conn: socket.socket):
        self.file = conn.makefile("rwb")
        self.receive()                       # the greeting
        self.call({"execute": "qmp_capabilities"})

    def receive(self) -> dict:
        while True:
            line = self.file.readline()
            if not line:
                raise ConnectionError("QEMU's monitor closed")
            message = json.loads(line)
            if "event" not in message:
                return message

    def call(self, command: dict) -> object:
        self.file.write(json.dumps(command).encode() + b"\n")
        self.file.flush()
        reply = self.receive()
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply.get("return")

    def read(self, address: int) -> int:
        text = self.call({"execute": "human-monitor-command", "arguments": {"command-line": f"xp /1xw {address:#x}"}})
        return int(str(text).rsplit(":", 1)[1].split()[0], 16)


# --- the board -----------------------------------------------------------------

def unread(conn: socket.socket) -> int:
    """The bytes sent on `conn` that its peer has not read yet (Linux's SIOCOUTQ)."""
    try:
        return struct.unpack("i", fcntl.ioctl(conn.fileno(), termios.TIOCOUTQ, b"\0" * 4))[0]
    except OSError:
        return 0


class Board:
    """QEMU's serial ports and monitor, served on sockets in `run`, and the
    meter on whichever UARTs reach its pins."""

    RELAY_POLL, ROUTING_POLL = 0.1, 1.0

    def __init__(self, chip: str, kind: kinds.Kind | None, run: Path, console):
        self.soc = SOCS[chip]
        self.held_low = HELD_LOW.get(kind.key, ()) if kind else ()
        self.wiring = Wiring.of(kind) if kind else None
        self.meter = None
        if self.wiring is not None:
            self.meter = (Cse7766 if self.wiring.chip == "cse7766" else Cse7761)(self.wiring)
        self.run = run
        self.console = console
        self.outputs = 0
        self.route = Routing()
        self.qmp: Qmp | None = None
        self.lock = threading.Lock()   # one QMP call at a time
        self.listeners = [self.listen(f"uart{n}.sock") for n in range(self.soc.uarts)]
        self.monitor = self.listen("board-qmp.sock")
        self.uarts: list[socket.socket | None] = [None] * self.soc.uarts

    def listen(self, name: str) -> socket.socket:
        path = self.run / name
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        return server

    def qemu_args(self) -> list[str]:
        args = []
        for n in range(self.soc.uarts):
            args += ["-chardev", f"socket,id=uart{n},path={self.run / f'uart{n}.sock'}", "-serial", f"chardev:uart{n}"]
        if self.held_low:
            args += input_levels(self.soc, self.held_low)
        return args + ["-chardev", f"socket,id=board-qmp,path={self.run / 'board-qmp.sock'}",
                       "-mon", "chardev=board-qmp,mode=control"]

    def watch(self) -> None:
        """Keeps the relay pins' state and the meter's routing up to date."""
        conn, _ = self.monitor.accept()
        try:
            with self.lock:
                self.qmp = Qmp(conn)
            routed, said = 0.0, set()
            while True:
                try:
                    with self.lock:
                        self.outputs = relay_outputs(self.soc, self.qmp.read)
                    if time.monotonic() - routed >= self.ROUTING_POLL:
                        self.reroute()
                        routed = time.monotonic()
                except (ValueError, RuntimeError, IndexError) as exc:
                    if repr(exc) not in said:   # once each, not every 100 ms
                        said.add(repr(exc))
                        sys.stderr.write(f"board: the monitor's answer was not a register's: {exc!r}\n")
                time.sleep(self.RELAY_POLL)
        except OSError:
            self.qmp = None
            return   # QEMU has gone, and its monitor with it

    def reroute(self) -> None:
        """The meter's routing read again now."""
        with self.lock:
            if self.qmp is not None:
                self.route = routing(self.soc, self.wiring, self.qmp.read)

    # What a UART's socket may hold that QEMU has not read: its FIFO's length.
    BACKLOG = 128

    def send(self, uarts: frozenset[int], data: bytes) -> None:
        """`data` to each of `uarts`, never waiting and never queued: what a
        UART cannot take now is lost, as bytes on a wire into a full FIFO are.
        Queued, a firmware that read late - QEMU then paces its reads to the
        baud rate - would be minutes behind the meter. And one the firmware
        never reads - UART2 takes GPIO16 through the IOMUX until it is set
        otherwise - would stop the board, and with it every UART."""
        for n in uarts:
            conn = self.uarts[n]
            if conn is None or not data or unread(conn) > self.BACKLOG:
                continue
            try:
                conn.send(data, socket.MSG_DONTWAIT)
            except BlockingIOError:
                pass
            except OSError:
                self.uarts[n] = None

    def heard(self, n: int, data: bytes) -> None:
        """What UART `n` sent: to the console from UART0, and to the meter
        from a UART routed to it."""
        if n == 0:
            self.console.write(data)
            self.console.flush()
        if not isinstance(self.meter, Cse7761):
            return
        if n not in self.route.hears and 0xA5 in data:
            # What may be a command, on a UART not routed to the meter when
            # last looked: a firmware sends its first as it sets the UART's
            # pins, before the next look.
            try:
                self.reroute()
            except (OSError, ValueError, RuntimeError, IndexError):
                pass
        if n in self.route.hears:
            self.send(self.route.tells, self.meter.feed(data, self.outputs, time.monotonic()))

    def serve(self, qemu: subprocess.Popen) -> int:
        """Until QEMU exits: its UARTs' bytes where they are wired."""
        threading.Thread(target=self.watch, daemon=True).start()
        selector = selectors.DefaultSelector()
        for n, server in enumerate(self.listeners):
            selector.register(server, selectors.EVENT_READ, ("accept", n))
        next_frame = time.monotonic()
        while qemu.poll() is None:
            timeout = max(0.0, next_frame - time.monotonic()) if isinstance(self.meter, Cse7766) else 0.2
            for key, _ in selector.select(min(timeout, 0.2)):
                what, n = key.data
                if what == "accept":
                    conn, _ = key.fileobj.accept()
                    self.uarts[n] = conn
                    selector.register(conn, selectors.EVENT_READ, ("data", n))
                    continue
                try:
                    data = key.fileobj.recv(4096)
                except OSError:
                    data = b""
                if not data:
                    selector.unregister(key.fileobj)
                    self.uarts[n] = None
                    continue
                self.heard(n, data)
            if isinstance(self.meter, Cse7766) and time.monotonic() >= next_frame:
                self.send(self.route.tells, self.meter.frame(self.outputs, time.monotonic()))
                next_frame += Cse7766.PERIOD
                if next_frame < time.monotonic():
                    next_frame = time.monotonic() + Cse7766.PERIOD
        return qemu.wait()


def find_kind(key: str) -> kinds.Kind | None:
    return next((kind for kind in kinds.EVERY if kind.key == key), None) if key else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--chip", choices=sorted(SOCS), required=True)
    parser.add_argument("--kind", default="", help="the device's kind (kinds.py), whose template wires its meter")
    parser.add_argument("--run", type=Path, default=Path("/run/autoflash-emu"))
    parser.add_argument("qemu", nargs=argparse.REMAINDER, help="-- and QEMU's command line")
    args = parser.parse_args(argv)
    command = args.qemu[1:] if args.qemu[:1] == ["--"] else args.qemu
    if not command:
        parser.error("no QEMU command line after --")
    board = Board(args.chip, find_kind(args.kind), args.run, sys.stdout.buffer)
    qemu = subprocess.Popen([*command, *board.qemu_args()], stdin=subprocess.DEVNULL)
    signal.signal(signal.SIGTERM, lambda *_: qemu.terminate())
    try:
        return board.serve(qemu)
    finally:
        if qemu.poll() is None:
            qemu.kill()
        for name in [f"uart{n}.sock" for n in range(board.soc.uarts)] + ["board-qmp.sock"]:
            try:
                os.unlink(board.run / name)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
