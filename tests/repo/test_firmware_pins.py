"""firmware.json, the files it pins, and everything that names them, agreeing.

tools/repo/update_firmware.py writes firmware.json and the files; the rest is by
hand (docs/updating-firmware.md), and these say what was missed.
"""

import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.beken import rbl, uf2
from tools.repo import update_firmware as uf
from tools.autoflash import kinds, tasmota_settings
from tools.espressif import chips
from tools.espressif.safeboot.safeboot_migration import OFFICIAL_URLS, PINNED_ARTIFACTS, sha256_bytes

ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8"))
ESPRESSIF = PINS["espressif"]
TASMOTA = ESPRESSIF["esp32c3"]["tasmota"]
# The ESPHome builds of the S60's config: its image, and the ESP32-C3's mover
# and bootloader, which come of the same release.
S60_ESPHOME = {"image": ESPRESSIF["sonoff-s60"]["esphome"], "mover": ESPRESSIF["esp32c3"]["mover"],
               "bootloader": ESPRESSIF["esp32c3"]["bootloader"]}


class LayoutTests(unittest.TestCase):
    """Each family's pins are kept alike: by what they belong to (a chip, which
    every device on it shares, or a device), then by firmware."""

    FAMILIES = {"espressif": {chip.key for chip in chips.ALL}, "beken": {uf.READOUT_BUILD}}

    def test_firmware_json_holds_each_family_and_nothing_else(self):
        self.assertLessEqual(set(PINS) - {"about", "hold"}, set(self.FAMILIES))
        self.assertEqual(set(PINS) & set(self.FAMILIES), set(self.FAMILIES))

    def test_the_two_families_have_the_one_shape(self):
        for family, shared in self.FAMILIES.items():
            devices = {kind.key for kind in kinds.EVERY if (kind.chip.startswith("bk")) == (family == "beken")}
            for owner, section in PINS[family].items():
                self.assertIn(owner, shared | devices, (family, owner))
                for name, pin in section.items():
                    self.assertIsInstance(pin, dict, (family, owner, name))
                    # A file published here, or one fetched from its project and checked against the pin.
                    self.assertTrue("file" in pin or "files" in pin or "url" in pin, (family, owner, name))
                    if "file" in pin:
                        self.assertTrue(pin["file"].startswith("artifacts/"), pin["file"])
                        self.assertIn("sha256", pin, pin["file"])

    def test_each_devices_esphome_is_in_its_own_section_in_either_family(self):
        for kind in kinds.READY:
            family = "beken" if kind.chip.startswith("bk") else "espressif"
            self.assertIn("esphome", PINS[family][kind.key], kind.key)
            self.assertNotIn(kind.chip, PINS["beken" if family == "espressif" else "espressif"], kind.key)

    def test_the_readmes_badges_read_firmware_json_where_it_pins_each(self):
        # shields.io reads main's firmware.json each time the README is shown.
        from urllib.parse import unquote
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        queries = [unquote(query) for query in re.findall(r"firmware\.json&query=([^&]+)&", readme)]
        self.assertEqual(len(queries), 3)
        for query in queries:
            entry = PINS
            for key in re.findall(r'\.(\w+)|\["([^"]+)"\]', query.removeprefix("$")):
                entry = entry[key[0] or key[1]]
            self.assertRegex(entry, r"^\d+\.\d+\.\d+$", query)


