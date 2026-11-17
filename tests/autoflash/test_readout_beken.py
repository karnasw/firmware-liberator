"""A readout of a Beken device: the stock firmware found in the offered file,
the round trip driven through the proven helpers, nothing sent without PROCEED."""
import unittest
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import readout_beken as rb
from tools.beken import fota_wrap, layouts, rbl


def a_ctx() -> SimpleNamespace:
    state = SimpleNamespace(_d={}, set_input=lambda k, v: state._d.__setitem__(k, v),
                            input=lambda k: state._d.get(k), require=lambda *k: [state._d[x] for x in k])
    state._d["device_ip"] = "192.0.2.60"
    state._d["device_mac"] = "12:34:56:ab:cd:ef"
    return SimpleNamespace(state=state, dir=None, listen_ip=lambda: "192.0.2.141",
                           email=lambda: "me@example.com", ewelink_env=lambda: {"EWELINK_PASSWORD": "pw"},
                           ewelink=mock.sentinel.account)


def s61_app() -> bytes:
    """A minimal app carrying the known 4 MB BK7238 table, as layouts reads it."""
    from tests.beken.test_layouts import an_app
    return an_app(layouts.BEKEN_4M.partitions)


def setUpModule():
    # The readout's stage lines are its tests' (test_readout): here, where
    # the read ends and putting Sonoff's firmware back begins.
    patch = mock.patch.object(rb.readout, "restoring")
    patch.start()
    unittest.addModuleCleanup(patch.stop)


class StockFromOfferedTests(unittest.TestCase):
    def offered(self, data):
        return [SimpleNamespace(name="user1.bin", version="1.0.2", data=data)]

    def test_the_offered_stock_fota_gives_its_rbl_and_table(self):
        app = s61_app()
        rbl_image = rbl.pack(app, name="app", version="1.0.2")
        fota = fota_wrap.wrap(rbl_image, name="FWSW-01-S61-BK7238", version="1.0.2")
        found = rb.stock_from_offered(self.offered(fota))
        self.assertIsNotNone(found)
        self.assertEqual(found.rbl, rbl_image)
        self.assertEqual(found.layout.key, layouts.BEKEN_4M.key)

    def test_its_version_is_the_images_own_else_the_offers(self):
        rbl_image = rbl.pack(s61_app(), name="app", version="1.0.3")
        listed = [SimpleNamespace(name="user1.bin", version="1.0.2", data=b"fota")]
        for inside, expected in (("1.0.3", "1.0.3"), ("", "1.0.2")):
            with self.subTest(inside), \
                    mock.patch.object(rb.fota_wrap, "unwrap", return_value=[("app", inside, rbl_image)]):
                self.assertEqual(rb.stock_from_offered(listed).version, expected)

    def test_a_file_that_is_not_a_known_table_is_passed_over(self):
        self.assertIsNone(rb.stock_from_offered(self.offered(b"not a fota")))


