"""From Sonoff's firmware: what the device reports through eWeLink (B1), and
the new firmware sent to it (B2)."""
import dataclasses
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.routes.beken.test_beken import a_context
from tools.autoflash import detect, errors, kinds, records
from tools.autoflash.routes.beken import images, stock
from tools.beken import fota_wrap, interim_marker, layouts, rbl
from tools.ewelink import ota_push
from tools.ewelink.get_device_key import EwelinkError


S61 = kinds.SONOFF_S61
MAC = "12:34:56:ab:cd:ef"
OFFERED_LATEST = stock.offered_latest   # itself, as the tests below stand it in for B1


class FromStockTests(unittest.TestCase):
    DEVICE = {"deviceid": "1001234abc", "apikey": "k", "online": True, "productModel": "S61STPF",
              "extra": {"model": "SN-BK7238-S61-01"}, "params": {"fwVersion": "1.0.2"}}

    def setUp(self):
        noted = mock.patch.object(stock.account, "note_firmware_name", return_value="FWSW-01-S61-BK7238")
        self.noted = noted.start()
        self.addCleanup(noted.stop)
        offered = mock.patch.object(stock, "offered_latest", return_value={"offers": [{"binList": []}]})
        self.offered = offered.start()
        self.addCleanup(offered.stop)
        # Its official update file, checked at B1 and again at B2 (images.official).
        official = mock.patch.object(images, "official", return_value=images.Official(
            b"image", S61.wrapper_model, "1.1.0", layouts.BEKEN_4M, (), images.PUBLIC_COPY))
        self.official = official.start()
        self.addCleanup(official.stop)

    def test_b1_takes_the_devices_identity_from_its_account_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_kind=None)   # what it is, B1 finds
            ctx.account_devices = lambda: [{"deviceid": "other"}, self.DEVICE]
            devices_dir = Path(tmp) / "devices"
            with mock.patch.object(records, "DEVICES", devices_dir), mock.patch.object(records, "CAPTURES", Path(tmp)):
                records.remember_device(MAC, ewelink_id="1001234abc")
                with mock.patch.object(stock.prompts, "section"):
                    result = stock.step_identify(ctx)
            self.assertEqual(result, {"stock_version": "1.0.2", "model": "SN-BK7238-S61-01", "product": "S61STPF",
                                      "signal_dbm": None, "firmware_name": "FWSW-01-S61-BK7238",
                                      "official_name": S61.wrapper_model, "official_version": "1.1.0",
                                      "layout": "beken-4m"})
            # The firmware name from eWeLink's offer, asked with the device's own id.
            self.noted.assert_called_once_with(S61, {"offers": [{"binList": []}]}, "1001234abc", "S61STPF",
                                               "SN-BK7238-S61-01")
            self.offered.assert_called_once_with(ctx, {"deviceid": "1001234abc", "model": "SN-BK7238-S61-01",
                                                       "version": "1.0.2"})
            self.assertEqual([ctx.state.input(key) for key in ("device_kind", "stock_version", "product_model")],
                             [S61.key, "1.0.2", "S61STPF"])
            # Another model on the account is never the device: by its product
            # model, or its firmware's, as A2 tells an Espressif one.
            ctx.account_devices = lambda: [dict(self.DEVICE, productModel="MINIR4", extra={"model": "SN-ESP32-MINIR4"})]
            with mock.patch.object(records, "DEVICES", devices_dir), mock.patch.object(records, "CAPTURES", Path(tmp)):
                with self.assertRaisesRegex(errors.Abort, "does not convert"):
                    stock.step_identify(ctx)

    def test_b1_tells_a_kind_by_its_product_model_where_that_decides(self):
        twin = dataclasses.replace(S61, key="made-up-twin", product=re.compile(r"S61TWIN"), product_model_decides=True)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, device_kind=None)
            ctx.account_devices = lambda: [dict(self.DEVICE, productModel="S61TWIN")]
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                    mock.patch.object(records, "CAPTURES", Path(tmp)), \
                    mock.patch.object(stock.kinds.for_account, "__defaults__", ((twin, S61),)), \
                    mock.patch.object(stock.prompts, "section"):
                records.remember_device(MAC, ewelink_id="1001234abc")
                stock.step_identify(ctx)
            self.assertEqual(ctx.state.input("device_kind"), "made-up-twin")

    def test_b1_asks_ewelink_for_the_latest_offer_and_goes_on_without_it(self):
        identity = {"deviceid": "1001234abc", "model": "SN-BK7238-S61-01", "version": "1.0.2"}
        asked = []

        def latest(region, token, query):
            asked.append((region, token, query))
            return {"offers": ["the offer"]}

        def use(call):
            return call("eu", {"at": "token"})

        def unreachable(call):
            raise EwelinkError("eWeLink could not be reached")

        ctx = SimpleNamespace(ewelink=SimpleNamespace(use=use))
        with mock.patch.object(stock, "latest_offer", latest):
            self.assertEqual(OFFERED_LATEST(ctx, identity), {"offers": ["the offer"]})
            self.assertEqual(asked, [("eu", "token", identity)])
            ctx.ewelink.use = unreachable
            self.assertEqual(OFFERED_LATEST(ctx, identity), {"problem": "EwelinkError"})

    def test_b1_reads_the_wifi_signal_the_device_gave_ewelink_and_asks_about_a_weak_one(self):
        for rssi, asked in ((-61, False), (-85, True)):
            with tempfile.TemporaryDirectory() as tmp:
                ctx = a_context(tmp)
                device = dict(self.DEVICE, params={"fwVersion": "1.0.2", "rssi": rssi})
                ctx.account_devices = lambda device=device: [device]
                with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)):
                    records.remember_device(MAC, ewelink_id="1001234abc")
                    with mock.patch.object(stock.prompts, "section"), \
                            mock.patch.object(stock.join.prompts, "section"), \
                            mock.patch.object(stock.join.output, "transient_begin"), \
                            mock.patch.object(stock.join.output, "transient_end"), \
                            mock.patch.object(stock.join.prompts, "ask_yes_no", return_value=False) as question:
                        if asked:
                            with self.assertRaisesRegex(errors.Abort, "weak Wi-Fi signal; nothing was written"):
                                stock.step_identify(ctx)
                        else:
                            self.assertEqual(stock.step_identify(ctx)["signal_dbm"], rssi)
                self.assertEqual(question.called, asked)

    def test_b2_wraps_the_image_and_sends_it_through_ewelink(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="openbeken")
            del ctx.consented   # the go-ahead not given yet in this run
            ctx.account_device = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            sent = {}

            def deliver(**kwargs):
                sent.update(kwargs)
                kwargs["check_device"](self.DEVICE, "SN-BK7238-S61-01")
                return ota_push.Delivery(sent=True, complete=True, bytes_served=len(kwargs["firmware"]), seconds=9.5)

            with mock.patch.object(stock.firewall, "choose_port", return_value=8088), \
                    mock.patch.object(stock.firewall, "ensure_firewall") as opened, \
                    mock.patch.object(stock.prompts, "warning_box") as warned, \
                    mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(stock.output, "say"), \
                    mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                    mock.patch.object(stock.time, "monotonic", side_effect=[100.0, 109.54]), \
                    mock.patch.object(ota_push, "deliver", side_effect=deliver):
                result = stock.step_push(ctx)
            opened.assert_called_once_with(ctx)   # its port let through before the device asks
            # As C2's: what is backed up, and the risk.
            lines = warned.call_args.args[1]
            self.assertIn("Its own data is backed up to this computer once the new firmware runs.", lines)
            self.assertIn("Only go ahead with a device you can afford to lose.", lines)
            self.assertEqual(result["seconds"], 9.5)
            image, version = images.image(ctx, "openbeken")
            self.assertEqual(result["firmware"], "openbeken")
            self.assertEqual(result["version"], version)
            # The image alone: a run to OpenBeken keeps it, so sends no marker.
            self.assertEqual(sent["firmware"], fota_wrap.wrap(image, name=S61.wrapper_model, version=images.FOTA_VERSION))
            self.assertEqual(result["bytes"], len(sent["firmware"]))
            self.assertEqual((sent["device_id"], sent["device_ip"], sent["listen_ip"], sent["listen_port"]),
                             ("1001234abc", "127.0.0.1", "192.0.2.2", 8088))
            self.assertEqual(sent["names"], ("user1.bin",))
            self.assertIs(sent["live"], False)   # what it fetches is logged, never drawn over the stage line
            # The device must be as it was at B1: another model, or firmware, is refused before sending.
            with self.assertRaisesRegex(EwelinkError, "1.0.3"):
                sent["check_device"](dict(self.DEVICE, params={"fwVersion": "1.0.3"}), "SN-BK7238-S61-01")

    def test_b2_on_a_run_to_esphome_refuses_before_writing_if_esphomes_image_would_be(self):
        # Refused only at J2, it would leave the device on the interim firmware.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="esphome")
            ctx.account_device = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            real = images.image

            def image(ctx, firmware):
                if firmware == "esphome":
                    raise errors.Abort("the published esphome image is missing: gone")
                return real(ctx, firmware)
            with mock.patch.object(images, "image", side_effect=image), \
                    mock.patch.object(ota_push, "deliver", side_effect=AssertionError("nothing sent")), \
                    mock.patch.object(stock.firewall, "ensure_firewall"):
                with self.assertRaisesRegex(errors.Abort, "esphome image is missing"):
                    stock.step_push(ctx)

    def test_b2_on_a_run_to_esphome_sends_the_interim_firmware(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="esphome")
            ctx.account_device = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            sent = {}

            def deliver(**kwargs):
                sent.update(kwargs)
                return ota_push.Delivery(sent=True, complete=True, bytes_served=len(kwargs["firmware"]), seconds=9.5)

            with mock.patch.object(stock.firewall, "choose_port", return_value=8088), \
                    mock.patch.object(stock.firewall, "ensure_firewall"), \
                    mock.patch.object(stock.prompts, "warning_box"), \
                    mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(stock.output, "say") as said, \
                    mock.patch.object(ota_push, "deliver", side_effect=deliver):
                result = stock.step_push(ctx)
            image, _ = images.image(ctx, "openbeken")
            self.assertEqual(result["firmware"], "openbeken")
            # The same image and the marker after it, by which it knows itself the
            # interim firmware - all of it in the container, its CRC over all of it.
            sent_payload = interim_marker.with_marker(image)
            self.assertEqual(sent["firmware"], fota_wrap.wrap(sent_payload, name=S61.wrapper_model,
                                                              version=images.FOTA_VERSION))
            self.assertEqual(sent["firmware"][116:], sent_payload)
            self.assertEqual(interim_marker.find(sent_payload), interim_marker.offset_after(len(image)))
            # And it is for the app that image installs, and only that.
            marker = interim_marker.read(sent_payload, interim_marker.offset_after(len(image)))
            self.assertTrue(interim_marker.for_app(marker, rbl.unpack(image)))
            self.assertFalse(interim_marker.for_app(marker, rbl.unpack(images.image(ctx, "esphome")[0])))
            shown = " ".join(str(call.args[0]) for call in said.call_args_list)
            self.assertIn("Sending the interim firmware", shown)
            self.assertNotIn("openbeken", shown.lower())
            with mock.patch.object(detect, "identify", return_value="interim"):
                self.assertEqual(stock.evidence_pushed(ctx),
                                 (True, "The device answers as the interim firmware: the firmware was delivered "
                                        "and installed."))
            # The device's own OpenBeken would be another firmware than the one sent.
            with mock.patch.object(detect, "identify", return_value="openbeken"):
                self.assertIsNone(stock.evidence_pushed(ctx)[0])

    def test_a_spoilt_image_fails_only_the_bootloaders_check_and_only_once(self):
        # --simulate-bad-image: fetched whole (the update's CRC is made over it
        # as sent), but the RBL's own CRC of its body no longer holds.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")
            image, _ = images.image(ctx, "openbeken")
            with mock.patch.object(images, "SPOIL_STEP", "B2"), mock.patch.object(images.output, "say"):
                spoilt = stock.payload(ctx, "openbeken", image)
                again = stock.payload(ctx, "openbeken", image)
                self.assertEqual(images.as_sent(ctx, "J2", image), image)   # another step's: as it is
            self.assertEqual((len(spoilt), spoilt[:rbl.HEADER]), (len(image), image[:rbl.HEADER]))
            with self.assertRaises(rbl.RblError):
                rbl.unpack(spoilt)
            # Once a run, by its record: the image sent again - by this run, or
            # one run again after it, asked the same - is the good one.
            self.assertEqual(again, image)
            self.assertTrue(ctx.state.data["captures"]["B2.spoilt"])

    def test_spoilt_as_many_times_as_asked_then_sent_whole(self):
        # --simulate-bad-image O1:3: past the times a run sends an image again,
        # so the run restarts the device before sending it whole.
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="esphome")
            image = b"RBL" + bytes(rbl.HEADER) + b"x" * 64
            with mock.patch.object(images, "SPOIL_STEP", "O1"), mock.patch.object(images, "SPOIL_TIMES", 3), \
                    mock.patch.object(images.output, "say"):
                sent = [images.as_sent(ctx, "O1", image) for _ in range(4)]
            self.assertEqual([each == image for each in sent], [False, False, False, True])
            self.assertEqual(ctx.state.data["captures"]["O1.spoilt"], 3)

    def test_b2_not_confirmed_writes_nothing_and_a_cut_off_delivery_stays_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, stock_version="1.0.2", firmware="esphome")
            del ctx.consented   # the go-ahead not given yet in this run
            ctx.account_device = SimpleNamespace(device_id="1001234abc")
            ctx.account_devices = lambda: [self.DEVICE]
            common = [mock.patch.object(stock.firewall, "choose_port", return_value=8088),
                      mock.patch.object(stock.firewall, "ensure_firewall"),
                      mock.patch.object(stock.prompts, "warning_box"), mock.patch.object(stock.output, "say")]
            for patch in common:
                patch.start()
                self.addCleanup(patch.stop)
            with mock.patch.object(stock.prompts, "require_word", return_value=False):
                with self.assertRaises(errors.NothingWritten):
                    stock.step_push(ctx)
            with mock.patch.object(stock.prompts, "require_word", return_value=True) as asked, \
                    mock.patch.object(ota_push, "deliver", return_value=ota_push.Delivery(sent=False, error="login failed")):
                with self.assertRaisesRegex(errors.Abort, "was not sent: login failed"):
                    stock.step_push(ctx)
                # Given once a run: B2 sent again (errors.GoBack) does not ask again.
                with self.assertRaisesRegex(errors.Abort, "was not sent: login failed"):
                    stock.step_push(ctx)
            asked.assert_called_once()
            with mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(ota_push, "deliver",
                                      return_value=ota_push.Delivery(sent=True, complete=False, bytes_served=100)):
                # Said plainly, not as something unexpected (seen after a power cut
                # part way in the fault suite), and the step left attempted.
                with self.assertRaisesRegex(errors.StoppedPartWay, "fetched 100 of .*switch it off and on"):
                    stock.step_push(ctx)
            # Turned down while an update cut off part way holds the device's
            # updater (seen in the fault suite): switching it off and on frees it.
            refused = ota_push.Delivery(sent=True, complete=False,
                                        error="Cloud rejected upgrade command: error 410")
            with mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(stock.prompts, "UNATTENDED", True), \
                    mock.patch.object(ota_push, "deliver", return_value=refused):
                with self.assertRaisesRegex(errors.Abort, "switch it off and on"):
                    stock.step_push(ctx)
            # Attended, the person switches it off and on, it is counted, and it is
            # sent again once eWeLink shows it online - as C2 does.
            whole = ota_push.Delivery(sent=True, complete=True, bytes_served=100, seconds=9.5)
            with mock.patch.object(stock.prompts, "require_word", return_value=True), \
                    mock.patch.object(ota_push, "deliver", side_effect=[refused, whole]) as sent, \
                    mock.patch.object(stock.arrival.prompts, "section"), \
                    mock.patch.object(stock.arrival.prompts, "ask_choice", return_value=0), \
                    mock.patch.object(stock.arrival, "online_in_ewelink", return_value=True):
                self.assertIn("seconds", stock.step_push(ctx))
            self.assertEqual(sent.call_count, 2)
            self.assertEqual(ctx.state.input("power_cycles"), {"by hand": 1})

    def test_the_evidence_of_a_push_is_what_the_device_answers_as(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")
            for running, verdict in (("openbeken", True), ("sonoff", False), (None, None), ("esphome", None),
                                     ("interim", None)):
                with mock.patch.object(detect, "identify", return_value=running):
                    self.assertEqual(stock.evidence_pushed(ctx)[0], verdict, running)


class BusyDeviceTests(unittest.TestCase):
    """A device whose updater an update cut off part way still holds answers a
    new one with error 410: through ota_push.deliver to the step's advice."""

    def test_the_devices_410_reaches_the_step_as_switch_it_off_and_on(self):
        from tools.ewelink import ota_push, relay
        device = {"deviceid": "1000abcdef", "apikey": "made-up", "devicekey": "made-up", "online": True,
                  "extra": {"model": "SN-BK7238-S61-01"}, "params": {"fwVersion": "1.0.2"}}

        class Socket:
            sent: list = []

            def send_json(self, value):
                self.sent.append(value)

            def receive_json(self, timeout=None):
                last = self.sent[-1]
                if last.get("action") == "userOnline":
                    return {"error": 0}
                return {"error": 410, "sequence": last["sequence"], "deviceid": "1000abcdef"}

            def close(self):
                pass

        with mock.patch.object(ota_push, "get_devices", return_value=[device]), \
                mock.patch.object(relay, "api_json", return_value={"domain": "192.0.2.1", "port": 443}), \
                mock.patch.object(ota_push.WebSocket, "connect", return_value=Socket()):
            delivered = ota_push.deliver(account=ota_push.Account(lambda: ("eu", {"at": "t", "user": {"apikey": "a"}})),
                                         device_id="1000abcdef",
                                         firmware=b"image", version="9.9.9", listen_ip="127.0.0.1",
                                         listen_port=0, device_ip="127.0.0.1", wait=5, say=lambda *a: None)
        self.assertTrue(delivered.sent, delivered.error)
        self.assertTrue(ota_push.busy(delivered.error), delivered.error)


if __name__ == "__main__":
    unittest.main()
