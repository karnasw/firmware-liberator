"""A readout of an Espressif device: only the bridge sent to the device, and
only once PROCEED is typed."""
import unittest
from pathlib import Path
from unittest import mock

from tests.autoflash.test_readout import learned, record, start_run
from tests.espressif.test_scrub import flash as wifi_flash
from tools.autoflash import account, errors, hostsys, kinds, records, prompts, readout, readout_espressif, safety_locks

# A kind a whole readout is for (Readout wanted) that the tool does not convert:
# the bridge has never run on a real one.
BASICR4 = kinds.SONOFF_BASICR4
AS_BASICR4 = account.Asked("1.1.0", BASICR4.stock_model, "BASICR4", [], {}, "1000abcdef")
DEVICE = record("BASICR4", BASICR4.stock_model)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        start_run(self)
        asked = mock.patch.object(account, "ask_account", return_value=AS_BASICR4)
        asked.start()
        self.addCleanup(asked.stop)

    def test_nothing_is_sent_without_proceed(self):
        with learned(BASICR4.wrapper_model, "ESP32C3"), \
                mock.patch.object(prompts, "require_word", return_value=False), \
                mock.patch.object(hostsys, "run_tool") as sent:
            with self.assertRaises(errors.NothingWritten):
                readout.run(self.ctx, DEVICE)
        sent.assert_not_called()
        warned = " ".join(prompts.warning_box.call_args.args[1])
        self.assertIn("New to this tool", warned)

    def test_proceed_sends_the_bridge_wrapped_as_its_firmware_name(self):
        from tools.autoflash import firewall
        from tools.espressif import analyze_vendor_ota, dump_stock_flash
        state_then = []

        def found(ctx, *, sent):
            state_then.append(ctx.state.input(readout.STATE_KEY))   # a stop from here on reads through it
            return "192.0.2.77"
        with learned(BASICR4.wrapper_model, "ESP32C3"), \
                mock.patch.object(prompts, "require_word", return_value=True), \
                mock.patch.object(firewall, "choose_port", return_value=8088), \
                mock.patch.object(firewall, "ensure_firewall"), \
                mock.patch.object(safety_locks, "lifted"), mock.patch.object(safety_locks, "say_lock_back"), \
                mock.patch.object(hostsys, "run_tool", return_value=0) as sent, \
                mock.patch.object(readout_espressif, "find_bridge", side_effect=found) as waited, \
                mock.patch.object(dump_stock_flash, "read_dump",
                                  return_value=(wifi_flash(), b"\0" * 124, {"size": 1, "sha256": "ab"})), \
                mock.patch.object(dump_stock_flash, "summary", return_value=[]), \
                mock.patch.object(dump_stock_flash, "read_info", return_value=b"chip: ESP32-C3"), \
                mock.patch.object(records, "save_backup", return_value=(Path(self.tmp.name), False)), \
                mock.patch.object(readout_espressif, "back_on_sonoff",
                                  side_effect=lambda ctx: self.stages.append("back on Sonoff's")) as back, \
                mock.patch.object(readout, "offer", return_value="done") as offered:
            self.assertEqual(readout.run(self.ctx, DEVICE), "done")
        back.assert_called_once()
        # Putting Sonoff's firmware back is a stage of its own, after the read.
        at = self.stages.index("6 Restoring Sonoff's firmware")
        self.assertEqual(self.stages[at - 2:at + 2], ["5 Reading the device's memory", "complete",
                                                      "6 Restoring Sonoff's firmware", "back on Sonoff's"])
        command = sent.call_args.args[0]
        self.assertEqual(command[1], "-u")  # each line of the sender's in the log as it comes
        self.assertEqual(command[command.index("--readout-model") + 1], BASICR4.wrapper_model)
        # Sent to the account's device by its id, not by its eWeLink discovery advert.
        self.assertEqual(command[command.index("--device-id") + 1], DEVICE["deviceid"])
        self.assertNotIn("--mdns-capture", command)
        wrapped = Path(command[command.index("--firmware") + 1]).read_bytes()
        self.assertEqual(analyze_vendor_ota.analyze(wrapped)["wrapper"]["model"], BASICR4.wrapper_model)
        manifest, files = offered.call_args.args[1:3]
        self.assertTrue(manifest["read"])
        for key in ("firmware", "facts", "wifi_blanked", "identity_replaced"):
            self.assertIn(key, manifest)
        self.assertEqual(manifest["flash"], {"size": 1, "sha256": "ab", "matches_device": None})
        self.assertTrue(manifest["wifi_blanked"])
        self.assertEqual(files["info.txt"], b"chip: ESP32-C3")
        self.assertEqual(state_then, ["sent"])
        self.assertIs(waited.call_args.kwargs["sent"], True)
        self.assertTrue(self.ctx.state.input(readout.STATE_KEY))

    def test_on_the_reading_program_already_it_reads_through_it_sending_nothing(self):
        from tools.autoflash import firewall
        from tools.espressif import dump_stock_flash
        self.ctx.state.set_input("device_ip", "192.0.2.77")
        with mock.patch.object(prompts, "require_word", return_value=True), \
                mock.patch.object(firewall, "choose_port", return_value=8088), \
                mock.patch.object(firewall, "ensure_firewall") as firewalled, \
                mock.patch.object(readout_espressif.installer, "bridge_answers", return_value=True), \
                mock.patch.object(hostsys, "run_tool") as sent, \
                mock.patch.object(readout_espressif, "find_bridge") as waited, \
                mock.patch.object(dump_stock_flash, "read_dump",
                                  return_value=(wifi_flash(), b"\0" * 124, {"size": 1, "sha256": "ab"})) as read, \
                mock.patch.object(dump_stock_flash, "summary", return_value=[]), \
                mock.patch.object(dump_stock_flash, "read_info", return_value=None):
            readout_espressif.read_out(self.ctx, readout_espressif.chips.ESP32C3, "FWSW-E32NEW-NEW-ESP32C3", "1.0.0",
                                      "1000abcdef")
        sent.assert_not_called()
        waited.assert_not_called()
        # A firewall rule the stopped run left is taken over, to be removed as this run ends.
        firewalled.assert_called_once_with(self.ctx)
        self.assertEqual(read.call_args.args[0], "192.0.2.77")
        self.assertEqual(self.ctx.state.input(readout.STATE_KEY), "sent")


