"""The kinds of device the tool converts, and what is each one's own."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.autoflash import kinds
from tools.autoflash.state import RunState


S60 = kinds.SONOFF_S60
ROOT = Path(__file__).resolve().parents[2]


class KnownByTests(unittest.TestCase):
    def test_an_account_device_is_known_by_its_product_model_or_its_firmware(self):
        self.assertIs(kinds.for_account("S60TPG", ""), S60)
        self.assertIs(kinds.for_account(" s60tpf ", None), S60)
        self.assertIs(kinds.for_account("", "SN-ESP32C3-S60-01"), S60)
        self.assertIsNone(kinds.for_account("MINIR4M", "SN-ESP32-MINIR4M"))
        self.assertIsNone(kinds.for_account("S60ZBTPF", ""))  # the Zigbee S60

    def test_a_kind_with_one_product_model_gives_it_and_one_with_several_does_not(self):
        self.assertEqual(kinds.SONOFF_POW_ELITE.only_product, "POWR316D")
        self.assertEqual(kinds.SONOFF_BASIC_1GS.only_product, "BASIC-1GS")
        for several in (S60, *(kind for kind in kinds.EVERY if kind.product.pattern.startswith(("NSPANEL", "DUALR3",
                                                                                                "M5-")))):
            self.assertEqual(several.only_product, "", several.key)
        for kind in kinds.EVERY:
            if kind.only_product:
                self.assertIs(kinds.for_account(kind.only_product, None, among=kinds.EVERY), kind)

    def test_a_stock_firmware_more_than_one_device_calls_itself_is_not_enough(self):
        # The POWR320D's relay is a latching one on other pins, and the Matter
        # M5's firmware is locked: neither is to be taken for its neighbour.
        self.assertIs(kinds.for_account("POWR316D", "SN-ESP32D0-POWR3-01"), kinds.SONOFF_POW_ELITE)
        self.assertIsNone(kinds.for_account("POWR320D", "SN-ESP32D0-POWR3-01"))
        self.assertIsNone(kinds.for_account("", "SN-ESP32D0-POWR3-01"))
        self.assertIsNone(kinds.for_account("THR316D", "SN-ESP32D0-THR3-01"))
        self.assertIsNone(kinds.for_account("M5-3C-86W", ""))

    def test_each_template_has_a_pin_for_each_of_its_chips(self):
        counts = {"esp32": 36, "esp32c3": 22}
        for kind in kinds.ALL:
            if kind.family == "espressif":
                self.assertEqual(len(kind.tasmota_gpio), counts[kind.chip], kind.key)

    def test_a_stock_firmware_by_what_it_calls_itself(self):
        self.assertIs(kinds.for_stock_model("SN-ESP32C3-S60-01"), S60)
        self.assertIsNone(kinds.for_stock_model("SN-ESP32-MINIR4M"))
        self.assertIsNone(kinds.for_stock_model(None))

    def test_a_converted_tasmota_by_its_template(self):
        self.assertIs(kinds.for_tasmota("Sonoff-S60"), S60)
        self.assertIs(kinds.for_tasmota(" sonoff-s60 "), S60)
        # As earlier releases named it.
        self.assertIs(kinds.for_tasmota("Sonoff S60TPG"), S60)
        self.assertIs(kinds.for_tasmota("Sonoff S60"), S60)
        self.assertIsNone(kinds.for_tasmota("Generic"))
        self.assertIsNone(kinds.for_tasmota(None))

    def test_a_tasmota_template_only_naming_the_kind_in_passing_is_not_its(self):
        # A name holding S60 is not enough: another device's, or the user's own.
        for name in ("Sonoff S600", "Sonoff S60ZBTPF", "Sonoff-S60 v2", "My S60 lamp", "Sonoff-S61"):
            with self.subTest(name=name):
                self.assertIsNone(kinds.for_tasmota(name))

    def test_by_its_key(self):
        self.assertIs(kinds.by_key("sonoff-s60"), S60)
        self.assertIsNone(kinds.by_key("sonoff-minir4m"))
        self.assertIsNone(kinds.by_key(None))


class NamesTests(unittest.TestCase):
    def test_every_firmware_says_what_device_it_is_alike(self):
        # One for every model of the kind: one image serves them all.
        self.assertEqual((S60.display_name, kinds.SONOFF_S61.display_name), ("Sonoff-S60", "Sonoff-S61"))
        self.assertEqual(json.loads(S60.tasmota_template())["NAME"], "Sonoff-S60")

    def test_each_kinds_esphome_is_built_as_the_project_its_updates_look_for(self):
        from tools.autoflash.runs import ROOT
        for kind in kinds.ALL:
            config = (ROOT / "devices" / kind.key / "esphome.yaml").read_text(encoding="utf-8")
            self.assertRegex(config, rf"(?m)^  project:\n    name: {re.escape(kind.esphome_project)}\n", kind.key)

    def test_its_esphome_is_known_by_the_name_images_before_2_4_0_had_too(self):
        self.assertEqual(S60.esphome_project, "liberator.sonoff-s60")
        self.assertEqual(S60.esphome_projects, ("liberator.sonoff-s60", "adamsthws.sonoff-s60"))

    def test_an_esphome_is_known_by_its_comment_whatever_its_name(self):
        S61 = kinds.SONOFF_S61
        self.assertIs(kinds.for_esphome("esphome abcdef", "Sonoff-S61"), S61)
        self.assertIs(kinds.for_esphome("Kitchen", "Sonoff-S60"), S60)   # renamed
        self.assertIsNone(kinds.for_esphome("esphome abcdef", ""))
        self.assertIsNone(kinds.for_esphome(None, None))

    def test_an_s60_esphome_from_before_the_comment_is_known_by_its_friendly_name(self):
        # Released S60 images (1.4.0 and before) gave no comment.
        for title in ("Sonoff S60", "sonoff s60 abcdef"):
            self.assertIs(kinds.for_esphome(title, ""), S60, title)
        self.assertIsNone(kinds.for_esphome("Sonoff S61 abcdef", ""))   # never released so
        self.assertIsNone(kinds.for_esphome("Sonoff S60 kitchen", ""))

    def test_an_openbeken_is_asked_on_our_chips_alone(self):
        self.assertEqual(kinds.on_chip("BK7238"), (kinds.SONOFF_S61, kinds.SONOFF_BASIC_1GS))
        self.assertEqual(kinds.on_chip("BK7231N"), ())
        self.assertEqual(kinds.on_chip(None), ())

    def test_each_kinds_firmware_says_what_the_tool_looks_for(self):
        # What each firmware says it is, in the device's own files, against what
        # the tool tells the kind by: a kind added with one of them missed is
        # never recognised once converted.
        for kind in kinds.ALL:
            with self.subTest(kind=kind.key):
                config = (ROOT / "devices" / kind.key / "esphome.yaml").read_text(encoding="utf-8")
                self.assertRegex(config, rf"(?m)^  comment: {re.escape(kind.display_name)}$")
                self.assertIs(kinds.for_esphome("esphome abcdef", kind.display_name), kind)
                if kind.family != "beken":
                    continue
                # Its OpenBeken answers AutoflashKind by its table for the driver.
                tables = list((ROOT / "devices" / kind.key).glob("drv_autoflash_*.c"))
                self.assertEqual(len(tables), 1, tables)
                table = tables[0].read_text(encoding="utf-8")
                self.assertIn(f'.kind = "{kind.key}",', table)
                self.assertIn(f'.stock_model = "{kind.stock_model}",', table)
                self.assertIn(kind, kinds.on_chip(kind.chip.upper()))

    def test_before_the_device_is_known_every_kind_is_named(self):
        self.assertIn("Sonoff S60, Sonoff S61", kinds.names())
        self.assertRegex(kinds.names("short"), r"^S60, S61.* or \S+$")
        self.assertLessEqual({"sonoff", "s60", "s61"}, kinds.name_words())

    def test_every_kind_is_named_once_the_last_after_or(self):
        named = kinds.names().split(", ")
        named[-1:] = named[-1].split(" or ")
        self.assertEqual(named, [kind.name for kind in kinds.ALL])


class RunTests(unittest.TestCase):
    def state(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return RunState.open(Path(directory.name))

    def test_a_run_converts_the_kind_it_recorded(self):
        state = self.state()
        state.set_input("device_kind", "sonoff-s60")
        self.assertIs(kinds.of_run(state), S60)

    def test_one_that_recorded_none_the_first_the_tool_knows(self):
        # A run from before kinds were recorded: the S60's, the only kind then.
        self.assertIs(kinds.of_run(self.state()), kinds.ALL[0])


class ConversionTests(unittest.TestCase):
    def test_only_the_s60s_meter_is_calibrated_as_the_s60s(self):
        self.assertEqual([kind for kind in kinds.EVERY if kind.calibrated_as_s60], [S60])


class LayoutTests(unittest.TestCase):
    def test_a_dual_r3_is_laid_out_as_a_real_ones_boot_log_shows(self):
        self.assertEqual(kinds.SONOFF_DUALR3.layout, "sonoff-th-elite")

    def test_each_beken_kind_is_on_a_beken_table(self):
        from tools.beken import layouts as beken_layouts
        for kind in kinds.EVERY:
            if kind.family == "beken":
                with self.subTest(kind=kind.key):
                    self.assertEqual(beken_layouts.of(kind.layout).key, kind.layout)


class BasicR4Tests(unittest.TestCase):
    """Its pins as published: Blakadder's template, which ESPHome's device page
    and digiblur's agree with but for the LED's polarity."""
    KIND = kinds.SONOFF_BASICR4

    def config(self):
        return (ROOT / self.KIND.folder / "esphome.yaml").read_text(encoding="utf-8")

    def test_its_template_is_blakadders(self):
        relay, led_link, button = 224, 544, 32
        gpio = dict(enumerate(self.KIND.tasmota_gpio))
        self.assertEqual({pin: use for pin, use in gpio.items() if use}, {4: relay, 6: led_link, 9: button})

    def test_its_esphome_has_the_same_pins(self):
        config = self.config()
        self.assertRegex(config, r"(?m)^  - id: relay\n    platform: gpio\n    name: \"Relay\"\n    pin: GPIO4\n")
        self.assertRegex(config, r"(?m)^status_led:\n  pin:\n    number: GPIO6\n    inverted: false\n")
        self.assertRegex(config, r"(?m)^    pin:\n      number: GPIO9\n      mode: INPUT_PULLUP\n      inverted: true\n")

    def test_nothing_drives_its_zero_cross_input(self):
        # GPIO5 is driven by the board, for Sonoff's Magic Switch: neither firmware sets it.
        self.assertEqual(self.KIND.tasmota_gpio[5], 0)
        code = "\n".join(line.split("#", 1)[0] for line in self.config().splitlines())
        self.assertNotRegex(code, r"GPIO_?(NUM_)?0?5\b")


class M5Tests(unittest.TestCase):
    """Each M5's template wires what its ESPHome does: a button, a relay for
    each gang, and the status LED - and nothing else."""

    def test_its_template_has_the_pins_its_esphome_has(self):
        from tools.espressif.emulator.board import TEMPLATE_GPIOS
        button, relay, led_link_inverted = 32, 224, 576
        for kind in (kinds.SONOFF_M5_1C, kinds.SONOFF_M5_2C, kinds.SONOFF_M5_3C):
            with self.subTest(kind=kind.key):
                config = (ROOT / kind.folder / "esphome.yaml").read_text(encoding="utf-8")
                expected = {int(gpio): relay + int(n or 1) - 1 for n, gpio in re.findall(
                    r"(?m)^  - id: relay(?:_(\d))?\n    platform: gpio\n    name: .*\n    pin: GPIO(\d+)\n", config)}
                expected |= {int(gpio): button + int(n) - 1 for n, gpio in re.findall(
                    r"(?m)^    id: button_(\d)\n    internal: true\n    pin:\n      number: GPIO(\d+)\n", config)}
                led = re.search(r"(?m)^status_led:\n  pin:\n    number: GPIO(\d+)\n    inverted: true\n", config)
                expected[int(led.group(1))] = led_link_inverted
                gang = int(kind.key[-2])
                self.assertEqual(len(expected), 2 * gang + 1)
                gpio = dict(zip(TEMPLATE_GPIOS[kind.chip], kind.tasmota_gpio))
                self.assertEqual({pin: use for pin, use in gpio.items() if use}, expected)


class NotReadyYetTests(unittest.TestCase):
    def test_the_tool_turns_them_down_as_any_device_it_does_not_know(self):
        for kind in kinds.NOT_READY_YET:
            self.assertNotIn(kind, kinds.ALL)
            self.assertIsNone(kinds.by_key(kind.key))
            self.assertIsNone(kinds.for_account(kind.example_product_model, kind.stock_model))

    def test_the_tooling_that_works_on_them_still_finds_them(self):
        for kind in kinds.NOT_READY_YET:
            self.assertIs(kinds.find(kind.key), kind)
            self.assertTrue((ROOT / kind.folder / "esphome.yaml").is_file(), kind.key)
            self.assertFalse(kind.ready)

    def test_the_emulator_can_ask_for_them(self):
        code = "from tools.autoflash import kinds; print(kinds.for_account('M5-3C-120', '').key)"
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                              env={**os.environ, "AUTOFLASH_NOT_READY_YET": "1"}, check=True)
        self.assertEqual(done.stdout.strip(), "sonoff-m5-3c")


class MeterTests(unittest.TestCase):
    def test_a_kind_has_a_meter_exactly_where_its_esphome_reads_one(self):
        # The report's Meter calibration line is only for a device with one.
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        chip = re.compile(r"platform: *(cse7766|cse7761|bl0942|bl0937|bl0940|hlw8012|hlw8032|ade7953)")
        for kind in kinds.READY + kinds.NOT_READY_YET:
            config = root / "devices" / kind.key / "esphome.yaml"
            if config.exists():
                self.assertEqual(kind.has_meter, bool(chip.search(config.read_text(encoding="utf-8"))), kind.key)

    def test_a_bl0937s_sel_is_inverted_for_esphome(self):
        # hlw8012 drives SEL high for the current, as an HLW8012 takes it; a BL0937
        # measures the voltage with SEL high (ESPHome's hlw8012 page, Tasmota's
        # driver, OpenBeken's S61 template). Not inverted, each reads the other.
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for config in sorted(root.glob("devices/*/esphome.yaml")):
            text = config.read_text(encoding="utf-8")
            for meter in re.finditer(r"(?m)^  - platform: hlw8012\n((?:    .*\n)+)", text):
                if re.search(r"model: BL0937\n", meter.group(1)):
                    self.assertRegex(meter.group(1), r"sel_pin:\n +number: \w+\n +inverted: true\n",
                                     config.parent.name)

    def test_a_meters_wiring_is_the_template_tasmota_publishes_for_it(self):
        # Tasmota 16.0.0's drivers give these (xnrg_19_cse7761, xdrv_87_esp32_sonoff_tm1621),
        # as Blakadder's templates do the POW Elite's and the Dual R3's. Which CSE7761 RX
        # a template uses, Rx 1 (7328) or Rx 2 (7329), tells Tasmota which device's meter it is.
        # The POWCT's is xnrg_19's, with its status LED as Led1 (288) on GPIO13; xdrv_87's
        # copy still has Led_i (320) there, which Sonoff's own firmware shows is wrong.
        published = {
            kinds.SONOFF_POW_ELITE: [32, 0, 0, 0, 0, 576, 0, 0, 0, 224, 9280, 0, 3104, 0, 320, 0, 0, 0, 0, 0, 0,
                                     9184, 9248, 9216, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            kinds.SONOFF_POWCT: [32, 0, 0, 0, 0, 9280, 0, 0, 0, 288, 0, 576, 0, 9184, 9216, 0, 0, 224, 0, 9248, 0,
                                 7329, 7296, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            kinds.SONOFF_DUALR3: [32, 0, 0, 0, 0, 0, 0, 0, 0, 576, 225, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 7296, 7328, 224,
                                  0, 0, 0, 0, 160, 161, 0, 0, 0, 0, 0, 0],
        }
        for kind, gpio in published.items():
            self.assertEqual(json.loads(kind.tasmota_template())["GPIO"], gpio, kind.key)


class TemplateNameTests(unittest.TestCase):
    """Tasmota's template is named as every firmware names the device."""

    def test_the_wiring_is_the_tested_s60tpg_template(self):
        # Byte for byte the template proven on the UK devices, apart from the name.
        self.assertEqual(
            kinds.SONOFF_S60.tasmota_template(),
            '{"NAME":"Sonoff-S60","GPIO":[1,1,1,1,224,544,1,3104,1,32,1,0,0,0,0,0,0,0,'
            '1,1,1,1],"FLAG":0,"BASE":1}',
        )


if __name__ == "__main__":
    unittest.main()
