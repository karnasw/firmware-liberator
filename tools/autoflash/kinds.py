"""The kinds of device the tool converts, and what is each one's own.

What differs from one kind of device to the next is kept here: how eWeLink and
its stock firmware name it, the model its maker's updater wants a file
wrapped for, its wiring for Tasmota, what its ESPHome calls itself, and what
a person calls it.  The firmware published for a kind is pinned where it is
used, each file named for the kind it is for: its installer
(tools.autoflash.routes.espressif.installer.BRIDGE_OTA, wrapped for its updater), its ESPHome and the
bootloader built with it (routes.espressif.images.IMAGE and BOOTLOADER), and
its maker's partition table (SONOFF_TABLE there).  The final firmware's names are the
same for every kind - tasmota-abcdef, esphome-abcdef, openbeken-abcdef - so the
kind is told by what else each firmware says, which renaming the device
leaves alone: Tasmota's template, ESPHome's comment, and the answer
OpenBeken's driver gives (openbeken.kind).

A run keeps the kind it converts (its "device_kind"): from the eWeLink account
(A1), from what the stock firmware says it is (A2), or from a converted
Tasmota's template.  The kind names its chip family, which is whose steps the
run takes (tools/autoflash/routes).  Official Tasmota's template is named after the kind,
and ESPHome's comment by the image made for it; every firmware part way
through a conversion is the tool's own, the same for every kind.

Espressif devices have one route (tools/autoflash/routes/espressif), whichever
chip they have (tools/espressif/chips.py). Beken devices have another.
docs/adding-a-device.md says how to add a kind.

How far each kind is proven is kept in devices/status.json, alone: the READMEs'
badges read it from GitHub, and the tool from here.
"""

from __future__ import annotations

import enum
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


# The namespace every kind's ESPHome project is named in (ESPHome wants one,
# before a dot): liberator.sonoff-s60. Images before 3.0.0 were named in
# EARLIER_ESPHOME_NAMESPACES: a device on one still runs this project's.
ESPHOME_NAMESPACE = "liberator"
EARLIER_ESPHOME_NAMESPACES = ("adamsthws",)


class Status(enum.Enum):
    """How far a kind is proven, as devices/status.json says it, by these ids."""

    # A real one has been converted and reported: converted with no warning.
    SUPPORTED = "supported"
    # Converts in the emulator, or is added from its official file and a
    # published pin map; no real one reported yet: converted after a
    # typed warning, asking for the run's log (tools/autoflash/testers.py).
    TESTERS_WANTED = "testers-wanted"
    # Not converted yet, but its readout through the bridge is proven safe in
    # the emulator: read out in full, to add it.
    READOUT_WANTED = "readout-wanted"
    # Neither: turned down like a device the tool does not know, and read out
    # only as far as Sonoff's own firmware tells (readout.py).
    NOT_SUPPORTED = "not-supported"

    @property
    def label(self) -> str:
        """Its words, as its badge and the README say them."""
        return {Status.SUPPORTED: "Supported", Status.TESTERS_WANTED: "Testers wanted",
                Status.READOUT_WANTED: "Readout wanted", Status.NOT_SUPPORTED: "Not supported yet"}[self]

    @property
    def colour(self) -> str:
        """Its badge's colour, one the README's badges use: the Tests badge's
        passing green, the others' blue, the firmware badges' purple (the
        FirmwareRoutes.svg border's), and shields.io's orange."""
        return {Status.SUPPORTED: "brightgreen", Status.TESTERS_WANTED: "blue",
                Status.READOUT_WANTED: "7C3AED", Status.NOT_SUPPORTED: "orange"}[self]

    @property
    def converts(self) -> bool:
        """Whether the tool converts a kind of this status."""
        return self in (Status.SUPPORTED, Status.TESTERS_WANTED)

    @property
    def reads_out(self) -> bool:
        """Whether a readout may send it the bridge: only where the bridge is
        proven to start and leave it as it was, as on every kind converted."""
        return self is not Status.NOT_SUPPORTED


STATUS_FILE = Path(__file__).resolve().parents[2] / "devices" / "status.json"
STATUSES = {key: Status(value) for key, value in json.loads(STATUS_FILE.read_text(encoding="utf-8")).items()}