class BackOnSonoffTests(unittest.TestCase):
    """A readout's end: the device restarted by its bridge, else switched off
    and on by the person."""

    def setUp(self):
        start_run(self)

    def test_restarted_by_the_bridge_nobody_is_asked(self):
        self.ctx.state.set_input(readout_espressif.BRIDGE_HOST, "192.0.2.77")
        with mock.patch.object(readout_espressif.installer, "restart_into_sonoff", return_value=True) as restarted, \
                mock.patch.object(prompts, "wait_for_key") as asked:
            self.assertTrue(readout_espressif.back_on_sonoff(self.ctx))
        restarted.assert_called_once_with(self.ctx, "192.0.2.77")
        asked.assert_not_called()
        self.assertEqual(self.ctx.state.input(readout.STATE_KEY), "read")

    def test_a_bridge_that_will_not_restart_has_the_person_asked(self):
        self.ctx.state.set_input(readout_espressif.BRIDGE_HOST, "192.0.2.77")
        with mock.patch.object(readout_espressif.installer, "restart_into_sonoff", return_value=False), \
                mock.patch.object(prompts, "wait_for_key") as asked, \
                mock.patch.object(readout_espressif.installer, "back_on_sonoff", return_value=True):
            self.assertTrue(readout_espressif.back_on_sonoff(self.ctx))
        asked.assert_called_once()

    def switched_back(self, answers):
        """back_on_sonoff after the person switched it off and on, Sonoff's
        firmware answering as `answers` says, 200 s passing at each look."""
        import itertools
        with mock.patch.object(prompts, "wait_for_key"), \
                mock.patch.object(readout_espressif.installer, "back_on_sonoff", return_value=answers), \
                mock.patch.object(readout_espressif.time, "monotonic", side_effect=itertools.count(0, 200)), \
                mock.patch.object(readout_espressif.time, "sleep"):
            return readout_espressif.back_on_sonoff(self.ctx)

    def test_switched_back_on_it_is_read_once_sonoff_answers(self):
        self.assertTrue(self.switched_back(True))
        self.assertEqual(self.ctx.state.input(readout.STATE_KEY), "read")

    def test_never_answering_as_sonoff_is_said_so(self):
        self.assertFalse(self.switched_back(False))
        self.assertNotEqual(self.ctx.state.input(readout.STATE_KEY), "read")

    def test_on_its_own_network_the_person_is_asked_to_rejoin_theirs(self):
        self.ctx.state.set_input(readout_espressif.BRIDGE_HOST, readout_espressif.setup_network.SETUP_HOST)
        with mock.patch.object(readout_espressif.installer, "restart_into_sonoff") as restarted, \
                mock.patch.object(prompts, "wait_for_key") as asked, \
                mock.patch.object(readout_espressif.installer, "back_on_sonoff", return_value=True):
            readout_espressif.back_on_sonoff(self.ctx)
        restarted.assert_not_called()
        asked.assert_called_once()


