import io
import os
import re
import struct
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tools.autoflash import kinds
from tools.beken import fota_wrap
from tools.espressif.emulator import public_dump
from tools.ewelink import firmware_name
from tools.ewelink.firmware_name import chip_of, learn, main_name, name_listed, name_offered, names_in_file

DEVICE = "10000e32ab"  # made up, as long as a real one

# Only names of public files, as listed, no part of any firmware.
PUBLIC_FILES = [
    "EARU_EAKCB_(CK-BL602-W102SW18-01)_BL602_v1.2.1_user1.bin",
    "SONOFF_S60TPG-UK_(FWSW-E32S60-S60-ESP32C3FN4)_ESP32-C3_v1.2.0_user1.bin",
    "SONOFF_S60TPG-UK_(FWSW-E32S60-S60-ESP32C3FN4)_ESP32-C3_v1.2.0_user2.bin",
    "SONOFF_S60ZBTPF_TLSR8656_v2.1.3_user1.bin",
    "SONOFF_S61s_(FWSW-01-S61-BK7238)_BK7238_v1.1.0_user1.bin",
    "SONOFF_POWR3_(FWSW_01P76OYO)_ESP8266_v1.2.1_user1.bin",
    "SONOFF_POWR316D_(FWSW-E32POWP-POWPRO-ESP32D0WDV3)_ESP32_v1.2.0_user1.bin",
    "SONOFF_MINIR4M_(FWSW-E32R4-MINIR4M-ESP32C3)_ESP32-C3_v1.2.0_user1.bin",
    "SONOFF_MINIR4_(FWSW-E32R4-MINIR4-ESP32D0WDR2V3)_ESP32_v1.2.0_user1.bin",
    "SONOFF_NSPanel_(FWPNL-E32NSP-NSPANEL-ESP32D0WDV3+FWPNL-E32NSP-NSPANEL-HMIX120)_ESP32_v1.6.0_user1.bin",
    "SONOFF_SPM-Main_(METERMCU-GD32F103RET6+METERMAIN-ESP32D0WDV3)_ESP32_v1.3.0_user1.bin",
    "SONOFF_Micro_(CKA-DM4-GL)_(FWSWES-SV04MATE)_SV6166F_v3.7.1_user1.bin",
    "SONOFF_L3-5M_(SN-OPL1200-L3RGB)_OPL1200_v1000.2.1060_user1.bin",
    "SONOFF_M5-1C-3C_(FWSWES-E32T5-T5-ESP32D0WDV3)_ESP32_v1.3.0_user1.bin",
    "SONOFF_M5-3C-80W_(FWSW-E32M5-M5M-ESP32DOWDR2V3)_ESP32_v1.2.0_user1.bin",
    "SONOFF_T5-3C-86_(FWSWES-T5TOUCH-T5-CA51F3+FWSWES-T5TOUCH-T5-ESP32D0WDV3)_ESP32_v9.9.2_user1.bin",
    "SONOFF_L3-5M_(SN-OPL1200-L3RGBX)_OPL1200_v1000.2.1062_user1.bin",
]


def wrapper(*names: str) -> bytes:
    """A file's first bytes, laid out as the public update files are."""
    head = bytes([3, len(names)]) + b"1.2.0".ljust(22, b"\0")
    for name in names:
        head += name.encode().ljust(32, b"\0")[:32] + bytes(44)
    return head + b"\xe9" + bytes(40)


def fota(body: bytes, extra: int) -> bytes:
    """A Beken file's first bytes: its outer header, `extra` bytes more of it, the wrapper."""
    return (struct.pack("<I", firmware_name.FOTA_MAGIC) + bytes(8) + struct.pack("<H", extra) + bytes(2)
            + bytes(extra) + body)


class NamesInFileTests(unittest.TestCase):
    def test_an_espressif_wrapper(self):
        self.assertEqual(names_in_file(wrapper("FWSW-E32S60-S60-ESP32C3FN4")), ["FWSW-E32S60-S60-ESP32C3FN4"])

    def test_a_name_that_fills_its_field_ends_there(self):
        name = "FWSWES-E32DR3-DUALR3-ESP32D0WDV3"
        self.assertEqual(len(name), 32)
        self.assertEqual(names_in_file(wrapper(name)), [name])

    def test_several_names(self):
        self.assertEqual(names_in_file(wrapper("METERMCU-GD32F103RET6", "METERMAIN-ESP32D0WDV3")),
                         ["METERMCU-GD32F103RET6", "METERMAIN-ESP32D0WDV3"])

    def test_a_beken_file_has_a_longer_outer_header_than_ours(self):
        self.assertEqual(names_in_file(fota(wrapper("FWSW-01-S61-BK7238"), 63)), ["FWSW-01-S61-BK7238"])

    def test_the_file_this_tool_wraps_for_beken(self):
        rbl = b"RBL\0" + bytes(0x18) + b"1.0.0".ljust(24, b"\0") + bytes(200)
        self.assertEqual(names_in_file(fota_wrap.wrap(rbl)), [fota_wrap.STAGING_NAME])

    def test_no_wrapper_no_name(self):
        self.assertEqual(names_in_file(b"\xea\x04\x00\x01\x04\x00\x10@" + bytes(range(200))), [])

    def test_too_short(self):
        self.assertEqual(names_in_file(b""), [])
        self.assertEqual(names_in_file(b"\x03\x05"), [])
        self.assertEqual(names_in_file(struct.pack("<I", firmware_name.FOTA_MAGIC) + bytes(8) + b"\xff\xff" + bytes(2)), [])


