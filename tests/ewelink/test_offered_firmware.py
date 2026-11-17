"""The firmware eWeLink offers a device, fetched whole as the device asks for
it, for its readout: never more than an update file, never twice, never
stopping the readout."""
import hashlib
import unittest

from tools.ewelink import offered_firmware

OFFERS = [{"version": "1.1.0", "binList": [
    {"name": "user1.bin", "downloadUrl": "https://example.com/fw/user1.bin?x=1"},
    {"name": "user2.bin", "downloadUrl": "https://example.com/fw/user2.bin"},
    {"name": "local.bin", "downloadUrl": "file:///etc/passwd"},
]}]


class FetchTests(unittest.TestCase):
    def test_each_file_is_asked_for_as_the_device_asks(self):
        asked = []

        def get(url, deviceid):
            asked.append(offered_firmware.as_device(url, deviceid))
            return url.encode()

        found, problems = offered_firmware.fetch(OFFERS, "1000abcdef", get)
        self.assertEqual([(item.name, item.version) for item in found], [("user1.bin", "1.1.0"), ("user2.bin", "1.1.0")])
        self.assertEqual(problems, [])
        self.assertEqual(asked[0], "https://example.com/fw/user1.bin?x=1&deviceid=1000abcdef&ts=1&sign=0")

    def test_a_file_not_matching_its_digest_is_left_out(self):
        offers = [{"version": "1.1.0", "binList": [
            {"name": "user1.bin", "downloadUrl": "https://example.com/1", "digest": hashlib.sha256(b"one").hexdigest()},
            {"name": "user2.bin", "downloadUrl": "https://example.com/2", "digest": hashlib.sha256(b"two").hexdigest()},
        ]}]
        found, problems = offered_firmware.fetch(offers, "1000abcdef", lambda url, deviceid: b"one",
                                                 wait=lambda seconds: None)
        self.assertEqual([item.name for item in found], ["user1.bin"])
        self.assertEqual(problems, ["user2.bin: not the file its digest names, 3 tries"])

    def test_a_file_that_does_not_come_whole_is_tried_again_after_a_pause(self):
        tries, pauses = iter([OSError("reset"), b"cut", b"whole"]), []

        def get(url, deviceid):
            answer = next(tries)
            if isinstance(answer, Exception):
                raise answer
            return answer
        offers = [{"version": "1.1.0", "binList": [{"name": "user1.bin", "downloadUrl": "https://example.com/1",
                                                    "digest": hashlib.sha256(b"whole").hexdigest()}]}]
        found, problems = offered_firmware.fetch(offers, "1000abcdef", get, wait=pauses.append)
        self.assertEqual([item.data for item in found], [b"whole"])
        self.assertEqual(problems, [])
        self.assertEqual(pauses, [offered_firmware.RETRY_PAUSE] * 2)

    def test_the_same_bytes_twice_are_kept_once(self):
        found, _ = offered_firmware.fetch(OFFERS, "1000abcdef", lambda url, deviceid: b"same")
        self.assertEqual(len(found), 1)

    def test_a_failure_is_noted_by_its_type_alone(self):
        def get(url, deviceid):
            raise OSError(f"could not fetch {url}")

        found, problems = offered_firmware.fetch(OFFERS, "1000abcdef", get, wait=lambda seconds: None)
        self.assertEqual(found, [])
        self.assertEqual(problems, ["user1.bin: OSError, 3 tries", "user2.bin: OSError, 3 tries"])
        self.assertNotIn("example.com", " ".join(problems))

    def test_a_file_held_already_is_not_fetched_by_the_digest_ewelink_gives(self):
        offers = [{"version": "1.1.0", "binList": [{**OFFERS[0]["binList"][0], "digest": "A" * 64},
                                                   OFFERS[0]["binList"][1]]}]
        asked = []
        found, _ = offered_firmware.fetch(offers, "1000abcdef", lambda url, deviceid: asked.append(url) or url.encode(),
                                          skip=frozenset({"a" * 64}))
        self.assertEqual([item.name for item in found], ["user2.bin"])
        self.assertEqual(len(asked), 1)

    def test_nothing_is_asked_without_the_devices_id(self):
        self.assertEqual(offered_firmware.fetch(OFFERS, "", lambda url, deviceid: b"x"),
                         ([], ["no device id to ask with"]))


if __name__ == "__main__":
    unittest.main()
