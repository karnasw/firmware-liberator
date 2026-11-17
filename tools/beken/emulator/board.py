"""The board around an emulated Beken device's chip: its power meter, a BL0937,
which measures a load while the relay that switches it is on.

device.py attaches it to the emulator it builds (`attach`). It runs from the
checkout's copy in the container (/work), as device.py does, so changing it
needs no new image.

The BL0937 reports by pulses, on two of the chip's GPIOs: CF at a rate that
is the power, CF1 at a rate that is the voltage while SEL is driven high and
the current while it is low (the BL0937's sense, the HLW8012's inverted).
OpenBeken and ESPHome count them by the GPIO interrupt, so the board drives
those pins' input levels, as an external chip does, at the rate the load
gives; Sonoff's firmware has the chip's PWM block capture them, timing each
period, so where a PWM channel is capturing a pin the board gives the count it
latches and its interrupt too. It reads the SEL and relay pins' outputs from
the GPIO registers as the firmware left them. The relay pin driven high switches the load on; the voltage is
measured on the supply side, so it is there either way.

Rates come from the board's calibration: the factory trim in the public S61
dump's store (elecCalibCoef), as the stock firmware, OpenBeken's import and
ESPHome's config each apply it (devices/sonoff-s61/research.md), so a firmware
that applies it reads the load as it is.

The emulator moves its clock on a chunk of instructions at a time, 0.2 ms of
the device's time, and the board sets the pins between chunks, so a pulse
lands up to a chunk late, while the count over any longer time is right. The
idle loop is not skipped past a pulse (the emulator may skip ten chunks at a
time), which would lose it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Load:
    """What is plugged in: a resistive load on the mains."""
    volts: float = 230.0
    watts: float = 100.0

    @property
    def amps(self) -> float:
        return self.watts / self.volts


LOAD = Load()


@dataclass(frozen=True)
class Wiring:
    """Which of the chip's GPIOs the relay and the meter are on (Pn = n), and
    what one hertz of each meter output measures."""
    relay: int
    cf: int
    cf1: int
    sel: int
    watts_per_hz: float
    volts_per_hz: float
    amps_per_hz: float


# The S61's wiring, as devices/sonoff-s61/drv_autoflash_sonoff_s61.c and
# esphome.yaml have it. Per hertz: the stock firmware's nominal scale (a 1.1 V
# reference, a 1 mOhm shunt and a 2040:1 divider) times the dump's factory
# trim, b = 930747 / 879893 / 954547 for power, voltage and current.
WIRING = {
    "sonoff-s61": Wiring(relay=26, cf=8, cf1=6, sel=24,
                         watts_per_hz=1.21 * 2040 * 1000 / 1721506 * 0.930747,
                         volts_per_hz=1.1 * 2040 / 15397 * 0.879893,
                         amps_per_hz=1.1 * 1000 / 94638 * 0.954547),
}


def driven_high(config: int) -> bool:
    """Whether a GPIO config word drives its pin high: the output driver on
    (bit 3 clear: it disables it) and the level, bit 1, set."""
    return not config & 0x08 and bool(config & 0x02)


class Bl0937:
    """The meter's two outputs: how many cycles each has run, at its rate
    as the relay and SEL are, by a given time of the device's."""

    def __init__(self, wiring: Wiring, load: Load = LOAD):
        self.wiring, self.load = wiring, load
        self.at: float | None = None

    def rates(self, relay_on: bool, sel_high: bool) -> dict[str, float]:
        """Each output's frequency (Hz) with the relay and SEL as given."""
        w = self.wiring
        watts, amps = (self.load.watts, self.load.amps) if relay_on else (0.0, 0.0)
        return {"cf": watts / w.watts_per_hz,
                "cf1": self.load.volts / w.volts_per_hz if sel_high else amps / w.amps_per_hz}

    def cycles(self, seconds: float, relay_on: bool, sel_high: bool) -> dict[str, float]:
        """The cycles each output has run since the last call, by `seconds`."""
        elapsed = 0.0 if self.at is None else max(0.0, seconds - self.at)
        self.at = seconds
        return {name: hz * elapsed for name, hz in self.rates(relay_on, sel_high).items()}


# The most cycles an output keeps owing the firmware, in seconds of its rate.
BACKLOG = 0.25

# The BK7238's PWM block (pwm_new: three groups of two channels, 0x40 apart),
# which can capture a pin's edges instead of driving it: each channel's pin,
# its capture modes (on rising, falling or both edges) and its clock, 26 MHz
# before the group's divider (beken378/driver/pwm/pwm_bk7231n.h).
PWMN_BASE = 0x00802B00
PWM_CHANNEL = {6: 0, 7: 1, 8: 2, 9: 3, 24: 4, 26: 5}
CAPTURE_MODES = (4, 5, 6)
PWM_CLOCK = 26_000_000


@dataclass(frozen=True)
class Capture:
    """A PWM channel capturing a pin's edges, as its group's control word says."""
    group: int
    post: int           # which of the group's two channels
    mode: int
    interrupt: int
    divider: int

    @property
    def status_bit(self) -> int:
        """Its interrupt status, as the emulator keeps it (state.pwmn_stat)."""
        return 2 * self.group + self.post


