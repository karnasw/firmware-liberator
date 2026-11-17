import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.ewelink import get_device_key
from tools.ewelink.get_device_key import (
    APP_SECRET,
    EwelinkError,
    select_device,
    signed_login_body,
    target_id_from_mdns,
    write_secret,
)


class TurnTests(unittest.TestCase):
    def test_a_sign_in_waits_for_the_suite_to_give_the_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            turn = Path(tmp) / "captures" / "cloud-turn"
            sleeps = []

            def sleep(_seconds):
                sleeps.append(turn.exists())
                if len(sleeps) == 3:
                    turn.unlink()

            def api_json(*_args, **_kwargs):
                self.assertFalse(turn.exists(), "signed in before its turn")
                return {"error": 0, "data": {"at": "t"}}

            with mock.patch.object(get_device_key, "_TURN_FILE", str(turn)), \
                    mock.patch.object(get_device_key.time, "sleep", sleep), \
                    mock.patch.object(get_device_key, "api_json", api_json):
                get_device_key.login_session("owner@example.test", "pw", "+44", "eu")
            self.assertEqual(sleeps, [True, True, True])

    def test_a_normal_run_does_not_wait(self):
        with mock.patch.object(get_device_key, "_TURN_FILE", ""), \
                mock.patch.object(get_device_key.time, "sleep", side_effect=AssertionError), \
                mock.patch.object(get_device_key, "api_json", return_value={"error": 0, "data": {"at": "t"}}):
            self.assertEqual(get_device_key.login_session("owner@example.test", "pw", "+44", "eu")[0], "eu")


class AccountTests(unittest.TestCase):
    def test_one_sign_in_serves_every_use(self):
        sign_in = mock.Mock(return_value=("eu", {"at": "t"}))
        account = get_device_key.Account(sign_in)
        self.assertEqual([account.use(lambda region, session: session["at"]) for _ in range(3)], ["t"] * 3)
        sign_in.assert_called_once()

    def test_a_sign_in_ended_twice_running_is_given_up(self):
        sign_in = mock.Mock(return_value=("eu", {"at": "t"}))
        account = get_device_key.Account(sign_in)
        ended = mock.Mock(side_effect=get_device_key.SessionEnded("ended"))
        with self.assertRaises(get_device_key.SessionEnded):
            account.use(ended)
        self.assertEqual((sign_in.call_count, ended.call_count), (2, 2))

    def test_ended_signs_in_again_next_time(self):
        sign_in = mock.Mock(side_effect=[("eu", {"at": "a"}), ("us", {"at": "b"})])
        account = get_device_key.Account(sign_in)
        account.session()
        account.end()
        self.assertEqual(account.session(), ("us", {"at": "b"}))

    def test_a_sign_in_handed_to_a_helper_is_what_it_uses(self):
        session = {"at": "t", "rt": "refresh", "user": {"apikey": "a", "email": "owner@example.test"}}
        given = get_device_key.session_env("us", session)
        self.assertNotIn("refresh", given)          # only what a helper uses
        self.assertNotIn("owner@example.test", given)
        with mock.patch.dict(os.environ, {get_device_key.SESSION_ENV: given}):
            self.assertEqual(get_device_key.given_session(), ("us", {"at": "t", "user": {"apikey": "a"}}))
        for broken in ("", "null", "[]", "{}", '{"region": "mars", "at": "t"}', '{"region": "eu", "at": ""}'):
            with mock.patch.dict(os.environ, {get_device_key.SESSION_ENV: broken}):
                self.assertIsNone(get_device_key.given_session(), broken)

    def test_a_token_eWeLink_refuses_is_a_sign_in_ended(self):
        with mock.patch.object(get_device_key, "api_json", return_value={"error": 401, "msg": "token"}), \
                self.assertRaises(get_device_key.SessionEnded):
            get_device_key.get_devices("eu", "old")
        with mock.patch.object(get_device_key, "api_json", return_value={"error": 500, "msg": "busy"}), \
                self.assertRaises(get_device_key.EwelinkError) as raised:
            get_device_key.get_devices("eu", "t")
        self.assertNotIsInstance(raised.exception, get_device_key.SessionEnded)


