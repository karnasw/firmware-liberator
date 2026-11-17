"""The device's Sonoff identity: found in the sector Tasmota's layout keeps it
in, and kept there on the way to Tasmota (K1)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.test_device import FakeDevice
from tests.autoflash.test_plans import stock_flash
from tests.espressif.test_sonoff_identity import sonoff_nvs
from tools.autoflash import kinds, output, plans, records
from tools.autoflash.device import DeviceError
from tools.autoflash.routes import espressif
from tools.autoflash.routes.espressif import hub, identity, images
from tools.autoflash.state import DONE, RunState
from tools.espressif import sonoff_identity
from tools.espressif.inspect_stock_nvs import parse
from tools.espressif.safeboot.safeboot_migration import OFFICIAL_TABLE_SHA256, TARGET_TABLE_SHA256


MAC = "12:34:56:ab:cd:ef"
IDENTITY = sonoff_identity.from_nvs(sonoff_nvs())


def on_tasmota(table=None, sector=None):
    flash = bytearray(b"\xff" * 0x400000)
    flash[0x8000:0x9000] = table or bytes(0x1000)
    if sector:
        flash[sonoff_identity.SECTOR_AT:sonoff_identity.SECTOR_AT + len(sector)] = sector
    return flash


def settings_of(nvs: bytes) -> dict:
    names, values = parse(nvs)
    return {(names[space], key): value.data[:value.size] for (space, key), value in values.items()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        for patch in (mock.patch.object(records, "DEVICES", self.tmp / "devices"),
                      mock.patch.object(output, "say"), mock.patch.object(output, "tell"),
                      mock.patch.object(output, "log_only")):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, flash, **inputs):
        state = RunState.open(self.tmp / "run")
        for key, value in {"device_kind": "sonoff-s60", "device_mac": MAC, **inputs}.items():
            state.set_input(key, value)
        return SimpleNamespace(state=state, device=FakeDevice(flash), dry_run=False,
                               wifi=lambda: ("Home-2G", "pw"), account_devices=lambda: [],
                               ewelink_region=lambda: "eu")

    def back_up(self, nvs=None):
        directory = records.backup_dir(MAC)
        directory.mkdir(parents=True)
        flash, _ = stock_flash()
        flash = bytearray(flash)
        flash[0x9000:0x19000] = nvs or sonoff_nvs()
        (directory / "flash.bin").write_bytes(bytes(flash))
        (directory / "efuse.bin").write_bytes(b"\0" * 1024)


class KeptOnTasmotaTests(Fixture):
    def test_found_by_what_the_sector_says_it_is(self):
        flash = on_tasmota(sector=sonoff_identity.to_sector(IDENTITY))
        flash[0x8000:0x9000] = b"\x01" * 0x1000
        ctx = self.ctx(flash)
        with mock.patch.object(ctx.device, "sha_region", return_value=TARGET_TABLE_SHA256):
            self.assertEqual(identity.kept_in_sector(ctx), IDENTITY)
        output.say.assert_not_called()
        # Its table's entry gone with Tasmota written over it whole: said, and found all the same.
        with mock.patch.object(ctx.device, "sha_region", return_value=OFFICIAL_TABLE_SHA256):
            self.assertEqual(identity.kept_in_sector(ctx), IDENTITY)
        self.assertIn("no longer has the entry", output.say.call_args_list[0].args[0])

    def test_a_sector_that_cannot_be_read_is_not_none(self):
        ctx = self.ctx(on_tasmota(sector=sonoff_identity.to_sector(IDENTITY)))
        with mock.patch.object(ctx.device, "read_region", side_effect=DeviceError("timed out")), \
                self.assertRaises(DeviceError):
            identity.kept_in_sector(ctx)

    def test_no_sector_or_a_damaged_one_is_none(self):
        self.assertIsNone(identity.kept_in_sector(self.ctx(on_tasmota())))
        sector = bytearray(sonoff_identity.to_sector(IDENTITY))
        sector[40] ^= 1
        self.assertIsNone(identity.kept_in_sector(self.ctx(on_tasmota(sector=bytes(sector)))))
        # Only as much as the record is long is read.
        ctx = self.ctx(on_tasmota(sector=sonoff_identity.to_sector(IDENTITY)))
        identity.kept_in_sector(ctx)
        self.assertLess(sum(length for _, length in ctx.device.reads), 1000)


class NoIdentityAnywhereTests(Fixture):
    def test_a_test_can_have_a_device_keep_none_and_its_account_hold_none(self):
        # cli.py --simulate-no-identity: a device that does keep one, and is in
        # its account, taken for one that is neither - for the emulator's
        # attended run back to Sonoff, which puts it back where it came from.
        ctx = self.ctx(on_tasmota(sector=sonoff_identity.to_sector(IDENTITY)))
        ctx.account_devices = lambda: [{"deviceid": IDENTITY.deviceid, "params": {"staMac": MAC}}]
        self.assertEqual(identity.kept_in_sector(ctx), IDENTITY)
        self.assertIsNotNone(identity.account_record(ctx))
        with mock.patch.object(identity, "SIMULATE_NONE", True):
            self.assertIsNone(identity.kept_in_sector(ctx))
            self.assertIsNone(identity.account_record(ctx))
            self.assertIsNone(identity.from_settings(ctx, images.layout_of(kinds.SONOFF_S60)))


class AccountRecordTests(Fixture):
    def test_found_by_its_id_else_by_its_mac(self):
        ctx = self.ctx(on_tasmota())
        by_mac = {"deviceid": "1000fedcba", "params": {"staMac": MAC.upper()}}
        by_id = {"deviceid": IDENTITY.deviceid, "params": {}}
        other = {"deviceid": "1000000001", "params": {"staMac": "12:34:56:ab:cd:01"}}
        ctx.account_devices = lambda: [other, by_mac, by_id]
        self.assertIs(identity.account_record(ctx, IDENTITY), by_mac)   # the first that is the device
        ctx.account_devices = lambda: [other, by_id]
        self.assertIs(identity.account_record(ctx, IDENTITY), by_id)
        self.assertIsNone(identity.account_record(ctx))


class KeepIdentityTests(Fixture):
    """K1: from the backup on this computer, else the device's own settings."""

    def run_k1(self, ctx):
        order = []
        with mock.patch.object(hub, "sonoff_layout_back", side_effect=lambda ctx: order.append("layout back")), \
                mock.patch.object(hub, "run_plan", side_effect=lambda *_: order.append("kept") or
                                  {"plan": "keep_identity"}) as run:
            result = identity.step_keep_identity(ctx)
        self.order = order
        return result, (run.call_args.args[1] if run.called else None)

    def test_from_the_backup(self):
        self.back_up()
        result, plan = self.run_k1(self.ctx(b"\xff" * 0x400000))
        self.assertEqual(result["from"], "its Sonoff backup on this computer")
        self.assertIs(result["kept"], True)
        # Written from the hub on Sonoff's layout, put back first.
        self.assertEqual(self.order, ["layout back", "kept"])
        self.assertEqual(plan.steps, [plans.Write("identity", sonoff_identity.SECTOR_AT, whole=True)])
        self.assertEqual(sonoff_identity.from_sector(plan.blobs["identity"]), IDENTITY)

    def test_from_the_devices_own_settings_with_no_backup(self):
        flash, _ = stock_flash()
        flash = bytearray(flash)
        flash[0x9000:0x19000] = sonoff_nvs()
        ctx = self.ctx(flash)
        result, plan = self.run_k1(ctx)
        self.assertEqual(result["from"], "its own settings")
        # Every page of its settings partition looked at, as long as Sonoff's table says it is.
        from tools.espressif import flashmap
        nvs = flashmap.nvs(bytes(flash))
        self.assertEqual([address for address, length in ctx.device.reads if length == 4],
                         list(range(nvs.offset, nvs.offset + nvs.size, 0x1000)))
        self.assertEqual(sonoff_identity.from_sector(plan.blobs["identity"]), IDENTITY)

    def test_with_neither_nothing_is_written(self):
        result, plan = self.run_k1(self.ctx(b"\xff" * 0x400000))
        self.assertEqual((result, plan), ({"kept": False}, None))

    def test_a_device_that_does_not_answer_is_not_taken_for_one_that_holds_none(self):
        ctx = self.ctx(b"\xff" * 0x400000)
        with mock.patch.object(ctx.device, "read_nvs", side_effect=DeviceError("timed out")), \
                self.assertRaises(DeviceError):
            self.run_k1(ctx)

    def test_a_run_past_its_commit_leaves_it_out(self):
        ctx = self.ctx(b"\xff" * 0x400000)
        ctx.state.mark("E3", DONE)
        self.assertIn("skipped", self.run_k1(ctx)[0])

    def test_it_comes_first_on_the_way_to_tasmota(self):
        steps = [step for step, _, _ in espressif.steps_for("tasmota", "sonoff")]
        self.assertEqual(steps[steps.index("D5") + 1:steps.index("E1")], ["K1"])
        self.assertIn("K1", espressif.RETRY_STEPS)


if __name__ == "__main__":
    unittest.main()