class Board:
    """The meter, wired to an emulator (src/emulator.py's), and set between
    its chunks of instructions.

    Each cycle of an output is one short low pulse: the pin falls and rises
    again between two chunks, latching one interrupt whichever edge the
    firmware takes - the falling one (OpenBeken), or either, as LibreTiny's
    change interrupt switches between them (ESPHome, which counts a pulse when
    it reads the pin high in the interrupt). A square wave's two edges cost two
    interrupts a cycle, more than the emulated CPU takes at the voltage's
    1.8 kHz: ESPHome read 160 V of 230.

    And a pulse is given only once the firmware has taken the interrupt the
    last one latched (the GPIO controller's INTSTA bit, or the capturing PWM
    channel's status, cleared), else it would
    be lost to the firmware. On the chip an interrupt is taken within
    microseconds; in the emulator a firmware's stretches with interrupts off
    last far longer in the device's time, and free-running at 1.8 kHz a
    quarter of the pulses were lost (OpenBeken read 176 V). A cycle not given
    yet is owed, and given as soon as the firmware is ready, so the count over
    a second is right; at most BACKLOG's worth is owed, as a firmware that
    never takes them would otherwise read a burst later."""

    def __init__(self, emu, wiring: Wiring, load: Load = LOAD, seconds_per_insn: float = 0.0):
        self.emu, self.wiring = emu, wiring
        self.meter = Bl0937(wiring, load)
        self.seconds_per_insn = seconds_per_insn
        self.owed = {"cf": 0.0, "cf1": 0.0}
        self.sel_high: bool | None = None

    def output(self, pin: int) -> bool:
        return driven_high(self.emu._gpio_config_word(pin))

    def pins(self) -> dict[str, int]:
        return {"cf": self.wiring.cf, "cf1": self.wiring.cf1}

    def tick(self, state) -> None:
        """Pulse each output the firmware is ready for and owed a cycle, at
        the device's time now, as the relay and SEL are."""
        relay_on, sel_high = self.output(self.wiring.relay), self.output(self.wiring.sel)
        if sel_high != self.sel_high:
            # CF1 measures the other quantity now: what it owed of the last is not given.
            self.sel_high = sel_high
            self.owed["cf1"] -= int(self.owed["cf1"])
        rates = self.meter.rates(relay_on, sel_high)
        for name, cycles in self.meter.cycles(state.insn_count * self.seconds_per_insn, relay_on, sel_high).items():
            self.owed[name] = min(self.owed[name] + cycles, max(2.0, rates[name] * BACKLOG))
        for name, pin in self.pins().items():
            capture = self.capture(pin)
            taken = not (state.gpio_hw.int_status >> pin & 1
                         or capture and state.pwmn_stat >> capture.status_bit & 1)
            if self.owed[name] >= 1 and taken:
                self.owed[name] -= 1
                config = self.emu._gpio_config_word(pin)
                state.gpio_hw.drive(pin, 0, config)
                state.gpio_hw.drive(pin, 1, config)
                if capture:
                    self.captured(state, capture, rates[name])

    def capture(self, pin: int) -> Capture | None:
        """The PWM channel on `pin`, if the firmware has it capturing edges -
        as Sonoff's firmware counts the BL0937's pulses - else None."""
        channel = PWM_CHANNEL.get(pin)
        if channel is None:
            return None
        group, post = divmod(channel, 2)
        control = int.from_bytes(self.emu.mu.mem_read(PWMN_BASE + 0x40 * group, 4), "little")
        mode, enabled = control >> 8 * post & 0x7, control >> 8 * post + 3 & 1
        if mode not in CAPTURE_MODES or not enabled:
            return None
        return Capture(group, post, mode, control >> 8 * post + 4 & 1, (control >> 16 & 0xFF) + 1)

    def captured(self, state, capture: Capture, hz: float) -> None:
        """The pulse as the channel latches it: the clock's count since the
        last edge it captures (both edges in edge mode), and its interrupt."""
        counts = PWM_CLOCK / capture.divider / hz / (2 if capture.mode == CAPTURE_MODES[-1] else 1)
        self.emu.mu.mem_write(PWMN_BASE + 0x40 * capture.group + 0x28 + 4 * capture.post,
                              min(round(counts), 0xFFFFFFFF).to_bytes(4, "little"))
        if capture.interrupt:
            state.pwmn_stat |= 1 << capture.status_bit

    def pulsing(self) -> bool:
        """Whether an output may pulse before the next slow tick."""
        rates = self.meter.rates(self.output(self.wiring.relay), self.output(self.wiring.sel))
        return any(rates[name] > 0 or self.owed[name] >= 1 for name in self.pins())


def attach(emu, kind: str, load: Load = LOAD, seconds_per_insn: float | None = None) -> Board | None:
    """Wire the board of the device `kind` to `emu` before it runs: its meter
    set between each chunk of instructions (the emulator's _pacing, whose
    GPIO interrupt sync then follows), and the idle loop skipped only while
    neither output is due to change. None for a kind with no board here."""
    wiring = WIRING.get(kind)
    if wiring is None:
        return None
    if seconds_per_insn is None:
        from src.emulator import SLOW_TICK_INSNS, SLOW_TICK_MS   # the device clock (needs the emulator image)
        seconds_per_insn = SLOW_TICK_MS / 1000 / SLOW_TICK_INSNS
    board = Board(emu, wiring, load, seconds_per_insn)
    pacing, fast_tick_idle = emu._pacing, emu._fast_tick_idle

    def paced(state):
        board.tick(state)
        pacing(state)

    def idle(state):
        return fast_tick_idle(state) and not board.pulsing()

    emu._pacing, emu._fast_tick_idle = paced, idle
    return board