class ChipTests(unittest.TestCase):
    def test_what_a_name_implies(self):
        for name, chip in (("FWSW-E32S60-S60-ESP32C3FN4", "ESP32C3"), ("FWSW-E32R4-BASICR4-ESP32C3", "ESP32C3"),
                           ("FWSW-E322GS-SWITCHES-ESP32C3X", "ESP32C3"),
                           ("FWSWES-E32DR3-DUALR3-ESP32D0WDV3", "ESP32D0WD"),
                           ("FWSW-E32R4-MINIR4-ESP32D0WDR2V3", "ESP32D0WD"),
                           ("FWSW-E32M5-M5M-ESP32DOWDR2V3", "ESP32D0WD"),  # with a letter O, as it is published
                           ("FWSW-01-S61-BK7238", "BK7238"), ("FWSW-BLS40-S40-BL602L", "BL602"),
                           ("FWSW-0185", ""), ("FWPNL-E32NSP-NSPANEL-HMIX120", ""), ("METERMCU-GD32F103RET6", "")):
            self.assertEqual(chip_of(name), chip, name)

    def test_every_kinds_name_implies_its_chip(self):
        expected = {"esp32c3": "ESP32C3", "esp32": "ESP32D0WD", "bk7238": "BK7238"}
        for kind in kinds.EVERY:
            self.assertEqual(chip_of(kind.wrapper_model), expected[kind.chip], kind.key)

    def test_the_main_chips_name_wherever_it_is_listed(self):
        self.assertEqual(main_name(["METERMCU-GD32F103RET6", "METERMAIN-ESP32D0WDV3"]), "METERMAIN-ESP32D0WDV3")
        self.assertEqual(main_name(["FWPNL-E32NSP-NSPANEL-ESP32D0WDV3", "FWPNL-E32NSP-NSPANEL-HMIX120"]),
                         "FWPNL-E32NSP-NSPANEL-ESP32D0WDV3")
        self.assertEqual(main_name(["FWSW-0185"]), "FWSW-0185")
        self.assertEqual(main_name([]), "")


