"""report_data: a run's report as the API takes it, and nothing private in it."""

from __future__ import annotations

import hashlib
import json
import unittest
from unittest import mock

from tests.autoflash.routes.espressif.test_espressif import RouteTestCase
from tools.autoflash import join, report_data
from tools.repo import report_schema

REPORT_ID = "0123456789abcdef"
MAC = "12:34:56:ab:cd:ef"


class PayloadTests(RouteTestCase):
    def payload(self, ctx, ending="converted", hardware=None, failure=""):
        ctx.last_failure = failure
        ctx.last_failed_step = "D4" if failure else ""
        data = report_data.payload(ctx, REPORT_ID, 512.4, False, ending, hardware)
        # Held to the schema the API holds it to, as written: report-schema.json.
        written = json.loads(report_schema.PATH.read_text(encoding="utf-8"))
        problems = report_schema.validate(written, {**data, "proof": "1794000000:1"})
        self.assertEqual(problems, [], data)
        return data

    def test_a_converted_run_fits_the_schema(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60")
        data = self.payload(ctx, hardware={"Relay": "switches (meter confirms)"})
        self.assertEqual((data["result"], data["stopped"], data["checks"]), ("converted", None, "passed"))
        self.assertEqual((data["device"], data["model"], data["stock_version"]), ("sonoff-s60", "S60TPF", "1.2.0"))
        self.assertEqual(data["seconds"], 512)

    def test_nothing_private_goes_in_it(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60", device_mac=MAC, email="me@example.com",
                       device_name="kitchen")
        sent = json.dumps(self.payload(ctx, "failed",
                                       failure="nothing answers at 192.0.2.50 (12:34:56:ab:cd:ef): check it"))
        for private in ("192.0.2.50", MAC, MAC.replace(":", ""), "me@example.com", "kitchen"):
            self.assertNotIn(private.lower(), sent.lower(), private)
        self.assertIn(report_data.device_hash(MAC), sent)

    def test_a_failed_run_says_where_and_what_kind_in_plain_words(self):
        ctx = self.ctx(firmware="esphome", device_kind="sonoff-s60")
        data = self.payload(ctx, "failed", failure="the device at 192.0.2.50 did not answer within 30s — café")
        self.assertEqual(data["stopped"]["step"], "D4")
        self.assertEqual(data["stopped"]["error"], "device-no-answer")
        self.assertEqual(data["stopped"]["text"], "the device at <address> did not answer within 30s  caf")
        self.assertEqual(self.payload(ctx, "crashed", failure="KeyError: x")["stopped"]["error"], "crash")

    def test_a_conversion_gives_the_devices_number_on_this_computer_and_nothing_else_does(self):
        def nth(ending="converted", **kwargs):
            ctx = self.ctx(device_kind="sonoff-s60", device_mac=MAC, **kwargs)
            with mock.patch.object(report_data.records, "read_device", return_value={"converted_number": 4}):
                return self.payload(ctx, ending, failure="the device did not answer" if ending != "converted" else "")[
                    "nth_device"]
        self.assertEqual(nth(firmware="tasmota"), 4)
        self.assertEqual(nth(firmware="esphome", source="tasmota"), 4, "from another firmware too")
        self.assertIsNone(nth(firmware="sonoff", source="tasmota"), "back to Sonoff's is no conversion")
        self.assertIsNone(nth(firmware="tasmota", source="outdated"), "nor an update")
        self.assertIsNone(nth(firmware="tasmota", ending="failed"))

    def test_a_crash_says_where_in_the_tools_code_and_nothing_made_up(self):
        ctx = self.ctx(firmware="esphome", device_kind="sonoff-s60")
        ctx.crash_site = "KeyError at tools/autoflash/report.py:212"
        self.assertEqual(self.payload(ctx, "crashed", failure="KeyError: x")["stopped"]["crash_site"], ctx.crash_site)
        ctx.crash_site = "KeyError at /home/someone/x.py:1"   # never sent: not the tool's own
        self.assertIsNone(self.payload(ctx, "crashed", failure="KeyError: x")["stopped"]["crash_site"])

    def test_what_the_run_and_this_computer_say_of_themselves(self):
        from types import SimpleNamespace
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60", device_ip="192.0.2.50")
        ctx.ewelink = SimpleNamespace(held=("eu", {"at": "token"}))
        ctx.state.set_input("hardware", {"revision": "0.4", "flash_id": "c84016", "flash_mb": 4})
        ctx.state.set_input("firewall_outcome", "added")
        ctx.state.set_input("back_after_power_cycle", 41)
        ctx.state.capture("A1.seconds", 12)
        ctx.state.capture("A1.waited", 75)
        with mock.patch.dict(report_data.os.environ, {"AUTOFLASH_LAUNCHER": "start.cmd"}), \
                mock.patch.object(report_data.hostsys, "connection_towards", return_value="wifi") as towards, \
                mock.patch.object(report_data.hostsys, "vpn_active", return_value=False), \
                mock.patch.object(report_data.hostsys, "lan_networks", return_value={"192.0.2.10": 24}), \
                mock.patch.object(report_data.hostsys, "asked_for_admin", True), \
                mock.patch.object(report_data.hostsys, "security_note_shown", False), \
                mock.patch.object(report_data.interrupt, "carried_on", return_value=2):
            data = self.payload(ctx)
        towards.assert_called_once_with("192.0.2.50")
        self.assertEqual(data["chip"], {"revision": "0.4", "flash_id": "c84016", "flash_mb": 4})
        self.assertEqual(data["computer"], {"connection": "wifi", "vpn": False, "networks": 1, "security_note": False,
                                            "firewall": "added", "admin": True})
        self.assertEqual((data["ewelink_region"], data["ctrl_c_carried_on"], data["launcher"], data["back_after_power_cycle"]),
                         ("eu", 2, "start.cmd", 41))
        self.assertIn(data["install"], report_data.INSTALLS)
        self.assertIn(data["os"]["arch"], report_data.ARCHES)
        self.assertEqual(data["stages"]["A1"]["wait"], 75)

    def test_the_firmware_name_the_device_checks_is_sent_and_whether_it_is_the_tools(self):
        from tools.autoflash import kinds
        for name, expected in ((kinds.SONOFF_S60.wrapper_model, True), ("FWSW-E32S60B-S60-ESP32C3FN4", False)):
            with self.subTest(name=name):
                ctx = self.ctx(run=f"name-{expected}", firmware="tasmota", device_kind="sonoff-s60")
                ctx.state.capture("A2.firmware_name", name)
                data = self.payload(ctx)
                self.assertEqual((data["firmware_name"], data["firmware_name_expected"]), (name, expected))
        ctx = self.ctx(run="name-none", firmware="tasmota", device_kind="sonoff-s60")
        ctx.state.capture("A2.firmware_name", "")   # not learned
        self.assertEqual((self.payload(ctx)["firmware_name"], self.payload(ctx)["firmware_name_expected"]), (None, None))

    def test_the_version_that_began_a_run_is_sent_only_when_another_carried_it_on(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60")
        ctx.state.set_input("begun_on", "2.4.0")
        self.assertIsNone(report_data.payload(ctx, REPORT_ID, 1, False, "converted")["begun_on"], "not resumed")
        self.assertEqual(report_data.payload(ctx, REPORT_ID, 1, True, "converted")["begun_on"], "2.4.0")
        ctx.state.set_input("begun_on", report_data.__version__)
        self.assertIsNone(report_data.payload(ctx, REPORT_ID, 1, True, "converted")["begun_on"], "the same version")

    def test_a_run_the_person_stopped_says_where_and_how_with_no_words(self):
        ctx = self.ctx(firmware="esphome", device_kind="sonoff-s60")
        for how in report_data.STOPPED_BY:
            with self.subTest(how=how):
                ctx.stopped_by = how
                data = self.payload(ctx, "cancelled")
                self.assertEqual(data["result"], "cancelled")
                # The stage it stopped at: the first not done.
                self.assertEqual(data["stopped"], {"step": report_data.stopped_step(ctx), "error": how, "text": "",
                                                   "crash_site": None})
                self.assertIsNotNone(data["stopped"]["step"])

    def test_a_failure_is_classed_by_the_words_sent_but_for_words_cut_short(self):
        ctx = self.ctx(firmware="esphome", device_kind="sonoff-s60")
        # The API checks the words sent read as their class (website/api/review.js).
        data = self.payload(ctx, "failed", failure="the delivery stopped part way: x")
        self.assertEqual((data["stopped"]["error"], data["stopped"]["text"]),
                         ("upload-cut", "the delivery stopped part way: x"))
        long = "x " * 150 + "the delivery stopped part way"
        data = self.payload(ctx, "failed", failure=long)
        self.assertTrue(data["stopped"]["text"].endswith("..."))
        self.assertEqual(data["stopped"]["error"], "upload-cut", "from the whole reason, as the words sent lose it")
        data = self.payload(ctx, "failed", failure="y" * 300)
        self.assertEqual(len(data["stopped"]["text"]), 200)   # the API's limit, as written in its schema

    def test_a_device_put_back_says_so_and_where_it_came_from(self):
        ctx = self.ctx(firmware="esphome", source="midway", device_kind="sonoff-s60",
                       put_back={"came_from": "esphome", "why": "no identity"})
        data = self.payload(ctx)
        self.assertEqual((data["result"], data["source"]), ("put-back", "esphome"))

    def test_an_unknown_device_is_named_by_its_model_alone(self):
        ctx = self.ctx(product_model="NEWTHING", stock_version="not a version")
        data = self.payload(ctx, "failed", failure="the device is a NEWTHING, which this tool does not convert yet")
        self.assertEqual((data["device"], data["family"], data["model"], data["stock_version"]),
                         (None, None, "NEWTHING", None))
        self.assertEqual(data["stopped"]["error"], "unsupported-device")

    def test_the_model_and_stock_version_come_from_the_devices_record_if_the_run_has_none(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60", device_mac=MAC, product_model="",
                       stock_version="")
        with mock.patch.object(report_data.records, "read_device",
                               return_value={"model": "s60tpf", "stock_version": "1.2.0"}) as read:
            data = self.payload(ctx)
        read.assert_called_with(MAC)
        self.assertEqual((data["model"], data["stock_version"]), ("S60TPF", "1.2.0"))

    def test_a_kind_with_one_model_gives_it_where_nothing_else_does(self):
        # A converted POW Elite on a computer with no record of it.
        ctx = self.ctx(firmware="esphome", source="tasmota", device_kind="sonoff-pow-elite", device_mac=MAC,
                       product_model="")
        with mock.patch.object(report_data.records, "read_device", return_value={}):
            self.assertEqual(self.payload(ctx)["model"], "POWR316D")
        # An S60's region only eWeLink says: none, rather than a guess.
        ctx = self.ctx(firmware="esphome", source="tasmota", device_kind="sonoff-s60", device_mac=MAC,
                       product_model="", run="s60")
        with mock.patch.object(report_data.records, "read_device", return_value={"model": "S60"}):
            self.assertIsNone(self.payload(ctx)["model"], "the kind's short name an earlier version kept")

    def test_a_run_signed_in_already_takes_the_model_from_the_account_and_keeps_it(self):
        ctx = self.ctx(firmware="sonoff", source="esphome", device_kind="sonoff-s60", device_mac=MAC,
                       product_model="")
        listed = [{"productModel": "S60TPB", "params": {"staMac": "12:34:56:00:00:01"}},
                  {"productModel": "S60TPG", "params": {"staMac": MAC.upper()}}]
        ctx.ewelink, ctx.account_devices = mock.Mock(held=None), mock.Mock(return_value=listed)
        with mock.patch.object(report_data.records, "read_device", return_value={}), \
                mock.patch.object(report_data.records, "remember_device") as remember:
            self.assertIsNone(self.payload(ctx)["model"])
            ctx.account_devices.assert_not_called()   # no sign-in, and no password asked, for a report
            ctx.ewelink.held = ("eu", {"at": "token"})
            self.assertEqual(self.payload(ctx)["model"], "S60TPG")
        remember.assert_called_with(MAC, model="S60TPG")

    def test_the_report_shown_and_the_report_sent_give_the_same_model(self):
        from tools.autoflash import report
        for inputs, record in (({"product_model": "S60TPG"}, {}), ({"product_model": ""}, {"model": "S60TPF"}),
                               ({"product_model": "", "device_kind": "sonoff-pow-elite"}, {})):
            ctx = self.ctx(firmware="tasmota", device_mac=MAC, **{"device_kind": "sonoff-s60", **inputs},
                           run=str(inputs))
            with mock.patch.object(report_data.records, "read_device", return_value=record), \
                    mock.patch.object(report, "check_release", return_value={}):
                self.assertEqual(report.run_model(ctx), self.payload(ctx)["model"], inputs)

    def test_the_signal_is_the_weakest_reading_of_any_step(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60")
        for key, value in {"D1.signal_dbm": -48, "D4.signal_dbm": -61, "D4.seconds": 30, "E1.signal_dbm": True,
                           "H1.signal_dbm": "-90"}.items():
            ctx.state.capture(key, value)
        self.assertEqual(self.payload(ctx)["signal"], -61)

    def test_each_stages_time_and_how_long_it_kept_the_device_busy_are_sent(self):
        # The screen's report leaves them out (report.report_fields): only this has them.
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60")
        for key, value in {"E2.seconds": 36, "E2.device_busy_seconds": 34.2, "E2.attempts": 2,
                           "G1.seconds": 43}.items():
            ctx.state.capture(key, value)
        self.assertEqual(self.payload(ctx)["stages"], {
            "E2": {"seconds": 36, "busy": 34, "tries": 2, "wait": 0, "setup_network": None, "power_cycles": 0},
            "G1": {"seconds": 43, "busy": 0, "tries": 1, "wait": 0, "setup_network": None, "power_cycles": 0}})

    def test_each_stage_says_why_it_needed_the_setup_network_and_how_many_power_cycles(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60")
        for key, value in {"C3.seconds": 90, f"C3.{join.OWN_NETWORK}": join.NOT_JOINED, "E3.seconds": 50,
                           "E3.power_cycles": 2, "F4.seconds": 70, f"F4.{join.OWN_NETWORK}": join.SETTINGS_LOST,
                           "G1.seconds": 43, f"G1.{join.OWN_NETWORK}": "a word it never sends"}.items():
            ctx.state.capture(key, value)
        stages = self.payload(ctx)["stages"]
        self.assertEqual({step: (stage["setup_network"], stage["power_cycles"]) for step, stage in stages.items()},
                         {"C3": ("not joined", 0), "E3": (None, 2), "F4": ("settings lost", 0), "G1": (None, 0)})

    def test_a_beken_device_set_up_through_its_own_network_says_so(self):
        ctx = self.ctx(run="beken-own", firmware="esphome", device_kind="sonoff-s61")
        self.assertIs(self.payload(ctx)["setup_network"], False)
        ctx.state.set_input(join.OWN_NETWORK, [join.NOT_JOINED])
        self.assertIs(self.payload(ctx)["setup_network"], True)

    def test_what_is_asked_of_each_family(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60", wifi_typed=True, power_cycles=2)
        data = self.payload(ctx)
        # Switched off and on, on either family (arrival.until_back).
        self.assertEqual((data["wifi_typed"], data["setup_network"], data["power_cycled"]), (True, False, True))
        ctx = self.ctx(run="beken", firmware="openbeken", device_kind="sonoff-s61", wifi_typed=True, power_cycles=2)
        data = self.payload(ctx)
        self.assertEqual((data["wifi_typed"], data["setup_network"], data["power_cycled"]), (None, False, True))

    def test_dev_runs_are_tagged(self):
        ctx = self.ctx(firmware="tasmota", device_kind="sonoff-s60")
        with mock.patch.dict("os.environ", {"AUTOFLASH_REPORT_TAG": "dev"}):
            self.assertEqual(self.payload(ctx)["tag"], "dev")
        with mock.patch.dict("os.environ", {"AUTOFLASH_REPORT_TAG": "anything else"}):
            self.assertEqual(self.payload(ctx)["tag"], "user")


class OutcomeTests(unittest.TestCase):
    def ctx(self, **more):
        from types import SimpleNamespace
        return SimpleNamespace(**{"last_failure": "words of no class", "last_failure_class": None,
                                  "last_failed_step": "J2", "crash_site": None, **more})

    def test_a_stop_that_said_its_class_is_sent_as_it_said_it(self):
        self.assertEqual(report_data.outcome(self.ctx(last_failure_class="upload-cut"), "failed")[1]["error"],
                         "upload-cut")
        self.assertEqual(report_data.outcome(self.ctx(), "failed")[1]["error"], "other", "else by its words")
        self.assertEqual(report_data.outcome(self.ctx(last_failure_class="not-a-class"), "failed")[1]["error"],
                         "other")

    def test_a_crash_outside_any_step_is_filed_under_none(self):
        # Not the first step left to do: it never began.
        self.assertIsNone(report_data.outcome(self.ctx(last_failed_step=""), "crashed")[1]["step"])
        self.assertEqual(report_data.outcome(self.ctx(last_failed_step="K2"), "crashed")[1]["step"], "K2")


class CalibrationWordsTests(unittest.TestCase):
    """The meter's word in a report is read from the words the firmware
    says: every word our ESPHome configs and OpenBeken's driver can say has a
    word of its own (report_data.CALIBRATION_WORDS), never "reported"."""

    def test_every_word_the_firmware_says_has_its_own_word(self):
        import re
        from pathlib import Path
        from tools.autoflash import openbeken
        root = Path(__file__).resolve().parents[2]
        said = set(openbeken.CALIBRATION_STATES.values()) | {openbeken.CALIBRATION_CHANGED}
        for config in root.glob("devices/*/esphome.yaml"):
            said.update(re.findall(r'(?:\bsource|calibration_status\)) = (?:std::string\()?"([^"]+)"',
                                   config.read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(said), 5)   # the configs' words were found
        words = {word: report_data.meter_word({"K2.calibration": word}, "esphome") for word in said}
        self.assertEqual({word: got for word, got in words.items() if got == "reported"}, {},
                         "add how it begins to report_data.CALIBRATION_WORDS")


class ReadoutPayloadTests(unittest.TestCase):
    """A readout's report, one for each device's readout: nothing of its name
    in the account nor its reference, how many devices this computer has
    converted, and every value one the schema's "readout" allows."""

    def became(self, **changes):
        from tools.autoflash import readout
        return readout.Became(**{"name": "'Kitchen' (BASICR4)", "product": "BASICR4", "model": "SN-ESP32C3-BASICR4-01",
                                 "version": "1.1.0", "whole": True, "outcome": report_data.ACCEPTED,
                                 "detail": "r-1", "mac": "12:34:56:ab:cd:ef", **changes})

    def payload(self, **changes):
        with mock.patch.object(report_data.records, "converted_here", return_value=4):
            return report_data.readout_payload("a" * 16, self.became(**changes), 75.4)

    def test_it_says_what_the_readout_was_and_nothing_of_whose(self):
        sent = self.payload()
        self.assertEqual({key: sent[key] for key in ("kind", "result", "error", "family", "device", "model",
                                                     "stock_model", "stock_version", "whole", "seconds", "converted_here")},
                         {"kind": "readout", "result": "accepted", "error": None, "family": "espressif",
                          "device": "sonoff-basicr4", "model": "BASICR4", "stock_model": "SN-ESP32C3-BASICR4-01",
                          "stock_version": "1.1.0", "whole": True, "seconds": 75, "converted_here": 4})
        self.assertEqual(sent["device_hash"], report_data.device_hash("12:34:56:ab:cd:ef"))
        shown = json.dumps(sent)
        for private in ("Kitchen", "r-1", "12:34:56"):
            self.assertNotIn(private, shown)

    def test_a_device_ewelink_names_no_product_of_is_named_by_its_kind_where_it_has_one_model(self):
        from tools.autoflash import kinds
        powct = kinds.SONOFF_POWCT.stock_model
        self.assertEqual(self.payload(product="", model=powct)["model"], "POWCT")
        # A BASICR4 only its product model tells from its neighbours: no kind, so no model.
        self.assertIsNone(self.payload(product="")["model"])

    def test_every_result_fits_the_schema(self):
        schema = report_schema.current()["readout"]
        for result in report_data.READOUT_RESULTS:
            sent = {**self.payload(outcome=result, error="other"), "proof": "1794000000:198"}
            self.assertEqual(report_schema.validate(schema, sent), [], result)
            self.assertEqual(sent["error"], "other" if result == report_data.FAILED else None, result)

    def test_what_is_not_as_the_tool_writes_it_is_sent_as_not_known(self):
        sent = self.payload(product="a product", model="", version="v1", mac=None, whole=None)
        self.assertEqual([sent[key] for key in ("model", "stock_model", "stock_version", "device_hash", "whole", "device")],
                         [None] * 6)


class WordTests(RouteTestCase):
    def test_a_device_is_the_same_hash_however_its_mac_is_written(self):
        expected = report_data.device_hash(MAC)
        self.assertRegex(expected, r"^[0-9a-f]{64}$")
        for written in ("12-34-56-AB-CD-EF", "123456abcdef", " 12:34:56:AB:CD:EF "):
            self.assertEqual(report_data.device_hash(written), expected, written)
        for nothing in (None, "", "12:34:56", "not a mac"):
            self.assertIsNone(report_data.device_hash(nothing))

    def test_the_hash_is_of_the_mac_in_one_written_form(self):
        # The API knows a device again from any computer's report only if each hashes the same.
        self.assertEqual(report_data.device_hash("12-34-56-AB-CD-EF"),
                         hashlib.sha256(b"firmware-liberator device:12:34:56:ab:cd:ef").hexdigest())

    def test_the_operating_system_in_words_the_api_allows(self):
        for said, version in (("Linux Ubuntu 24.04.1 LTS", "Ubuntu 24.04.1 LTS"),
                              ("Linux Debian\u00a0GNU/Linux 13 (trixie) \u2014 x86_64", "DebianGNU/Linux 13 (trixie)  x86_64"),
                              ("Linux " + "a" * 60, "a" * 48),
                              ("Linux \u2014", "unknown"),
                              ("", "unknown")):
            with self.subTest(said=said), mock.patch.object(report_data.report, "computer_description",
                                                            return_value=said):
                self.assertEqual(report_data.os_info()["version"], version)

    def test_the_meter_in_a_word(self):
        for firmware, captures, word in (
                ("sonoff", {}, "sonoff"),
                ("tasmota", {"F4.meter": {"changed": True, "factory": True}}, "carried-factory"),
                ("tasmota", {"F4.meter": {"changed": False, "factory": False}}, "carried"),
                ("tasmota", {"F4.meter": {"skipped": "the meter did not report"}}, "skipped"),
                ("esphome", {"U2.meter": "stock NVS, factor 1.02"}, "stock-nvs"),
                ("esphome", {"U2.meter": "from backup, factor 1.00"}, "from-backup"),
                ("esphome", {"U2.meter": "missing"}, "missing"),
                # An S61's OpenBeken (openbeken.calibration_state): Sonoff's store, or changed since.
                ("openbeken", {"K1.calibration": "stock store"}, "stock-store"),
                ("openbeken", {"K1.calibration": "not the stock store's"}, "changed"),
                ("esphome", {"K3.calibration": {"calibration": "something new"}}, "reported"),
                ("esphome", {}, "not-checked")):
            self.assertEqual(report_data.meter_word(captures, firmware), word, captures)

    def test_the_relay_check_in_a_word(self):
        for relay, word in (("switches (meter confirms)", "passed"), ("switches (through eWeLink)", "passed"),
                            ("did not switch", "failed"), ("could not be checked", "failed"),
                            ("motor mode", "not-run"), (None, "not-run")):
            self.assertEqual(report_data.checks_word({"Relay": relay} if relay else {}), word, relay)
