"""Back to Sonoff's firmware (tools/autoflash/routes/espressif/sonoff.py): with
no backup on this computer (R1), the flash its backup would have held, put
together; and a device whose Sonoff firmware ran from its second slot."""
import json
import unittest
from unittest import mock

from tests.autoflash.routes.espressif.test_identity import Fixture, IDENTITY, MAC, settings_of
from tests.autoflash.routes.espressif.test_sonoff_firmware import SONOFF_APP
from tests.espressif.test_sonoff_identity import FRAME
from tools.autoflash import errors, output, plans, records
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import NothingWritten
from tools.autoflash.routes.espressif import hub, images, sonoff, sonoff_firmware
from tools.espressif import sonoff_identity
from tools.ewelink import get_device_key


class RebuiltTests(Fixture):
    """R1 with no backup: Sonoff's firmware, its layout and the identity."""

    def restore(self, ctx):
        with mock.patch.object(sonoff_firmware, "official_app",
                               return_value=(SONOFF_APP, "the file eWeLink offers the device")) as fetched, \
                mock.patch.object(sonoff, "out_of_sonoffs_slot", return_value=1), \
                mock.patch.object(plans, "app_length", return_value=len(SONOFF_APP)), \
                mock.patch.object(hub, "run_plan", return_value={"plan": "x"}) as run:
            result = sonoff.step_sonoff_restore(ctx)
        first, second = (call.args[1] for call in run.call_args_list)
        return result, fetched, first, second

    def test_from_the_identity_the_device_kept(self):
        flash = bytearray(b"\xff" * 0x400000)
        flash[0x8000:0x9000] = images.sonoff_table()
        flash[0x9000:0x19000] = sonoff_identity.nvs(IDENTITY, 0x10000)
        ctx = self.ctx(flash)
        result, fetched, first, second = self.restore(ctx)
        self.assertEqual(fetched.call_args.args[2], IDENTITY)
        self.assertEqual(ctx.state.data["captures"]["R1.rebuilt"]["identity"], "kept")
        self.assertEqual(ctx.state.data["captures"]["R1.identity"], "kept")   # for the report
        self.assertEqual(first.steps[0].address, 0x20000)
        written = settings_of(second.blobs["settings"])
        frame = json.loads(written[("frame_params", "content")])
        self.assertEqual((frame["deviceid"], frame["ssid"], frame["passwd"]), (FRAME["deviceid"], "Home-2G", "pw"))
        self.assertEqual(written[("productId", "productId")], b"24440700000000\r\n")
        self.assertEqual(result["from"], "the Sonoff identity the device kept")

    def test_from_the_sector_when_the_startup_firmware_wiped_the_settings(self):
        # As it does on the way from the mover or ESPHome to the hub; the end of
        # ota_1 is past every image written there since.
        flash = bytearray(b"\xff" * 0x400000)
        flash[0x8000:0x9000] = images.sonoff_table()
        flash[0x3FF000:0x400000] = sonoff_identity.to_sector(IDENTITY)
        ctx = self.ctx(flash)
        _, fetched, _, second = self.restore(ctx)
        self.assertEqual(fetched.call_args.args[2], IDENTITY)
        self.assertEqual(ctx.state.data["captures"]["R1.rebuilt"]["identity"], "kept")

    def test_from_the_account_record_when_the_device_kept_none(self):
        ctx = self.ctx(b"\xff" * 0x400000)
        record = {"deviceid": "10000e32ab", "devicekey": "made-up-key", "params": {"staMac": MAC.upper()},
                  "extra": {"model": "SN-ESP32C3-S60-01", "uiid": 190}}
        ctx.account_devices = lambda: [{"deviceid": "other", "params": {"staMac": "12:34:56:00:00:01"}}, record]
        _, _, _, second = self.restore(ctx)
        frame = json.loads(settings_of(second.blobs["settings"])[("frame_params", "content")])
        self.assertEqual((frame["deviceid"], frame["apikey"], frame["dis_server"]),
                         ("10000e32ab", "made-up-key", get_device_key.dispatch_server("eu")[0]))
        self.assertEqual(ctx.state.data["captures"]["R1.rebuilt"]["identity"], "account")
        self.assertEqual(ctx.state.data["captures"]["R1.identity"], "account")

    def test_a_device_that_does_not_answer_is_not_taken_for_one_that_kept_none(self):
        # Seen in the fault suite: switched off as R1 read the identity, it was
        # rebuilt from the account, the device's own passed by.
        ctx = self.ctx(b"\xff" * 0x400000)
        ctx.account_devices = mock.Mock(side_effect=AssertionError("not the account"))
        with mock.patch.object(ctx.device, "read_nvs", side_effect=DeviceError("timed out")), \
                self.assertRaises(DeviceError):
            self.restore(ctx)
        self.assertNotIn("R1.rebuilt", ctx.state.data.get("captures", {}))

    def test_with_nothing_to_rebuild_from_it_goes_back_where_it_came_from(self):
        # Never left part way: from ESPHome back to ESPHome, the rest to
        # Tasmota, nothing written at R1, and the route on from there.
        from tools.autoflash import routes
        for came, firmware in (("esphome", "esphome"), ("tasmota", "tasmota"), ("midway", "tasmota")):
            with self.subTest(came=came):
                ctx = self.ctx(b"\xff" * 0x400000, source=came, firmware="sonoff")
                with mock.patch.object(routes, "go_on_instead") as instead, \
                        mock.patch.object(hub, "run_plan") as run, mock.patch.object(output, "tell") as told:
                    result = sonoff.step_sonoff_restore(ctx)
                run.assert_not_called()
                instead.assert_called_once_with("R1", firmware, "midway")
                self.assertEqual(result, {"stage_outcome": "stopped", "put_back": firmware})
                self.assertEqual((ctx.state.input("firmware"), ctx.state.input("source")), (firmware, "midway"))
                self.assertEqual(ctx.state.input("put_back")["came_from"], came)
                self.assertIn("no record", told.call_args_list[0].args[0])
                (self.tmp / "run" / "autoflash-state.json").unlink()

    def test_a_firmware_that_cannot_be_fetched_goes_back_too(self):
        from tools.autoflash import routes
        ctx = self.ctx(b"\xff" * 0x400000, source="esphome", firmware="sonoff")
        with mock.patch.object(sonoff, "rebuilt",
                               side_effect=NothingWritten("Sonoff's firmware cannot be put back without a backup: "
                                                          "eWeLink did not answer")), \
                mock.patch.object(routes, "go_on_instead") as instead, mock.patch.object(output, "tell"):
            self.assertEqual(sonoff.step_sonoff_restore(ctx)["put_back"], "esphome")
        instead.assert_called_once_with("R1", "esphome", "midway")

    def test_the_backup_comes_first(self):
        self.back_up()
        ctx = self.ctx(b"\xff" * 0x400000)
        with mock.patch.object(sonoff, "rebuilt", side_effect=AssertionError("not with a backup")):
            result, _, _, second = self.restore(ctx)
        self.assertEqual(result["from"], "backup")
        self.assertEqual(ctx.state.data["captures"]["R1.identity"], "backup")


