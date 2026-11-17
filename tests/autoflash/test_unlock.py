"""A converted device's own passwords and keys (tools/autoflash/unlock.py)."""
import base64
import unittest
from unittest import mock

from tools.autoflash import errors, esphome, esphome_ota, handover, output, prompts, unlock


class NoAnswerTests(unittest.TestCase):
    """A password check the device gives no answer to is asked again by itself,
    with what was typed, before the person is asked again."""

    def test_it_asks_the_device_again_then_the_person(self):
        answers = iter([None, None, False])
        checks = []

        def check(value):
            checks.append(value)
            return next(answers)
        secret = unlock.Secret("web password", lambda: "typed-once", check)
        with mock.patch.object(prompts, "ask_choice", return_value=0) as asked, \
             mock.patch.object(unlock.time, "sleep"), mock.patch.object(prompts, "section"), \
             mock.patch.object(output, "say"), mock.patch.object(output, "tell"), \
             mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"):
            self.assertEqual(unlock.secret_or_retry("t", [], [secret], lambda: None, still="")[1], "typed-once")
        asked.assert_called_once()                       # not again for the device's silence
        self.assertEqual(checks, ["typed-once"] * 3)     # nor typed again

    def test_nothing_typed_is_never_taken_for_the_secret(self):
        typed = iter(["", "right"])
        secret = unlock.Secret("web password", lambda: next(typed), lambda value: value != "right")
        with mock.patch.object(prompts, "ask_choice", return_value=0) as asked, \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"), \
             mock.patch.object(output, "tell"), \
             mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"):
            self.assertEqual(unlock.secret_or_retry("t", [], [secret], lambda: None, still="")[1], "right")
        self.assertEqual(asked.call_count, 2)

    def test_a_device_that_never_answers_comes_back_to_the_person(self):
        with mock.patch.object(unlock.time, "sleep"), mock.patch.object(output, "say"):
            self.assertIsNone(unlock.answered(lambda: None))


