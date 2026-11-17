"""The device in its eWeLink account (tools/autoflash/account.py): the devices
there matched on the network (A1), and only a kind this tool converts let past
A2, with the firmware name its updater checks."""

import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import (account, errors, esphome, hostsys, kinds, openbeken, output, records, prompts, runs,
                             setup_network)
from tools.autoflash.routes.espressif import bundle
from tools.autoflash.state import RunState
from tools.ewelink import discover_ewelink, firmware_name

S60 = {"deviceid": "1001234abc", "name": "Kitchen", "productModel": "S60TPG", "online": True,
       "devicekey": "secret", "extra": {"model": "SN-ESP32C3-S60-01"},
       "params": {"staMac": "AA:BB:CC:12:34:56", "ssid": "Home-2G"}}
S61 = {"deviceid": "1001234abd", "name": "Desk", "productModel": "S61STPF", "online": True,
       "extra": {"model": "SN-BK7238-S61-01"}, "params": {"staMac": "12:34:56:AB:CD:EF"}}
SWITCH = {"deviceid": "1000aaaaaa", "name": "Hall light", "productModel": "MINIR4M", "online": True,
          "extra": {"model": "SN-ESP32-MINIR4M"}, "params": {}}


class AccountDeviceTests(unittest.TestCase):
    def test_an_s60_is_kept_without_its_key(self):
        device = account.account_device(S60)
        self.assertEqual((device.device_id, device.name, device.model, device.online, device.mac, device.ssid, device.kind),
                         ("1001234abc", "Kitchen", "S60TPG", True, "aa:bb:cc:12:34:56", "Home-2G", "sonoff-s60"))
        self.assertNotIn("secret", repr(device))

    def test_other_sonoff_devices_are_not_offered(self):
        self.assertIsNone(account.account_device(SWITCH))

    def test_the_firmware_family_alone_is_enough(self):
        self.assertIsNotNone(account.account_device({**S60, "productModel": ""}))


class MatchTests(unittest.TestCase):
    """Each device in the account is found by the hardware address its record
    gives, whatever its chip family: Sonoff's firmware need answer nothing on
    the network, so LAN Control is never needed."""
    device = account.account_device(S60)

    def test_found_by_the_hardware_address_its_record_gives(self):
        [seen] = account.match_devices([self.device], {"192.0.2.50": "aa:bb:cc:12:34:56"})
        self.assertEqual(seen.ip, "192.0.2.50")

    def test_found_by_a_hardware_address_given_without_separators(self):
        # The DualR3's firmware reports its staMac so, and eWeLink keeps it.
        device = account.account_device({**S60, "params": {"staMac": "AABBCC123456"}})
        [seen] = account.match_devices([device], {"192.0.2.50": "aa:bb:cc:12:34:56"})
        self.assertEqual(seen.ip, "192.0.2.50")

    def test_another_device_on_the_network_is_not_mistaken_for_it(self):
        [seen] = account.match_devices([self.device], {"192.0.2.60": "aa:bb:cc:00:00:01"})
        self.assertIsNone(seen.ip)

    def test_every_kind_online_at_its_hardware_address_is_ready(self):
        for record, address, mac in ((S60, "192.0.2.50", "aa:bb:cc:12:34:56"),
                                     (S61, "192.0.2.51", "12:34:56:ab:cd:ef")):
            with self.subTest(record["productModel"]):
                [seen] = account.match_devices([account.account_device(record)], {address: mac})
                self.assertTrue(account.ready(seen))

    def test_offline_in_ewelink_is_not_ready(self):
        for record, address, mac in ((S60, "192.0.2.50", "aa:bb:cc:12:34:56"),
                                     (S61, "192.0.2.51", "12:34:56:ab:cd:ef")):
            with self.subTest(record["productModel"]):
                [seen] = account.match_devices([account.account_device({**record, "online": False})],
                                               {address: mac})
                self.assertFalse(account.ready(seen))

    def test_offline_in_ewelink_says_so(self):
        [seen] = account.match_devices([account.account_device({**S60, "online": False})], {})
        self.assertIn("offline", account.why_not_found(seen))

    def test_here_but_offline_in_ewelink_means_no_internet(self):
        offline = account.account_device({**S60, "online": False})
        [seen] = account.match_devices([offline], {"192.0.2.50": "aa:bb:cc:12:34:56"})
        self.assertTrue(account.cut_off(seen))
        self.assertIn("let it reach the internet", account.why_not_found(seen))

    def test_online_but_unseen_names_its_network(self):
        [seen] = account.match_devices([self.device], {})
        self.assertIn("(it is on 'Home-2G')", account.why_not_found(seen))

    def test_lan_control_is_never_asked_for(self):
        for record, neighbours in ((S60, {}), ({**S60, "online": False}, {}),
                                   ({**S60, "online": False}, {"192.0.2.50": "aa:bb:cc:12:34:56"})):
            [seen] = account.match_devices([account.account_device(record)], neighbours)
            self.assertNotIn("LAN Control", account.why_not_found(seen))