class OfferTests(unittest.TestCase):
    OFFER = [{"version": "1.2.0", "binList": [{"name": "user2.bin", "downloadUrl": "http://192.0.2.9:8088/b/user2.bin"},
                                              {"name": "user1.bin", "downloadUrl": "http://192.0.2.9:8088/b/user1.bin"}]}]

    def test_reads_user1_alone_with_the_devices_own_id(self):
        read = mock.Mock(return_value=wrapper("FWSW-E32S60-S60-ESP32C3FN4"))
        self.assertEqual(name_offered(self.OFFER, DEVICE, read), "FWSW-E32S60-S60-ESP32C3FN4")
        read.assert_called_once_with("http://192.0.2.9:8088/b/user1.bin", DEVICE)

    def test_no_id_to_ask_with_reads_nothing(self):
        read = mock.Mock()
        self.assertEqual(name_offered(self.OFFER, "", read), "")
        read.assert_not_called()

    def test_nothing_offered(self):
        read = mock.Mock()
        self.assertEqual(name_offered([], DEVICE, read), "")
        self.assertEqual(name_offered([{"version": "1"}], DEVICE, read), "")
        read.assert_not_called()

    def test_only_web_links_are_fetched(self):
        read = mock.Mock()
        self.assertEqual(name_offered([{"binList": [{"downloadUrl": "file:///etc/passwd"}, {}]}], DEVICE, read), "")
        read.assert_not_called()

    def test_adds_to_a_link_that_already_has_a_query(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = b"head"
        with mock.patch.object(firmware_name, "urlopen", return_value=reply) as opened:
            firmware_name.read_head("http://192.0.2.9:8088/a?x=1", DEVICE)
        self.assertEqual(opened.call_args.args[0].full_url, f"http://192.0.2.9:8088/a?x=1&deviceid={DEVICE}&ts=1&sign=0")


class SonoffsServer(BaseHTTPRequestHandler):
    """Answers as Sonoff's download server does: an empty 400 unless asked as a
    device asks, and a range when asked for one."""

    FILE = wrapper("FWSW-E32S60-S60-ESP32C3FN4") + bytes(100000)

    def do_GET(self):
        self.server.asked.append((self.path, self.headers.get("Range")))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if query.get("deviceid") != [DEVICE] or "ts" not in query or "sign" not in query:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        found = re.fullmatch(r"bytes=0-(\d+)", self.headers.get("Range") or "")
        body = self.FILE[:int(found.group(1)) + 1] if found else self.FILE
        self.send_response(206 if found else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ReadingAnOfferTests(unittest.TestCase):
    """The file's first bytes, from a server here that answers as Sonoff's does."""

    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SonoffsServer)
        self.server.asked = []
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/b/user1.1024.new.2.bin"
        environment = mock.patch.dict(os.environ, {"AUTOFLASH_EWELINK_API_BASE": "https://stub.test", "no_proxy": "*"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_link_as_given_is_refused_and_the_device_must_ask_with_its_id(self):
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(self.url, timeout=5)
        refused.exception.close()
        self.assertEqual(refused.exception.code, 400)
        self.assertEqual(firmware_name.read_head(self.url, DEVICE)[:2], b"\x03\x01")

    def test_learns_the_name_asking_only_for_the_first_bytes(self):
        offer = [{"version": "1.2.0", "binList": [{"name": "user1.bin", "downloadUrl": self.url}]}]
        learned, problems = learn(offer, DEVICE, "", "")
        self.assertEqual(learned, firmware_name.Learned("FWSW-E32S60-S60-ESP32C3FN4", "ESP32C3", "eWeLink's update file"))
        self.assertEqual(problems, [])
        self.assertEqual(self.server.asked, [(f"/b/user1.1024.new.2.bin?deviceid={DEVICE}&ts=1&sign=0",
                                              f"bytes=0-{firmware_name.HEAD_SIZE - 1}")])

    def test_another_id_gets_a_400_which_stops_nothing_and_shows_no_id_or_link(self):
        offer = [{"binList": [{"name": "user1.bin", "downloadUrl": self.url}]}]
        learned, problems = learn(offer, "10000abcde", "", "")
        self.assertIsNone(learned)
        self.assertEqual(problems, ["eWeLink's update file could not be read (HTTPError)"])

    def test_reads_no_more_than_the_header_of_a_server_that_ignores_the_range(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = b"head"
        with mock.patch.object(firmware_name, "urlopen", return_value=reply):
            self.assertEqual(firmware_name.read_head(self.url, DEVICE), b"head")
        reply.__enter__.return_value.read.assert_called_once_with(firmware_name.HEAD_SIZE)


class PublicListTests(unittest.TestCase):
    def test_the_commit_is_the_one_public_dump_fetches_from(self):
        self.assertEqual(firmware_name.PUBLIC_COMMIT, public_dump.COMMIT)

    def test_by_product_model(self):
        self.assertEqual(name_listed(PUBLIC_FILES, "S60TPG", ""), "FWSW-E32S60-S60-ESP32C3FN4")
        self.assertEqual(name_listed(PUBLIC_FILES, "POWR316D", ""), "FWSW-E32POWP-POWPRO-ESP32D0WDV3")
        self.assertEqual(name_listed(PUBLIC_FILES, "NSPANEL", ""), "FWPNL-E32NSP-NSPANEL-ESP32D0WDV3")

    def test_a_model_is_not_one_it_only_starts_with(self):
        self.assertEqual(name_listed(["SONOFF_POWR3_(FWSW_01P76OYO)_ESP8266_v1.2.1_user1.bin"], "POWR316D", ""), "")
        self.assertEqual(name_listed(PUBLIC_FILES, "MINIR4", ""), "FWSW-E32R4-MINIR4-ESP32D0WDR2V3")

    def test_by_the_model_the_stock_firmware_names_when_the_product_is_not_listed(self):
        self.assertEqual(name_listed(PUBLIC_FILES, "S60TPF", "SN-ESP32C3-S60-01"), "FWSW-E32S60-S60-ESP32C3FN4")
        self.assertEqual(name_listed(PUBLIC_FILES, "S61STPG", "SN-BK7238-S61-01"), "FWSW-01-S61-BK7238")

    def test_a_basicr4_by_the_model_its_official_file_names(self):
        # Its kind's stock model, read from eWeLink's official file, finds the
        # name the installer is wrapped for, with no product model to go by.
        kind = kinds.SONOFF_BASICR4
        official = "SONOFF_BASICR4_(FWSW-E32R4-BASICR4-ESP32C3)_ESP32-C3_v1.1.0_user1.bin"
        self.assertEqual(name_listed([official], "", kind.stock_model), kind.wrapper_model)

    def test_the_stock_model_must_agree_on_the_chip(self):
        self.assertEqual(name_listed(PUBLIC_FILES, "", "SN-BK7238-S60-01"), "")

    def test_an_m5_by_the_file_for_its_run_of_gangs(self):
        for gangs in (1, 2, 3):
            kind = kinds.find(f"sonoff-m5-{gangs}c")
            for product in (f"M5-{gangs}C-86", f"M5-{gangs}C-120"):
                with self.subTest(product=product):
                    self.assertTrue(kind.product.fullmatch(product))
                    self.assertEqual(name_listed(PUBLIC_FILES, product, kind.stock_model), kind.wrapper_model)

    def test_a_run_of_gangs_is_only_those_gangs_of_that_device(self):
        self.assertEqual(name_listed(PUBLIC_FILES, "M5-4C-86", ""), "")
        self.assertEqual(name_listed(PUBLIC_FILES, "T5-2C-86", ""), "")
        self.assertEqual(name_listed(PUBLIC_FILES, "M5-3C-80W", ""), "FWSW-E32M5-M5M-ESP32DOWDR2V3")

    def test_two_names_for_one_model_give_none(self):
        self.assertEqual(name_listed(PUBLIC_FILES, "L3-5M", ""), "")

    def test_an_unlisted_device_or_nothing_to_match(self):
        self.assertEqual(name_listed(PUBLIC_FILES, "THR320D", "SN-ESP32D0-THR3-01"), "")
        self.assertEqual(name_listed(PUBLIC_FILES, "", ""), "")

    def test_lists_the_file_names_of_the_answer(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value = io.BytesIO(b'{"tree": [{"path": "a.bin"}, {"path": "b.bin"}]}')
        with mock.patch.object(firmware_name, "urlopen", return_value=reply):
            self.assertEqual(firmware_name.public_files(), ["a.bin", "b.bin"])


class LearnTests(unittest.TestCase):
    OFFER = [{"binList": [{"downloadUrl": "https://updates.example/a?token=secret-link"}]}]

    def setUp(self):
        patched = mock.patch.dict(os.environ)
        patched.start()
        self.addCleanup(patched.stop)
        os.environ.pop("AUTOFLASH_EWELINK_API_BASE", None)

    def test_the_offer_comes_first_and_gives_the_chip(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = wrapper("FWSW-E32S60-S60-ESP32C3FN4")
        with mock.patch.object(firmware_name, "urlopen", return_value=reply), \
                mock.patch.object(firmware_name, "public_files") as listed:
            learned, problems = learn(self.OFFER, DEVICE, "S60TPG", "")
        self.assertEqual(learned, firmware_name.Learned("FWSW-E32S60-S60-ESP32C3FN4", "ESP32C3", "eWeLink's update file"))
        self.assertEqual(problems, [])
        listed.assert_not_called()

    def test_nothing_offered_goes_to_the_public_list(self):
        with mock.patch.object(firmware_name, "public_files", return_value=PUBLIC_FILES):
            learned, problems = learn([], DEVICE, "S60TPG", "SN-ESP32C3-S60-01")
        self.assertEqual((learned.name, learned.chip), ("FWSW-E32S60-S60-ESP32C3FN4", "ESP32C3"))
        self.assertIn("public file list", learned.source)
        self.assertEqual(problems, [])

    def test_a_failure_never_raises_nor_shows_a_link(self):
        with mock.patch.object(firmware_name, "urlopen",
                               side_effect=urllib.error.URLError("https://updates.example/a?token=secret-link")):
            learned, problems = learn(self.OFFER, DEVICE, "S60TPG", "")
        self.assertIsNone(learned)
        self.assertEqual(len(problems), 2)
        self.assertNotIn("secret-link", " ".join(problems))
        self.assertNotIn(DEVICE, " ".join(problems))

    def test_a_malformed_answer_never_raises(self):
        with mock.patch.object(firmware_name, "public_files", return_value=PUBLIC_FILES):
            learned, problems = learn([{"binList": "nonsense"}], DEVICE, "S60TPG", "")
        self.assertEqual(learned.name, "FWSW-E32S60-S60-ESP32C3FN4")
        self.assertEqual(len(problems), 1)

    def test_a_run_on_the_stub_asks_nothing_outside(self):
        os.environ["AUTOFLASH_EWELINK_API_BASE"] = "https://apia.autoflash-stub.test"
        with mock.patch.object(firmware_name, "public_files") as listed:
            self.assertEqual(learn([], DEVICE, "S60TPG", ""), (None, []))
        listed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