class ReadoutImageTests(unittest.TestCase):
    def test_it_refuses_a_file_that_is_not_the_pin(self):
        with mock.patch.object(rb, "PIN", {"file": "artifacts/openbeken-readout-1.18.320-1.rbl",
                                           "sha256": "0" * 64}):
            with self.assertRaisesRegex(rb.Abort, "not the file firmware.json pins"):
                rb.readout_image()

    def test_the_pinned_file_is_returned(self):
        self.assertGreater(len(rb.readout_image()), 1000)


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        for name in ("say", "tell", "transient_begin", "transient_end", "progress", "progress_clear"):
            patch = mock.patch.object(rb.output, name)
            patch.start()
            self.addCleanup(patch.stop)
        self.found = rb.Stock(b"RBL\0stock", "1.0.2", layouts.BEKEN_4M)
        self.device = {"deviceid": "1000abcdef"}
        self.ctx = a_ctx()
        # On Sonoff's firmware, as a readout begins: not the readout firmware already.
        patch = mock.patch.object(rb.openbeken, "is_readout", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def test_nothing_is_sent_without_proceed(self):
        asked = mock.Mock()
        with mock.patch.object(rb.prompts, "warning_box", asked.warning), \
                mock.patch.object(rb.prompts, "require_word", asked.word), \
                mock.patch.object(rb, "deliver_readout") as sent:
            asked.word.return_value = False
            with self.assertRaises(rb.NothingWritten):
                rb.read_out(self.ctx, "FWSW-01-S61-BK7238", "1.0.2", self.device, self.found)
        sent.assert_not_called()
        # Warned not to cut its power before being asked.
        self.assertEqual([call[0] for call in asked.mock_calls], ["warning", "word"])
        self.assertIn("DO NOT CUT ITS POWER", " ".join(asked.warning.call_args.args[1]))

    def test_proceed_runs_the_round_trip_in_order(self):
        order = []
        with mock.patch.object(rb.prompts, "warning_box"), \
                mock.patch.object(rb.prompts, "require_word", return_value=True), \
                mock.patch.object(rb, "deliver_readout", side_effect=lambda *a: order.append("deliver")), \
                mock.patch.object(rb, "wait_for_openbeken", side_effect=lambda ctx: order.append("wait") or "192.0.2.60"), \
                mock.patch.object(rb.openbeken, "info", return_value={"build": "obk", "chipset": "BK7238"}), \
                mock.patch.object(rb, "read_whole_flash", side_effect=lambda ctx, host: order.append("read") or b"\xff" * rb.WHOLE_FLASH), \
                mock.patch.object(rb.records, "save_beken_backup", return_value=(None, False)), \
                mock.patch.object(rb, "revert_to_sonoff", side_effect=lambda *a: order.append("revert")), \
                mock.patch.object(rb, "wait_for_sonoff", side_effect=lambda ctx, did: order.append("sonoff") or True), \
                mock.patch.object(rb.readout, "restoring", side_effect=lambda: order.append("restoring stage")):
            flash, info, back = rb.read_out(self.ctx, "FWSW-01-S61-BK7238", "1.0.2", self.device, self.found)
        self.assertEqual(order, ["deliver", "wait", "read", "restoring stage", "revert", "sonoff"])
        self.assertEqual((len(flash), info, back), (rb.WHOLE_FLASH, {"openbeken": "obk", "chipset": "BK7238"}, True))

    def test_the_record_says_the_reader_stands_in_for_sonoff_until_it_is_fetched_back(self):
        # So a Ctrl-C meanwhile ends by saying to run the readout again: on a
        # Beken device, switching it off and on starts the reader, not Sonoff.
        seen = {}
        key = rb.readout.STATE_KEY
        with mock.patch.object(rb.prompts, "warning_box"), \
                mock.patch.object(rb.prompts, "require_word", return_value=True), \
                mock.patch.object(rb, "deliver_readout", side_effect=lambda *a: seen.setdefault(
                    "delivering", self.ctx.state.input(key))), \
                mock.patch.object(rb, "wait_for_openbeken", return_value="192.0.2.60"), \
                mock.patch.object(rb.openbeken, "info", return_value={}), \
                mock.patch.object(rb, "read_whole_flash", return_value=b"\xff" * rb.WHOLE_FLASH), \
                mock.patch.object(rb.records, "save_beken_backup", return_value=(None, False)), \
                mock.patch.object(rb, "revert_to_sonoff", side_effect=lambda *a: seen.setdefault(
                    "reverting", self.ctx.state.input(key))), \
                mock.patch.object(rb, "wait_for_sonoff", return_value=True):
            rb.read_out(self.ctx, "FWSW-01-S61-BK7238", "1.0.2", self.device, self.found)
        self.assertEqual(seen, {"delivering": rb.readout.REPLACED, "reverting": rb.readout.REPLACED})
        self.assertEqual(self.ctx.state.input(key), "read")

    def test_the_devices_own_data_is_kept_from_the_whole_flash_read(self):
        flash = bytes(range(256)) * (rb.WHOLE_FLASH // 256)
        with mock.patch.object(rb.prompts, "warning_box"), \
                mock.patch.object(rb.prompts, "require_word", return_value=True), \
                mock.patch.object(rb, "deliver_readout"), \
                mock.patch.object(rb, "wait_for_openbeken", return_value="192.0.2.60"), \
                mock.patch.object(rb.openbeken, "info", return_value={}), \
                mock.patch.object(rb, "read_whole_flash", return_value=flash), \
                mock.patch.object(rb.records, "save_beken_backup", return_value=(None, False)) as kept, \
                mock.patch.object(rb, "revert_to_sonoff"), mock.patch.object(rb, "wait_for_sonoff", return_value=True):
            rb.read_out(self.ctx, "FWSW-01-S61-BK7238", "1.0.2", self.device, self.found)
        mac, data, stock, version = kept.call_args.args
        self.assertEqual((mac, stock, version), ("12:34:56:ab:cd:ef", b"RBL\0stock", "1.0.2"))
        self.assertEqual(data, flash[rb.records.BEKEN_REGION[0]:])

    def test_revert_uses_the_revert_command_and_the_offered_rbl(self):
        served = {}
        with mock.patch.object(rb.onward, "serve_to_device",
                               side_effect=lambda ctx, image, what, fetch, unstick=None: served.update(image=image, what=what) or 1.0), \
                mock.patch.object(rb.openbeken, "command") as command:
            rb.revert_to_sonoff(self.ctx, "192.0.2.60", self.found)
            fetch = rb.onward.serve_to_device.call_args.args[3]
            fetch("http://192.0.2.141:8088/firmware.rbl")
        self.assertEqual(served["image"], b"RBL\0stock")
        command.assert_called_once_with("192.0.2.60", "AutoflashRevertToStock http://192.0.2.141:8088/firmware.rbl")

    def test_a_readout_found_already_on_the_readout_firmware_reads_through_it(self):
        rb.openbeken.is_readout.return_value = True
        with mock.patch.object(rb.prompts, "warning_box"), \
                mock.patch.object(rb.prompts, "require_word", return_value=True), \
                mock.patch.object(rb, "deliver_readout") as sent, \
                mock.patch.object(rb, "wait_for_openbeken") as waited, \
                mock.patch.object(rb.openbeken, "info", return_value={"build": "obk", "chipset": "BK7238"}), \
                mock.patch.object(rb, "read_whole_flash", return_value=b"\xff" * rb.WHOLE_FLASH) as read, \
                mock.patch.object(rb.records, "save_beken_backup", return_value=(None, False)), \
                mock.patch.object(rb, "revert_to_sonoff"), mock.patch.object(rb, "wait_for_sonoff", return_value=True):
            rb.read_out(self.ctx, "FWSW-01-S61-BK7238", "1.0.2", self.device, self.found)
        sent.assert_not_called()
        waited.assert_not_called()
        self.assertEqual(read.call_args.args[1], "192.0.2.60")


class PutBackTests(unittest.TestCase):
    """A device a readout stopped part way left on the readout firmware: its
    Sonoff firmware put back by the next run."""

    def setUp(self):
        for name in ("say", "tell", "log_only"):
            patch = mock.patch.object(rb.output, name)
            patch.start()
            self.addCleanup(patch.stop)
        self.ctx = a_ctx()
        self.found = rb.Stock(b"RBL\0stock", "1.0.2", layouts.BEKEN_4M)
        for patch in (mock.patch.object(rb.records, "read_device", return_value={"ewelink_id": "1000abcdef"}),
                      mock.patch.object(rb, "revert_to_sonoff"),
                      mock.patch.object(rb, "wait_for_sonoff", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_from_the_firmware_kept_with_the_backup_eWeLink_asked_nothing(self):
        from tools.autoflash import account
        with mock.patch.object(rb, "saved_stock", return_value=self.found), \
                mock.patch.object(account, "ask_account") as asked:
            said = rb.put_back(self.ctx, "192.0.2.60")
        asked.assert_not_called()
        self.assertEqual(rb.revert_to_sonoff.call_args.args[1:], ("192.0.2.60", self.found))
        rb.wait_for_sonoff.assert_called_once_with(self.ctx, "1000abcdef")
        self.assertIn("Sonoff's firmware 1.0.2 is back", said)

    def test_else_from_the_file_ewelink_offers_it(self):
        from tools.autoflash import account
        from tools.ewelink import offered_firmware
        offered = account.Asked("1.0.2", "SN-BK7238-NEW-01", "NEWBK", [],
                                {"offers": [{"x": 1}]}, "1000abcdef")
        with mock.patch.object(rb, "saved_stock", return_value=None), \
                mock.patch.object(account, "ask_account", return_value=offered) as asked, \
                mock.patch.object(offered_firmware, "fetch", return_value=(["file"], [])), \
                mock.patch.object(rb, "stock_from_offered", return_value=self.found):
            rb.put_back(self.ctx, "192.0.2.60")
        self.assertEqual(asked.call_args.args[1], "1000abcdef")
        self.assertEqual(rb.revert_to_sonoff.call_args.args[2], self.found)

    def test_with_neither_it_stops_having_sent_nothing(self):
        with mock.patch.object(rb, "saved_stock", return_value=None), \
                mock.patch.object(rb.records, "read_device", return_value={}), \
                mock.patch("tools.autoflash.readout.device_at", return_value={}):
            self.ctx.account_devices = lambda: []
            with self.assertRaisesRegex(rb.Abort, "found neither"):
                rb.put_back(self.ctx, "192.0.2.60")
        rb.revert_to_sonoff.assert_not_called()

    def test_a_device_with_no_record_is_found_in_the_account_by_its_address(self):
        from tools.autoflash import account
        from tools.ewelink import offered_firmware
        offered = account.Asked("1.0.2", "SN-BK7238-NEW-01", "NEWBK", [],
                                {"offers": [{"x": 1}]}, "1000abcdef")
        listed = [{"deviceid": "1000abcdef"}]
        self.ctx.account_devices = lambda: listed
        with mock.patch.object(rb, "saved_stock", return_value=None), \
                mock.patch.object(rb.records, "read_device", return_value={}), \
                mock.patch("tools.autoflash.readout.device_at", side_effect=lambda host, devices: devices[0]) as at, \
                mock.patch.object(account, "ask_account", return_value=offered) as asked, \
                mock.patch.object(offered_firmware, "fetch", return_value=(["file"], [])) as fetched, \
                mock.patch.object(rb, "stock_from_offered", return_value=self.found):
            rb.put_back(self.ctx, "192.0.2.60")
        self.assertEqual(at.call_args.args, ("192.0.2.60", listed))
        self.assertEqual(asked.call_args.args[1], "1000abcdef")
        self.assertEqual(fetched.call_args.args, ([{"x": 1}], "1000abcdef"))

    def test_the_kept_firmware_is_read_from_the_backup(self):
        import tempfile
        from pathlib import Path
        app = s61_app()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stock-app-1.0.2.rbl").write_bytes(rbl.pack(app, name="app", version="1.0.2"))
            backup = SimpleNamespace(family="beken", directory=Path(tmp))
            with mock.patch.object(rb.records, "find_backup", return_value=backup):
                found = rb.saved_stock("12:34:56:ab:cd:ef")
        self.assertEqual((found.version, found.layout), ("1.0.2", layouts.BEKEN_4M))
        with mock.patch.object(rb.records, "find_backup", return_value=None):
            self.assertIsNone(rb.saved_stock("12:34:56:ab:cd:ef"))



class ReadWholeFlashTests(unittest.TestCase):
    def test_the_whole_flash_is_the_4_mb_of_the_table_it_reads(self):
        self.assertEqual(rb.WHOLE_FLASH, layouts.BEKEN_4M.flash_size)

    def test_a_read_cut_off_is_a_stop_to_act_on_not_a_crash(self):
        # A raw OSError here was recorded as the readout having crashed.
        import contextlib
        from tools.autoflash import errors, failures, output
        for read in (mock.Mock(side_effect=OSError("short read of the device's flash at 0x1000: 0 of 4096 bytes")),
                     mock.Mock(return_value=b"\xff" * 100)):
            with mock.patch.object(output, "working", side_effect=lambda text: contextlib.nullcontext()), \
                    mock.patch.object(output, "log_only"), mock.patch.object(output, "say"), \
                    mock.patch.object(output, "progress_clear"), mock.patch.object(rb.openbeken, "read_flash", read):
                with self.assertRaises(errors.Abort) as stopped:
                    rb.read_whole_flash(a_ctx(), "192.0.2.60")
            self.assertIn("run the readout again", str(stopped.exception))
            self.assertEqual(failures.classify(str(stopped.exception)), "device-lost")

    def test_a_delivery_cut_part_way_is_a_stop_to_act_on_not_a_crash(self):
        from types import SimpleNamespace
        from tools.autoflash import errors, failures
        cut = SimpleNamespace(sent=True, complete=False, bytes_served=1024, error=None)
        ctx = a_ctx()
        ctx.state.set_input("device_ip", "192.0.2.60")
        ctx.listen_ip = lambda: "192.0.2.10"
        with mock.patch.object(rb, "readout_image", return_value=b"image"), \
                mock.patch.object(rb.fota_wrap, "wrap", return_value=b"wrapped"), \
                mock.patch.object(rb.firewall, "choose_port", return_value=8088), \
                mock.patch.object(rb.firewall, "ensure_firewall"), mock.patch.object(rb.output, "say"), \
                mock.patch.object(rb.safety_locks, "say_lock_back"), \
                mock.patch.object(rb.ota_push, "deliver", return_value=cut):
            with self.assertRaises(errors.StoppedPartWay) as stopped:
                rb.deliver_readout(ctx, "name", "1.0.2", {"deviceid": "1000abcdef"})
        self.assertEqual(failures.classify(str(stopped.exception)), "upload-cut")

    def test_the_reads_start_and_end_are_events_and_its_start_is_in_the_runs_log(self):
        # The Beken fault suite times a fault in the read from its events
        # (READOUT_PHASES); on a terminal the wait is shown, not logged.
        import contextlib
        from tools.autoflash import output
        from tools.beken.emulator import fault_suite
        logged = []
        with mock.patch.object(output, "working", side_effect=lambda text: contextlib.nullcontext()), \
                mock.patch.object(output, "log_only", side_effect=logged.append), \
                mock.patch.object(output, "say", side_effect=logged.append), \
                mock.patch.object(output, "event", side_effect=lambda name, **f: logged.append((name, f))), \
                mock.patch.object(output, "progress_clear"), \
                mock.patch.object(rb.openbeken, "read_flash", return_value=b"\xff" * rb.WHOLE_FLASH) as read:
            self.assertEqual(rb.read_whole_flash(a_ctx(), "192.0.2.60"), b"\xff" * rb.WHOLE_FLASH)
        self.assertEqual(read.call_args.args, ("192.0.2.60", 0, rb.WHOLE_FLASH))   # all of it, from the start
        start, end, _ = fault_suite.READOUT_PHASES["read"]
        self.assertLess(logged.index(("phase", {"phase": start})), logged.index(("phase", {"phase": end})))
        self.assertIn("Reading the device's memory...", logged)


if __name__ == "__main__":
    unittest.main()