@dataclass(frozen=True, eq=False)
class Kind:
    """A kind of device the tool converts."""

    # In file names (esphome-sonoff-s60-imprintable-*.bin) and a run's record.
    key: str
    # Its chip family (routes.FAMILIES: how firmware gets onto its flash), its
    # chip, and the ecosystem its stock firmware belongs to - how it is paired,
    # found and sent its first new firmware.  Only the first is code of its
    # own so far: every kind runs eWeLink's firmware.
    family: str
    chip: str
    ecosystem: str
    # What a person calls it, its maker, and its name for short in running text.
    name: str
    maker: str
    short: str
    # eWeLink's product model for one, as the account gives it.
    product: re.Pattern[str]
    # What its stock firmware calls itself, to eWeLink and to the updater.
    stock_model: str
    # The model a file must be wrapped for, for the stock updater to take it.
    wrapper_model: str
    # Its wiring, as a Tasmota template gives it; none for a kind Tasmota does not run on.
    tasmota_gpio: tuple[int, ...]
    # The names its own network had in earlier ESPHome builds: the first with
    # the last two bytes of the device's MAC.
    esphome_legacy_setup_prefix: str
    esphome_legacy_prefix: str
    # Its stock partition table, by key: tools/espressif/layouts.py's for an
    # Espressif device, tools/beken/layouts.py's for a Beken one. These four
    # have no default, so a new kind cannot take one meant for another.
    layout: str
    # Whether it has a power meter: the report says how its calibration went,
    # and the end-of-run check reads it (report.check_hardware).
    has_meter: bool
    # Whether it has a relay, which the end-of-run check switches: a device
    # without one has no such check, nor a Checks line in its report.
    has_relay: bool
    # What its sensors measure (report.SENSOR_RANGES' keys), which the
    # end-of-run check reads and finds plausible; () for a device with none.
    sensors: tuple[str, ...]
    # Its ESPHome's friendly name in releases before its comment said the kind.
    esphome_legacy_friendly_name: str = ""
    # For a Beken device: the command its OpenBeken (built with this project's
    # driver, tools/beken/openbeken/) takes to fetch and apply Sonoff's
    # firmware again, checking the slot it goes to first; "" for a kind that
    # has none.
    openbeken_revert: str = ""
    # Whether another device's stock firmware calls itself the same, with other
    # wiring (the POW Elite's 16 A and 20 A): only eWeLink's product model then
    # says it is this kind, and a device it does not name is not converted.
    product_model_decides: bool = False
    # Whether its meter is calibrated as the S60's (tools/autoflash/calibration.py).
    calibrated_as_s60: bool = False
    # eWeLink's name for one, for the emulator's cloud stub to give.
    example_product_model: str = ""
    # The external RAM (MB, as QEMU's -m takes it) its stock firmware stops
    # without at start-up: only the emulator asks.
    psram_mb: int = 0
    # What its board's analog inputs read at rest, as the emulator's ADC takes them
    # (QEMU property, raw 12-bit sample), where stock firmware acts on one: only
    # the emulator asks.
    adc_readings: tuple[tuple[str, int], ...] = ()

    @property
    def status(self) -> Status:
        """How far it is proven (devices/status.json); one not listed there is not supported."""
        return STATUSES.get(self.key, Status.NOT_SUPPORTED)

    @property
    def ready(self) -> bool:
        """Whether the tool converts it."""
        return self.status.converts

    @property
    def folder(self) -> str:
        """Its own folder, from the repository's root, whatever its status."""
        return f"devices/{self.key}"

    def product_of(self, product_model: str | None) -> str:
        """`product_model` as eWeLink gives it, if it is one of this kind's; else ""."""
        product = (product_model or "").strip().upper()
        return product if self.product.fullmatch(product) else ""

    @property
    def only_product(self) -> str:
        """Its product model where it has only one (POWR316D), so knowing the
        kind is knowing the model; "" where it has several (S60TPG and S60TPF,
        by region), which only eWeLink tells apart."""
        pattern = self.product.pattern
        return pattern if re.fullmatch(r"[A-Z0-9-]+", pattern) else ""

    @property
    def esphome_project(self) -> str:
        """The project its ESPHome is built as (devices/<kind>/esphome.yaml),
        which the device says through its API, with the project's version."""
        return f"{ESPHOME_NAMESPACE}.{self.key}"

    @property
    def esphome_projects(self) -> tuple[str, ...]:
        """Every project name this project's ESPHome for it has been built as:
        esphome_project, and those of images published before it."""
        return (self.esphome_project, *(f"{namespace}.{self.key}" for namespace in EARLIER_ESPHOME_NAMESPACES))

    @property
    def display_name(self) -> str:
        """What the firmware the tool installs says the device is (Sonoff-S60):
        Tasmota's template and ESPHome's comment.  One for every model of the
        kind, so one image serves them all."""
        return f"{self.maker}-{self.short}"

    def tasmota_template(self) -> str:
        """Its Tasmota template, as the Template command takes it."""
        return json.dumps({"NAME": self.display_name, "GPIO": list(self.tasmota_gpio),
                           "FLAG": 0, "BASE": 1}, separators=(",", ":"))

    def names_its_tasmota(self, template_name: str | None) -> bool:
        """Whether a Tasmota's template, by its name, is the one the tool gives
        this kind (Sonoff-S60) - or one an earlier release gave: the kind's
        name, or its maker and product model (Sonoff S60, Sonoff S60TPG)."""
        name = (template_name or "").strip().upper()
        if not self.tasmota_gpio or not name:
            return False
        maker, _, product = name.partition(" ")
        return (name in (self.display_name.upper(), self.name.upper())
                or (maker == self.maker.upper() and bool(self.product_of(product))))

    def names_its_esphome(self, title: str | None, comment: str | None) -> bool:
        """Whether an ESPHome, by its page's comment and title, is the one the
        tool installs on this kind: its comment is the kind's
        (devices/<kind>/esphome.yaml: Sonoff-S61), whatever the device was
        renamed - or, from a release before the comment, its friendly name
        is, alone or with the MAC suffix its page shows (Sonoff S60 abcdef)."""
        if (comment or "").strip().lower() == self.display_name.lower():
            return True
        return bool(self.esphome_legacy_friendly_name) and re.fullmatch(
            re.escape(self.esphome_legacy_friendly_name.lower()) + r"( [0-9a-f]{6})?",
            (title or "").strip().lower()) is not None