class SightTests(unittest.TestCase):
    """The account's devices are looked for by hardware address, never by
    eWeLink's local discovery, which Sonoff's firmware answers only with LAN
    Control on."""
    device = account.account_device(S60)

    def sight(self, table, pings=True, swept=None, lan=None):
        with mock.patch.object(hostsys, "neighbour_table", return_value=table), \
             mock.patch.object(hostsys, "ping_ok", return_value=pings), \
             mock.patch.object(hostsys, "lan_networks", return_value=lan or {"192.0.2.10": 24}), \
             mock.patch.object(hostsys, "sweep", return_value=swept or {}) as self.swept, \
             mock.patch.object(discover_ewelink, "discover") as discovered:
            sightings = account.sight_devices([self.device])
        discovered.assert_not_called()
        return sightings

    def test_where_this_computer_last_saw_it_is_asked_first(self):
        [seen] = self.sight({"192.0.2.50": "aa:bb:cc:12:34:56"})
        self.assertEqual(seen.ip, "192.0.2.50")
        self.swept.assert_not_called()

    def test_one_not_seen_lately_is_swept_for_on_this_computers_networks(self):
        [seen] = self.sight({}, swept={"192.0.2.1": "aa:bb:cc:00:00:01", "192.0.2.77": "aa:bb:cc:12:34:56"})
        self.assertEqual(seen.ip, "192.0.2.77")
        self.swept.assert_called_once_with(["192.0.2.10"])

    def test_an_address_it_no_longer_answers_at_is_not_taken(self):
        [seen] = self.sight({"192.0.2.50": "aa:bb:cc:12:34:56"}, pings=False,
                            swept={"192.0.2.77": "aa:bb:cc:12:34:56"})
        self.assertEqual(seen.ip, "192.0.2.77")


class TypedAddressTests(unittest.TestCase):
    """An address typed in is used only once the hardware address answering
    there is one the account gives its devices."""

    def check(self, address, mac):
        with mock.patch.object(hostsys, "mac_of", return_value=mac), mock.patch.object(output, "tell"):
            return account.device_at({"aa:bb:cc:12:34:56"})(address)

    def test_the_accounts_device_will_do(self):
        self.assertEqual(self.check("192.0.2.50", "AA-BB-CC-12-34-56"), [])

    def test_another_device_is_not_taken_for_it(self):
        problem = self.check("192.0.2.1", "aa:bb:cc:00:00:01")
        self.assertIn("192.0.2.1 is another device", problem[0])

    def test_nothing_answering_is_said(self):
        self.assertIn("Nothing answered at 192.0.2.99", self.check("192.0.2.99", None)[0])