def flash_with(apps, nvs=b"settings", tail=b""):
    """A 2 MB flash: a table at 0x8000 (nvs, ota_0, ota_1), and in each slot an
    app of (project, version, build hash), or nothing; `nvs` its settings,
    `tail` what an earlier firmware left past the app in its slot."""
    import struct
    flash = bytearray(b"\xff" * 0x200000)
    entries = [("nvs", 1, 2, 0x9000, 0x5000), ("ota_0", 0, 0x10, 0x10000, 0x80000), ("ota_1", 0, 0x11, 0x90000, 0x80000)]
    for n, (label, kind, subtype, offset, size) in enumerate(entries):
        flash[0x8000 + 32 * n:0x8000 + 32 * (n + 1)] = (b"\xaa\x50" + bytes([kind, subtype]) + struct.pack("<II", offset, size)
                                                       + label.encode().ljust(16, b"\0") + b"\0" * 4)
    flash[0x9000:0x9000 + len(nvs)] = nvs
    for (_, _, _, offset, _), app in zip(entries[1:], apps):
        if app is None:
            continue
        project, version, built = app
        image = bytearray(b"\xe9" + b"\0" * 511)
        struct.pack_into("<I", image, 32, 0xABCD5432)
        image[32 + 16:32 + 48] = version.encode().ljust(32, b"\0")
        image[32 + 48:32 + 80] = project.encode().ljust(32, b"\0")
        image[32 + 144:32 + 176] = built
        flash[offset:offset + len(image)] = image
        flash[offset + 0x40000:offset + 0x40000 + len(tail)] = tail
    return bytes(flash)


class FirmwareOfTests(unittest.TestCase):
    """What every genuine readout of a model and version shares, for the
    website to tell those that agree from one that does not."""

    SONOFF = ("esp32_fw", "1.1.0", bytes(range(32)))
    BRIDGE = ("autoflash_bridge", "4.4.6", bytes(32))

    def test_two_devices_settings_and_leftovers_apart_agree(self):
        one = readout_espressif.firmware_of(flash_with([self.SONOFF, self.BRIDGE], nvs=b"mine", tail=b"old"))
        two = readout_espressif.firmware_of(flash_with([self.BRIDGE, self.SONOFF], nvs=b"yours", tail=b"older"))
        self.assertEqual(one, two)
        self.assertEqual(one["apps"], [{"project": "esp32_fw", "version": "1.1.0", "elf_sha256": bytes(range(32)).hex()}])
        self.assertRegex(one["table"], r"^[0-9a-f]{64}$")

    def test_another_build_does_not(self):
        other = ("esp32_fw", "1.1.0", bytes(range(1, 33)))
        self.assertNotEqual(readout_espressif.firmware_of(flash_with([self.SONOFF, self.BRIDGE])),
                            readout_espressif.firmware_of(flash_with([other, self.BRIDGE])))

    def test_a_flash_with_no_table_has_none(self):
        self.assertEqual(readout_espressif.firmware_of(b"\xff" * 0x10000), {})


if __name__ == "__main__":
    unittest.main()
