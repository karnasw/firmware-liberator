"""Sonoff's own firmware for a device with no backup: the file eWeLink offers
it, else the public one, checked, and the flash made from it."""
import hashlib
import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.espressif.test_dump_stock_flash import esp_image
from tests.espressif.test_sonoff_identity import FRAME
from tests.autoflash.routes.espressif.test_identity import IDENTITY
from tools.autoflash import kinds, plans
from tools.autoflash.routes.espressif import sonoff_firmware
from tools.espressif import build_vendor_ota, sonoff_identity
from tools.ewelink.get_device_key import Account, EwelinkError
from tools.autoflash.routes.espressif import images


def app_image(project, size=0x3000):
    """An ESP32-C3 app, its description naming `project`, long enough to be
    written in two halves."""
    small = esp_image("1.2.0", project)
    data = bytearray(small[32:32 + 256]) + bytes(size)
    header = bytearray(small[:24])
    body = bytes(header) + struct.pack("<II", 0x3C000020, len(data)) + bytes(data)
    checksum = 0xEF
    for value in data:
        checksum ^= value
    padded = body + bytes(((len(body) // 16) + 1) * 16 - 1 - len(body)) + bytes([checksum])
    return padded + hashlib.sha256(padded).digest()


SONOFF_APP = app_image("esp32_fw")


class SonoffFirmwareTests(unittest.TestCase):
    def wrapped(self, app=SONOFF_APP, model=kinds.SONOFF_S60.wrapper_model):
        return build_vendor_ota.build_wrapper(app, version="1.2.0", model=model)

    def test_the_app_is_taken_from_its_update_file_and_checked(self):
        app = sonoff_firmware.app_in_file(self.wrapped(), kinds.SONOFF_S60.wrapper_model)
        self.assertEqual(sonoff_firmware.checked_app(app, kinds.SONOFF_S60, 0x1F0000), SONOFF_APP)
        with self.assertRaisesRegex(sonoff_firmware.FirmwareError, "no firmware called"):
            sonoff_firmware.app_in_file(self.wrapped(), kinds.SONOFF_POW_ELITE.wrapper_model)
        damaged = bytearray(self.wrapped())
        damaged[-40] ^= 1
        with self.assertRaisesRegex(sonoff_firmware.FirmwareError, "not whole"):
            sonoff_firmware.app_in_file(bytes(damaged), kinds.SONOFF_S60.wrapper_model)

    def test_the_app_is_found_among_several_a_file_holds(self):
        # Sonoff's files can hold the firmware of several models, a record each.
        first = self.wrapped(model="FWSW-E32S60-OTHER-ESP32C3")[0x18:0x64]  # another model's
        own = bytearray(self.wrapped()[0x18:0x64])
        records_end = 0x18 + 2 * 76
        struct.pack_into(">I", own, 0x30, records_end)
        data = bytearray(self.wrapped()[:0x18]) + first + own + SONOFF_APP
        data[1] = 2
        self.assertEqual(sonoff_firmware.app_in_file(bytes(data), kinds.SONOFF_S60.wrapper_model), SONOFF_APP)

    def test_an_app_filling_the_slot_fits_and_a_byte_more_does_not(self):
        size = len(SONOFF_APP)
        self.assertEqual(sonoff_firmware.checked_app(SONOFF_APP, kinds.SONOFF_S60, size), SONOFF_APP)
        with self.assertRaisesRegex(sonoff_firmware.FirmwareError, "does not fit"):
            sonoff_firmware.checked_app(SONOFF_APP, kinds.SONOFF_S60, size - 1)

    def test_anything_but_sonoffs_firmware_for_the_chip_is_refused(self):
        for app, kind, why in ((app_image("autoflash_bridge"), kinds.SONOFF_S60, "not Sonoff's"),
                               (SONOFF_APP, kinds.SONOFF_POW_ELITE, "not for"),
                               (SONOFF_APP[:-1] + bytes([SONOFF_APP[-1] ^ 1]), kinds.SONOFF_S60, "hash")):
            with self.subTest(why=why), self.assertRaisesRegex(sonoff_firmware.FirmwareError, why):
                sonoff_firmware.checked_app(app, kind, 0x1F0000)

    def test_eWeLink_is_asked_as_the_device_asks(self):
        identity_ = IDENTITY
        ctx = SimpleNamespace(ewelink=Account(lambda: ("eu", {"at": "token"})))
        offer = {"offers": [{"binList": [{"name": "user2.bin", "downloadUrl": "https://example.test/2"},
                                         {"name": "user1.bin", "downloadUrl": "https://example.test/1?x=1"}]}]}
        with mock.patch.object(sonoff_firmware, "latest_offer", return_value=offer) as asked, \
                mock.patch.object(sonoff_firmware, "fetch", return_value=b"file") as fetched:
            self.assertEqual(sonoff_firmware.offered(ctx, identity_), b"file")
        self.assertEqual(asked.call_args.args[2], {"deviceid": FRAME["deviceid"], "model": FRAME["model"],
                                                   "version": "0.0.1"})
        self.assertEqual(fetched.call_args.args, ("https://example.test/1?x=1",
                                                  {"deviceid": FRAME["deviceid"], "ts": 1, "sign": 0}))

    def test_a_sign_in_a_helper_ended_is_made_again_for_the_offer(self):
        sign_in = mock.Mock(side_effect=[("eu", {"at": "old"}), ("eu", {"at": "new"})])
        ctx = SimpleNamespace(ewelink=Account(sign_in))
        ctx.ewelink.session()
        offer = {"offers": [{"binList": [{"name": "user1.bin", "downloadUrl": "https://example.test/1"}]}]}
        tokens = []

        def api_json(_url, *, headers, **_more):
            tokens.append(headers["Authorization"].removeprefix("Bearer "))
            if tokens[-1] == "old":   # as eWeLink answers a token a later sign-in ended
                return {"error": 401, "msg": "Access token authentication error"}
            return {"error": 0, "data": {"otaInfoList": offer["offers"]}}
        with mock.patch("tools.ewelink.query_ota.api_json", api_json), \
                mock.patch.object(sonoff_firmware, "fetch", return_value=b"file"):
            self.assertEqual(sonoff_firmware.offered(ctx, IDENTITY), b"file")
        self.assertEqual(tokens, ["old", "new"])

    def test_the_stub_is_all_a_run_against_it_reaches(self):
        ctx = SimpleNamespace(ewelink=Account(mock.Mock(side_effect=EwelinkError("not reached"))))
        layout = images.layout_of(kinds.SONOFF_S60)
        with mock.patch.dict("os.environ", {"AUTOFLASH_EWELINK_API_BASE": "https://apia.example.test"}), \
                mock.patch.object(sonoff_firmware.public_copies, "fetch", side_effect=AssertionError("not reached")):
            with self.assertRaisesRegex(sonoff_firmware.FirmwareError, "could not be fetched"):
                sonoff_firmware.official_app(ctx, kinds.SONOFF_S60, IDENTITY, layout)
        with mock.patch.dict("os.environ", {"AUTOFLASH_EWELINK_API_BASE": ""}), \
                mock.patch.object(sonoff_firmware.public_copies, "fetch", return_value=self.wrapped()), \
                mock.patch.object(sonoff_firmware.output, "log_only") as logged:
            app, where = sonoff_firmware.official_app(ctx, kinds.SONOFF_S60, IDENTITY, layout)
        self.assertEqual((app, where), (SONOFF_APP, "the public copy of Sonoff's update file (FlashDumps)"))
        # Why eWeLink's was passed over, though the next source gave it.
        logged.assert_called_once_with("    Sonoff's firmware: the file eWeLink offers the device: there is none")

    def test_a_test_can_have_it_out_of_reach(self):
        # cli.py --simulate-no-sonoff-firmware: nothing asked of eWeLink or the public copy.
        ctx = SimpleNamespace(ewelink=Account(mock.Mock(side_effect=AssertionError("not asked"))))
        with mock.patch.object(sonoff_firmware, "SIMULATE_NONE", True), \
                mock.patch.object(sonoff_firmware.public_copies, "fetch", side_effect=AssertionError("not asked")), \
                self.assertRaisesRegex(sonoff_firmware.FirmwareError, "could not be fetched"):
            sonoff_firmware.official_app(ctx, kinds.SONOFF_S60, IDENTITY, images.layout_of(kinds.SONOFF_S60))

    def test_what_a_backup_would_hold_is_written_as_one(self):
        layout = images.layout_of(kinds.SONOFF_S60)
        settings = sonoff_identity.nvs(IDENTITY, 0x10000)
        flash = sonoff_firmware.flash_of(images.sonoff_table(), settings, SONOFF_APP, layout)
        with mock.patch.object(plans, "app_length", return_value=len(SONOFF_APP)):
            first, second = plans.back_to_sonoff(flash, running=1, layout=layout)
        self.assertEqual(plans.stock_slot(flash, layout), 0)
        self.assertEqual(second.blobs["settings"][:len(settings)], settings)

    def test_settings_filling_their_partition_fit_and_a_byte_more_does_not(self):
        layout = images.layout_of(kinds.SONOFF_S60)
        room = layout.otadata - layout.nvs
        flash = sonoff_firmware.flash_of(images.sonoff_table(), b"\x5a" * room, SONOFF_APP, layout)
        self.assertEqual(flash[layout.nvs:layout.otadata], b"\x5a" * room)
        with self.assertRaisesRegex(sonoff_firmware.FirmwareError, "do not fit"):
            sonoff_firmware.flash_of(images.sonoff_table(), b"\x5a" * (room + 1), SONOFF_APP, layout)


if __name__ == "__main__":
    unittest.main()