class AccountRecordTests(unittest.TestCase):
    """The device's record in the account, for every family: by the id A1
    found or this computer keeps, else by its Wi-Fi MAC there."""
    MAC = "12:34:56:ab:cd:ef"

    def ctx(self, tmp, devices):
        state = RunState.open(Path(tmp))
        state.set_input("device_mac", self.MAC)
        return types.SimpleNamespace(state=state, account_device=None, account_devices=lambda: devices,
                                     email=lambda: "owner@example.com")

    def test_a_device_with_no_id_kept_is_found_in_the_account_by_its_mac(self):
        found = {"deviceid": "1001234abc", "params": {"staMac": self.MAC.upper()}}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                mock.patch.object(records, "CAPTURES", Path(tmp)):
            ctx = self.ctx(tmp, [{"deviceid": "other", "params": {"staMac": "12:34:56:AB:CD:00"}}, found])
            self.assertEqual(account.device_id(ctx), "1001234abc")
            self.assertIs(account.account_record(ctx), found)
            self.assertEqual(account.device_id(ctx, "12:34:56:ab:cd:00"), "other", "by the MAC given")
            ctx.account_devices = lambda: None   # eWeLink not reached
            self.assertEqual(account.device_id(ctx), "")
            records.remember_device(self.MAC, ewelink_id="1001234def")
            self.assertEqual(account.device_id(ctx), "1001234def", "the one this computer keeps")

    def test_a_device_not_in_the_account_stops_the_step(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                mock.patch.object(records, "CAPTURES", Path(tmp)):
            with self.assertRaisesRegex(errors.Abort, "device id is not known"):
                account.account_record(self.ctx(tmp, []))

    def test_the_signal_is_only_a_reading_a_radio_gives(self):
        self.assertIsNone(account.account_signal({"params": {"rssi": "strong"}}))
        # 0 is what a device says for none.
        for rssi, kept in ((-1, -1), (-119, -119), (0, None), (-120, None), (True, None)):
            self.assertEqual(account.account_signal({"params": {"rssi": rssi}}), kept, rssi)


class DescribeTests(unittest.TestCase):
    def test_a_name_that_is_the_model_is_not_repeated(self):
        self.assertEqual(account.describe_device(account.account_device({**S60, "name": "S60TPG"})), "'S60TPG'")
        self.assertEqual(account.describe_device(account.account_device(S60)), "'Kitchen' (S60TPG)")


class FindAccountDeviceTests(unittest.TestCase):
    def ctx(self):
        return types.SimpleNamespace(email=lambda: "owner@example.com", forget_account=mock.Mock(),
                                     account_devices=mock.Mock(return_value=[S60]))

    def test_the_one_s60_answering_is_chosen_without_asking(self):
        sighting = account.Sighting(account.account_device(S60), "192.0.2.50")
        with mock.patch.object(account, "sight_devices", return_value=[sighting]), \
             mock.patch.object(prompts, "ask_choice") as asked, \
             mock.patch.object(output, "tell") as shown, mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(self.ctx(), [S60, SWITCH]), "192.0.2.50")
        asked.assert_not_called()
        shown.assert_not_called()  # the stage line completing is all the screen needs

    def test_it_is_looked_for_again_by_itself_before_anyone_is_asked(self):
        # Just plugged in, or a busy Wi-Fi: it answers a moment later.
        device = account.account_device(S60)
        with mock.patch.object(account, "sight_devices", side_effect=[
                [account.Sighting(device, None)],
                [account.Sighting(device, "192.0.2.50")]]), \
             mock.patch.object(prompts, "ask_choice") as asked, mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(self.ctx(), [S60]), "192.0.2.50")
        asked.assert_not_called()

    def test_looking_again_finds_it_once_it_answers(self):
        device = account.account_device(S60)
        unseen = [account.Sighting(device, None)]
        with mock.patch.object(account, "sight_devices", side_effect=[unseen] * (account.LOOK_AGAIN + 1) + [
                [account.Sighting(device, "192.0.2.50")]]), \
             mock.patch.object(prompts, "ask_choice", return_value=0) as asked, mock.patch.object(time, "sleep"), \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(self.ctx(), [S60]), "192.0.2.50")
        asked.assert_called_once()   # only once it had been looked for again by itself
        self.assertIn("not seen here", shown.call_args.args[1][0])

    def clock(self):
        """A clock that moves on 10 s for every sleep, and no real waiting."""
        now = [0.0]
        return (mock.patch.object(time, "monotonic", side_effect=lambda: now[0]),
                mock.patch.object(time, "sleep", side_effect=lambda s: now.__setitem__(0, now[0] + s)))

    def test_a_device_just_started_is_waited_for_until_ewelink_sees_it(self):
        # It answers here a few seconds before eWeLink shows it online.
        offline = {**S60, "online": False}
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(side_effect=[[offline], [S60]])
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_devices",
                               side_effect=lambda devices: [account.Sighting(p, "192.0.2.50") for p in devices]), \
             mock.patch.object(prompts, "ask_choice") as asked, monotonic, sleep, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(ctx, [offline]), "192.0.2.50")
        asked.assert_not_called()

    def test_a_device_just_switched_on_is_waited_for_before_asking(self):
        # Plugged in as the run began: neither answering here nor online yet.
        offline = {**S60, "online": False}
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(side_effect=[[offline], [S60]])
        device = account.account_device(offline)
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_devices", side_effect=[
                [account.Sighting(device, None)],
                [account.Sighting(account.account_device(S60), "192.0.2.50")]]), \
             mock.patch.object(prompts, "ask_choice") as asked, monotonic, sleep, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(ctx, [offline]), "192.0.2.50")
        asked.assert_not_called()

    def test_a_device_offline_for_a_minute_is_asked_about(self):
        offline = {**S60, "online": False}
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(return_value=[offline])
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_devices",
                               side_effect=lambda devices: [account.Sighting(p, None) for p in devices]), \
             mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.0.2.99"), \
             mock.patch.object(prompts, "section") as shown, monotonic, sleep, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(ctx, [offline]), "192.0.2.99")
        self.assertIn("is offline in eWeLink", shown.call_args.args[1][0])
        # Looked again every 10 s for the minute, then LOOK_AGAIN times more, before asking.
        self.assertEqual(ctx.account_devices.call_count, int(account.CLOUD_CATCH_UP // 10) + account.LOOK_AGAIN)

    def test_without_ewelink_the_run_stops_with_nothing_written(self):
        # A device on Sonoff's firmware is found, and converted, through eWeLink.
        ctx = self.ctx()
        ctx.account_devices = mock.Mock(return_value=None)
        with mock.patch.object(account, "sight_devices", return_value=[account.Sighting(account.account_device(S60), None)]), \
             mock.patch.object(prompts, "ask_choice", return_value=0), mock.patch.object(time, "sleep"), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"), \
             self.assertRaisesRegex(errors.Abort, "eWeLink could not be reached.*Nothing was written"):
            account.find_account_device(ctx, [S60])

    def test_a_device_without_internet_is_not_chosen(self):
        sighting = account.Sighting(account.account_device({**S60, "online": False}), "192.0.2.50")
        monotonic, sleep = self.clock()
        with mock.patch.object(account, "sight_devices", return_value=[sighting]), monotonic, sleep, \
             mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.0.2.99"), \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(self.ctx(), [S60]), "192.0.2.99")
        self.assertEqual("Your device is on this network but cannot reach the internet",
                         shown.call_args.args[0])
        self.assertIn("let it reach the internet", shown.call_args.args[1][0])

    def test_another_account_chosen_is_signed_in_to_and_its_devices_looked_at(self):
        ctx = self.ctx()
        sighting = account.Sighting(account.account_device(S60), "192.0.2.50")
        with mock.patch.object(prompts, "ask_choice", side_effect=[1]), \
             mock.patch.object(account, "sight_devices", return_value=[sighting]), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            self.assertEqual(account.find_account_device(ctx, [SWITCH]), "192.0.2.50")
        ctx.forget_account.assert_called_once()
        ctx.account_devices.assert_called_once()

    def test_an_account_without_an_s60_says_what_it_has(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", return_value="192.0.2.50"), \
             mock.patch.object(prompts, "section") as shown:
            self.assertEqual(account.find_account_device(ctx, [SWITCH]), "192.0.2.50")
        self.assertIn("No supported device in the eWeLink account owner@example.com", shown.call_args.args[0])
        self.assertIn("- 'Hall light' (MINIR4M)", shown.call_args.args[1])


class ModelGuardTests(unittest.TestCase):
    """Only a device of a kind this tool converts may continue past A2 - another
    Sonoff on the account must not."""

    learned = (None, [])
    saved: dict = {}

    written = True

    def discover_with_model(self, model):
        import json
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = self.root = Path(directory.name)
        state = self.state = RunState.open(root)
        state.set_input("device_ip", "10.0.0.63")
        ctx = SimpleNamespace(dir=root, state=state, email=lambda: "a@b.c",
                              ewelink_env=lambda: {}, helper_env=lambda: {}, ewelink_region=lambda: "us",
                              account_device=account.account_device(S60),
                              account_devices=lambda: [SWITCH, {**S60, "params": {**S60["params"], "rssi": -61}}])

        def fake_tool(args, **_):
            if "tools/ewelink/query_ota.py" in args and self.written:
                self.tool_args = args
                (root / "ota-metadata.json").write_text(json.dumps({
                    "query": {"version": "1.2.0", "model": model, "deviceid": self.saved.get("deviceid", "")},
                    "response": {"data": {"otaInfoList": []}},
                    "latest_offer": self.saved.get("latest_offer"),
                }))
            return 0

        # Sonoff's firmware answers nothing on the network: no LAN Control.
        with mock.patch.object(hostsys, "run_tool", side_effect=fake_tool), \
             mock.patch.object(discover_ewelink, "discover") as self.discovered, \
             mock.patch.object(setup_network, "page", return_value=None), \
             mock.patch.object(esphome, "is_esphome", return_value=False), \
             mock.patch.object(openbeken, "is_openbeken", return_value=False), \
             mock.patch.object(firmware_name, "learn", return_value=self.learned) as self.learn, \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            return account.step_discover(ctx)

    def test_an_s60_continues(self):
        self.assertEqual(self.discover_with_model("SN-ESP32C3-S60-01")["model"], "SN-ESP32C3-S60-01")
        self.assertEqual(self.state.input("device_kind"), "sonoff-s60")  # kept for the run
        self.assertEqual(self.state.input("stock_version"), "1.2.0")
        self.assertEqual(self.state.input("product_model"), "")

    def test_the_ota_metadata_is_not_kept(self):
        # Its download links may work as access tokens.
        self.discover_with_model("SN-ESP32C3-S60-01")
        self.assertFalse((self.root / "ota-metadata.json").exists())

    def test_ewelink_is_asked_by_the_accounts_id_with_no_discovery(self):
        self.discover_with_model("SN-ESP32C3-S60-01")
        self.assertEqual(self.tool_args[self.tool_args.index("--device-id") + 1], "1001234abc")
        self.assertNotIn("--mdns-capture", self.tool_args)
        self.discovered.assert_not_called()

    def test_the_signal_is_the_one_the_device_last_told_ewelink(self):
        self.assertEqual(self.discover_with_model("SN-ESP32C3-S60-01")["signal_dbm"], -61)

    def test_metadata_the_query_did_not_write_stops_the_step_plainly(self):
        self.written = False
        with self.assertRaisesRegex(errors.Abort, "could not read the OTA metadata"):
            self.discover_with_model("SN-ESP32C3-S60-01")

    def test_the_name_learned_is_kept_with_the_step(self):
        self.learned = (firmware_name.Learned("FWSW-E32S60-S60-ESP32C3FN4", "ESP32C3", "a test"), [])
        result = self.discover_with_model("SN-ESP32C3-S60-01")
        self.assertEqual(result["firmware_name"], "FWSW-E32S60-S60-ESP32C3FN4")
        self.learn.assert_called_once_with([], "", "", "SN-ESP32C3-S60-01")

    def test_the_latest_offer_names_the_firmware_and_is_not_an_update_on_offer(self):
        offer = [{"version": "1.2.0", "binList": [{"name": "user1.bin", "downloadUrl": "http://192.0.2.9:8088/user1.bin"}]}]
        self.saved = {"latest_offer": {"offers": offer}, "deviceid": "10000e32ab"}
        result = self.discover_with_model("SN-ESP32C3-S60-01")
        self.learn.assert_called_once_with(offer, "10000e32ab", "", "SN-ESP32C3-S60-01")
        self.assertFalse(result["update_offered"])
        self.assertNotIn("10000e32ab", str(result))

    def test_asks_query_ota_for_the_latest_offer(self):
        self.discover_with_model("SN-ESP32C3-S60-01")
        self.assertIn("--latest-offer", self.tool_args)
        # In the account's own region, which the run's sign-in found.
        self.assertEqual(self.tool_args[self.tool_args.index("--region") + 1], "us")

    def test_the_offer_failing_is_logged_and_the_run_goes_on(self):
        self.saved = {"latest_offer": {"problem": "EwelinkError"}}
        with mock.patch.object(output, "log_only") as logged:
            result = self.discover_with_model("SN-ESP32C3-S60-01")
        self.assertEqual(result["firmware_name"], "")
        self.assertIn("EwelinkError", logged.call_args_list[0].args[0])

    def test_a_device_turned_down_has_its_name_looked_for_first(self):
        with self.assertRaises(errors.Abort):
            self.discover_with_model("SN-ESP32-MINIR4")
        self.learn.assert_called_once()

    def test_any_other_model_is_refused_before_anything_is_sent(self):
        with self.assertRaisesRegex(errors.Abort, "SN-ESP32-MINIR4, which this tool does not convert yet"):
            self.discover_with_model("SN-ESP32-MINIR4")


class FirmwareNameTests(unittest.TestCase):
    """A2 logs the name a stock updater checks, and goes on whatever it learns."""

    def note(self, kind, learned, problems=()):
        answer = firmware_name.Learned(learned, firmware_name.chip_of(learned), "eWeLink's update file") if learned else None
        with mock.patch.object(firmware_name, "learn", return_value=(answer, list(problems))), \
             mock.patch.object(output, "log_only") as logged, mock.patch.object(output, "tell") as told:
            name = account.note_firmware_name(kind, {}, "", "S60TPG", "SN-ESP32C3-S60-01")
        return name, [call.args[0] for call in logged.call_args_list], [call.args[0] for call in told.call_args_list]

    def test_a_name_that_agrees_is_only_logged(self):
        wrapper = kinds.SONOFF_S60.wrapper_model
        name, logged, told = self.note(kinds.SONOFF_S60, wrapper)
        self.assertEqual(name, wrapper)
        self.assertEqual(told, [])
        self.assertIn(wrapper, logged[0])
        self.assertIn("ESP32C3", logged[0])

    def test_a_name_that_differs_is_logged_for_the_report_not_shown(self):
        # Nothing to ask of anyone, and the run's report carries it.
        name, logged, told = self.note(kinds.SONOFF_S60, "FWSW-E32S60B-S60-ESP32C3FN4")
        self.assertEqual(name, "FWSW-E32S60B-S60-ESP32C3FN4")
        self.assertEqual(told, [])
        said = " ".join(logged)
        self.assertIn("FWSW-E32S60B-S60-ESP32C3FN4", said)
        self.assertIn(kinds.SONOFF_S60.wrapper_model, said)

    def test_an_unknown_device_has_no_name_to_differ_from(self):
        name, logged, told = self.note(None, "FWSW-E32R9-NEW-ESP32C3")
        self.assertEqual(told, [])
        self.assertIn("FWSW-E32R9-NEW-ESP32C3", logged[0])
        self.assertEqual(name, "FWSW-E32R9-NEW-ESP32C3")

    def test_not_learned_is_logged_with_why(self):
        name, logged, told = self.note(kinds.SONOFF_S60, "", ["the public file list could not be read (URLError)"])
        self.assertEqual(name, "")
        self.assertEqual(told, [])
        self.assertIn("URLError", logged[0])
        self.assertIn("not learned", logged[1])


class AlreadyConvertingTests(unittest.TestCase):
    """A device part way through a conversion, or converted, is named before
    eWeLink, whose record of it is out of date, is asked about it."""

    def silent_discover(self, page, running=None):
        """`running`: "esphome" or "openbeken", if that answers at the address."""
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_ip", "10.0.0.63")
        ctx = SimpleNamespace(dir=Path(directory.name), state=state, account_device=account.account_device(S60),
                              account_devices=lambda: [S60])
        asked = account.Asked("1.2.0", "SN-ESP32C3-S60-01", "S60TPG", [], {}, "1001234abc")
        with mock.patch.object(setup_network, "page", return_value=page), \
             mock.patch.object(esphome, "is_esphome", return_value=running == "esphome"), \
             mock.patch.object(openbeken, "is_openbeken", return_value=running == "openbeken"), \
             mock.patch.object(account, "_ask", return_value=asked) as self.asked, \
             mock.patch.object(firmware_name, "learn", return_value=(None, [])), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            return account.step_discover(ctx)

    def test_the_bridge_is_named(self):
        with self.assertRaisesRegex(errors.Abort, "is on this tool.s installer from an earlier run"):
            self.silent_discover("<title>Autoflash bridge</title>")

    def test_tasmota_is_named(self):
        with self.assertRaisesRegex(errors.Abort, "running Tasmota, not Sonoff's firmware"):
            self.silent_discover("<title>Tasmota</title>")

    def test_a_converted_device_says_what_it_runs(self):
        # The eWeLink account still lists a device converted since.
        for running, name in (("esphome", "ESPHome"), ("openbeken", "OpenBeken")):
            with self.subTest(running), self.assertRaises(account.NotOnSonoff) as raised:
                self.silent_discover("<html></html>", running)
            self.assertEqual(raised.exception.firmware, running)
            self.assertIn(f"running {name}, not Sonoff's firmware: it is converted already", str(raised.exception))

    def test_sonoff_firmware_answering_nothing_known_is_asked_of_ewelink(self):
        for page in ("<html></html>", None):
            with self.subTest(page):
                self.assertEqual(self.silent_discover(page)["model"], "SN-ESP32C3-S60-01")
                self.asked.assert_called_once_with(mock.ANY, "--device-id", "1001234abc")

    def test_a_converted_device_is_not_asked_of_ewelink(self):
        with self.assertRaises(errors.Abort):
            self.silent_discover("<title>Tasmota</title>")
        self.asked.assert_not_called()


class PairStepTests(unittest.TestCase):
    """A1 asks only what matters: it looks again if no device is found, and the
    device it finds in the account says the run's kind."""

    def ctx(self):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return SimpleNamespace(state=RunState.open(root), evidence=root / "live",
                               manifest_path=(runs.ROOT / bundle.DEFAULT_MANIFEST).resolve(),
                               wifi=lambda: ("Home-2G", "password"))

    def test_without_ewelink_the_run_stops(self):
        ctx = self.ctx()
        ctx.account_devices = lambda: None  # eWeLink could not be reached
        with mock.patch.object(output, "say"), \
             self.assertRaisesRegex(errors.Abort, "eWeLink could not be reached"):
            account.step_pair(ctx)

    def test_the_account_devices_kind_is_kept_for_the_run(self):
        from unittest import mock
        ctx = self.ctx()
        ctx.account_devices = lambda: [{"deviceid": "1000000001", "productModel": "S60TPG",
                                        "name": "Sonoff S60", "online": True, "params": {}}]
        with mock.patch.object(account, "sight_devices",
                               side_effect=lambda devices: [account.Sighting(p, "192.168.60.50")
                                                          for p in devices]), \
             mock.patch.object(hostsys, "mac_of", return_value=None), \
             mock.patch.object(output, "say"):
            self.assertEqual(account.step_pair(ctx)["device_ip"], "192.168.60.50")
        self.assertEqual(ctx.state.input("device_kind"), "sonoff-s60")

    def pair(self, ctx, arp_mac):
        account_devices = [{"deviceid": "1000000001", "productModel": "S60TPG", "name": "Sonoff S60",
                            "online": True, "params": {"staMac": "12:34:56:AB:CD:EF"}}]
        ctx.account_devices = lambda: account_devices
        with mock.patch.object(account, "sight_devices",
                               side_effect=lambda devices: [account.Sighting(p, "192.0.2.50")
                                                          for p in devices]), \
             mock.patch.object(hostsys, "mac_of", return_value=arp_mac), \
             mock.patch.object(runs, "file_device") as filed, mock.patch.object(output, "say"):
            result = account.step_pair(ctx)
        return result, filed

    def test_the_device_found_is_kept_for_the_run_and_filed_under_its_hardware_address(self):
        ctx = self.ctx()
        result, filed = self.pair(ctx, "12:34:56:ab:cd:e0")
        self.assertEqual(result, {"device_ip": "192.0.2.50", "device_mac": "12:34:56:ab:cd:e0"})
        self.assertEqual((ctx.state.input("device_ip"), ctx.state.input("device_mac")),
                         ("192.0.2.50", "12:34:56:ab:cd:e0"))
        self.assertEqual(filed.call_args.args[:2], (ctx, "12:34:56:ab:cd:e0"))
        self.assertEqual(filed.call_args.args[2].device_id, "1000000001")

    def test_one_this_computer_cannot_see_the_hardware_address_of_takes_the_accounts(self):
        ctx = self.ctx()
        result, filed = self.pair(ctx, None)
        self.assertEqual(ctx.state.input("device_mac"), "12:34:56:ab:cd:ef")
        filed.assert_called_once()

    def test_a_typed_address_leaves_the_screen_with_its_question(self):
        # Asked after the "did not answer" menu, it is a block of its own, taken
        # off once answered - not left under the stage line.
        from unittest import mock
        ctx = self.ctx()
        ctx.account_devices = lambda: [{"deviceid": "1000000001", "productModel": "S60TPG",
                                        "name": "Sonoff S60", "online": True, "params": {}}]
        screen = mock.Mock()
        with mock.patch.object(account, "sight_devices",
                               side_effect=lambda devices: [account.Sighting(p, None) for p in devices]), \
             mock.patch.object(prompts, "ask_choice", return_value=2), \
             mock.patch.object(prompts, "ask_ipv4", screen.ask_ipv4), \
             mock.patch.object(output, "transient_begin", screen.begin), \
             mock.patch.object(output, "transient_end", screen.end), \
             mock.patch.object(hostsys, "security_software_note", return_value=[]), \
             mock.patch.object(hostsys, "mac_of", return_value=None), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            screen.ask_ipv4.return_value = "192.168.60.50"
            result = account.step_pair(ctx)
        self.assertEqual(result["device_ip"], "192.168.60.50")
        calls = [name for name, _, _ in screen.mock_calls]
        self.assertEqual(calls[-3:], ["begin", "ask_ipv4", "end"])


if __name__ == "__main__":
    unittest.main()
