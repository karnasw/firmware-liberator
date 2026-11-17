"""stage_apply.py, the apply half of a conversion in the emulator."""

import unittest

import ef40
import stage_apply
from tests.beken.test_ef40 import kv40_store
from tests.beken.test_rbl import rbl_header


class StageApplyTests(unittest.TestCase):
    """stage_apply.py puts an rt_ota image where the stock bootloader applies
    from, and test credentials where the installed firmware reads them."""

    def flash(self):
        flash = bytearray(b"\x00" * 0x286000 + b"\xa5" * (0x3e0000 - 0x286000) + b"\xff" * 0x20000)
        flash[0x3e0000:0x3e1000] = kv40_store([("factory", b"x")])
        return flash

    def test_the_slot_is_erased_and_the_image_laid_at_its_start(self):
        image = rbl_header() + b"body" * 100
        flash = stage_apply.stage(self.flash(), image)
        start, end = stage_apply.DOWNLOAD_SLOT
        self.assertEqual(flash[start:start + len(image)], image)
        self.assertEqual(set(flash[start + len(image):end]), {0xff})
        self.assertEqual(flash[:start], b"\x00" * start)   # the app and bootloader untouched

    def test_credentials_join_the_store_and_the_slot_can_be_left(self):
        flash = stage_apply.stage(self.flash(), None, {"wifi-ssid": b"Net", "wifi-pass": b"secret12"})
        self.assertEqual(ef40.parse_store(flash)["wifi-ssid"], b"Net")
        self.assertEqual(set(flash[0x286000:0x3e0000]), {0xa5})

    def test_only_an_rbl_that_fits_is_staged(self):
        with self.assertRaises(ValueError):
            stage_apply.stage(self.flash(), b"not an image")
        with self.assertRaises(ValueError):
            stage_apply.stage(self.flash(), rbl_header() + b"\0" * (0x3e0000 - 0x286000))


class StagedTests(unittest.TestCase):
    def test_an_image_is_staged_only_byte_for_byte_at_the_slot(self):
        image = rbl_header() + b"body" * 100
        flash = stage_apply.stage(bytearray(b"\xff" * 0x400000), image)
        self.assertTrue(stage_apply.staged(flash, image))
        flash[0x286000 + 200] ^= 1
        self.assertFalse(stage_apply.staged(flash, image))
        self.assertFalse(stage_apply.staged(bytes(0x400000), image))


if __name__ == "__main__":
    unittest.main()