class SonoffInTheHubsSlotTests(unittest.TestCase):
    """A device whose Sonoff firmware ran from its second slot has it there in its
    backup, the slot the hub runs from (the public S60 dump: seen stopping at R1)."""

    def restore(self, stock_slot, slots):
        from types import SimpleNamespace
        device = mock.Mock()
        ctx = SimpleNamespace(state=SimpleNamespace(input={"device_kind": "sonoff-s60", "device_mac": "12:34:56:ab:cd:ef"}.get,
                                                    capture=lambda key, value: None),
                              device=device, dry_run=False)
        with mock.patch.object(sonoff, "sonoff_backup", return_value=b"backup"), \
             mock.patch.object(records, "find_backup", return_value="its backup"), \
             mock.patch.object(plans, "stock_slot", return_value=stock_slot), \
             mock.patch.object(plans, "back_to_sonoff", return_value=("first", "second")) as planned, \
             mock.patch.object(hub, "running_slot", side_effect=slots), \
             mock.patch.object(hub, "upload_or_prompt") as uploaded, \
             mock.patch.object(hub, "run_plan", return_value={"plan": "x"}), \
             mock.patch.object(output, "say"):
            sonoff.step_sonoff_restore(ctx)
        return planned.call_args.kwargs["running"], uploaded.called, device

    def test_in_the_other_slot_nothing_moves(self):
        running, uploaded, device = self.restore(0, [1])
        self.assertEqual((running, uploaded), (1, False))
        device.wait_for_reboot.assert_not_called()

    def test_in_the_slot_tasmota_runs_from_tasmota_moves_first(self):
        running, uploaded, device = self.restore(1, [1, 0])
        self.assertEqual((running, uploaded), (0, True))
        device.wait_for_reboot.assert_called_once()

    def test_a_rerun_finds_it_moved(self):
        self.assertEqual(self.restore(1, [0])[:2], (0, False))

    def test_tasmota_that_will_not_move_stops_the_run_before_anything_is_written(self):
        with self.assertRaisesRegex(errors.Abort, "still running from slot 1"):
            self.restore(1, [1, 1])


if __name__ == "__main__":
    unittest.main()
