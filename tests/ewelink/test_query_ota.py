import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ewelink import query_ota
from tools.ewelink.get_device_key import EwelinkError
from tools.ewelink.query_ota import ota_endpoints, ota_identity


class QueryOtaTests(unittest.TestCase):
    def test_uses_module_model_and_reported_firmware(self):
        device = {
            "deviceid": "abc",
            "productModel": "S60TPG",
            "extra": {"model": "SN-ESP32C3-S60-01"},
            "params": {"fwVersion": "1.1.1"},
        }
        self.assertEqual(
            ota_identity(device),
            {"deviceid": "abc", "model": "SN-ESP32C3-S60-01", "version": "1.1.1"},
        )

    def test_requires_version(self):
        with self.assertRaises(EwelinkError):
            ota_identity({"deviceid": "abc", "productModel": "S60TPG"})

    def test_extracts_safe_unique_router_endpoints(self):
        ota_list = [
            {
                "binList": [
                    {"downloadUrl": "http://192.0.2.10:8088/private/one?token=secret"},
                    {"downloadUrl": "http://192.0.2.10:8088/private/two?token=secret"},
                    {"downloadUrl": "https://updates.example/private/three"},
                ]
            }
        ]

        self.assertEqual(
            ota_endpoints(ota_list),
            [("192.0.2.10", 8088), ("updates.example", 443)],
        )


class LatestOfferTests(unittest.TestCase):
    IDENTITY = {"deviceid": "10000e32ab", "model": "SN-ESP32C3-S60-01", "version": "1.2.0"}

    def test_asks_as_a_device_behind_all_with_its_own_id(self):
        offer = [{"version": "1.2.0", "binList": []}]
        with mock.patch.object(query_ota, "query_ota", return_value={"error": 0, "data": {"otaInfoList": offer}}) as asked:
            self.assertEqual(query_ota.latest_offer("eu", "token", self.IDENTITY), {"offers": offer})
        asked.assert_called_once_with("eu", "token", dict(self.IDENTITY, version="0.0.1"))
        self.assertEqual(self.IDENTITY["version"], "1.2.0")   # what the run reports stays the device's

    def test_a_failure_is_only_noted(self):
        for problem, name in ((EwelinkError("OTA metadata query failed"), "EwelinkError"), (TimeoutError(), "TimeoutError")):
            with mock.patch.object(query_ota, "query_ota", side_effect=problem):
                self.assertEqual(query_ota.latest_offer("eu", "token", self.IDENTITY), {"problem": name})
        with mock.patch.object(query_ota, "query_ota", return_value={"error": 0}):
            self.assertEqual(query_ota.latest_offer("eu", "token", self.IDENTITY), {"problem": "KeyError"})

    def saved(self, *flags, offered=(), env=None, refused=()):
        """What main() saves, and the versions it asked with; self.signed_in
        and self.tokens, what it signed in with and asked with. `refused`:
        tokens eWeLink has ended."""
        device = {"deviceid": "10000e32ab", "devicekey": "k", "extra": {"model": "SN-ESP32C3-S60-01"},
                  "params": {"fwVersion": "1.2.0"}}
        asked = []

        self.tokens = []

        def devices(_region, token):
            self.tokens.append(token)
            if token in refused:
                raise query_ota.SessionEnded("eWeLink ended the sign-in")
            return [device]

        def answer(_region, _token, identity):
            asked.append(identity["version"])
            found = [{"version": "1.2.0"}] if identity["version"] == "0.0.1" else list(offered)
            return {"error": 0, "data": {"otaInfoList": found}}

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "meta.json"
            arguments = ["query_ota", "--email", "a@b.c", "--device-id", "10000e32ab", "--output", str(output), *flags]
            with mock.patch.object(sys, "argv", arguments), \
                    mock.patch.dict(os.environ, {"EWELINK_PASSWORD": "p", **(env or {})}), \
                    mock.patch.object(query_ota, "login_session", return_value=("eu", {"at": "token"})) as signed, \
                    mock.patch.object(query_ota, "get_devices", devices), \
                    mock.patch.object(query_ota, "query_ota", side_effect=answer), \
                    mock.patch("builtins.print"):
                self.assertEqual(query_ota.main(), 0)
            self.signed_in = signed.call_count
            return json.loads(output.read_text(encoding="utf-8")), asked

    def test_a_run_asks_for_it_beside_the_query_it_reports(self):
        saved, asked = self.saved("--latest-offer")
        self.assertEqual(asked, ["1.2.0", "0.0.1"])
        self.assertEqual(saved["response"]["data"]["otaInfoList"], [])   # up to date, as it is
        self.assertEqual(saved["query"]["version"], "1.2.0")
        self.assertEqual(saved["latest_offer"], {"offers": [{"version": "1.2.0"}]})

    def test_a_real_offer_needs_no_second_call(self):
        saved, asked = self.saved("--latest-offer", offered=[{"version": "1.3.0"}])
        self.assertEqual(asked, ["1.2.0"])
        self.assertEqual(saved["latest_offer"], {"offers": [{"version": "1.3.0"}]})

    def test_a_sign_in_the_run_hands_over_is_used_and_none_made(self):
        # One of its own would end the run's (get_device_key.Account).
        session = {"EWELINK_SESSION": '{"region": "us", "at": "run-token", "user": {"apikey": "a"}}'}
        self.saved(env=session)
        self.assertEqual((self.signed_in, self.tokens), (0, ["run-token"]))

    def test_one_eWeLink_has_ended_is_replaced_by_its_own(self):
        session = {"EWELINK_SESSION": '{"region": "eu", "at": "run-token", "user": {"apikey": "a"}}'}
        self.saved(env=session, refused=("run-token",))
        self.assertEqual((self.signed_in, self.tokens), (1, ["run-token", "token"]))

    def test_without_one_it_signs_in_itself(self):
        self.saved(env={"EWELINK_SESSION": "not json"})
        self.assertEqual((self.signed_in, self.tokens), (1, ["token"]))

    def test_other_callers_ask_once_as_before(self):
        saved, asked = self.saved()
        self.assertEqual(asked, ["1.2.0"])
        self.assertNotIn("latest_offer", saved)


if __name__ == "__main__":
    unittest.main()
