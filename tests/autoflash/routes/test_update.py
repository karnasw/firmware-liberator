"""Updates (tools/autoflash/routes/update.py): replacing a person's own build."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import esphome, kinds, prompts
from tools.autoflash.errors import Abort
from tools.autoflash.routes import update
from tools.autoflash.state import RunState
from tests.autoflash.test_unlock import drawn


class ReplaceOwnBuildTests(unittest.TestCase):
    """Its warning and go-ahead leave the screen once agreed, as every other
    go-ahead does, and stay when declined: they say why the run stops."""

    def replace(self, agreed):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_ip", "192.0.2.50")
        state.set_input("device_kind", "sonoff-s60")
        ctx = self.ctx = SimpleNamespace(state=state, esphome_key=None)
        info = {"project_name": kinds.SONOFF_S60.esphome_project, "built": "someone's own", "encrypted": True}
        result = {}

        def body(look):
            with mock.patch.object(esphome, "api_device_info", return_value=info), \
                 mock.patch.object(prompts, "require_word", side_effect=lambda word: look() or agreed):
                try:
                    result["replaced"] = update.replace_own_build(ctx, "esphome", "2026.10.0-1")
                except Abort as exc:
                    result["refused"] = str(exc)
        asked, final = drawn(body)
        return asked[0], final, result

    def test_agreed_its_warning_goes(self):
        asked, final, result = self.replace(agreed=True)
        self.assertIn("Replacing your own build", asked)
        self.assertNotIn("Replacing your own build", final)
        self.assertTrue(result["replaced"])
        # Recorded as an update, with the build it replaces: the check after
        # it tells the published build from the one that was there.
        state = self.ctx.state
        self.assertEqual((state.input("replaced_build"), state.input("update_from"), state.input("update_to")),
                         ("someone's own", update.OWN_BUILD, "2026.10.0-1"))

    def test_declined_it_stays_to_say_why(self):
        _, final, result = self.replace(agreed=False)
        self.assertIn("Replacing your own build", final)
        self.assertEqual(result["refused"], "not confirmed")


class ReplaceOnlyEsphomeTests(unittest.TestCase):
    """--replace-own-build is for this project's ESPHome alone."""

    def test_a_device_running_another_firmware_is_refused_before_it_is_asked_anything(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_ip", "192.0.2.50")
        state.set_input("device_kind", "sonoff-s60")
        ctx = SimpleNamespace(state=state, esphome_key=None)
        with mock.patch.object(esphome, "api_device_info", side_effect=AssertionError("asked")):
            with self.assertRaisesRegex(Abort, "does not answer as that: it runs Tasmota"):
                update.replace_own_build(ctx, "tasmota", "16.0.0")
            # Nor an ESPHome with no build of this project's published for its kind.
            with self.assertRaisesRegex(Abort, "is for this project's ESPHome"):
                update.replace_own_build(ctx, "esphome", None)


if __name__ == "__main__":
    unittest.main()
