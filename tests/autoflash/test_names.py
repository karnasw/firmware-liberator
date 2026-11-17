"""What a device's networks are called (tools/autoflash/names.py): its own setup
network, and the names a person finds it by."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tools.autoflash import names, prompts
from tools.autoflash.state import RunState


class SetupNetworkTestCase(unittest.TestCase):
    def ctx(self, mac="12:34:56:ab:cd:ef"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        if mac:
            state.set_input("device_mac", mac)
        return SimpleNamespace(state=state)


class NamesTests(SetupNetworkTestCase):
    def test_this_devices_names_are_known_from_its_mac(self):
        # Converted: its name, whatever the device. Started from its defaults:
        # Tasmota's own name. And ESP-IDF's default - the hotspot's MAC, one more.
        self.assertEqual(names.setup_networks(self.ctx()), ("tasmota-abcdef", "tasmota-ABCDEF-", "ESP_ABCDF0"))
        self.assertEqual(names.setup_network_hint(self.ctx()),
                         "tasmota-abcdef, tasmota-ABCDEF-NNNN, or ESP_ABCDF0")
        self.assertEqual(names.final_name(self.ctx(), "tasmota"), "tasmota-abcdef")
        self.assertEqual(names.final_name(self.ctx(), "esphome"), "esphome-abcdef")
        # Every firmware of this project's part way through: one name, from
        # the last two bytes of the device's own MAC.
        self.assertEqual(names.our_setup_network(self.ctx()), "autoflash-recovery-CDEF")
        self.assertEqual(names.mover_network_hint(self.ctx()), "autoflash-recovery-CDEF")
        # And by the names they had before: the installer before 4.4.4 and
        # 4.4.3, the first movers.
        self.assertEqual(names.bridge_networks(self.ctx()),
                         ("autoflash-recovery-CDEF", "Sonoff-Setup-CDEF", "S60-OTA-Bridge"))
        self.assertEqual(names.mover_networks(self.ctx()),
                         ("autoflash-recovery-CDEF", "Sonoff-Setup-CDEF", "sonoff-s60-mover-"))
        # ESPHome's has its name, as Tasmota's; and the names 1.1.0's and
        # earlier builds' had.
        self.assertEqual(names.esphome_network_hint(self.ctx()), "esphome-abcdef")
        self.assertEqual(names.esphome_networks(self.ctx()),
                         ("esphome-abcdef", "Sonoff-Setup-CDEF", "sonoff-s60-"))

    def test_a_classic_esp32s_default_name_is_its_own_mac(self):
        # Seen in the emulator on the POW Elite and TH Elite: ESP_ and the
        # Wi-Fi MAC itself, not the hotspot's.
        ctx = self.ctx()
        ctx.state.set_input("device_kind", "sonoff-pow-elite")
        self.assertEqual(names.setup_networks(ctx)[2], "ESP_ABCDEF")
        self.assertEqual(names.setup_network_hint(ctx), "tasmota-abcdef, tasmota-ABCDEF-NNNN, or ESP_ABCDEF")

    def test_esphome_with_no_earlier_build_goes_by_its_name_alone(self):
        # Every kind but the S60 had no earlier ESPHome build, so no old names:
        # an empty one, as a prefix, would take the home Wi-Fi - the first
        # network listed - for ESPHome's own.
        for mac, wanted in (("12:34:56:ab:cd:ef", ("esphome-abcdef",)), (None, ("esphome-",))):
            ctx = self.ctx(mac=mac)
            ctx.state.set_input("device_kind", "sonoff-pow-elite")
            self.assertEqual(names.esphome_networks(ctx), wanted)
            self.assertIsNone(prompts._best_ssid(["HomeWifi", "Neighbour"], names.esphome_networks(ctx)))

    def test_one_more_carries_into_the_next_byte(self):
        self.assertEqual(names.setup_networks(self.ctx("12-34-56-AB-FF-FF"))[2], "ESP_AC0000")

    def test_without_the_mac_any_devices_will_do(self):
        self.assertEqual(names.setup_networks(self.ctx(mac=None)), ("tasmota-",))
        self.assertEqual(names.setup_network_hint(self.ctx(mac=None)), "tasmota-xxxxxx or tasmota-XXXXXX-NNNN")
        self.assertEqual(names.esphome_network_hint(self.ctx(mac=None)), "esphome-xxxxxx")
        self.assertEqual(names.bridge_networks(self.ctx(mac=None)),
                         ("autoflash-recovery-", "Sonoff-Setup-", "S60-OTA-Bridge"))
        self.assertEqual(names.esphome_networks(self.ctx(mac=None)), ("esphome-", "Sonoff-Setup-", "sonoff-s60-"))


if __name__ == "__main__":
    unittest.main()