class FileNameTests(unittest.TestCase):
    """A published file a chip's devices share names the chip, and a device's own
    names the device, so the two chips' are told apart by name. Those published
    before the rule keep their names - published files never change - each only
    while it is that exact build: the build that replaces one is named by the
    rule (update_firmware.CHIP_FILES, tasmota_settings.template_path), and takes
    it out of here. docs/updating-firmware.md says so beside each build."""

    UNTIL_REBUILT = {
        "esphome-sonoff-s60-bootloader-1.2.0.bin": "3792bb665916d69c410c9fd10f69d1a6968e61f2a9e4969829a1c49f9ea6f433",
        "autoflash-recovery-safeboot-imprintable.bin": "ece36fd964c1443ef9fb74af87a0a257501c9e5ecfb1fb7a1c3aff1744bc29fd",
        "tasmota-settings-template-15.6.0.0.bin": "d63f03cb99540407037f36dc8155ec9b02d24425d3b02c52299713233e98f4a5",
    }
    # Tasmota's own build, under Tasmota's own name for it.
    UPSTREAM = {"tasmota32c3-bluetooth.bin"}

    @staticmethod
    def published():
        """Each file in artifacts/SHA256SUMS, and its hash."""
        lines = (ROOT / "artifacts" / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        return {name: digest for digest, name in (line.split("  ", 1) for line in lines)}

    @staticmethod
    def owners():
        """Each file published, and what it belongs to: where firmware.json pins
        it, or for those pinned in the code, what the code takes it for."""
        from tools.autoflash.routes.espressif import images
        from tools.espressif.safeboot import safeboot_migration
        found = {Path(pin["file"]).name: owner for family in ("espressif", "beken")
                 for owner, section in PINS[family].items() for pin in section.values() if "file" in pin}
        for chip in chips.ALL:
            found.setdefault(safeboot_migration.published_recovery_file(chip).name, chip.key)
            found.setdefault(tasmota_settings.template_path(ROOT, chip).name, chip.key)
        found.setdefault(images.SONOFF_TABLE.name, kinds.SONOFF_S60.key)
        return found

    @staticmethod
    def named_by(name, owner):
        return f"-{owner}-" in f"-{Path(name).stem}-"

    def test_each_published_file_belongs_to_a_chip_or_a_device(self):
        self.assertEqual(set(self.published()) - self.UPSTREAM - set(self.owners()), set())

    def test_each_published_file_names_what_it_belongs_to(self):
        published = self.published()
        wrong = [f"{name} ({owner}'s)" for name, owner in self.owners().items()
                 if not self.named_by(name, owner) and self.UNTIL_REBUILT.get(name) != published.get(name)]
        self.assertEqual(wrong, [], "named by the rule: update_firmware.CHIP_FILES")

    def test_each_file_named_before_the_rule_is_still_that_build(self):
        published = self.published()
        for name, digest in self.UNTIL_REBUILT.items():
            self.assertEqual(published.get(name), digest,
                             f"{name} was built again, so named by the rule: take it out of UNTIL_REBUILT")

    def test_update_firmware_names_each_chips_files_by_the_rule(self):
        for chip in chips.ALL:
            for name in uf.CHIP_FILES:
                file = uf.chip_file(chip, name, version="1.0.0", suffix=".bin")
                self.assertTrue(self.named_by(file, chip.key), file)
                self.assertFalse(any(self.named_by(file, other.key) for other in chips.ALL if other is not chip), file)
        self.assertEqual(uf.ESPHOME_BUILDS["mover"][1], uf.chip_file(chips.ESP32C3, "mover"))
        self.assertEqual(uf.BOOTLOADER_FILE, uf.chip_file(chips.ESP32C3, "bootloader"))

    def test_a_template_captured_at_another_release_names_its_chip(self):
        from unittest import mock
        with mock.patch.object(tasmota_settings, "TEMPLATE_VERSION", 0x10000000):
            for chip in chips.ALL:
                self.assertTrue(self.named_by(tasmota_settings.template_path(ROOT, chip).name, chip.key), chip.key)

    def test_each_chips_mover_is_built_from_its_chips_folder(self):
        for chip in chips.ALL:
            self.assertEqual(chip.pin("mover")["config"], f"devices/{chip.key}/mover.yaml", chip.key)
            self.assertEqual(uf.mover_config(chip), chip.pin("mover")["config"], chip.key)
        self.assertEqual(uf.ESPHOME_BUILDS["mover"][0], uf.mover_config(chips.ESP32C3))


class TasmotaPinTests(unittest.TestCase):
    def test_the_files_come_from_the_release_pinned(self):
        tasmota = TASMOTA
        self.assertEqual(tasmota["tag"], f"v{tasmota['version']}")
        for key, name in uf.TASMOTA_FILES.items():
            self.assertEqual(tasmota["files"][key]["name"], name)
            self.assertEqual(OFFICIAL_URLS[key],
                             f"https://github.com/arendst/Tasmota/releases/download/{tasmota['tag']}/{name}")
            self.assertEqual(PINNED_ARTIFACTS[key]["sha256"], tasmota["files"][key]["sha256"])

    def test_each_earlier_release_is_kept_by_its_files(self):
        current = {(f["size"], f["sha256"]) for f in TASMOTA["files"].values()}
        versions = [entry["version"] for entry in TASMOTA.get("earlier", [])]
        self.assertNotIn(TASMOTA["version"], versions)
        self.assertEqual(len(versions), len(set(versions)))
        for entry in TASMOTA.get("earlier", []):
            self.assertEqual(set(entry["files"]), set(uf.TASMOTA_FILES), entry["version"])
            for file in entry["files"].values():
                self.assertNotIn((file["size"], file["sha256"]), current, entry["version"])

    def test_the_release_takes_the_record_the_recovery_safeboot_saves(self):
        self.assertEqual(TASMOTA["cfg_holder"], tasmota_settings.CFG_HOLDER)


# ESPHome reads a current transformer as one from 2026.11.0 (esphome#7836,
# current_input: current_transformer: gain 1 and the x5 scale Tasmota's POWCT
# model uses). Before it, cse7761 reads the POWCT's clamp as a shunt - 3.2
# times too much - and its config divides by pi, as ESPHome's device page
# says, leaving it 1.9% high.
CLAMP_FROM = (2026, 11, 0)


def clamp_problems(esphome: str, config: str) -> list[str]:
    """What the POWCT's ESPHome `config` still does wrong for its clamp, built
    with ESPHome `esphome`: nothing before CLAMP_FROM."""
    if tuple(int(part) for part in re.findall(r"\d+", esphome)[:3]) < CLAMP_FROM:
        return []
    problems = []
    if not re.search(r"^\s*current_input:\s*current_transformer\s*$", config, re.M):
        problems.append("set current_input: current_transformer under the cse7761 sensor")
    if "M_PI" in config:
        problems.append("drop the divide-by-pi filters: the clamp is read as one now")
    return problems


class PowctClampTests(unittest.TestCase):
    def test_its_status_led_is_lit_when_high_as_in_its_tasmota_template(self):
        # Sonoff's firmware drives GPIO13 with the relay, uninverted
        # (devices/sonoff-powct/README.md), as its Tasmota template's Led1 (288,
        # tests/autoflash/test_kinds.py) has it.
        config = (ROOT / "devices/sonoff-powct/esphome.yaml").read_text(encoding="utf-8")
        block = config.split("id: relay_led\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("GPIO13", block)
        self.assertNotIn("inverted", block)

    def test_the_clamp_is_read_as_one_once_esphome_can(self):
        pin = ESPRESSIF["sonoff-powct"]["esphome"]
        config = (ROOT / pin["config"]).read_text(encoding="utf-8")
        self.assertEqual(clamp_problems(pin["esphome"], config), [],
                         f"{pin['config']} is built with ESPHome {pin['esphome']}")

    def test_it_asks_for_both_changes_from_2026_11_and_nothing_before(self):
        old = "    current_1:\n      filters:\n        - lambda: return x / M_PI;\n"
        new = "    current_input: current_transformer\n    current_1:\n      name: Current\n"
        self.assertEqual(clamp_problems("2026.10.2", old), [])
        self.assertEqual(len(clamp_problems("2026.11.0", old)), 2)
        self.assertEqual(len(clamp_problems("2026.12.1", old)), 2)
        self.assertEqual(clamp_problems("2026.11.0", new), [])


class ESPHomePinTests(unittest.TestCase):
    def test_each_file_is_the_one_pinned_and_in_sha256sums(self):
        sums = (ROOT / "artifacts" / "SHA256SUMS").read_text().splitlines()
        for key, pin in S60_ESPHOME.items():
            data = (ROOT / pin["file"]).read_bytes()
            self.assertEqual((len(data), sha256_bytes(data)), (pin["size"], pin["sha256"]), key)
            self.assertIn(f"{pin['sha256']}  {Path(pin['file']).name}", sums)

    def test_each_app_was_built_as_firmware_json_says(self):
        for key in ("image", "mover"):
            pin = S60_ESPHOME[key]
            described = uf.app_description((ROOT / pin["file"]).read_bytes())
            self.assertEqual(described["version"], pin["esphome"], key)
            self.assertEqual(described["esp_idf"], pin["esp_idf"], key)

    def test_each_config_is_at_the_version_published(self):
        # A config changed and its version raised, but not yet built and
        # published, fails here: what Home Assistant adopts must be what the
        # tool installs.
        for key in ("image", "mover"):
            pin = S60_ESPHOME[key]
            config = (ROOT / pin["config"]).read_text(encoding="utf-8")
            self.assertEqual(uf.config_version(config), pin["version"], key)

    def test_no_older_build_is_left_in_artifacts(self):
        published = {Path(pin["file"]).name for section in ESPRESSIF.values() for pin in section.values()
                     if "file" in pin}
        for pattern in ("esphome-sonoff-s60-*.bin", "autoflash-mover-*.bin"):
            for path in (ROOT / "artifacts").glob(pattern):
                self.assertIn(path.name, published)


class DocumentedTests(unittest.TestCase):
    def test_artifacts_readme_has_each_files_row(self):
        readme = (ROOT / "artifacts/README.md").read_text(encoding="utf-8")
        for pin in S60_ESPHOME.values():
            row = next((line for line in readme.splitlines()
                        if line.startswith(f"| `{Path(pin['file']).name}` |")), "")
            self.assertIn(f"`{pin['sha256']}`", row, pin["file"])

    def test_third_party_notices_name_each_file_and_its_build(self):
        notices = (ROOT / "NOTICES.md").read_text(encoding="utf-8")
        for pin in S60_ESPHOME.values():
            self.assertIn(f"`{pin['file']}`", notices)
            self.assertIn(f"ESPHome {pin['esphome']}", notices)
            self.assertIn(f"ESP-IDF {pin['esp_idf']}", notices)

    def test_the_esphome_readme_names_the_release_that_built_the_image(self):
        readme = (ROOT / "devices/sonoff-s60/esphome.md").read_text(encoding="utf-8")
        self.assertIn(f"ESPHome {S60_ESPHOME['image']['esphome']} built the current", readme)


class BekenPinTests(unittest.TestCase):
    """The Beken devices' images: OpenBeken and ESPHome on LibreTiny, rt_ota images
    pinned under "beken" by device."""

    def test_each_file_is_the_one_pinned_and_in_sha256sums(self):
        sums = (ROOT / "artifacts" / "SHA256SUMS").read_text().splitlines()
        for device, section in PINS.get("beken", {}).items():
            self.assertEqual(set(section), set(uf.BEKEN_BUILDS[device]), device)
            for key, pin in section.items():
                data = (ROOT / pin["file"]).read_bytes()
                self.assertEqual((len(data), sha256_bytes(data)), (pin["size"], pin["sha256"]), (device, key))
                self.assertIn(f"{pin['sha256']}  {Path(pin['file']).name}", sums)
                self.assertEqual(rbl.header(data).name, "app", (device, key))
                self.assertLessEqual(len(data), uf2.DOWNLOAD_SIZE, (device, key))

    def test_each_build_is_what_firmware_json_says(self):
        for device, section in PINS.get("beken", {}).items():
            if "openbeken" in section:
                pin = section["openbeken"]
                self.assertEqual(rbl.header((ROOT / pin["file"]).read_bytes()).version, pin["version"])
                # Built with the driver and the device's table, each as it is now.
                self.assertEqual(list(pin["sources"]), uf.openbeken_sources(device), device)
                for path, digest in pin["sources"].items():
                    self.assertEqual(sha256_bytes((ROOT / path).read_bytes()), digest, path)
                for patch, digest in pin["patches"].items():
                    self.assertEqual(sha256_bytes((uf.OPENBEKEN_DIR / patch).read_bytes()), digest, patch)
                # Built with every patch kept there - the module needs the hook one.
                self.assertEqual(set(pin["patches"]), set(uf.OPENBEKEN_PATCHES), device)
                self.assertEqual({path.name for path in uf.OPENBEKEN_DIR.glob("*.patch")}, set(uf.OPENBEKEN_PATCHES))
            if "esphome" in section:
                pin = section["esphome"]
                config = (ROOT / pin["config"]).read_text(encoding="utf-8")
                self.assertEqual(uf.config_version(config), pin["version"], device)
                firmware = rbl.unpack((ROOT / pin["file"]).read_bytes())
                self.assertIn(pin["version"].encode("ascii"), firmware)
                self.assertIn(pin["esphome"].encode("ascii"), firmware)

    def test_openbekens_pin_says_its_upstream_release_and_the_readme_badge_shows_it(self):
        pin = PINS["beken"]["sonoff-s61"]["openbeken"]
        self.assertRegex(pin["release"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(pin["version"], rf"^{re.escape(pin['release'])}-[1-9]\d*$")
        self.assertEqual(rbl.header((ROOT / pin["file"]).read_bytes()).version, pin["version"])
        self.assertTrue(pin["file"].endswith(f"-{pin['version']}.rbl"), pin["file"])
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("query=%24.beken%5B%22sonoff-s61%22%5D.openbeken.release&label=OpenBeken", readme)

    def test_a_version_counts_this_projects_builds_on_a_release(self):
        self.assertEqual(uf.build_version("1.18.320", {}), "1.18.320-1")
        self.assertEqual(uf.build_version("1.18.320", {"version": "1.1.0"}), "1.18.320-1")
        self.assertEqual(uf.build_version("1.18.320", {"version": "1.18.320-1"}), "1.18.320-2")
        self.assertEqual(uf.build_version("1.18.321", {"version": "1.18.320-2"}), "1.18.321-1")
        # One never released: a change folded into it, on the same release.
        self.assertEqual(uf.build_version("1.18.320", {"version": "1.18.320-2"}, folded=True), "1.18.320-2")
        self.assertEqual(uf.build_version("1.18.321", {"version": "1.18.320-2"}, folded=True), "1.18.321-1")

    def test_a_build_is_published_once_a_releases_firmware_json_pins_it(self):
        from unittest import mock
        pins = {"v2.3.0": '{"file": "artifacts/openbeken-sonoff-s61-1.1.0.rbl"}'}

        def run(args, **_):
            return mock.Mock(stdout=pins.get(args[-1].split(":")[0], ""))
        with mock.patch.object(uf, "git", return_value="v2.2.0\nv2.3.0"), mock.patch.object(uf.subprocess, "run", run):
            self.assertTrue(uf.published_by_a_release("artifacts/openbeken-sonoff-s61-1.1.0.rbl"))
            self.assertFalse(uf.published_by_a_release("artifacts/openbeken-sonoff-s61-1.18.320-2.rbl"))
        # No tags to tell by - a shallow clone - and none is taken for unreleased.
        with mock.patch.object(uf, "git", return_value=""):
            self.assertTrue(uf.published_by_a_release("artifacts/openbeken-sonoff-s61-1.18.320-2.rbl"))

    def test_each_esphome_version_says_the_release_it_is_built_on(self):
        pins = [section["esphome"] for family in ("espressif", "beken") for section in PINS[family].values()
                if "esphome" in section]
        for pin in pins:
            self.assertRegex(pin["version"], rf"^{re.escape(pin['esphome'])}-[1-9]\d*$", pin["file"])
            self.assertTrue(pin["file"].endswith(f"-{pin['version']}{Path(pin['file']).suffix}"), pin["file"])

    def test_openbekens_release_is_the_latest_at_or_before_the_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)

            def commit(message):
                uf.git("-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false",
                       "commit", "--quiet", "--allow-empty", "-m", message, cwd=source)

            uf.git("init", "--quiet", cwd=source)
            commit("a release")
            uf.git("-c", "tag.gpgsign=false", "tag", "1.18.320", cwd=source)
            self.assertEqual(uf.openbeken_release(source), "1.18.320")
            commit("after it")
            self.assertEqual(uf.openbeken_release(source), "1.18.320")

    def test_no_older_build_is_left_in_artifacts(self):
        published = {Path(pin["file"]).name for section in PINS.get("beken", {}).values() for pin in section.values()}
        for path in (ROOT / "artifacts").glob("*.rbl"):
            self.assertIn(path.name, published)

    def test_the_docs_name_each_file(self):
        readme = (ROOT / "artifacts/README.md").read_text(encoding="utf-8")
        notices = (ROOT / "NOTICES.md").read_text(encoding="utf-8")
        for device, section in PINS.get("beken", {}).items():
            for key, pin in section.items():
                row = next((line for line in readme.splitlines()
                            if line.startswith(f"| `{Path(pin['file']).name}` |")), "")
                self.assertIn(f"`{pin['sha256']}`", row, pin["file"])
                self.assertIn(f"`{pin['file']}`", notices)
                if key == "openbeken":
                    self.assertIn(pin["app_commit"], notices)
                else:
                    self.assertIn(f"ESPHome {pin['esphome']}", notices)
                    self.assertIn(f"LibreTiny {pin['libretiny']}", notices)
                    esphome_readme = (ROOT / f"devices/{device}/esphome.md").read_text(encoding="utf-8")
                    self.assertIn(f"ESPHome {pin['esphome']} built the current", esphome_readme)


class OpenBekenModuleTests(unittest.TestCase):
    MAKEFILE = "OBKM_SRC  += $(OBK_SRCS)driver/drv_main.c\nOBKM_SRC  += $(OBK_SRCS)driver/drv_sdcard.c\n"
    MAIN = "\tbSafeMode = 1;\n\tCFG_InitAndLoad();\n\n#if ENABLE_LITTLEFS\n"
    MODULE = ('void Autoflash_Init(void) {\n'
              '\tCMD_RegisterCommand("AutoflashImportWiFi", Cmd_Import, NULL);\n'
              '\tCMD_RegisterCommand("AutoflashRevertToStock", Cmd_Revert, NULL);\n}\n')

    def test_the_driver_and_table_are_built_and_started_after_the_config_is_loaded(self):
        makefile, main = uf.with_module(self.MAKEFILE, self.MAIN, ["drv_autoflash.c", "drv_autoflash_sonoff_s61.c"],
                                        "Autoflash_Init")
        self.assertEqual(makefile, "OBKM_SRC  += $(OBK_SRCS)driver/drv_main.c\n"
                                   "OBKM_SRC  += $(OBK_SRCS)driver/drv_autoflash.c\n"
                                   "OBKM_SRC  += $(OBK_SRCS)driver/drv_autoflash_sonoff_s61.c\n"
                                   "OBKM_SRC  += $(OBK_SRCS)driver/drv_sdcard.c\n")
        self.assertEqual(main, "\tbSafeMode = 1;\n\tCFG_InitAndLoad();\n\n"
                               "\textern void Autoflash_Init(void);\n\tAutoflash_Init();\n"
                               "\n#if ENABLE_LITTLEFS\n")

    def test_a_device_is_built_with_the_driver_and_its_own_table(self):
        self.assertEqual(uf.openbeken_sources("sonoff-s61"),
                         ["tools/beken/openbeken/drv_autoflash.c", "tools/beken/openbeken/drv_autoflash.h",
                          "devices/sonoff-s61/drv_autoflash_sonoff_s61.c"])
        for path in uf.openbeken_sources("sonoff-s61"):
            self.assertTrue((ROOT / path).is_file(), path)

    def test_an_upstream_without_the_anchors_is_refused(self):
        with self.assertRaisesRegex(uf.UpdateError, "drv_main.c"):
            uf.with_module("OBKM_SRC =\n", self.MAIN, ["m.c"], "M_Init")
        with self.assertRaisesRegex(uf.UpdateError, "CFG_InitAndLoad"):
            uf.with_module(self.MAKEFILE, self.MAIN * 2, ["m.c"], "M_Init")

    def test_the_modules_start_and_commands_are_read_from_its_source(self):
        self.assertEqual(uf.module_init(self.MODULE), "Autoflash_Init")
        self.assertEqual(uf.module_commands(self.MODULE), ["AutoflashImportWiFi", "AutoflashRevertToStock"])
        with self.assertRaises(uf.UpdateError):
            uf.module_init("static void helper(void) {}\n")
        # The driver, as published: its start, and the command the tool reverts with.
        driver = (uf.OPENBEKEN_DIR / uf.OPENBEKEN_DRIVER[0]).read_text(encoding="utf-8")
        self.assertEqual(uf.module_init(driver), "Autoflash_Init")
        self.assertIn(kinds.SONOFF_S61.openbeken_revert, uf.module_commands(driver))

    def test_the_driver_names_no_device(self):
        # What is one device's is in its table: the driver and its header hold
        # no Beken kind's names and none of a table's addresses.
        for name in uf.OPENBEKEN_DRIVER:
            driver = (uf.OPENBEKEN_DIR / name).read_text(encoding="utf-8").lower()
            for kind in kinds.ALL:
                if kind.family != "beken":
                    continue
                table = (ROOT / uf.BEKEN_BUILDS[kind.key]["openbeken"][0]).read_text(encoding="utf-8")
                for word in (kind.key, kind.short, kind.name, kind.stock_model, *re.findall(r"0x[0-9a-f]+", table)):
                    self.assertNotIn(word.lower(), driver, (name, word))

    def test_an_image_is_checked_against_what_it_was_to_hold(self):
        image = rbl.pack(b"firmware with AutoflashImportWiFi in it", version="1.0.0")
        self.assertEqual(uf.check_rbl(image, "it", "1.0.0", [b"AutoflashImportWiFi"]),
                         b"firmware with AutoflashImportWiFi in it")
        with self.assertRaisesRegex(uf.UpdateError, "1.0.1"):
            uf.check_rbl(image, "it", "1.0.1", [])
        with self.assertRaisesRegex(uf.UpdateError, "AutoflashRevertToStock"):
            uf.check_rbl(image, "it", None, [b"AutoflashRevertToStock"])
        with self.assertRaisesRegex(uf.UpdateError, "not an rt_ota image"):
            uf.check_rbl(b"not an image", "it", None, [])
        with self.assertRaisesRegex(uf.UpdateError, "not an app"):
            uf.check_rbl(rbl.pack(b"x", name="bootloader", version="1"), "it", None, [])


class UpdateHelperTests(unittest.TestCase):
    CONFIG = ('esphome:\n  name: esphome\n  project:\n    name: liberator.sonoff-s60\n'
              '    version: "1.2.0"\n  on_boot:\n    - priority: 1000\n'
              'ota:\n  - platform: esphome\n    version: 2\n')

    def test_a_build_says_one_build_time(self):
        self.assertEqual(uf.build_time(b"x 2026-11-04 14:38:48 +0000 y", "it"), "2026-11-04 14:38:48 +0000")
        for image in (b"no time", b"2026-11-04 14:38:48 +0000 and 2026-11-04 14:38:49 +0000"):
            with self.assertRaisesRegex(uf.UpdateError, "build times"):
                uf.build_time(image, "it")

    def test_an_images_earlier_builds_keep_each_earlier_version(self):
        old = {"version": "1.5.0", "built": "2026-01-02 00:00:00 +0000", "earlier_builds": ["2026-01-01 00:00:00 +0000"]}
        image = b"2026-01-03 00:00:00 +0000"
        self.assertEqual(uf.builds_record(image, "it", old, "1.6.0"),
                         {"built": "2026-01-03 00:00:00 +0000",
                          "earlier_builds": ["2026-01-01 00:00:00 +0000", "2026-01-02 00:00:00 +0000"]})
        # A rebuild of the same version replaces its build: the one before was never released.
        self.assertEqual(uf.builds_record(image, "it", old, "1.5.0")["earlier_builds"], ["2026-01-01 00:00:00 +0000"])

    def test_the_build_times_pinned_are_the_images_own(self):
        pins = json.loads((ROOT / "artifacts/firmware.json").read_text(encoding="utf-8"))
        s60, s61 = pins["espressif"]["sonoff-s60"]["esphome"], pins["beken"]["sonoff-s61"]["esphome"]
        self.assertEqual(s60["built"], uf.build_time((ROOT / s60["file"]).read_bytes(), s60["file"]))
        self.assertEqual(s61["built"], uf.build_time(rbl.unpack((ROOT / s61["file"]).read_bytes()), s61["file"]))

    def test_the_project_version_and_only_it_is_changed(self):
        changed = uf.with_config_version(self.CONFIG, "1.3.0")
        self.assertEqual(uf.config_version(changed), "1.3.0")
        self.assertEqual(changed, self.CONFIG.replace('"1.2.0"', '"1.3.0"'))

    def test_a_config_without_a_project_version_is_refused(self):
        with self.assertRaises(uf.UpdateError):
            uf.with_config_version("esphome:\n  name: esphome\n", "1.3.0")

    def test_the_next_image_version_is_the_next_minor(self):
        self.assertEqual(uf.next_minor("1.2.0"), "1.3.0")
        self.assertEqual(uf.next_minor("1.9.4"), "1.10.0")

    def test_a_published_files_line_takes_the_old_ones_place(self):
        sums = "a" * 64 + "  artifacts/one.bin\n" + "b" * 64 + "  artifacts/old-1.0.0.bin\n" + "c" * 64 + "  x\n"
        updated = uf.replace_sums_line(sums, "artifacts/old-1.0.0.bin", "artifacts/old-1.1.0.bin", b"new")
        self.assertEqual(updated.splitlines(), [
            "a" * 64 + "  artifacts/one.bin",
            sha256_bytes(b"new") + "  artifacts/old-1.1.0.bin",
            "c" * 64 + "  x",
        ])
        added = uf.replace_sums_line(sums, None, "artifacts/new.bin", b"new")
        self.assertEqual(added.splitlines()[-1], sha256_bytes(b"new") + "  artifacts/new.bin")
        # Built again under its own name: in its own place.
        again = uf.replace_sums_line(sums, None, "artifacts/one.bin", b"new")
        self.assertEqual(again.splitlines()[0], sha256_bytes(b"new") + "  artifacts/one.bin")
        self.assertEqual(len(again.splitlines()), 3)

    def test_the_new_file_is_let_in_where_the_old_one_was(self):
        ignore = "*.bin\n!artifacts/a-1.0.0.bin\n!artifacts/b.bin\n\n.env\n"
        self.assertEqual(uf.with_ignore_exception(ignore, "artifacts/a-1.0.0.bin", "artifacts/a-1.1.0.bin"),
                         "*.bin\n!artifacts/a-1.1.0.bin\n!artifacts/b.bin\n\n.env\n")
        self.assertEqual(uf.with_ignore_exception(ignore, "artifacts/gone.bin", "artifacts/c.bin"),
                         "*.bin\n!artifacts/a-1.0.0.bin\n!artifacts/b.bin\n!artifacts/c.bin\n\n.env\n")

    def test_each_published_file_is_let_past_gitignore(self):
        lines = (ROOT / ".gitignore").read_text().splitlines()
        for pin in S60_ESPHOME.values():
            self.assertIn(f"!{pin['file']}", lines)

    def test_cfg_holder_is_read_from_tasmotas_config(self):
        text = "// CFG_HOLDER below\n#define CFG_HOLDER             4617              // [Reset 1]\n"
        self.assertEqual(uf.cfg_holder(text), 4617)
        with self.assertRaises(uf.UpdateError):
            uf.cfg_holder("#define OTHER 1\n")

    def test_a_factory_image_with_another_layout_is_refused(self):
        blank = bytes(0x400000)
        with self.assertRaisesRegex(uf.UpdateError, "partition table"):
            uf.check_tasmota_files({"factory": blank, "safeboot": b"", "app": b""})

    def test_a_factory_image_on_tasmotas_own_table_is_the_layout_the_conversion_writes(self):
        # The conversion adds the entry for the device's Sonoff identity to it
        # (safeboot_migration.target_table); a factory image already carrying
        # that, or any other, is not Tasmota's.
        from tests.espressif.safeboot.test_safeboot_migration import partition_sector
        from tools.espressif.safeboot import safeboot_migration as migration
        for table, refused in ((partition_sector(migration.OFFICIAL_PARTITIONS), "Safeboot"),
                               (partition_sector(migration.TARGET_PARTITIONS), "partition table")):
            factory = bytearray(0x400000)
            factory[0x8000:0x9000] = table
            with self.subTest(refused=refused), self.assertRaisesRegex(uf.UpdateError, refused):
                uf.check_tasmota_files({"factory": bytes(factory), "safeboot": b"x", "app": b""})


class ESPHomeDocTests(unittest.TestCase):
    """What an ESPHome update writes, from the image published now to a made-up one."""

    def texts(self, esphome):
        from unittest import mock
        old = uf.load_pins()["espressif"]["sonoff-s60"]["esphome"]
        new = {"file": "artifacts/esphome-sonoff-s60-imprintable-9.9.0.bin", "version": "9.9.0",
               "esphome": esphome, "esp_idf": "5.5.9", "sha256": "0" * 64}
        with mock.patch.object(uf, "released", return_value=True):
            texts, _version = uf.esphome_doc_texts(old, new)
        return old, texts

    def test_a_new_release_builds_the_same_config(self):
        _, texts = self.texts("2099.1.0")
        self.assertIn("ESPHome for the S60 moved to [2099.1.0]", " ".join(texts[uf.CHANGELOG].split()))

    def test_the_same_release_builds_a_changed_config(self):
        _, texts = self.texts(uf.load_pins()["espressif"]["sonoff-s60"]["esphome"]["esphome"])
        self.assertIn("ESPHome for the S60 rebuilt with [", " ".join(texts[uf.CHANGELOG].split()))

    def test_the_docs_say_what_it_is_and_not_how_it_got_there(self):
        old, texts = self.texts("2099.1.0")
        readme = texts[uf.ARTIFACTS_README]
        self.assertIn("esphome-sonoff-s60-imprintable-9.9.0.bin", readme)
        self.assertNotIn(old["file"], readme)
        self.assertEqual(readme.count("\n\n"), uf.ARTIFACTS_README.read_text(encoding="utf-8").count("\n\n"))


class BekenESPHomeDocTests(unittest.TestCase):
    """What an update of a Beken device's ESPHome writes, to a made-up one."""

    def setUp(self):
        from unittest import mock
        self.old = uf.load_pins()["beken"]["sonoff-s61"]["esphome"]
        self.new = {"file": "artifacts/esphome-sonoff-s61-2099.1.0-1.rbl", "version": "2099.1.0-1",
                    "esphome": "2099.1.0", "libretiny": "9.9.9", "sha256": "0" * 64}
        with mock.patch.object(uf, "released", return_value=True):
            self.texts, _version = uf.beken_esphome_doc_texts("sonoff-s61", self.old, self.new)

    def test_each_doc_says_the_new_image(self):
        readme = self.texts[uf.ARTIFACTS_README]
        self.assertIn("| `esphome-sonoff-s61-2099.1.0-1.rbl` |", readme)
        self.assertIn(f"`{self.new['sha256']}`", readme)
        self.assertNotIn(Path(self.old["file"]).name, readme)
        self.assertIn("built with ESPHome 2099.1.0 on LibreTiny 9.9.9", readme)
        page = self.texts[uf.ROOT / "devices/sonoff-s61/esphome.md"]
        self.assertIn(f"ESPHome 2099.1.0 built the current image, `{self.new['file']}`,", page)
        self.assertIn("update_firmware.py esphome 2099.1.0 --device sonoff-s61", page)
        self.assertIn("ESPHome for the Sonoff S61 moved to [2099.1.0]", " ".join(self.texts[uf.CHANGELOG].split()))

    def test_the_notices_change_the_s61s_paragraph_and_not_the_s60s(self):
        notices = self.texts[uf.NOTICES]
        self.assertIn(f"`{self.new['file']}` is built with ESPHome 2099.1.0 and\nLibreTiny 9.9.9", notices)
        self.assertIn("[ESPHome 2099.1.0 release](https://github.com/esphome/esphome/tree/2099.1.0)", notices)
        self.assertIn("[LibreTiny 9.9.9](https://github.com/libretiny-eu/libretiny/tree/v9.9.9)", notices)
        s60 = uf.load_pins()["espressif"]["sonoff-s60"]["esphome"]["esphome"]
        self.assertIn(f"[ESPHome {s60} release]", notices.split(self.new["file"])[0])


def docker_there(test):
    """The commands that need Docker are all stood in for: whether this
    computer has it is not the test's to depend on."""
    from unittest import mock
    which = uf.shutil.which
    patch = mock.patch.object(uf.shutil, "which",
                              side_effect=lambda name, *args, **kwargs: "/usr/bin/docker" if name == "docker"
                              else which(name, *args, **kwargs))
    patch.start()
    test.addCleanup(patch.stop)


class EveryDeviceUpdatedTests(unittest.TestCase):
    """The weekly update leaves no device behind, a device added included."""

    def setUp(self):
        docker_there(self)

    def test_every_device_has_its_esphome_built(self):
        for kind in kinds.READY:
            self.assertIsNotNone(uf.esphome_command(kind), kind.key)

    def test_every_devices_esphome_is_pinned_where_its_command_writes_it(self):
        for kind in kinds.READY:
            command = uf.esphome_command(kind)
            pin = PINS["beken" if command is uf.command_esphome_beken else "espressif"][kind.key]["esphome"]
            self.assertTrue((ROOT / pin["file"]).is_file(), kind.key)

    def test_every_espressif_chip_takes_each_tasmota_release(self):
        for kind in kinds.READY:
            if kind.chip.startswith("esp32") and kind.chip != "esp32c3":
                self.assertIn(kind.chip, uf.OTHER_CHIPS_TASMOTA, kind.key)
                self.assertIn("tasmota", ESPRESSIF[kind.chip], kind.key)

    def test_every_device_is_built_by_one_run(self):
        from unittest import mock
        ran = []
        commands = {name: mock.Mock(side_effect=lambda args, name=name: ran.append((name, args.device)) or 0)
                    for name in ("command_esphome", "command_esphome_beken", "command_esphome_kind")}
        with mock.patch.multiple(uf, **commands):
            self.assertEqual(uf.main(["esphome", "2099.1.0", "--every-device", "--no-docs"]), 0)
        self.assertEqual({device or "sonoff-s60" for _, device in ran}, {kind.key for kind in kinds.READY})

    def test_every_device_gets_one_changelog_line_and_none_of_its_own(self):
        from unittest import mock
        seen, lines = [], []
        commands = {name: mock.Mock(side_effect=lambda args: seen.append(args.every) or 0)
                    for name in ("command_esphome", "command_esphome_beken", "command_esphome_kind")}
        with mock.patch.multiple(uf, **commands), \
                mock.patch.object(uf, "stable_release", return_value={"tag_name": "2099.1.0"}), \
                mock.patch.object(uf, "changelog_with", side_effect=lambda entry, texts: lines.append(entry) or "9.9.0"), \
                mock.patch.object(uf, "built_versions", side_effect=[{"a": "2026.10.0-2"}, {"a": "2099.1.0-1"}]), \
                mock.patch.object(uf, "write_texts"):
            self.assertEqual(uf.main(["esphome", "2099.1.0", "--every-device"]), 0)
        self.assertEqual(seen, [True] * len(kinds.READY))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("ESPHome moved to [2099.1.0]"), lines[0])

    def test_each_firmwares_line_is_the_one_form(self):
        self.assertEqual(uf.moved("Tasmota", "16.1.0", "16.0.0"),
                         "Tasmota moved to [16.1.0](https://github.com/arendst/Tasmota/releases/tag/v16.1.0), "
                         "from [16.0.0](https://github.com/arendst/Tasmota/releases/tag/v16.0.0).")
        self.assertEqual(uf.moved("ESPHome", "2026.11.2", "2026.10.0"),
                         "ESPHome moved to [2026.11.2](https://github.com/esphome/esphome/releases/tag/2026.11.2), "
                         "from [2026.10.0](https://github.com/esphome/esphome/releases/tag/2026.10.0).")
        self.assertTrue(uf.moved("OpenBeken", "1.18.321", "1.18.320").startswith(
            "OpenBeken moved to [1.18.321](https://github.com/openshwprojects/OpenBK7231T_App/releases/tag/1.18.321)"))

    def test_one_devices_line_names_it_and_a_rebuild_says_so(self):
        self.assertTrue(uf.moved("ESPHome", "2026.11.2", "2026.10.0", "Sonoff S61").startswith("ESPHome for the Sonoff S61 moved to"))
        self.assertTrue(uf.moved("ESPHome", "2026.10.0", "2026.10.0").startswith("ESPHome rebuilt with [2026.10.0]"))

    def test_tasmotas_line_is_that_form(self):
        from unittest import mock
        with mock.patch.object(uf, "released", return_value=True):
            tasmota = uf.tasmota_doc_texts("16.0.0", "16.1.0")[0][uf.CHANGELOG]
        self.assertIn("Tasmota moved to [16.1.0]", " ".join(tasmota.split()))

    def test_a_device_built_as_one_of_every_device_writes_no_line_of_its_own(self):
        from unittest import mock
        old = uf.load_pins()["beken"]["sonoff-s61"]["esphome"]
        new = {"file": "artifacts/esphome-sonoff-s61-2099.1.0-1.rbl", "version": "2099.1.0-1",
               "esphome": "2099.1.0", "libretiny": "9.9.9", "sha256": "0" * 64}
        with mock.patch.object(uf, "released", return_value=True):
            texts, version = uf.beken_esphome_doc_texts("sonoff-s61", old, new, changelog=False)
            s60, s60_version = uf.esphome_doc_texts(uf.load_pins()["espressif"]["sonoff-s60"]["esphome"],
                                                    {**new, "file": "artifacts/esphome-sonoff-s60-imprintable-9.9.0.bin",
                                                     "esp_idf": "5.5.9"}, changelog=False)
        self.assertNotIn(uf.CHANGELOG, texts)
        self.assertNotIn(uf.CHANGELOG, s60)
        self.assertIsNone(version)
        self.assertIsNone(s60_version)

    def test_it_is_not_asked_for_one_device(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            uf.main(["esphome", "--every-device", "--device", "sonoff-s61"])


class OpenBekenRefTests(unittest.TestCase):
    """OpenBeken moves only to a stable release."""

    def stable(self, tag="1.18.321"):
        from unittest import mock
        return mock.patch.object(uf, "stable_release", return_value={"tag_name": tag})

    def test_a_release_asked_for_is_checked_with_its_project(self):
        with self.stable() as stable:
            self.assertEqual(uf.openbeken_ref("1.18.321", {"app_commit": "c" * 40}), "1.18.321")
        stable.assert_called_once_with(uf.OPENBEKEN_REPO, "1.18.321")

    def test_a_tag_not_published_as_stable_is_refused(self):
        from unittest import mock
        with mock.patch.object(uf, "stable_release", side_effect=uf.UpdateError("not a stable release")):
            with self.assertRaises(uf.UpdateError):
                uf.openbeken_ref("1.18.999-beta", {"app_commit": "c" * 40})

    def test_with_none_asked_for_it_stays_on_the_commit_pinned(self):
        with self.stable() as stable:
            self.assertEqual(uf.openbeken_ref(None, {"app_commit": "c" * 40}), "c" * 40)
        stable.assert_not_called()

    def test_with_nothing_pinned_it_starts_on_the_latest_release(self):
        with self.stable():
            self.assertEqual(uf.openbeken_ref(None, {}), "1.18.321")

    def test_a_commit_cannot_be_asked_for(self):
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            uf.main(["openbeken", "--commit", "c" * 40])


class OpenBekenPinnedTests(unittest.TestCase):
    def test_the_oldest_beken_devices_build_is_the_one_compared(self):
        pins = {"beken": {"a": {"openbeken": {"version": "1.18.321-1", "release": "1.18.321"}},
                          "b": {"openbeken": {"version": "1.18.99-2", "release": "1.18.99"}},
                          "c": {"esphome": {}}}}
        self.assertEqual(uf.openbeken_pinned(pins), "1.18.99")

    def test_every_beken_device_now(self):
        self.assertTrue(uf.openbeken_pinned(PINS))


class OpenBekenDueTests(unittest.TestCase):
    """OpenBeken taken by the settled-release rule: each release a series of its own."""

    @staticmethod
    def releases(*pairs):
        import datetime as dt
        return [{"tag": tag, "published": dt.datetime.fromisoformat(day + "T12:00:00+00:00")} for tag, day in pairs]

    @staticmethod
    def on(day):
        import datetime as dt
        return dt.datetime.fromisoformat(day + "T13:00:00+00:00")

    def due(self, history, day, pinned="1.18.320"):
        return uf.due("openbeken", history, self.on(day), pinned, [])["due"]

    def test_a_release_with_none_after_it_waits_fourteen_days(self):
        history = self.releases(("1.18.320", "2026-10-28"), ("1.18.321", "2026-11-05"))
        self.assertIsNone(self.due(history, "2026-11-18"))
        self.assertEqual(self.due(history, "2026-11-19"), "1.18.321")

    def test_one_a_newer_release_has_followed_waits_seven(self):
        history = self.releases(("1.18.320", "2026-10-28"), ("1.18.321", "2026-11-05"), ("1.18.322", "2026-11-09"))
        self.assertIsNone(self.due(history, "2026-11-11"))
        self.assertEqual(self.due(history, "2026-11-12"), "1.18.321")

    def test_the_build_on_the_latest_takes_nothing(self):
        self.assertIsNone(self.due(self.releases(("1.18.321", "2026-11-05")), "2026-12-01", pinned="1.18.321"))

    def test_a_tag_that_is_not_a_release_number_is_passed_over(self):
        history = self.releases(("1.18.321", "2026-11-05"), ("nightly", "2026-11-06"))
        self.assertEqual(self.due(history, "2026-11-19"), "1.18.321")

    def test_its_pin_is_the_oldest_beken_devices_build(self):
        self.assertEqual(uf.pinned_version(PINS, "openbeken"), uf.openbeken_pinned(PINS))
        self.assertIn("openbeken", uf.PROJECTS)


class OpenBekenEveryDeviceTests(unittest.TestCase):
    def setUp(self):
        docker_there(self)

    def test_every_beken_device_is_built_on_the_one_release(self):
        from unittest import mock
        built = []
        with mock.patch.object(uf, "stable_release", return_value={"tag_name": "1.18.321"}), \
                mock.patch.object(uf, "command_openbeken",
                                  side_effect=lambda args: built.append((args.device, args.version)) or 0):
            self.assertEqual(uf.main(["openbeken", "1.18.321", "--every-device", "--dry-run"]), 0)
        self.assertEqual(built, [(device, "1.18.321") for device in uf.BEKEN_BUILDS])

    def test_each_projects_version_is_given_alike(self):
        from unittest import mock
        for project, version in (("tasmota", "16.0.0"), ("esphome", "2026.10.0"), ("openbeken", "1.18.321")):
            seen = []
            with mock.patch.object(uf, f"command_{project}", side_effect=lambda args: seen.append(args.version) or 0), \
                    mock.patch.object(uf.shutil, "which", return_value="/usr/bin/docker"):
                self.assertEqual(uf.main([project, version]), 0)
            self.assertEqual(seen, [version], project)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            uf.main(["openbeken", "--release", "1.18.321"])

    def test_every_beken_device_has_its_openbeken_pinned(self):
        for device in uf.BEKEN_BUILDS:
            self.assertIn("release", PINS["beken"][device]["openbeken"], device)


class OpenBekenDocTests(unittest.TestCase):
    """What a new OpenBeken writes, to a made-up one."""

    def setUp(self):
        from unittest import mock
        self.old = uf.load_pins()["beken"]["sonoff-s61"]["openbeken"]
        self.new = {"file": "artifacts/openbeken-sonoff-s61-1.18.999-1.rbl", "version": "1.18.999-1",
                    "release": "1.18.999", "app_commit": "a" * 40, "sdk_commit": "b" * 40, "sha256": "0" * 64}
        with mock.patch.object(uf, "released", return_value=True):
            self.texts = uf.openbeken_doc_texts("sonoff-s61", self.old, self.new)

    def test_its_row_says_the_new_file(self):
        readme = self.texts[uf.ARTIFACTS_README]
        self.assertIn("| `openbeken-sonoff-s61-1.18.999-1.rbl` |", readme)
        self.assertNotIn(Path(self.old["file"]).name, readme)
        self.assertIn(f"`{self.new['sha256']}`", readme)

    def test_the_notices_name_its_file_and_both_commits(self):
        notices = self.texts[uf.NOTICES]
        self.assertIn(f"`{self.new['file']}`", notices)
        self.assertNotIn(self.old["app_commit"], notices)
        self.assertIn("a" * 40, notices)
        self.assertIn("b" * 40, notices)

    def test_the_changelog_says_the_release(self):
        self.assertIn("OpenBeken for the Sonoff S61 moved to [1.18.999]", " ".join(self.texts[uf.CHANGELOG].split()))

    def test_for_every_device_the_changelog_is_left_to_the_caller(self):
        from unittest import mock
        with mock.patch.object(uf, "released", return_value=True):
            texts = uf.openbeken_doc_texts("sonoff-s61", self.old, self.new, changelog=False)
        self.assertNotIn(uf.CHANGELOG, texts)


class WorkflowTests(unittest.TestCase):
    """The weekly update, as the workflow is written: GitHub alone can run it."""

    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/firmware-update.yml").read_text(encoding="utf-8")

    def test_every_devices_esphome_moves_together(self):
        self.assertIn('update_firmware.py esphome "$ESPHOME" --every-device', self.workflow)

    def test_every_devices_emulator_check_runs_on_the_update(self):
        # Started on its branch, and required by main before the merge job merges it.
        self.assertRegex(self.workflow, r"for workflow in [^;]*emulator-check\.yml")
        from tools.repo import github_settings
        self.assertIn("emulator", github_settings.REQUIRED_CHECKS)

    def test_a_settled_openbeken_is_built_for_every_beken_device(self):
        self.assertIn('["openbeken"]["due"]', self.workflow)
        self.assertIn('update_firmware.py openbeken "$OPENBEKEN" --every-device', self.workflow)

    def test_no_openbeken_merges_without_its_six_scenarios(self):
        self.assertIn("fault_suite.py list openbeken", self.workflow)
        self.assertRegex(self.workflow, r'passed=.*"\$SCENARIOS" != failure')


class SettledReleaseTests(unittest.TestCase):
    """The settled-release rule (update_firmware.SETTLE_DAYS), on made-up histories."""

    @staticmethod
    def releases(*pairs):
        import datetime as dt
        return [{"tag": tag, "published": dt.datetime.fromisoformat(day + "T12:00:00+00:00")} for tag, day in pairs]

    @staticmethod
    def on(day):
        import datetime as dt
        return dt.datetime.fromisoformat(day + "T13:00:00+00:00")

    def test_tasmota_waits_fourteen_days(self):
        history = self.releases(("v15.6.0", "2026-08-25"), ("v16.0.0", "2026-10-16"))
        due = lambda day: uf.due("tasmota", history, self.on(day), "15.6.0", [])["due"]  # noqa: E731
        self.assertIsNone(due("2026-10-29"))
        self.assertEqual(due("2026-10-30"), "16.0.0")

    def test_a_fix_release_starts_the_wait_again(self):
        history = self.releases(("v15.6.0", "2026-08-25"), ("v16.0.0", "2026-10-16"), ("v16.0.1", "2026-10-27"))
        self.assertIsNone(uf.due("tasmota", history, self.on("2026-11-05"), "15.6.0", [])["due"])
        self.assertEqual(uf.due("tasmota", history, self.on("2026-11-10"), "15.6.0", [])["due"], "16.0.1")

    def test_esphome_takes_last_months_last_point_release_once_this_months_is_out(self):
        history = self.releases(("2026.9.0", "2026-09-16"), ("2026.9.1", "2026-09-29"),
                                ("2026.10.0", "2026-10-21"), ("2026.10.1", "2026-10-30"),
                                ("2026.11.0", "2026-11-18"))
        due = lambda day: uf.due("esphome", history, self.on(day), "2026.9.1", [])["due"]  # noqa: E731
        self.assertIsNone(due("2026-11-10"))                     # 2026.10.1 only 11 days old, no 2026.11 yet
        self.assertEqual(due("2026-11-13"), "2026.10.1")         # 14 days old, before 2026.11.0
        self.assertEqual(due("2026-11-18"), "2026.10.1")         # 2026.11.0 out, and never itself so soon

    def test_a_series_followed_by_a_newer_one_still_waits_seven_days(self):
        history = self.releases(("2026.9.1", "2026-09-29"), ("2026.10.0", "2026-10-21"),
                                ("2026.10.1", "2026-10-24"), ("2026.11.0", "2026-10-26"))
        self.assertIsNone(uf.due("esphome", history, self.on("2026-10-29"), "2026.9.1", [])["due"])
        self.assertEqual(uf.due("esphome", history, self.on("2026-10-31"), "2026.9.1", [])["due"], "2026.10.1")

    def test_a_release_on_hold_is_never_taken(self):
        history = self.releases(("v15.6.0", "2026-08-25"), ("v16.0.0", "2026-10-16"))
        self.assertIsNone(uf.due("tasmota", history, self.on("2026-12-01"), "15.6.0", ["16.0.0"])["due"])

    def test_nothing_older_than_the_pin_is_ever_due(self):
        history = self.releases(("2026.9.1", "2026-09-29"), ("2026.10.0", "2026-10-21"))
        self.assertIsNone(uf.due("esphome", history, self.on("2026-12-01"), "2026.10.0", [])["due"])

    def test_a_pinned_release_withdrawn_is_replaced_by_its_fix_at_once(self):
        # As 15.0.0 was: withdrawn the day after, 15.0.1 in its place.
        history = self.releases(("v14.6.0", "2025-04-16"), ("v15.0.1", "2025-06-14"))
        state = uf.due("tasmota", history, self.on("2025-06-15"), "15.0.0", [])
        self.assertTrue(state["pinned_missing"])
        self.assertEqual(state["due"], "15.0.1")


class ChangelogTests(unittest.TestCase):
    """An update's line goes under the release to come: merging it is that release."""

    def write(self, directory, changelog, version):
        from unittest import mock
        changelog_path, version_path = Path(directory) / "CHANGELOG.md", Path(directory) / "__init__.py"
        changelog_path.write_text(changelog)
        version_path.write_text(f'"""x"""\n__version__ = "{version}"\n')
        return mock.patch.multiple(uf, CHANGELOG=changelog_path, VERSION_FILE=version_path)

    def test_after_a_release_a_new_minor_version_is_opened(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, self.write(tmp, "# Changelog\n\nRules.\n\n## 2.1.0\n\n- Old.\n", "2.1.0"), \
             mock.patch.object(uf, "released", return_value=True):
            texts = {}
            self.assertEqual(uf.changelog_with("A converted device ends on Tasmota 16.1.0.", texts), "2.2.0")
            changelog, version = texts[uf.CHANGELOG], texts[uf.VERSION_FILE]
        self.assertIn("## 2.2.0\n\n- A converted device ends on Tasmota 16.1.0.\n\n## 2.1.0\n\n- Old.", changelog)
        self.assertIn('__version__ = "2.2.0"', version)

    def test_before_it_the_line_joins_the_release_to_come(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, \
             self.write(tmp, "# Changelog\n\n## 2.2.0\n\n- First.\n\n## 2.1.0\n\n- Old.\n", "2.2.0"), \
             mock.patch.object(uf, "released", return_value=False):
            texts = {}
            self.assertEqual(uf.changelog_with("Second.", texts), "2.2.0")
            changelog = texts[uf.CHANGELOG]
        self.assertIn("## 2.2.0\n\n- First.\n- Second.\n\n## 2.1.0", changelog)

    def test_a_config_is_adopted_from_the_release_it_comes_out_in(self):
        import tempfile
        from unittest import mock
        config = ('esphome:\n  project:\n    name: x\n    version: "1.0.0"\ndashboard_import:\n'
                  '  package_import_url: github://someone/repo/esphome.yaml@v1.0.0\n  import_full_config: false\n')
        for out_already, tag in ((False, "v2.2.0"), (True, "v2.3.0")):
            with tempfile.TemporaryDirectory() as tmp, self.write(tmp, "# Changelog\n\n## 2.2.0\n\n- First.\n", "2.2.0"), \
                 mock.patch.object(uf, "released", return_value=out_already):
                built = uf.as_built(config, "1.1.0")
            self.assertIn(f"esphome.yaml@{tag}\n", built)
            self.assertIn('version: "1.1.0"', built)

    def test_a_config_no_one_adopts_only_changes_its_version(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, self.write(tmp, "# Changelog\n\n## 2.2.0\n\n- First.\n", "2.2.0"):
            built = uf.as_built('esphome:\n  project:\n    name: x\n    version: "1.0.0"\n', "1.1.0")
        self.assertEqual(built, 'esphome:\n  project:\n    name: x\n    version: "1.1.0"\n')

    def test_a_passage_not_found_changes_nothing(self):
        with self.assertRaisesRegex(uf.UpdateError, "nothing was changed"):
            uf.substituted("no such text", r"ESPHome \S+ built the current", "x", "the release")


def uf_root() -> Path:
    """The real checkout, while UpdateCommandTests point update_firmware at a made-up one."""
    return Path(__file__).resolve().parents[2]


class UpdateCommandTests(unittest.TestCase):
    """update_firmware.py's tasmota and esphome commands, on a made-up checkout
    and releases: what each refuses, and what it writes."""

    CONFIG = 'esphome:\n  name: {name}\n  project:\n    name: x\n    version: "{version}"\n'
    # Where each ESPHome build of the S60's config is pinned: its section, and its name there.
    ESPHOME_PINS = {"image": ("sonoff-s60", "esphome"), "bootloader": ("esp32c3", "bootloader"),
                    "mover": ("esp32c3", "mover")}

    def setUp(self):
        import tempfile
        from unittest import mock
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = root = Path(directory.name)
        (root / "artifacts").mkdir()
        for key, version in (("image", "1.4.0"), ("mover", "1.1.0")):
            (root / uf.ESPHOME_BUILDS[key][0]).parent.mkdir(parents=True)
            (root / uf.ESPHOME_BUILDS[key][0]).write_text(self.CONFIG.format(name=key, version=version),
                                                          encoding="utf-8")
        pins = {"espressif": {"esp32c3": {"tasmota": self.tasmota_pin("15.0.0", earlier=[self.tasmota_pin("14.0.0")])},
                              "sonoff-s60": {}}}
        for key, file, version in (("image", "artifacts/esphome-sonoff-s60-imprintable-1.4.0.bin", "1.4.0"),
                                   ("bootloader", "artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin", "1.2.0"),
                                   ("mover", "artifacts/autoflash-mover-1.1.0.bin", "1.1.0")):
            (root / file).write_bytes(f"{key} {version}".encode())
            owner, name = self.ESPHOME_PINS[key]
            pins["espressif"][owner][name] = {"file": file, "version": version, "esphome": "2026.1.0", "esp_idf": "5.5.5"}
        (root / "artifacts" / "firmware.json").write_text(json.dumps(pins), encoding="utf-8")
        files = [pin["file"] for pin in self.esphome_pins(pins).values()]
        (root / "artifacts" / "SHA256SUMS").write_text("".join(f"{sha256_bytes(b'x')}  {Path(file).name}\n"
                                                               for file in files), encoding="utf-8")
        (root / ".gitignore").write_text("*.bin\n" + "".join(f"!{file}\n" for file in files), encoding="utf-8")
        self.before = self.snapshot()
        for name, value in (("ROOT", root), ("FIRMWARE_JSON", root / "artifacts" / "firmware.json"),
                            ("SHA256SUMS", root / "artifacts" / "SHA256SUMS"), ("GITIGNORE", root / ".gitignore"),
                            ("BUILD_CACHE", root / "build")):
            mock.patch.object(uf, name, value).start()
        mock.patch.object(uf.shutil, "which", return_value="/usr/bin/docker").start()
        # Each build pinned here came out in a release: a new one takes a new version.
        mock.patch.object(uf, "published_by_a_release", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def tasmota_pin(version, earlier=None):
        pin = {"version": version, "tag": f"v{version}", "commit": version * 4,
               "files": {key: {"name": name, "size": 1, "sha256": "0" * 64} for key, name in uf.TASMOTA_FILES.items()}}
        return pin | ({"cfg_holder": tasmota_settings.CFG_HOLDER, "earlier": earlier} if earlier is not None else {})

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def pins(self):
        return json.loads((self.root / "artifacts" / "firmware.json").read_text(encoding="utf-8"))

    def esphome_pins(self, pins=None):
        """The S60's config's ESPHome builds, by what each is."""
        espressif = (pins or self.pins())["espressif"]
        return {key: espressif[owner][name] for key, (owner, name) in self.ESPHOME_PINS.items()}

    def before_pins(self):
        return self.esphome_pins(json.loads(self.before["artifacts/firmware.json"]))

    def run_main(self, *argv):
        import contextlib
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = uf.main(list(argv))
        return code, errors.getvalue()

    # -- the ESP32-C3's bridge

    def test_the_esp32c3_bridge_is_built_wrapped_and_replaces_the_one_before(self):
        from unittest import mock
        from tools.espressif.analyze_vendor_ota import analyze
        from tools.espressif.build_vendor_ota import WRAPPER_SIZE, app_version
        from tools.espressif.chips import ESP32C3
        app = (uf_root() / ESP32C3.pin("bridge")["file"]).read_bytes()[WRAPPER_SIZE:]  # a valid image
        version = app_version(app)
        old = "artifacts/autoflash-bridge-sonoff-s60-0.0.1.ota"
        (self.root / old).write_bytes(b"the one before")
        pins = self.pins()
        pins["espressif"]["esp32c3"]["bridge"] = {"file": old, "version": "0.0.1"}
        (self.root / "artifacts" / "firmware.json").write_text(json.dumps(pins), encoding="utf-8")
        with (self.root / "artifacts" / "SHA256SUMS").open("a", encoding="utf-8") as sums:
            sums.write(f"{sha256_bytes(b'the one before')}  {Path(old).name}\n")
        with (self.root / ".gitignore").open("a", encoding="utf-8") as ignore:
            ignore.write(f"!{old}\n")
        (self.root / "bridge-source").mkdir()

        def build(script, *volumes):
            self.assertIn("pio run -e autoflash-bridge &&", script)
            built = self.root / "cache" / "bridge" / ".pio" / "build" / "autoflash-bridge"
            built.mkdir(parents=True)
            (built / "firmware.bin").write_bytes(app)

        with mock.patch.object(uf, "BRIDGE_SOURCE", self.root / "bridge-source"), \
             mock.patch.object(uf, "FIRMWARE_BUILD_CACHE", self.root / "cache"), \
             mock.patch.object(uf, "in_builder", side_effect=build):
            code, errors = self.run_main("esp32c3", "bridge")
        self.assertEqual(code, 0, errors)

        file = f"artifacts/autoflash-bridge-esp32c3-{version}.ota"
        wrapped = (self.root / file).read_bytes()
        self.assertEqual(wrapped[WRAPPER_SIZE:], app)
        self.assertEqual(analyze(wrapped)["wrapper"]["record_version"], version)
        self.assertFalse((self.root / old).exists())
        pin = self.pins()["espressif"]["esp32c3"]["bridge"]
        self.assertEqual((pin["file"], pin["version"], pin["size"], pin["sha256"]),
                         (file, version, len(wrapped), sha256_bytes(wrapped)))
        sums = (self.root / "artifacts" / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn(f"{sha256_bytes(wrapped)}  {Path(file).name}", sums)
        self.assertNotIn(Path(old).name, sums)
        ignored = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(f"!{file}", ignored)
        self.assertNotIn(old, ignored)

    def test_publishing_copes_with_a_section_where_not_every_entry_is_a_file(self):
        # firmware.json's esp32 section: its tasmota entry has "files", not "file".
        (self.root / "artifacts" / "old.bin").write_bytes(b"old")
        section = {"tasmota": {"version": "1"}, "bridge": {"file": "artifacts/old.bin"}}
        uf.publish(section, "bridge", "artifacts/new.bin", b"new", {})
        self.assertFalse((self.root / "artifacts" / "old.bin").exists())
        self.assertEqual(section["bridge"]["file"], "artifacts/new.bin")

    def test_a_part_of_a_chip_that_does_not_exist_is_refused(self):
        code, errors = self.run_main("esp32c3", "recovery")
        self.assertEqual(code, 1)
        self.assertIn("no such part", errors)

    # -- Tasmota

    def tasmota(self, *argv, version="16.0.0", files=None, sizes=None, holder=tasmota_settings.CFG_HOLDER,
                check=None):
        from unittest import mock
        files = files if files is not None else {name: name.encode() for name in uf.TASMOTA_FILES.values()}
        sizes = sizes or {}
        release = {"tag_name": f"v{version}", "html_url": "https://example.invalid/release",
                   "assets": [{"name": name, "size": sizes.get(name, len(data)), "browser_download_url": name}
                              for name, data in files.items()]}
        downloads = dict(files)
        downloads[f"https://raw.githubusercontent.com/{uf.TASMOTA_REPO}/{'c' * 40}/tasmota/my_user_config.h"] = \
            f"#define CFG_HOLDER {holder}\n".encode()
        with mock.patch.object(uf, "stable_release", return_value=release), \
             mock.patch.object(uf, "tag_commit", return_value="c" * 40), \
             mock.patch.object(uf, "download", side_effect=downloads.__getitem__), \
             mock.patch.object(uf, "check_tasmota_files", side_effect=check) as checked:
            code, errors = self.run_main("tasmota", *argv)
        return code, errors, checked

    def test_tasmota_pins_the_release_and_keeps_the_one_before(self):
        code, _, checked = self.tasmota("--no-docs")
        self.assertEqual(code, 0)
        checked.assert_called_once()
        pin = self.pins()["espressif"]["esp32c3"]["tasmota"]
        self.assertEqual((pin["version"], pin["tag"], pin["commit"]), ("16.0.0", "v16.0.0", "c" * 40))
        self.assertEqual(pin["files"]["app"], {"name": "tasmota32c3.bin", "size": len(b"tasmota32c3.bin"),
                                               "sha256": sha256_bytes(b"tasmota32c3.bin")})
        self.assertEqual([entry["version"] for entry in pin["earlier"]], ["15.0.0", "14.0.0"])
        self.assertNotIn("earlier", pin["earlier"][0])

    def test_tasmota_pinned_again_is_not_kept_as_its_own_earlier_release(self):
        self.assertEqual(self.tasmota("--no-docs", version="15.0.0")[0], 0)
        self.assertEqual([entry["version"] for entry in self.pins()["espressif"]["esp32c3"]["tasmota"]["earlier"]], ["14.0.0"])

    def test_tasmota_going_back_to_an_earlier_release_takes_it_out_of_earlier(self):
        self.assertEqual(self.tasmota("--no-docs", version="14.0.0")[0], 0)
        self.assertEqual([entry["version"] for entry in self.pins()["espressif"]["esp32c3"]["tasmota"]["earlier"]], ["15.0.0"])

    def test_tasmota_dry_run_changes_nothing(self):
        self.assertEqual(self.tasmota("--dry-run")[0], 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_tasmota_refusals_change_nothing(self):
        files = {name: name.encode() for name in uf.TASMOTA_FILES.values()}
        for case, kwargs, said in (
                ("a file short", {"sizes": {"tasmota32c3.bin": 999}}, "came down as"),
                ("a file missing", {"files": {k: v for k, v in files.items() if k != "tasmota32c3.bin"}},
                 "has no tasmota32c3.bin"),
                ("another CFG_HOLDER", {"holder": 1234}, "CFG_HOLDER is 1234")):
            code, errors, _ = self.tasmota("--no-docs", **kwargs)
            self.assertEqual(code, 1, case)
            self.assertIn(said, errors, case)
            self.assertEqual(self.snapshot(), self.before, case)

    def test_tasmota_files_the_conversion_cannot_take_change_nothing(self):
        code, errors, _ = self.tasmota("--no-docs", check=uf.UpdateError("another partition table"))
        self.assertEqual(code, 1)
        self.assertIn("another partition table", errors)
        self.assertEqual(self.snapshot(), self.before)

    # -- ESPHome

    def esphome(self, *argv, bootloader=b"bootloader 1.2.0", check=None):
        from unittest import mock
        built = []

        def build(builder, config_path, version, workdir):
            built.append((config_path.name, version))
            return f"app {config_path.name} {version} 2099-01-01 12:00:00 +0000".encode(), bootloader

        with mock.patch.object(uf, "stable_release", return_value={"tag_name": "2099.1.0"}), \
             mock.patch.object(uf, "esphome_builder", return_value="esphome:2099.1.0@sha256:d"), \
             mock.patch.object(uf, "check_esphome_internals"), \
             mock.patch.object(uf, "build", side_effect=build), \
             mock.patch.object(uf, "check_esphome_app",
                               side_effect=check or (lambda image, label, release: {"esp_idf": "5.5.9"})):
            code, errors = self.run_main("esphome", *argv)
        return code, errors, built

    def test_esphome_publishes_the_image_as_a_build_on_the_release_in_place_of_the_old(self):
        code, _, built = self.esphome("--no-docs")
        self.assertEqual(code, 0)
        self.assertEqual(built, [("esphome.yaml", "2099.1.0-1")])
        new = "artifacts/esphome-sonoff-s60-imprintable-2099.1.0-1.bin"
        pin = self.esphome_pins()["image"]
        self.assertEqual((pin["file"], pin["version"], pin["esphome"], pin["builder"]),
                         (new, "2099.1.0-1", "2099.1.0", "esphome:2099.1.0@sha256:d"))
        self.assertEqual(pin["sha256"], sha256_bytes((self.root / new).read_bytes()))
        self.assertEqual(pin["built"], "2099-01-01 12:00:00 +0000")
        self.assertFalse((self.root / "artifacts/esphome-sonoff-s60-imprintable-1.4.0.bin").exists())
        sums = (self.root / "artifacts" / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn(f"{pin['sha256']}  {Path(new).name}", sums)
        self.assertNotIn("imprintable-1.4.0", sums)
        self.assertIn(f"!{new}\n", (self.root / ".gitignore").read_text(encoding="utf-8"))
        self.assertIn('version: "2099.1.0-1"', (self.root / "devices/sonoff-s60/esphome.yaml").read_text(encoding="utf-8"))
        # Neither the mover nor the bootloader, byte for byte the same, is touched.
        self.assertEqual(self.esphome_pins()["mover"], self.before_pins()["mover"])
        self.assertEqual(self.esphome_pins()["bootloader"]["version"], "1.2.0")

    def test_esphome_builds_the_mover_at_its_own_next_minor(self):
        code, _, built = self.esphome("--no-docs", "--mover")
        self.assertEqual(code, 0)
        self.assertEqual(built, [("esphome.yaml", "2099.1.0-1"), ("mover.yaml", "1.2.0")])
        self.assertEqual(self.esphome_pins()["mover"]["file"], "artifacts/autoflash-mover-esp32c3-1.2.0.bin")

    def test_esphome_keeps_the_published_bootloader_unless_told(self):
        self.assertEqual(self.esphome("--no-docs", bootloader=b"another")[0], 0)
        self.assertEqual(self.esphome_pins()["bootloader"], self.before_pins()["bootloader"])
        self.assertTrue((self.root / "artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin").exists())

    def test_esphome_replaces_a_changed_bootloader_when_told(self):
        self.assertEqual(self.esphome("--no-docs", "--new-bootloader", bootloader=b"another")[0], 0)
        pin = self.esphome_pins()["bootloader"]
        self.assertEqual((pin["file"], pin["sha256"]),
                         ("artifacts/esphome-esp32c3-bootloader-2099.1.0-1.bin", sha256_bytes(b"another")))
        self.assertFalse((self.root / "artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin").exists())

    def test_esphome_dry_run_changes_nothing(self):
        self.assertEqual(self.esphome("--dry-run", "--mover", "--new-bootloader", bootloader=b"another")[0], 0)
        self.assertEqual(self.snapshot(), self.before)

    def test_esphome_a_build_that_fails_its_checks_changes_nothing(self):
        def check(image, label, release):
            raise uf.UpdateError(f"{label} has no untouched Wi-Fi placeholders")
        code, errors, _ = self.esphome("--no-docs", check=check)
        self.assertEqual(code, 1)
        self.assertIn("placeholders", errors)
        self.assertEqual(self.snapshot(), self.before)

    def test_esphome_without_docker_stops_at_once(self):
        from unittest import mock
        with mock.patch.object(uf.shutil, "which", return_value=None):
            code, errors, built = self.esphome("--no-docs")
        self.assertEqual((code, built), (2, []))
        self.assertIn("Docker is needed", errors)


if __name__ == "__main__":
    unittest.main()