SONOFF_S60 = Kind(
    key="sonoff-s60",
    family="espressif",
    chip="esp32c3",
    ecosystem="ewelink",
    name="Sonoff S60",
    maker="Sonoff",
    short="S60",
    # S60TP and the device-type letter: the UK's S60TPG, Europe's S60TPF.
    product=re.compile(r"S60TP[A-Z]"),
    stock_model="SN-ESP32C3-S60-01",
    wrapper_model="FWSW-E32S60-S60-ESP32C3FN4",
    # Relay GPIO4, status LED GPIO5, power meter GPIO7, button GPIO9.  The UK
    # S60TPG and the European S60TPF publish the same pins.
    tasmota_gpio=(1, 1, 1, 1, 224, 544, 1, 3104, 1, 32, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1),
    # devices/sonoff-s60/esphome.yaml. 1.1.0 raised Sonoff-Setup-CDEF; builds before
    # that sonoff-s60- and the MAC's last three bytes.
    esphome_legacy_setup_prefix="Sonoff-Setup-",
    esphome_legacy_prefix="sonoff-s60-",
    layout="sonoff-dual-ota",
    has_meter=True,
    has_relay=True,
    sensors=(),
    esphome_legacy_friendly_name="Sonoff S60",
    calibrated_as_s60=True,
    example_product_model="S60TPG",
)

