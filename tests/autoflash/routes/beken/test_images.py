"""The firmware a Beken device is given: the published images as pinned,
Sonoff's own from the public dump, and a kind known only by its official
update file, refused on any table but its own."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.beken import test_layouts
from tests.autoflash.routes.beken.test_beken import a_context
from tools.autoflash import errors, kinds, records
from tools.autoflash.routes import public_copies
from tools.autoflash.routes.beken import arrived, images, stock
from tools.beken import fota_wrap, rbl
from tools.ewelink import ota_push


S61 = kinds.SONOFF_S61
MAC = "12:34:56:ab:cd:ef"


class ImagesTests(unittest.TestCase):
    def test_the_published_images_are_the_ones_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp)
            for firmware in ("openbeken", "esphome"):
                data, version = images.image(ctx, firmware)
                self.assertEqual(rbl.header(data).name, "app", firmware)
                self.assertEqual(version, images.PINS["sonoff-s61"][firmware]["version"])
            with self.assertRaisesRegex(errors.Abort, "no tasmota image"):
                images.image(ctx, "tasmota")

    def test_a_changed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp)
            pinned = dict(images.PINS["sonoff-s61"]["openbeken"], sha256="0" * 64)
            with mock.patch.dict(images.PINS["sonoff-s61"], {"openbeken": pinned}):
                with self.assertRaisesRegex(errors.Abort, "not the file firmware.json pins"):
                    images.image(ctx, "openbeken")

    def test_an_esphome_built_by_hand_is_checked_as_an_rt_ota_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "own.rbl"
            own.write_bytes(rbl.pack(b"an esphome", version="26.10.30-bk7238"))
            ctx = a_context(tmp)
            ctx.esphome_firmware = own
            with mock.patch.object(images.output, "say"):
                self.assertEqual(images.image(ctx, "esphome"), (own.read_bytes(), "26.10.30-bk7238"))
            own.write_bytes(b"not an image")
            with self.assertRaisesRegex(errors.Abort, "not an rt_ota image"):
                images.image(ctx, "esphome")
            # Its header right, its body cut short: the whole image is checked.
            own.write_bytes(rbl.pack(b"an esphome", version="26.10.30-bk7238")[:-1])
            with self.assertRaisesRegex(errors.Abort, "not an rt_ota image"):
                images.image(ctx, "esphome")


class UntestedKindTests(unittest.TestCase):
    """A kind with no public dump, no real one of which has been converted: its
    official file's table checked, and the person told, before anything is written."""
    KIND = kinds.SONOFF_BASIC_1GS
    DEVICE = {"deviceid": "1001234abc", "apikey": "k", "online": True, "productModel": "BASIC-1GS",
              "extra": {"model": "SN-BK7238-BASICR5-01"}, "params": {"fwVersion": "1.0.4"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.use_official(test_layouts.FOUR_MB)
        for patch in (mock.patch.object(stock.firewall, "choose_port", return_value=8088),
                      mock.patch.object(stock.firewall, "ensure_firewall"),
                      mock.patch.object(stock.account, "note_firmware_name", return_value=""),
                      mock.patch.object(stock, "offered_latest", return_value={}),
                      mock.patch.object(images, "latest_offer", return_value={"problem": "none offered here"}),
                      mock.patch.object(stock.prompts, "section"),
                      mock.patch.object(stock.output, "transient_begin"),
                      mock.patch.object(stock.output, "transient_end"),
                      mock.patch.object(stock.output, "say")):
            patch.start()
            self.addCleanup(patch.stop)
        logged = mock.patch.object(stock.output, "log_only")
        self.logged = logged.start()
        self.addCleanup(logged.stop)
        boxes = mock.patch.object(stock.prompts, "warning_box")
        self.box = boxes.start()
        self.addCleanup(boxes.stop)

    def use_official(self, table):
        """The kind's official file: a made-up app holding `table`, kept where one fetched goes."""
        image = rbl.pack(test_layouts.an_app(table), name="app", version="1.0.4")
        data = fota_wrap.wrap(image, name=self.KIND.wrapper_model, version="1.0.4")
        (Path(self.tmp.name) / "official.bin").write_bytes(data)
        for patch in (mock.patch.dict(public_copies.COPIES, {self.KIND.key: ("official.bin",
                                                                             hashlib.sha256(data).hexdigest())}),
                      mock.patch.object(public_copies, "KEPT", Path(self.tmp.name))):
            patch.start()
            self.addCleanup(patch.stop)
        self.image = image

    def context(self, tmp, **inputs):
        ctx = a_context(tmp, device_kind=self.KIND.key, stock_version="1.0.4", firmware="openbeken", **inputs)
        ctx.account_device = SimpleNamespace(device_id="1001234abc")
        ctx.account_devices = lambda: [self.DEVICE]
        ctx.ewelink = SimpleNamespace(held=("eu", {"at": "token"}), use=lambda call: call("eu", {"at": "token"}))
        ctx.consented = False
        return ctx

    def logs(self):
        return "\n".join(call.args[0] for call in self.logged.call_args_list)

    def test_it_is_a_kind_the_tool_takes_by_its_model_alone(self):
        self.assertIs(kinds.for_stock_model("SN-BK7238-BASICR5-01"), self.KIND)
        self.assertIs(kinds.for_account("BASIC-1GS", ""), self.KIND)
        self.assertIsNone(kinds.for_account("BASIC-1GSP", ""))   # another firmware, a latching relay
        self.assertIs(self.KIND.status, kinds.Status.TESTERS_WANTED)

    def test_b1_logs_what_the_file_says_for_the_next_person(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.context(tmp)
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)):
                records.remember_device(MAC, ewelink_id="1001234abc")
                result = stock.step_identify(ctx)
        self.assertEqual((result["official_name"], result["official_version"], result["layout"]),
                         (self.KIND.wrapper_model, "1.0.4", "beken-4m"))
        logs = self.logs()
        self.assertIn(self.KIND.wrapper_model, logs)
        self.assertIn("version 1.0.4, from the public copy of Sonoff's update file (FlashDumps); "
                      "the device reports 1.0.4", logs)
        self.assertIn("ota 0x286000+0x15a000", logs)
        self.assertIn("Flash size: the stock firmware does not say; the table is for 4 MB", logs)

    def test_b1_refuses_a_file_on_another_table_before_anything_else(self):
        self.use_official(test_layouts.TWO_MB)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.context(tmp)
            with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), mock.patch.object(records, "CAPTURES", Path(tmp)):
                records.remember_device(MAC, ewelink_id="1001234abc")
                with self.assertRaisesRegex(errors.NothingWritten, "not one this tool knows.*nothing was written"):
                    stock.step_identify(ctx)

    def test_b2_refuses_it_too_and_sends_nothing(self):
        self.use_official(test_layouts.TWO_MB)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ota_push, "deliver") as sent, \
                mock.patch.object(stock.prompts, "require_word", return_value=True) as asked:
            with self.assertRaises(errors.NothingWritten):
                stock.step_push(self.context(tmp))
        sent.assert_not_called()
        asked.assert_not_called()

    def test_a_table_said_to_be_unknown_in_a_test_is_refused_as_one(self):
        with mock.patch.object(images, "UNKNOWN_TABLE", True), self.assertRaisesRegex(errors.NothingWritten, "0x1e1000"):
            images.official(self.KIND)

    def test_a_table_known_but_not_this_kinds_is_refused(self):
        other = kinds.Kind(**{**vars(self.KIND), "layout": "another"})
        with mock.patch.dict(public_copies.COPIES, {other.key: public_copies.COPIES[self.KIND.key]}), \
                self.assertRaisesRegex(errors.NothingWritten, "not the another one"):
            images.official(other)

    def test_a_file_that_is_not_the_one_known_is_refused(self):
        with mock.patch.dict(public_copies.COPIES, {self.KIND.key: ("official.bin", "0" * 64)}), \
                mock.patch.object(public_copies.hostsys, "download", side_effect=OSError("no network")), \
                mock.patch.object(images, "offered_file", return_value=None):
            with self.assertRaisesRegex(errors.NothingWritten, "could not be fetched.*Nothing was written"):
                images.official(self.KIND)

    def test_b2_asks_first_and_declined_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ota_push, "deliver") as sent, \
                mock.patch.object(stock.prompts, "require_word", return_value=False) as asked:
            with self.assertRaisesRegex(errors.NothingWritten, "not confirmed"):
                stock.step_push(self.context(tmp))
        sent.assert_not_called()
        asked.assert_called_once_with("proceed")
        said = " ".join(self.box.call_args.args[1])
        self.assertIn("Not yet proven on a real device of this kind", said)
        # The same words as for every kind, with nothing of its own.
        for words in ("serial cable", "flash size", "go wrong"):
            self.assertNotIn(words, said)

    def test_accepted_it_goes_on_and_is_not_asked_again_in_the_run(self):
        done = ota_push.Delivery(sent=True, complete=True, bytes_served=1, seconds=1.0)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ota_push, "deliver", return_value=done) as sent, \
                mock.patch.object(stock.prompts, "require_word", return_value=True) as asked:
            ctx = self.context(tmp)
            stock.step_push(ctx)
            stock.step_push(ctx)
        self.assertEqual([call.args[0] for call in asked.call_args_list], ["proceed"])
        self.assertEqual(sent.call_count, 2)

    def test_an_unattended_run_has_accepted_it(self):
        done = ota_push.Delivery(sent=True, complete=True, bytes_served=1, seconds=1.0)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ota_push, "deliver", return_value=done) as sent, \
                mock.patch.object(stock.prompts, "UNATTENDED", True), \
                mock.patch.object(stock.prompts, "answered_unattended") as answered:
            stock.step_push(self.context(tmp))
        sent.assert_called_once()
        self.assertIn("PROCEED", [call.args[1] for call in answered.call_args_list])

    def test_going_back_sends_the_official_files_image(self):
        self.assertEqual(images.stock_rbl(self.KIND), (self.image, "1.0.4"))

    def test_going_back_takes_the_devices_own_backup_first_as_the_espressif_route_does(self):
        image, data = self.offered()
        kept = rbl.pack(test_layouts.an_app(test_layouts.FOUR_MB), name="app", version="1.0.2")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                mock.patch.object(records, "CAPTURES", Path(tmp)):
            ctx = self.context(tmp, device_mac=MAC)
            records.save_beken_backup(MAC, b"own data", kept, "1.0.2", kind=self.KIND.name)
            with mock.patch.object(images, "offered_file", side_effect=AssertionError("not asked")):
                self.assertEqual(images.stock_rbl(self.KIND, ctx), (kept, "1.0.2"))
            self.assertIn("Sonoff's firmware 1.0.2: from the device's own backup", self.logs())
            # One that is not whole, as its manifest says: eWeLink's file next.
            (records.find_backup(MAC).directory / "stock-app-1.0.2.rbl").chmod(0o600)
            (records.find_backup(MAC).directory / "stock-app-1.0.2.rbl").write_bytes(kept[:-1] + b"x")
            with mock.patch.object(images, "offered_file", return_value=data):
                self.assertEqual(images.stock_rbl(self.KIND, ctx), (image, "1.0.6"))

    def test_going_back_is_refused_on_another_table(self):
        self.use_official(test_layouts.TWO_MB)
        with self.assertRaisesRegex(errors.Abort, "not one this tool knows"):
            images.stock_rbl(self.KIND)

    def offered(self, table=test_layouts.FOUR_MB, name=None, version="1.0.6"):
        """A file as eWeLink would offer the device: its image, and the file."""
        image = rbl.pack(test_layouts.an_app(table), name="app", version=version)
        return image, fota_wrap.wrap(image, name=name or self.KIND.wrapper_model, version=version)

    def test_the_file_ewelink_offers_comes_first_as_on_the_espressif_route(self):
        image, data = self.offered()
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.context(tmp)
            with mock.patch.object(images, "offered_file", return_value=data):
                found = images.official(self.KIND, ctx)
                self.assertEqual((found.image, found.version, found.source), (image, "1.0.6", images.OFFERED))
                self.assertEqual(images.stock_rbl(self.KIND, ctx), (image, "1.0.6"))
            # None offered: the public copy, as before.
            with mock.patch.object(images, "offered_file", return_value=None):
                found = images.official(self.KIND, ctx)
                self.assertEqual((found.image, found.source), (self.image, images.PUBLIC_COPY))
                self.assertEqual(images.stock_rbl(self.KIND, ctx), (self.image, "1.0.4"))

    def test_an_offered_file_that_fails_the_checks_gives_way_to_the_public_copy(self):
        for table, name, why in ((test_layouts.TWO_MB, None, "partition table"),
                                 (test_layouts.FOUR_MB, "FWSW-01-S61-BK7238", "is for FWSW-01-S61-BK7238")):
            _, data = self.offered(table, name)
            with self.subTest(why=why), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(images, "offered_file", return_value=data), \
                    mock.patch.object(images.output, "log_only") as logged:
                found = images.official(self.KIND, self.context(tmp))
                self.assertEqual((found.image, found.source), (self.image, images.PUBLIC_COPY))
                self.assertIn(why, logged.call_args.args[0])

    def test_ewelink_is_asked_once_a_run_with_the_devices_own_id_signing_in_if_need_be(self):
        _, data = self.offered()
        asked = []

        def latest(region, token, query):
            asked.append(query)
            return {"offers": [{"version": "1.0.6", "binList": [
                {"name": "user2.bin", "downloadUrl": "https://example.com/user2.bin"},
                {"name": "user1.bin", "downloadUrl": "https://example.com/user1.bin"}]}]}

        fetched = []

        def download(url, deviceid):
            fetched.append((url, deviceid))
            return data

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(images, "latest_offer", latest), \
                mock.patch.object(images.offered_firmware, "fetch",
                                  lambda offers, deviceid, fetch=images.offered_firmware.fetch:
                                  fetch(offers, deviceid, get=download)):
            ctx = self.context(tmp)
            ctx.ewelink = SimpleNamespace(held=None, use=lambda call: call("eu", {"at": "token"}))
            self.assertEqual(images.offered_file(ctx), data)
            self.assertEqual(images.offered_file(ctx), data)
        self.assertEqual(asked, [{"deviceid": "1001234abc", "model": self.KIND.stock_model}])
        self.assertEqual(fetched, [("https://example.com/user1.bin", "1001234abc")], "user1.bin alone, once")

    def test_the_stub_is_all_a_run_against_it_reaches(self):
        image, data = self.offered()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict("os.environ", {"AUTOFLASH_EWELINK_API_BASE": "https://apia.example.test"}), \
                mock.patch.object(public_copies, "fetch", side_effect=AssertionError("not reached")):
            ctx = self.context(tmp)
            with mock.patch.object(images, "offered_file", return_value=data):
                self.assertEqual(images.official(self.KIND, ctx).source, images.OFFERED)
                self.assertEqual(images.stock_rbl(self.KIND, ctx),
                                 (image, "1.0.6"))
            with mock.patch.object(images, "offered_file", return_value=None):
                with self.assertRaisesRegex(errors.NothingWritten, "could not be fetched from eWeLink"):
                    images.official(self.KIND, ctx)
                with self.assertRaisesRegex(errors.Abort, "could not be fetched from eWeLink"):
                    images.stock_rbl(S61, ctx)

    def test_the_backup_logs_whether_the_flash_matched(self):
        for matched, words in ((True, "was read at 0x3e0000: it matches"), (False, "was not read at 0x3e0000")):
            with tempfile.TemporaryDirectory() as tmp:
                ctx = self.context(tmp)
                arrived.note_flash(ctx, matched)
                self.assertIs(ctx.state.data["captures"]["B4.flash_matched"], matched)
                self.assertIn(words, self.logged.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
