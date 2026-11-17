#!/usr/bin/env python3
"""Build the autoflash bundle from published files, with no flash dump.

The older route froze a full 4 MiB dump of one device into the bundle, but the
migration never reads those frozen copies: preflight reads the partition table,
NVS and otadata live off the device and checks the table against the allow-listed
hash compiled into the tools. So the bundle needs only firmware:

  - the pinned Bluetooth Tasmota image from artifacts/
  - the official tasmota32c3 release files, pinned by commit and SHA-256
  - the published imprintable recovery Safeboot, with your Wi-Fi written in

The bundle can be built before the Wi-Fi is known: the autoflash run does that,
and writes the Wi-Fi in at E1 with `pin_wifi`, once it has read it off the device.

Every pinned-artifact, target-table and imprint-provenance check the migration
server applies is run here too before the manifest is reported as good.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import ssl
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    from tools.autoflash.imprint import ImprintError, imprint, imprinted_ssid
    from tools.autoflash import output
    from tools.autoflash.prompts import QuitRun, ask_secret, ask_text
    from tools.espressif.safeboot.prepare_safeboot_migration import artifact_record, obtain
    from tools.ewelink.get_device_key import INSPECTED
    from tools.espressif.safeboot.safeboot_migration import (
        APP0_OFFSET,
        APP0_SIZE,
        IMPRINTED_MODE,
        OFFICIAL_URLS,
        PUBLISHED_BLUETOOTH_FILE,
        PUBLISHED_RECOVERY_FILE,
        RECOVERY_ARTIFACT,
        SAFEBOOT_OFFSET,
        SAFEBOOT_SIZE,
        TABLE_OFFSET,
        TABLE_SIZE,
        TARGET_PARTITIONS,
        artifact_bytes,
        is_imprinted_manifest,
        load_manifest,
        manifest_chip,
        official_release,
        published_recovery_file,
        published_recovery_sha256,
        release_urls,
        provide_in_memory,
        parse_partition_sector,
        require_exact_partitions,
        require_pinned_artifacts,
        require_recovery_manifest,
        sha256_bytes,
        target_table as target_table_of,
        validate_native_image,
        verify_manifest_files,
    )
except ModuleNotFoundError:  # running from inside tools/
    from autoflash.imprint import ImprintError, imprint, imprinted_ssid
    from autoflash import output
    from autoflash.prompts import QuitRun, ask_secret, ask_text
    from prepare_safeboot_migration import artifact_record, obtain
    from get_device_key import INSPECTED
    from safeboot_migration import (
        APP0_OFFSET,
        APP0_SIZE,
        IMPRINTED_MODE,
        OFFICIAL_URLS,
        PUBLISHED_BLUETOOTH_FILE,
        PUBLISHED_RECOVERY_FILE,
        RECOVERY_ARTIFACT,
        SAFEBOOT_OFFSET,
        SAFEBOOT_SIZE,
        TABLE_OFFSET,
        TABLE_SIZE,
        TARGET_PARTITIONS,
        artifact_bytes,
        is_imprinted_manifest,
        load_manifest,
        manifest_chip,
        official_release,
        published_recovery_file,
        published_recovery_sha256,
        release_urls,
        provide_in_memory,
        parse_partition_sector,
        require_exact_partitions,
        require_pinned_artifacts,
        require_recovery_manifest,
        sha256_bytes,
        target_table as target_table_of,
        validate_native_image,
        verify_manifest_files,
    )

try:
    from tools.espressif import chips
except ModuleNotFoundError:
    import chips  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "captures" / "safeboot-recovery-migration"


RECOVERY_FILE = "tasmota32c3-safeboot-recovery.bin"


def recovery_file(chip: chips.Chip) -> str:
    return f"{chip.tasmota_prefix}-safeboot-recovery.bin"


def private_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if hasattr(os, "fchmod"):  # absent on Windows before Python 3.13
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # On Windows a virus scanner or indexer opening the file just written
        # makes replacing it fail for a moment: wait that out.
        for attempt in range(50):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if sys.platform != "win32" or attempt == 49:
                    raise
                time.sleep(0.1)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class BundleError(ValueError):
    pass


def build_bundle(
    ssid: str | None = None,
    password: str | None = None,
    output: Path = DEFAULT_OUTPUT,
    factory: Path | None = None,
    safeboot: Path | None = None,
    app: Path | None = None,
    release: dict[str, Any] | None = None,
    chip: chips.Chip = chips.ESP32C3,
) -> tuple[Path, bytes | None]:
    """Build and validate the bundle; return the manifest path and recovery image.

    The official files are those of the release pinned now, or of `release`,
    one pinned before (firmware.json's "earlier"): a run past F2 has its app
    on the device, and ends on it.

    The recovery image carries the Wi-Fi password, so it is never written:
    the manifest pins its size and SHA-256, and the caller keeps the bytes in
    memory for as long as the run needs them.  Without `ssid` the bundle is
    complete except for that image, and None is returned for it; `pin_wifi`
    adds it later.  Raises BundleError, leaving no manifest that claims to be
    good, when any check fails.
    """
    try:
        published = published_recovery_file(chip).read_bytes()
    except (OSError, ValueError) as exc:
        raise BundleError(str(exc)) from exc
    if sha256_bytes(published) != published_recovery_sha256(chip):
        raise BundleError("the published recovery image in this checkout has been altered")
    if ssid is not None:
        try:
            imprint(published, ssid, password or "")  # refuse unusable details before downloading
        except ImprintError as exc:
            raise BundleError(str(exc)) from exc

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    manifest_path = output / "manifest.json"
    # A half-built bundle must never look valid: drop the old manifest first.
    manifest_path.unlink(missing_ok=True)

    bluetooth_path = output / chip.interim_name
    factory_path = output / f"{chip.tasmota_prefix}.factory.bin"
    safeboot_path = output / f"{chip.tasmota_prefix}-safeboot.bin"
    app_path = output / f"{chip.tasmota_prefix}.bin"
    urls = release_urls(release or chip.pin("tasmota")) if release or chip is not chips.ESP32C3 else OFFICIAL_URLS
    try:
        # The ESP32-C3's is published here, its source a moving branch; the
        # other chips', a file of the official release, is fetched from it.
        if chip is chips.ESP32C3:
            bluetooth = PUBLISHED_BLUETOOTH_FILE.read_bytes()
            bluetooth_path.write_bytes(bluetooth)
            interim_source = str(PUBLISHED_BLUETOOTH_FILE)
        else:
            interim = chip.pin("interim")
            interim_source = interim["url"]
            bluetooth = obtain(None, interim_source, bluetooth_path)
            if sha256_bytes(bluetooth) != interim["sha256"]:
                raise BundleError(f"{interim_source} is not the file pinned")
        factory_bytes = obtain(factory, urls["factory"], factory_path)
        safeboot_bytes = obtain(safeboot, urls["safeboot"], safeboot_path)
        app_bytes = obtain(app, urls["app"], app_path)
    except OSError as exc:
        if isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError):
            raise BundleError("could not download the official release files: "
                              + INSPECTED.format(what="GitHub's")) from exc
        raise BundleError(f"could not download the official release files: {exc}") from exc

    try:
        bluetooth_report = validate_native_image(bluetooth, 0x1F0000, "Bluetooth", chip)
        safeboot_report = validate_native_image(safeboot_bytes, SAFEBOOT_SIZE, "Safeboot", chip)
        app_report = validate_native_image(app_bytes, APP0_SIZE, "normal Tasmota", chip)
    except ValueError as exc:
        raise BundleError(str(exc)) from exc

    if factory_bytes[SAFEBOOT_OFFSET : SAFEBOOT_OFFSET + len(safeboot_bytes)] != safeboot_bytes:
        raise BundleError("standalone Safeboot differs from the factory image payload")
    if factory_bytes[APP0_OFFSET : APP0_OFFSET + len(app_bytes)] != app_bytes:
        raise BundleError("standalone Tasmota differs from the factory image payload")
    if len(factory_bytes) != APP0_OFFSET + len(app_bytes):
        raise BundleError("factory image has unexpected bytes before/after app0")

    # Tasmota's own table, with the entry for the sector that keeps the
    # device's Sonoff identity (safeboot_migration.target_table).
    try:
        target_table = target_table_of(factory_bytes[TABLE_OFFSET : TABLE_OFFSET + TABLE_SIZE])
    except ValueError as exc:
        raise BundleError(f"the factory image's partition table: {exc}") from exc
    target_report = parse_partition_sector(target_table)
    require_exact_partitions(target_report, TARGET_PARTITIONS)
    target_table_path = output / "target-partition-table.bin"
    target_table_path.write_bytes(target_table)

    # An image from before the password was kept only in memory.
    (output / recovery_file(chip)).unlink(missing_ok=True)

    artifacts: dict[str, Any] = {
        "bluetooth": artifact_record(bluetooth_path.name, bluetooth, interim_source),
        "factory": artifact_record(factory_path.name, factory_bytes, str(factory.resolve()) if factory else urls["factory"]),
        "safeboot": artifact_record(safeboot_path.name, safeboot_bytes, str(safeboot.resolve()) if safeboot else urls["safeboot"]),
        "app": artifact_record(app_path.name, app_bytes, str(app.resolve()) if app else urls["app"]),
        "target_table": artifact_record(target_table_path.name, target_table, str(factory_path)),
    }
    manifest = {
        "schema": 1,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "chip": chip.key,
        "official_release": official_release(release, chip),
        "migration_mode": IMPRINTED_MODE,
        "imprinted_safeboot": {
            "artifact": RECOVERY_ARTIFACT,
            "published_sha256": published_recovery_sha256(chip),
            "ssid": None,  # set by pin_wifi; not secret, the password is never recorded
        },
        "target_table": target_report,
        "images": {
            "bluetooth": bluetooth_report,
            "safeboot": safeboot_report,
            "app": app_report,
        },
        "artifacts": artifacts,
    }
    private_write(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )

    # Re-read the manifest and run the same gates the migration server runs.
    try:
        validate_bundle(manifest_path)
    except ValueError as exc:
        manifest_path.unlink(missing_ok=True)
        raise BundleError(f"generated manifest failed validation: {exc}") from exc
    if ssid is None:
        return manifest_path, None
    try:
        return manifest_path, pin_wifi(manifest_path, ssid, password or "")
    except BundleError:
        manifest_path.unlink(missing_ok=True)
        raise


def pin_wifi(manifest_path: Path, ssid: str, password: str) -> bytes:
    """Write these Wi-Fi details into the startup firmware; return its image.

    The image stays in memory only; the manifest records its size and SHA-256
    and the network name.  Replaces any details pinned before, so a bundle
    built for one network can be set up for another without downloading
    anything again.  On failure the manifest is left as it was.
    """
    manifest = load_manifest(manifest_path)
    chip = manifest_chip(manifest)
    published = published_recovery_file(chip)
    try:
        image = imprint(published.read_bytes(), ssid, password)
    except (OSError, ImprintError) as exc:
        raise BundleError(str(exc)) from exc
    previous = manifest_path.read_bytes()
    manifest["artifacts"][RECOVERY_ARTIFACT] = {
        **artifact_record(recovery_file(chip), image, str(published)),
        "memory_only": True,
    }
    manifest.setdefault("imprinted_safeboot", {})["ssid"] = ssid
    private_write(manifest_path,
                  (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    try:
        validate_bundle(manifest_path, image)
    except ValueError as exc:
        private_write(manifest_path, previous)
        raise BundleError(f"the startup firmware for '{ssid}' failed its checks: {exc}") from exc
    return image


def validate_bundle(manifest_path: Path, recovery: bytes | None = None,
                    current_only: bool = False) -> dict[str, Any]:
    """The gates the migration server applies to a bundle; returns the manifest.

    Without `recovery` - the Wi-Fi not known yet - the memory-only image is
    skipped and everything else is checked. `current_only` refuses a bundle
    of an official release pinned before this one, which the servers take.
    """
    manifest = load_manifest(manifest_path)
    if is_imprinted_manifest(manifest) and \
            manifest.get("imprinted_safeboot", {}).get("published_sha256") != published_recovery_sha256(manifest_chip(manifest)):
        raise ValueError("it was built from a different startup firmware than this checkout ships")
    if recovery is not None:
        provide_in_memory(RECOVERY_ARTIFACT, recovery)
    verify_manifest_files(manifest_path, manifest, allow_unprovided=recovery is None)
    require_pinned_artifacts(manifest, current_only=current_only)
    if recovery is not None or not (is_memory_only(manifest) or awaits_wifi(manifest)):
        require_recovery_manifest(manifest_path, manifest)
    return manifest


def awaits_wifi(manifest: dict[str, Any]) -> bool:
    """Built before the Wi-Fi was known: everything but the startup firmware's copy."""
    return is_imprinted_manifest(manifest) and RECOVERY_ARTIFACT not in manifest.get("artifacts", {})


def is_memory_only(manifest: dict[str, Any]) -> bool:
    return bool(manifest.get("artifacts", {}).get(RECOVERY_ARTIFACT, {}).get("memory_only"))


def bundle_ssid(manifest_path: Path) -> str | None:
    """The network name the bundle is set up for; None if it has no Wi-Fi details."""
    manifest = load_manifest(manifest_path)
    if not is_imprinted_manifest(manifest) or awaits_wifi(manifest):
        return None
    recorded = manifest.get("imprinted_safeboot", {}).get("ssid")
    if recorded:
        return str(recorded)
    # A bundle from before the image was kept only in memory.
    image = artifact_bytes(manifest_path, manifest, RECOVERY_ARTIFACT)
    return imprinted_ssid(image, PUBLISHED_RECOVERY_FILE.read_bytes())


def startup_image(ssid: str, password: str, chip: chips.Chip = chips.ESP32C3) -> bytes:
    """The startup firmware with these Wi-Fi details written in, in memory only.

    Not checked against any bundle and pins nothing: for comparing with a
    record of the image a device was actually given.
    """
    try:
        return imprint(published_recovery_file(chip).read_bytes(), ssid, password)
    except (OSError, ImprintError) as exc:
        raise BundleError(str(exc)) from exc


def recovery_image(manifest_path: Path, ssid: str, password: str) -> bytes:
    """Rebuild the recovery image in memory and prove it is the one pinned.

    The image is deterministic, so the right password reproduces the exact
    SHA-256 recorded when the bundle was built - which also catches a mistyped
    password before anything uses it.
    """
    manifest = load_manifest(manifest_path)
    record = manifest["artifacts"].get(RECOVERY_ARTIFACT)
    if record is None:
        raise BundleError("this bundle has no Wi-Fi details yet")
    try:
        image = imprint(published_recovery_file(manifest_chip(manifest)).read_bytes(), ssid, password)
    except (OSError, ImprintError) as exc:
        raise BundleError(str(exc)) from exc
    if sha256_bytes(image) != record["sha256"]:
        raise BundleError("that password does not match the one this bundle was built with")
    return image


def forget_stored_password(manifest_path: Path) -> bool:
    """Convert a bundle that kept its recovery image on disk to memory-only.

    Records the (non-secret) network name and the image's pin, then deletes the
    image, so the password is no longer stored anywhere.  True if it changed.
    """
    try:
        manifest = load_manifest(manifest_path)
    except ValueError:
        return False
    record = manifest.get("artifacts", {}).get(RECOVERY_ARTIFACT)
    chip = manifest_chip(manifest)
    path = manifest_path.parent / recovery_file(chip)
    if record is None or record.get("memory_only"):
        path.unlink(missing_ok=True)
        return False
    if path.exists():
        image = path.read_bytes()
        manifest.setdefault("imprinted_safeboot", {})["ssid"] = imprinted_ssid(
            image, published_recovery_file(chip).read_bytes())
    record["memory_only"] = True
    private_write(manifest_path,
                  (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    path.unlink(missing_ok=True)
    return True


def ask_credentials() -> tuple[str, str]:
    """The network name and password, the password typed twice and never stored."""
    ssid = ask_text("Wi-Fi network name (2.4 GHz, exactly as it appears)", private="wifi-name")
    return ssid, ask_password()


def ask_password(ssid: str | None = None) -> str:
    """A Wi-Fi password, typed twice and never stored.  Two that differ are
    taken off the screen before it is asked again, only why left, as a wrong
    answer to any question is: they piled up under each other."""
    mark = output.mark()
    while True:
        password = ask_secret(f"Wi-Fi password for '{ssid}': " if ssid else "Wi-Fi password: ")
        if ask_secret("Same password again: ") == password:
            return password
        output.rewind(mark)
        output.show("    The two passwords did not match - try again.")


def _credentials(args: argparse.Namespace) -> tuple[str, str]:
    if not args.ssid:
        return ask_credentials()
    password = ask_secret("Wi-Fi password: ")
    if ask_secret("Same password again: ") != password:
        raise SystemExit("the passwords did not match")
    return args.ssid, password


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssid", help="2.4 GHz Wi-Fi network name (asked if omitted)")
    parser.add_argument("--without-wifi", action="store_true",
                        help="build everything but the startup firmware's Wi-Fi copy; "
                             "the autoflash run adds it, read off the device")
    parser.add_argument("--factory", type=Path, help="use a local factory image")
    parser.add_argument("--safeboot", type=Path, help="use a local Safeboot image")
    parser.add_argument("--app", type=Path, help="use a local native Tasmota image")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    ssid = password = None
    if not args.without_wifi:
        try:
            ssid, password = _credentials(args)
        except QuitRun:
            print("Stopped. Nothing was changed.")
            return 130

    try:
        manifest_path, _ = build_bundle(ssid, password, args.output, args.factory, args.safeboot, args.app)
    except BundleError as exc:
        parser.error(str(exc))

    print(f"\nManifest: {manifest_path}")
    if ssid is None:
        print("No Wi-Fi details in it yet: the autoflash run adds them at E1.")
    else:
        print(f"Set up for the network {ssid!r}. The password is not stored.")
    print("No flash dump used: preflight reads the source table and NVS live from the device.")
    print("\nBUNDLE PREPARATION: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