SONOFF_S61 = Kind(
    key="sonoff-s61",
    family="beken",
    chip="bk7238",
    ecosystem="ewelink",
    name="Sonoff S61",
    maker="Sonoff",
    short="S61",
    # The S61S (iPlug Gen2): S61STP and the device-type letter, Europe's S61STPF.
    product=re.compile(r"S61STP[A-Z]"),
    stock_model="SN-BK7238-S61-01",
    # The descriptor name in the FOTA container its stock updater stages (tools/beken/fota_wrap.py).
    wrapper_model="FWSW-01-S61-BK7238",
    tasmota_gpio=(),  # Tasmota does not run on the BK7238: OpenBeken does
    # devices/sonoff-s61/esphome.yaml; no earlier builds, so no older names.
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    layout="beken-4m",
    has_meter=True,
    has_relay=True,
    sensors=(),
    openbeken_revert="AutoflashRevertToStock",
    example_product_model="S61STPF",
)

# Known from eWeLink's official update file and a pin map, not from a dump of one
# (tools/beken/layouts.py checks that file's table): devices/sonoff-basic-1gs/.
SONOFF_BASIC_1GS = Kind(
    key="sonoff-basic-1gs",
    family="beken",
    chip="bk7238",
    ecosystem="ewelink",
    name="Sonoff Basic Gen5",
    maker="Sonoff",
    short="BASIC-1GS",
    # Not the BASIC-1GSP, the DIN rail one, on other firmware.
    product=re.compile(r"BASIC-1GS"),
    stock_model="SN-BK7238-BASICR5-01",
    wrapper_model="FWSW-01-BASICR5-BK7238",
    tasmota_gpio=(),  # Tasmota does not run on the BK7238: OpenBeken does
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    layout="beken-4m",
    has_meter=False,
    has_relay=True,
    sensors=(),
    openbeken_revert="AutoflashRevertToStock",
    example_product_model="BASIC-1GS",
    # docs/adding-a-device.md#a-beken-device
)

