"""A device's Sonoff identity: taken from its settings, kept in its own sector,
and written into the settings of the next firmware - never its Wi-Fi."""
import json
import unittest

from tools.espressif import nvs_image, sonoff_identity
from tools.espressif.inspect_stock_nvs import parse

FRAME = {"init": "true", "uiid": 190, "deviceid": "10000e32ab", "apikey": "00000000-1111-2222-3333-444444444444",
         "model": "SN-ESP32C3-S60-01", "sha256": "ab" * 32, "ssid": "Home-2G", "passwd": "secret",
         "dis_server": "disp.example.test", "dis_port": 443}


def sonoff_nvs(frame=FRAME):
    """Settings as Sonoff's firmware leaves them: the identity among much else."""
    items = [nvs_image.Item("nvs.net80211", "sta.ssid", "blob", b"\x07\0\0\0Home-2G".ljust(36, b"\0")),
             nvs_image.Item("frame_params", "content", "blob", json.dumps(frame).encode()),
             nvs_image.Item("data_backups", "content", "blob", b'{"init":"true","deviceid":"10000e32ab"}'),
             nvs_image.Item("productId", "productId", "blob", b"24440700000000\r\n"),
             nvs_image.Item("histEnergy", "day", "blob", b"\x01" * 400),
             nvs_image.Item("user_usual", "content", "blob", b'{"init":"true","chl0state":2}')]
    return nvs_image.build(items, 0x10000)


def read(partition):
    names, values = parse(partition)
    return {(names[space], key): value.data[:value.size] for (space, key), value in values.items()}


class IdentityTests(unittest.TestCase):
    def test_the_identity_is_a_few_entries_and_no_wifi(self):
        identity = sonoff_identity.from_nvs(sonoff_nvs())
        self.assertEqual([(i.namespace, i.key) for i in identity.items],
                         [("frame_params", "content"), ("data_backups", "content"), ("productId", "productId")])
        self.assertNotIn("ssid", identity.frame())
        self.assertNotIn("passwd", identity.frame())
        self.assertNotIn(b"Home-2G", sonoff_identity.to_sector(identity))
        self.assertNotIn(b"secret", sonoff_identity.to_sector(identity))
        self.assertEqual((identity.deviceid, identity.model), (FRAME["deviceid"], FRAME["model"]))
        self.assertEqual(identity.frame()["dis_server"], "disp.example.test")

    def test_settings_without_one_have_none(self):
        self.assertIsNone(sonoff_identity.from_nvs(b"\xff" * 0x10000))
        self.assertIsNone(sonoff_identity.from_nvs(sonoff_nvs({"init": "true"})))

    def test_the_sector_says_what_it_is(self):
        identity = sonoff_identity.from_nvs(sonoff_nvs())
        sector = sonoff_identity.to_sector(identity)
        self.assertEqual(len(sector), sonoff_identity.SECTOR_SIZE)
        self.assertTrue(sector.startswith(sonoff_identity.MAGIC))
        self.assertEqual(sonoff_identity.from_sector(sector), identity)

    def test_a_sector_not_whole_and_unchanged_is_not_taken(self):
        sector = bytearray(sonoff_identity.to_sector(sonoff_identity.from_nvs(sonoff_nvs())))
        self.assertIsNone(sonoff_identity.from_sector(b"\xff" * 4096))
        changed = bytearray(sector)
        changed[sonoff_identity.HEADER.size + 5] ^= 1
        self.assertIsNone(sonoff_identity.from_sector(bytes(changed)))
        other_version = bytearray(sector)
        other_version[8] = 2
        self.assertIsNone(sonoff_identity.from_sector(bytes(other_version)))
        self.assertIsNone(sonoff_identity.from_sector(bytes(sector[:30])))

    def test_written_back_it_is_what_sonoffs_firmware_kept(self):
        identity = sonoff_identity.from_nvs(sonoff_nvs())
        written = read(sonoff_identity.nvs(identity, 0x10000))
        self.assertEqual(set(written), {(i.namespace, i.key) for i in identity.items})
        self.assertEqual(written[("productId", "productId")], b"24440700000000\r\n")
        # With the Wi-Fi Sonoff's firmware is to join, as it keeps it.
        frame = json.loads(read(sonoff_identity.nvs(identity, 0x10000, ("Other", "pw")))[("frame_params", "content")])
        self.assertEqual((frame["ssid"], frame["passwd"], frame["deviceid"]), ("Other", "pw", FRAME["deviceid"]))

    def test_rebuilt_from_the_account_record(self):
        record = {"deviceid": "10000e32ab", "devicekey": "00000000-1111-2222-3333-444444444444",
                  "extra": {"model": "SN-ESP32C3-S60-01", "uiid": 190}}
        identity = sonoff_identity.from_account(record, ("eu-dispd.coolkit.cc", 443), encrypted=False)
        frame = identity.frame()
        self.assertEqual((frame["deviceid"], frame["apikey"], frame["model"], frame["uiid"]),
                         ("10000e32ab", record["devicekey"], "SN-ESP32C3-S60-01", 190))
        self.assertEqual((frame["dis_server"], frame["dis_port"]), ("eu-dispd.coolkit.cc", 443))
        self.assertNotIn("sha256", frame)
        self.assertNotIn("encrypt_key", frame)
        backups = json.loads(identity.items[1].data)
        self.assertEqual((backups["deviceid"], backups["apikey"]), ("10000e32ab", record["devicekey"]))
        # A classic ESP32 device's firmware also keeps its key as encrypt_key.
        frame = sonoff_identity.from_account(record, ("eu-dispd.coolkit.cc", 443), encrypted=True).frame()
        self.assertEqual((frame["encrypt_key"], frame["is_encrypt"]), (record["devicekey"], True))
        with self.assertRaises(sonoff_identity.IdentityError):
            sonoff_identity.from_account({"deviceid": "x"}, ("h", 443), encrypted=False)


if __name__ == "__main__":
    unittest.main()