class EsphomeUpdatePasswordTests(unittest.TestCase):
    """A device on ESPHome with an update password, or taking only encrypted
    updates, made with its API encryption key: asked for what it wants before
    the go-ahead where someone can type it - or tried again without, once its
    owner has removed that - and stopped, saying what to give, where nobody
    can.  Its key does instead of its password where it takes encrypted
    updates: ESPHome asks for none then."""

    KEY = bytes(range(32))
    KEY_TEXT = base64.b64encode(KEY).decode()

    def setUp(self):
        self.private = mock.Mock()
        for patch in (mock.patch.object(output, "say"), mock.patch.object(output, "tell"),
                      mock.patch.object(prompts, "section"),
                      mock.patch.object(output, "transient_begin"),
                      mock.patch.object(output, "transient_end"),
                      mock.patch.object(output, "progress"),
                      mock.patch.object(output, "progress_clear"),
                      mock.patch.object(output, "private", self.private),
                      mock.patch.object(prompts, "UNATTENDED", False),
                      # A question no test expects fails it, rather than waiting on a keyboard.
                      mock.patch.object(prompts, "ask_choice", side_effect=AssertionError("asked")),
                      mock.patch.object(prompts, "ask_secret", side_effect=AssertionError("asked"))):
            patch.start()
            self.addCleanup(patch.stop)
        # ESPHome on the device wants this update password, until its owner
        # removes it (None); has an API key (`key`), so takes encrypted
        # updates too - only those, while `encrypted`.
        self.wanted, self.encrypted, self.key = "s3cret-71", False, None
        ota = esphome_ota

        def wants(host, password=None, key=None, **_):
            if self.key is not None and key is not None:
                return ota.NOTHING if key == self.key else ota.KEY
            if self.encrypted:
                return ota.KEY
            return ota.PASSWORD if self.wanted is not None and password != self.wanted else ota.NOTHING

        def probe(host, **_):
            return ota.Offer(2, True, True, False, encrypted_only=self.encrypted,
                             encryption=self.key is not None)

        def api_access(host, key=None, **_):
            if self.key is None:
                return esphome.API_PLAIN
            return esphome.API_OPEN if key == self.key else esphome.API_KEY
        for patch in (mock.patch.object(ota, "wants", side_effect=wants),
                      mock.patch.object(ota, "probe", side_effect=probe),
                      mock.patch.object(esphome, "api_access", side_effect=api_access)):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, password=None, key=None):
        from types import SimpleNamespace
        inputs = {"device_ip": "192.0.2.50"}
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: False)
        return SimpleNamespace(state=state, esphome_password=password, esphome_key=key)

    def test_asked_for_before_the_go_ahead_and_sent_with_the_update(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "ask_choice", return_value=0) as menu, \
                mock.patch.object(prompts, "ask_secret", side_effect=["not-it-9", "s3cret-71"]) as asked:
            unlock.unlock_esphome(ctx)
        self.assertEqual(asked.call_count, 2)  # the wrong one refused on the device
        self.assertEqual(ctx.esphome_password, "s3cret-71")
        self.assertEqual(menu.call_args.args[1], [  # no key to offer: it takes no encrypted updates
            "Type its update password", "Try again - I have removed it"])
        self.private.assert_any_call("s3cret-71", "esphome-password")  # kept out of the log
        with mock.patch.object(esphome_ota, "upload") as upload:
            handover.send_as_esphome_update(ctx, b"image", "the startup firmware")
        self.assertEqual(upload.call_args.kwargs["password"], "s3cret-71")

    def test_given_in_advance_it_is_not_asked(self):
        ctx = self.ctx("s3cret-71")  # AUTOFLASH_ESPHOME_OTA_PASSWORD, in .env or the environment
        unlock.unlock_esphome(ctx)
        self.assertEqual(ctx.esphome_password, "s3cret-71")
        self.wanted = None
        unlock.unlock_esphome(self.ctx())  # and a device with none asks nothing

    def test_unattended_without_it_the_run_stops_saying_what_to_give(self):
        with mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(errors.Abort,
                                        "update password: give it with --esphome-ota-password"):
                unlock.unlock_esphome(self.ctx())
            with self.assertRaisesRegex(errors.Abort, "the one given is not it"):
                unlock.unlock_esphome(self.ctx("not-it-9"))

    def test_trying_again_once_it_is_removed_goes_on_without_one(self):
        ctx = self.ctx()
        choices = []

        def try_again(*_):
            choices.append(1)
            if len(choices) == 2:
                self.wanted = None  # removed from its config, and that installed, before choosing again
            return 1
        with mock.patch.object(prompts, "ask_choice", side_effect=try_again):
            unlock.unlock_esphome(ctx)
        self.assertIsNone(ctx.esphome_password)
        output.tell.assert_any_call("    ESPHome there still has an update password.")  # tried too soon

    def test_an_update_refused_for_it_later_is_asked_for_and_sent_again(self):
        ctx = self.ctx()  # none at the go-ahead: an X2 sending the startup firmware once more, say
        refused = esphome_ota.NeedsPassword(given=False)
        with mock.patch.object(esphome_ota, "upload", side_effect=[refused, None]) as upload, \
                mock.patch.object(prompts, "ask_choice", return_value=0), \
                mock.patch.object(prompts, "ask_secret", return_value="s3cret-71"):
            handover.send_as_esphome_update(ctx, b"image", "the startup firmware")
        self.assertEqual([call.kwargs["password"] for call in upload.call_args_list], [None, "s3cret-71"])

    def test_with_its_key_given_no_password_is_asked_and_the_update_goes_encrypted(self):
        self.key = self.KEY
        ctx = self.ctx(key=self.KEY)  # AUTOFLASH_ESPHOME_API_KEY
        unlock.unlock_esphome(ctx)  # nothing asked: its key unlocks the update too
        self.assertIsNone(ctx.esphome_password)
        with mock.patch.object(esphome_ota, "upload") as upload:
            handover.send_as_esphome_update(ctx, b"image", "the startup firmware")
        self.assertEqual(upload.call_args.kwargs["key"], self.KEY)

    def test_where_it_takes_encrypted_updates_its_key_is_offered_instead(self):
        self.key = self.KEY
        ctx = self.ctx()
        with mock.patch.object(prompts, "ask_choice", return_value=1) as menu, \
                mock.patch.object(prompts, "ask_secret", side_effect=["not base64!", self.KEY_TEXT]):
            unlock.unlock_esphome(ctx)
        self.assertEqual(menu.call_args.args[1][:2], ["Type its update password", "Type its API encryption key"])
        self.assertEqual((ctx.esphome_key, ctx.esphome_password), (self.KEY, None))
        told = " ".join(str(call.args[0]) for call in output.tell.call_args_list)
        self.assertIn("That is not an API encryption key", told)  # said why, and asked again
        self.private.assert_any_call(self.KEY_TEXT, "esphome-api-key")

    def test_one_taking_only_encrypted_updates_is_asked_for_its_key(self):
        self.key, self.encrypted = self.KEY, True
        ctx = self.ctx()
        with mock.patch.object(prompts, "ask_choice", return_value=0) as menu, \
                mock.patch.object(prompts, "ask_secret", side_effect=[base64.b64encode(bytes(32)).decode(),
                                                                       self.KEY_TEXT]):
            unlock.unlock_esphome(ctx)
        self.assertEqual(menu.call_args.args[1][0], "Type its API encryption key")
        self.assertEqual(ctx.esphome_key, self.KEY)  # the first, another key, refused on the device
        output.tell.assert_any_call("    That is not its API encryption key.")
        with mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(errors.Abort,
                                        "only encrypted updates.*ESPHome YAML.*--esphome-api-key"
                                        ".*remove that encryption: line"):
                unlock.unlock_esphome(self.ctx())

    def test_one_taking_only_encrypted_updates_can_be_changed_instead(self):
        self.key, self.encrypted = self.KEY, True
        ctx = self.ctx()
        choices = []

        def choose(*_):
            choices.append(len(choices))
            if len(choices) == 2:
                self.encrypted = False  # encryption: removed, an update password put instead
            return 1 if len(choices) <= 2 else 0
        with mock.patch.object(prompts, "ask_choice", side_effect=choose), \
                mock.patch.object(prompts, "ask_secret", return_value="s3cret-71"):
            unlock.unlock_esphome(ctx)
        output.tell.assert_any_call("    ESPHome there still takes only encrypted updates.")
        self.assertEqual((ctx.esphome_key, ctx.esphome_password), (None, "s3cret-71"))  # then asked for that

    def test_a_key_given_that_is_not_its_own_is_asked_for_or_gone_on_without(self):
        self.key, self.wanted = self.KEY, None
        with mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(errors.Abort, "not the one ESPHome on the device has.*--esphome-api-key"):
                unlock.unlock_esphome(self.ctx(key=bytes(32)))
        ctx = self.ctx(key=bytes(32))
        with mock.patch.object(prompts, "ask_choice", return_value=1) as menu:
            unlock.unlock_esphome(ctx)  # gone on without it: the update needs no key
        self.assertEqual(menu.call_args.args[1], ["Type its API encryption key", "Go on without it"])
        self.assertIsNone(ctx.esphome_key)
        # One given for a device whose API is not encrypted is not needed.
        self.key = None
        ctx = self.ctx(key=self.KEY)
        unlock.unlock_esphome(ctx)
        self.assertIsNone(ctx.esphome_key)