SONOFF_POW_ELITE = Kind(
    key="sonoff-pow-elite",
    family="espressif",
    chip="esp32",
    ecosystem="ewelink",
    name="Sonoff POW Elite 16A",
    maker="Sonoff",
    short="POWR316D",
    # The 16 A alone: the POWR320D has the same stock firmware's name, and a
    # latching relay on other pins, which this wiring would not drive.
    product=re.compile(r"POWR316D"),
    stock_model="SN-ESP32D0-POWR3-01",
    wrapper_model="FWSW-E32POWP-POWPRO-ESP32D0WDV3",
    # Blakadder's template: button GPIO0, Wi-Fi LED GPIO5, relay GPIO13, TM1621
    # display GPIO14/25/26/27, CSE7766 GPIO16, status LED GPIO18.
    tasmota_gpio=(32, 0, 0, 0, 0, 576, 0, 0, 0, 224, 9280, 0, 3104, 0, 320, 0, 0, 0, 0, 0, 0,
                  9184, 9248, 9216, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    layout="sonoff-dual-ota",
    has_meter=True,
    has_relay=True,
    sensors=(),
    product_model_decides=True,
    example_product_model="POWR316D",
)

SONOFF_POWCT = Kind(
    key="sonoff-powct",
    family="espressif",
    chip="esp32",
    ecosystem="ewelink",
    name="Sonoff POW Ring",
    maker="Sonoff",
    short="POWCT",
    product=re.compile(r"POWCT"),
    stock_model="SN-ESP32D0-POWCT-01",
    wrapper_model="FWSW-E32POWCT-POWCT-ESP32D0WDV3",
    # Tasmota's own template (its CSE7761 driver, xnrg_19): button GPIO0,
    # TM1621 GPIO5/17/18/23, status LED GPIO13 (Led1, not inverted: Sonoff's
    # firmware drives it high while the relay is on, devices/sonoff-powct/README.md),
    # Wi-Fi LED GPIO15 (inverted), relay GPIO21, CSE7761 RX GPIO25 and TX GPIO26.
    # The meter's RX is CSE7761 Rx 2 (7329), which tells Tasmota it is a
    # POWCT's: its clamp's gain and scale, and the display showing the meter.
    # Rx 1 (7328) is a Dual R3's, with a shunt.
    tasmota_gpio=(32, 0, 0, 0, 0, 9280, 0, 0, 0, 288, 0, 576, 0, 9184, 9216, 0, 0, 224, 0, 9248, 0,
                  7329, 7296, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    layout="sonoff-dual-ota",
    has_meter=True,
    has_relay=True,
    sensors=(),
    example_product_model="POWCT",
)

SONOFF_NSPANEL = Kind(
    key="sonoff-nspanel",
    family="espressif",
    chip="esp32",
    ecosystem="ewelink",
    name="Sonoff NSPanel",
    maker="Sonoff",
    short="NSPanel",
    # NSPanel-EU, NSPanel-US and the like; not the NSPanel Pro, which is another device.
    product=re.compile(r"NSPANEL(-[A-Z]+)?"),
    stock_model="E32-MSW-NX",
    wrapper_model="FWPNL-E32NSP-NSPANEL-ESP32D0WDV3",
    psram_mb=4,
    # Blakadder's template: screen power GPIO4, buttons GPIO14 and GPIO27, relays
    # GPIO22 and GPIO19, buzzer GPIO21, thermistor GPIO38.
    tasmota_gpio=(0, 0, 0, 0, 3872, 0, 0, 0, 0, 0, 32, 0, 0, 0, 0, 225, 0, 480, 224, 1, 0, 0, 0, 33, 0, 0, 0, 0,
                  0, 0, 0, 0, 0, 0, 4736, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    layout="sonoff-nspanel",
    has_meter=False,
    has_relay=True,
    # Not its room thermistor yet: its ESPHome does not read it, and in the
    # emulator every classic-ESP32 ADC reads 0 (devices/sonoff-nspanel/research.md).
    sensors=(),
    example_product_model="NSPANEL",
)

SONOFF_TH_ELITE = Kind(
    key="sonoff-th-elite",
    family="espressif",
    chip="esp32",
    ecosystem="ewelink",
    name="Sonoff TH Elite 20A",
    maker="Sonoff",
    short="THR320D",
    # The 20 A alone: the THR316D's stock firmware has the same name, and its
    # relay is wired otherwise.
    product=re.compile(r"THR320D"),
    stock_model="SN-ESP32D0-THR3-01",
    wrapper_model="FWSW-E32THR3-THR3-ESP32D0WDV3",
    # Blakadder's template: button GPIO0, dry contact GPIO4, TM1621 GPIO5/17/18/23,
    # LEDs GPIO13/15/16, the main relay's latching pair GPIO19 and GPIO22,
    # the sensor's data GPIO25 and power GPIO27.
    tasmota_gpio=(32, 0, 0, 0, 226, 9280, 0, 0, 0, 321, 0, 576, 320, 9184, 9216, 9312, 0, 0, 9313, 9248, 0, 1, 0,
                  3840, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    layout="sonoff-th-elite",
    has_meter=False,
    has_relay=True,
    # A THS01 on its sensor socket, if one is plugged in.
    sensors=("temperature", "humidity"),
    product_model_decides=True,
    example_product_model="THR320D",
)

# Known from eWeLink's public firmware for them, not from a dump of one: eWeLink's
# product model says what they are, and a table not listed for them stops the
# run while they can still go back (installer.require_known_layout). The
# BASICR4's stock model and firmware name are read from its official update
# file (tools/espressif/emulator/public_dump.py); its pins are published ones:
# devices/sonoff-basicr4/.
SONOFF_BASICR4 = Kind(
    key="sonoff-basicr4",
    family="espressif",
    chip="esp32c3",
    ecosystem="ewelink",
    name="Sonoff BASICR4",
    maker="Sonoff",
    short="BASICR4",
    product=re.compile(r"BASICR4"),
    stock_model="SN-ESP32C3-BASICR4-01",
    wrapper_model="FWSW-E32R4-BASICR4-ESP32C3",
    # Blakadder's template: relay GPIO4, Wi-Fi LED GPIO6 (LedLink), button GPIO9.
    # GPIO5 is left unset: a zero-cross detector drives it, for Sonoff's Magic
    # Switch, and nothing may drive it back.
    tasmota_gpio=(0, 0, 0, 0, 224, 0, 544, 0, 0, 32, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    # A stand-in: its table is at 0xC000 and the rest of it is not known
    # (devices/sonoff-basicr4/README.md). A readout of a real one decides its
    # layout before it is converted; require_known_layout stops a run until then.
    layout="sonoff-dual-ota",
    has_meter=False,
    has_relay=True,
    sensors=(),
    product_model_decides=True,
    example_product_model="BASICR4",
    # Its stock firmware watches a thermistor on GPIO3 (ADC1 channel 3) and, read
    # under 580 mV, takes itself to be overheating and refuses updates (error 407).
    # This reads about 1 V, as such a divider does at room temperature.
    adc_readings=(("adc1_ch3", 2700),),
)

SONOFF_MINIR4 = Kind(
    key="sonoff-minir4",
    family="espressif",
    chip="esp32",
    ecosystem="ewelink",
    name="Sonoff MINI Extreme",
    maker="Sonoff",
    short="MINIR4",
    # Not the MINIR4M, the Matter one, which is locked.
    product=re.compile(r"MINIR4"),
    # Read from its official update file (tools/espressif/emulator/public_dump.py).
    stock_model="SN-ESP32D0-MINIR4-01",
    wrapper_model="FWSW-E32R4-MINIR4-ESP32D0WDR2V3",
    psram_mb=2,  # the R2 in its chip's name
    # Blakadder's template: button GPIO0, LED GPIO19, relay GPIO26, wall switch GPIO27.
    tasmota_gpio=(32, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 576, 0, 0, 0, 0, 0, 0, 224, 160, 0, 0, 0, 0, 0, 0,
                  0, 0, 0, 0, 0, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    # The S60's, by the table published from a real one (devices/sonoff-minir4/).
    layout="sonoff-dual-ota",
    has_meter=False,
    has_relay=True,
    sensors=(),
    product_model_decides=True,
    example_product_model="MINIR4",
)

SONOFF_DUALR3 = Kind(
    key="sonoff-dualr3",
    family="espressif",
    chip="esp32",
    ecosystem="ewelink",
    name="Sonoff Dual R3",
    maker="Sonoff",
    short="DUALR3",
    product=re.compile(r"DUALR3(LITE)?"),
    stock_model="",
    wrapper_model="FWSWES-E32DR3-DUALR3-ESP32D0WDV3",
    # Blakadder's template: button GPIO0, LED GPIO13, relays GPIO27 and GPIO14,
    # CSE7761 GPIO25 and GPIO26, wall switches GPIO32 and GPIO33.
    tasmota_gpio=(32, 0, 0, 0, 0, 0, 0, 0, 0, 576, 225, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 7296, 7328, 224, 0, 0, 0, 0,
                  160, 161, 0, 0, 0, 0, 0, 0),
    esphome_legacy_setup_prefix="",
    esphome_legacy_prefix="",
    # A real one's boot log: laid out as the TH Elite is, not as the POW Elite.
    layout="sonoff-th-elite",
    has_meter=True,
    has_relay=True,
    sensors=(),
    product_model_decides=True,
    example_product_model="DUALR3",
)


def _sonoff_m5(gang: int, gpio: tuple[int, ...]) -> Kind:
    """An M5 wall switch of so many gangs: one firmware, wired each its own way.
    Not the Matter ones, whose part numbers end in W: those are locked."""
    return Kind(
        key=f"sonoff-m5-{gang}c",
        family="espressif",
        chip="esp32",
        ecosystem="ewelink",
        name=f"Sonoff M5 {gang}-gang",
        maker="Sonoff",
        short=f"M5-{gang}C",
        product=re.compile(rf"M5-{gang}C-\d+"),
        stock_model="",
        wrapper_model="FWSWES-E32T5-T5-ESP32D0WDV3",
        tasmota_gpio=gpio,
        esphome_legacy_setup_prefix="",
        esphome_legacy_prefix="",
        layout="sonoff-dual-ota",
        has_meter=False,
        has_relay=True,
        sensors=(),
        product_model_decides=True,
        example_product_model=f"M5-{gang}C-120",
    )


# From ESPHome's table: buttons, relays, status LED GPIO5.
SONOFF_M5_1C = _sonoff_m5(1, (32, 0, 0, 0, 0, 576, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 224, 0, 0, 0, 0, 0, 0, 0,
                              0, 0, 0, 0, 0, 0, 0, 0, 0))
SONOFF_M5_2C = _sonoff_m5(2, (0, 0, 0, 0, 32, 576, 0, 0, 0, 0, 0, 33, 0, 0, 0, 225, 0, 0, 0, 224, 0, 0, 0, 0, 0, 0, 0,
                              0, 0, 0, 0, 0, 0, 0, 0, 0))
SONOFF_M5_3C = _sonoff_m5(3, (33, 0, 0, 0, 32, 576, 0, 0, 0, 0, 0, 34, 0, 0, 0, 225, 0, 0, 226, 224, 0, 0, 0, 0, 0, 0,
                              0, 0, 0, 0, 0, 0, 0, 0, 0, 0))

# Every kind, the S60 first.
EVERY = (SONOFF_S60, SONOFF_S61, SONOFF_POW_ELITE, SONOFF_POWCT, SONOFF_TH_ELITE, SONOFF_DUALR3, SONOFF_BASIC_1GS,
         SONOFF_NSPANEL, SONOFF_BASICR4, SONOFF_MINIR4, SONOFF_M5_1C, SONOFF_M5_2C, SONOFF_M5_3C)
# Every kind the tool converts.
READY = tuple(kind for kind in EVERY if kind.ready)
# Being worked on: not converted yet. For the emulator, this variable has the
# tool take them as it does the others: never for a real device.
NOT_READY_YET = tuple(kind for kind in EVERY if not kind.ready)
ALL = READY + (NOT_READY_YET if os.environ.get("AUTOFLASH_NOT_READY_YET") else ())


def find(key: str | None) -> Kind | None:
    """The kind by its key, ready or not, for the tooling that works on them."""
    return next((kind for kind in EVERY if kind.key == key), None)


def by_key(key: str | None) -> Kind | None:
    return next((kind for kind in ALL if kind.key == key), None)


def for_account(product_model: str | None, stock_model: str | None, among: tuple[Kind, ...] = ALL) -> Kind | None:
    """The kind of a device in an eWeLink account, by its product model or by
    what its firmware calls itself (the account's extra.model); None for one
    the tool does not convert. `among`: the emulator's tooling, which runs a
    kind not ready yet too, gives EVERY."""
    return next((kind for kind in among
                 if kind.product_of(product_model)
                 or (stock_model and stock_model == kind.stock_model and not kind.product_model_decides)), None)


def for_stock_model(stock_model: str | None) -> Kind | None:
    """The kind whose stock firmware calls itself `stock_model`."""
    return next((kind for kind in ALL if stock_model and stock_model == kind.stock_model), None)


def for_tasmota(template_name: str | None) -> Kind | None:
    """The kind of a converted Tasmota, by its template's name."""
    return next((kind for kind in ALL if kind.names_its_tasmota(template_name)), None)


def on_chip(chipset: str | None) -> tuple[Kind, ...]:
    """The kinds an OpenBeken on this chip (as /api/info names it: BK7238)
    could be on: the ones to ask it about (openbeken.kind)."""
    chip = (chipset or "").strip().lower()
    return tuple(kind for kind in ALL if kind.family == "beken" and kind.chip == chip)


def for_esphome(title: str | None, comment: str | None) -> Kind | None:
    """The kind of a converted ESPHome, by its page's title and comment."""
    return next((kind for kind in ALL if kind.names_its_esphome(title, comment)), None)


def names(attribute: str = "name") -> str:
    """Every kind, as a person calls them (Sonoff S60): for before the device is known."""
    all_of_them = [getattr(kind, attribute) for kind in ALL]
    return ", ".join(all_of_them[:-1]) + " or " + all_of_them[-1]


def name_words() -> set[str]:
    """The words of every kind's name, in lower case: an eWeLink name of only
    these (and its product model) is the one eWeLink gave, which names nobody."""
    return {word for kind in ALL for word in kind.name.lower().split()}


def of_run(state) -> Kind:
    """The kind a run converts (`state`: its RunState): as it recorded once
    known, else the first the tool knows - the only one, so far."""
    return by_key(state.input("device_kind")) or ALL[0]
