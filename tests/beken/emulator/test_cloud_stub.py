"""cloud_stub.py, the emulated device's eWeLink."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class CloudStubTests(unittest.TestCase):
    """The S61's stub serves a leaf for its own router's address."""

    def test_a_device_started_on_another_network_is_served_a_leaf_for_it(self):
        made = []

        def make_certs(folder, address):
            made.append(address)
            for name in ("ca.crt", "leaf.crt", "leaf.key"):
                Path(folder, name).write_text(address, encoding="utf-8")
            return b"", "", ""

        online = SimpleNamespace(_make_certs=make_certs)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"cloud_online": online}):
            sys.modules.pop("cloud_stub", None)
            import cloud_stub
            state = Path(tmp)
            cloud_stub.certificates(state, "198.18.62.1")
            cloud_stub.certificates(state, "198.18.62.1")       # kept: the same router
            leaf, _ = cloud_stub.certificates(state, "198.18.63.1")
            sys.modules.pop("cloud_stub", None)
            self.assertEqual(made, ["198.18.62.1", "198.18.63.1"])
            self.assertEqual(leaf.read_text(encoding="utf-8"), "198.18.63.1")


class StubMemoryTests(unittest.TestCase):
    def test_what_a_device_said_of_itself_is_kept_across_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(sys.modules, {"cloud_online": SimpleNamespace(_make_certs=None)}):
            sys.modules.pop("cloud_stub", None)
            import cloud_stub
            import stub_server
            path = Path(tmp, "stub-reported.json")
            first = stub_server.Stub("192.0.2.1", 443, 10)
            cloud_stub.remember(first, path)
            first.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up"})
            again = stub_server.Stub("192.0.2.1", 443, 10)
            cloud_stub.remember(again, path)
            sys.modules.pop("cloud_stub", None)
        self.assertEqual(again.account_device()["deviceid"], "1000abcdef")


class StubModelTests(unittest.TestCase):
    """A flash rebuilt on an S61's settings says it is an S61: the stub's account
    gives the model of the firmware it started on."""

    def setUp(self):
        patched = mock.patch.dict(sys.modules, {"cloud_online": SimpleNamespace(_make_certs=None)})
        patched.start()
        self.addCleanup(patched.stop)
        sys.modules.pop("cloud_stub", None)
        import cloud_stub
        self.stub = cloud_stub
        self.addCleanup(sys.modules.pop, "cloud_stub", None)

    def test_the_model_is_the_one_the_app_holds_once(self):
        self.assertEqual(self.stub.model_in(b"\0SN-BK7238-BASICR5-01\0"), "SN-BK7238-BASICR5-01")
        self.assertEqual(self.stub.model_in(b"SN-BK7238-S61-01\0SN-BK7238-S61-01"), "SN-BK7238-S61-01")
        self.assertEqual(self.stub.model_in(b"SN-BK7238-S61-01\0SN-BK7238-S41-01"), "")
        self.assertEqual(self.stub.model_in(b"no model"), "")

    def test_the_record_is_that_kinds_and_stays_when_the_firmware_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, flash = Path(tmp), Path(tmp, "flash.bin")
            flash.write_bytes(b"\xff" * 16)
            with mock.patch.object(self.stub.stock_points, "app_of_flash", return_value=b"SN-BK7238-BASICR5-01\0"):
                record = self.stub.device(flash, state)
            self.assertEqual((record["model"], record["productModel"]), ("SN-BK7238-BASICR5-01", "BASIC-1GS"))
            with mock.patch.object(self.stub.stock_points, "app_of_flash", return_value=b"OpenBeken"):
                self.assertEqual(self.stub.device(flash, state)["model"], "SN-BK7238-BASICR5-01")

    def test_with_none_to_read_it_is_an_s61(self):
        with tempfile.TemporaryDirectory() as tmp:
            flash = Path(tmp, "flash.bin")
            flash.write_bytes(b"\xff" * 16)
            with mock.patch.object(self.stub.stock_points, "app_of_flash", side_effect=ValueError):
                self.assertEqual(self.stub.device(flash, Path(tmp))["model"], "SN-BK7238-S61-01")

    def test_what_the_device_says_of_its_model_gives_way(self):
        import stub_server
        with tempfile.TemporaryDirectory() as tmp:
            stub = stub_server.Stub("192.0.2.1", 443, 10)
            self.stub.remember(stub, Path(tmp, "stub-reported.json"), "SN-BK7238-BASICR5-01")
            stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up",
                        "model": "SN-BK7238-S61-01"})
        self.assertEqual(stub.reported["1000abcdef"]["model"], "SN-BK7238-BASICR5-01")


class StubOfferTests(unittest.TestCase):
    """The stub offers the device its own Sonoff firmware, as eWeLink offers a real
    one its official file: what a readout reverts with, and a run back to Sonoff
    with no backup fetches. Made once, from the stock firmware it first started."""

    def setUp(self):
        patched = mock.patch.dict(sys.modules, {"cloud_online": SimpleNamespace(_make_certs=None)})
        patched.start()
        self.addCleanup(patched.stop)
        sys.modules.pop("cloud_stub", None)
        import cloud_stub
        self.stub = cloud_stub
        self.addCleanup(sys.modules.pop, "cloud_stub", None)

    def test_it_is_the_stock_app_in_the_kinds_fota_made_once(self):
        import fota_wrap
        with tempfile.TemporaryDirectory() as tmp:
            state, flash = Path(tmp), Path(tmp, "flash.bin")
            flash.write_bytes(b"\xff" * 16)
            with mock.patch.object(self.stub.stock_app, "stock_rbl", return_value=(b"RBL\0stock app", "1.0.2")):
                first = self.stub.offered(flash, state, "SN-BK7238-S61-01")
            # Started again on another firmware: the same offer, not made again.
            with mock.patch.object(self.stub.stock_app, "stock_rbl", side_effect=ValueError) as made:
                again = self.stub.offered(flash, state, "SN-BK7238-S61-01")
            made.assert_not_called()
        self.assertEqual(first.version, "1.0.2")
        self.assertEqual(again.data, first.data)
        ((name, version, image),) = fota_wrap.unwrap(first.data)
        self.assertEqual((name, version, image), ("FWSW-01-S61-BK7238", "1.0.2", b"RBL\0stock app"))

    def test_with_no_stock_app_it_offers_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            flash = Path(tmp, "flash.bin")
            flash.write_bytes(b"\xff" * 16)
            with mock.patch.object(self.stub.stock_app, "stock_rbl", side_effect=ValueError):
                self.assertIsNone(self.stub.offered(flash, Path(tmp), "SN-BK7238-S61-01"))

    def test_the_device_asking_as_one_behind_is_offered_it_and_given_it(self):
        import stub_server
        update = SimpleNamespace(version="1.0.2", data=b"the fota")
        stub = stub_server.Stub("192.0.2.1", 443, 10, {"model": "SN-BK7238-S61-01"}, update)
        stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up"})
        asked = json.dumps({"deviceInfoList": [{"deviceid": "1000abcdef", "model": "SN-BK7238-S61-01",
                                                "version": "0.0.1"}]}).encode()
        offers = stub.ota_query(asked)["data"]["otaInfoList"]
        self.assertEqual([offer["version"] for offer in offers], ["1.0.2"])
        self.assertEqual(stub.ota_file("deviceid=1000abcdef&ts=1&sign=0"), b"the fota")
        self.assertIsNone(stub.ota_file("deviceid=1000000000"))


if __name__ == "__main__":
    unittest.main()