def drawn(body):
    """What a terminal shows once `body` has run in a stage of a concise run."""
    import io
    import os
    import tempfile
    from pathlib import Path

    class Terminal(io.StringIO):
        def isatty(self):
            return True
    terminal, seen = Terminal(), {}
    with tempfile.TemporaryDirectory() as directory, mock.patch("sys.stdout", terminal), \
            mock.patch.object(output, "colour_ok", return_value=True), \
            mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
        output.start(Path(directory) / "run.log", verbose=False)
        output.stage_begin(9, "Preparing memory layout")
        try:
            body(lambda: seen.setdefault("asked", []).append(screen_of(terminal.getvalue())))
        finally:
            output.finish()
    return seen.get("asked", []), screen_of(terminal.getvalue())


def screen_of(text):
    from tools.repo import unattended_run
    screen = unattended_run.Screen(100, 40)
    screen.feed(text.replace("\n", "\r\n"))
    return screen.render(colour=False)


class TriesTests(unittest.TestCase):
    """A wrong try is taken off the screen before the next, as a wrong answer
    to any question is: only why it was not taken is left."""

    def test_a_wrong_password_and_its_menu_go_before_the_next_try(self):
        typed = iter(["wrong", "right"])

        def body(look):
            def ask():
                look()   # the screen as each password is asked
                value = next(typed)
                output.show(f"Web password: {'*' * len(value)}")
                return value
            secret = unlock.Secret("web password", ask, lambda value: value != "right")
            with mock.patch.object(prompts, "ask_choice",
                                   side_effect=lambda q, options, **k: output.show(f"{q}: 1") or 0):
                unlock.secret_or_retry("The device wants its web password", ["It has one set."],
                                       [secret], lambda: None, still="")
        asked, final = drawn(body)
        second = asked[1]
        self.assertEqual(second.count("What now: 1"), 1, second)       # one menu, not two
        self.assertNotIn("Web password: *****", second)                # the wrong try gone
        self.assertIn("That is not its web password.", second)         # why, left
        self.assertNotIn("The device wants its web password", final)   # all gone once taken


if __name__ == "__main__":
    unittest.main()