class DeviceKeyTests(unittest.TestCase):
    def test_login_signature_covers_exact_body(self):
        body, headers = signed_login_body("owner@example.test", "not-a-real-password", "+44")
        expected = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).digest()
        self.assertEqual(json.loads(body)["email"], "owner@example.test")
        self.assertTrue(headers["Authorization"].startswith("Sign "))
        import base64
        self.assertEqual(base64.b64decode(headers["Authorization"][5:]), expected)

    def test_a_phone_number_signs_in_with_its_own_country_code(self):
        for typed, number, code in (("+44 7700 900123", "+447700900123", "+44"),
                                    ("+44-7700-900123", "+447700900123", "+44"),
                                    ("0086 138-0013-8000", "+8613800138000", "+86"),
                                    ("+1 (555) 010-0199", "+15550100199", "+1"),
                                    ("+353 85 123 4567", "+353851234567", "+353")):
            body = json.loads(signed_login_body(typed, "pw", "+44")[0])
            self.assertEqual(body, {"password": "pw", "countryCode": code, "phoneNumber": number})

    def test_an_email_needs_a_domain_with_a_top_level_one(self):
        for text in ("owner@example.test", "o.w+n@mail.example.co.uk", "owner@xn--80ak6aa92e.xn--p1ai",
                     " owner@example.com "):
            self.assertTrue(get_device_key.is_email(text), text)
        for text in ("test@test", "test@example.", "test@-example.com", "test@example.c",
                     "test @example.com", "owner"):
            self.assertFalse(get_device_key.is_email(text), text)

    def test_an_email_keeps_the_country_code_given(self):
        body = json.loads(signed_login_body("owner@example.test", "pw", "+44")[0])
        self.assertEqual(body, {"password": "pw", "countryCode": "+44", "email": "owner@example.test"})

    def test_a_number_without_its_country_code_is_not_sent(self):
        for typed in ("07700 900123", "+28 1234 5678", "+44 77", "owner"):
            self.assertIsNone(get_device_key.phone_number(typed))
            with self.assertRaises(EwelinkError):
                signed_login_body(typed, "pw", "+44")

    def test_no_calling_code_is_the_start_of_another(self):
        for code in get_device_key.CALLING_CODES:
            for other in get_device_key.CALLING_CODES - {code}:
                self.assertFalse(other.startswith(code), (code, other))

    def test_extracts_one_id_from_repeated_mdns_packets(self):
        capture = {
            "packets": [
                {"records": [{"type": "TXT", "value": ["id=abc123", "encrypt=true"]}]},
                {"records": [{"type": "TXT", "value": ["id=abc123"]}]},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mdns.json"
            path.write_text(json.dumps(capture))
            self.assertEqual(target_id_from_mdns(path), "abc123")

    def test_selects_device_and_requires_key(self):
        devices = [{"deviceid": "one", "devicekey": "secret"}, {"deviceid": "two"}]
        self.assertEqual(select_device(devices, "one")["devicekey"], "secret")
        with self.assertRaises(EwelinkError):
            select_device(devices, "two")

    def test_an_inspected_connection_is_named(self):
        import ssl
        import urllib.error
        from unittest import mock

        from tools.ewelink import get_device_key
        failure = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        with mock.patch.object(get_device_key.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(EwelinkError) as caught:
                get_device_key.api_json("https://eu-apia.coolkit.cc/v2/user/login")
        self.assertIn("inspects encrypted connections", str(caught.exception))

    def test_secret_file_permissions(self):
        device = {"deviceid": "abc", "devicekey": "secret", "name": "test", "extra": {"uiid": 5}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.json"
            write_secret(path, device)
            if os.name == "posix":  # Windows has no permission bits: the profile's ACL keeps it private
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["devicekey"], "secret")


class DispatchTests(unittest.TestCase):
    def test_a_rebuilt_device_asks_its_regions_dispatch_server(self):
        with mock.patch.object(get_device_key, "_STUB_API_BASE", ""):
            self.assertEqual(get_device_key.dispatch_server("eu"), ("eu-dispd.coolkit.cc", 443))
            self.assertEqual(get_device_key.dispatch_server("cn"), ("cn-dispd.coolkit.cn", 443))
        with mock.patch.object(get_device_key, "_STUB_API_BASE", "https://apia.autoflash-stub.test"):
            self.assertEqual(get_device_key.dispatch_server("eu"), ("disp.autoflash-stub.test", 443))


if __name__ == "__main__":
    unittest.main()
