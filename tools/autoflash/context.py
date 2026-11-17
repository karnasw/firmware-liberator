"""The run's context: its arguments, state and record, the secrets held in
memory for it, and the device and servers it talks through.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import time
from pathlib import Path
from typing import Any

from tools.autoflash import (
    account,
    credentials,
    device,
    firewall,
    hostsys,
    join,
    kinds,
    noise,
    output,
    phases,
    records,
    prompts,
    routes,
    runs,
    signup,
    unlock,
    wifi,
)
from tools.autoflash.errors import Abort
from tools.autoflash.phases import PhaseRunner
from tools.autoflash.routes.espressif import bundle, installer
from tools.espressif import chips
from tools.autoflash.runs import ROOT
from tools.autoflash.state import RunState
from tools.espressif.safeboot.prepare_imprinted_bundle import (
    BundleError,
    ask_credentials,
    build_bundle,
    bundle_ssid,
    pin_wifi,
    recovery_image,
)
from tools.ewelink.get_device_key import (API, SESSION_ENV, Account, EwelinkError, account_name, get_devices,
                                          is_email, login_session, phone_number, session_env)


class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.run = args.run
        self.dir = records.CAPTURES / self.run
        self.evidence = self.dir / "live"
        self.state = RunState.open(self.dir)
        # Given by hand with --manifest; else the bundle for the device's chip,
        # known only once its kind is (bundle.default_manifest).
        self._manifest = None if args.manifest == bundle.DEFAULT_MANIFEST else (ROOT / args.manifest).resolve()
        self.dry_run = args.dry_run
        # Research only: a different bridge, after which the run stops (see
        # archive/docs/reference/automatic-wifi.md).  The device goes back to stock on a power cycle.
        self.bridge = Path(getattr(args, "bridge", None) or installer.BRIDGE_OTA)
        self.research_bridge = self.bridge != installer.BRIDGE_OTA
        # Optional path to an ESPHome application firmware.ota.bin. When set,
        # the run stops after interim Tasmota on the *stock* partition table
        # (no Safeboot / E3 repartition) and uploads this image via Tasmota OTA.
        esphome = getattr(args, "esphome", None)
        self.esphome_firmware = Path(esphome).expanduser().resolve() if esphome else None
        # Supla GUI-Generic as the builder gave it (--supla PATH: its .zip),
        # which choosing Supla needs; asked for if not given.
        supla = getattr(args, "supla", None)
        self.supla_firmware = Path(supla).expanduser().resolve() if supla else None
        # --firmware: which to install, instead of being asked (--unattended needs it).
        self.replace_own_build = getattr(args, "replace_own_build", False)
        self.firmware_given = ("esphome" if esphome or self.replace_own_build else
                               "supla" if supla else getattr(args, "firmware", None))
        if args.device_ip:
            self.state.set_input("device_ip", args.device_ip)
        # An account given in advance is checked now: eWeLink is not asked
        # with one that cannot be an account.
        for given in (args.email, credentials.get("EWELINK_EMAIL")):
            if given and not is_email(given) and phone_number(given) is None:
                raise Abort("the eWeLink account given (--email, or EWELINK_EMAIL) is neither an email "
                            "address nor a phone number with its country code (as +44 7700 900123)")
        if args.email:
            self.remember_email(args.email)
        if args.web_user:
            self.state.set_input("web_user", args.web_user)
        # The device's Tasmota web password, if it has one: given, or asked for
        # when it is found wanting one (ask_web_password). Held in memory only.
        self.web_password = args.web_password or credentials.get("AUTOFLASH_TASMOTA_PASSWORD")
        output.private(self.web_password, "tasmota-password")
        # The same for ESPHome's update password (ask_esphome_password); its
        # web login (ask_esphome_login), user name and password; and its API
        # encryption key, for its encrypted API and updates (ask_esphome_key).
        self.esphome_password = args.esphome_ota_password or credentials.get("AUTOFLASH_ESPHOME_OTA_PASSWORD")
        output.private(self.esphome_password, "esphome-password")
        user = args.esphome_web_user or credentials.get("AUTOFLASH_ESPHOME_WEB_USER")
        password = args.esphome_web_password or credentials.get("AUTOFLASH_ESPHOME_WEB_PASSWORD")
        output.private(password, "esphome-web-password")
        self.esphome_login = (user, password) if user and password else None
        key = args.esphome_api_key or credentials.get("AUTOFLASH_ESPHOME_API_KEY")
        output.private(key, "esphome-api-key")
        try:
            self.esphome_key = noise.decode_key(key) if key else None
        except ValueError as exc:
            raise Abort(f"the ESPHome API encryption key given (--esphome-api-key, or "
                        f"AUTOFLASH_ESPHOME_API_KEY) is not one ({exc}): one is 32 bytes in base64, "
                        "as after key: under api: encryption: in the YAML config") from None
        # Its API found encrypted, its key not given: offered before the Wi-Fi is asked for.
        self.esphome_api_locked = False
        self._ewelink_password: str | None = None
        # From .env or the environment: tried once, then asked for if eWeLink refuses it.
        self._given_ewelink_password = credentials.get("EWELINK_PASSWORD")
        given_email = credentials.get("EWELINK_EMAIL")
        self._given_email = account_name(given_email) if given_email else None
        # The run's one sign-in to the account, held in memory only.
        self.ewelink = Account(self._sign_in)
        self._finder: join.DeviceFinder | None = None
        # The go-ahead to replace the device's firmware, given in this run.
        self.consented = False
        # The step a step found had not taken on the device (errors.GoBack),
        # which the run goes back to next.
        self.go_back: str | None = None
        # The account's record of the device A1 chose, for its device.json.
        self.account_device: account.AccountDevice | None = None
        # Held only in memory: the Wi-Fi details, and the recovery image built
        # from them.  Neither is ever written to disk.
        self._wifi: tuple[str, str] | None = None
        self._recovery: bytes | None = None
        # For the report: the phase runner a step used (its device-busy time), when
        # this session started, and whether the report has been offered.
        self.last_runner: PhaseRunner | None = None
        self.run_started = time.monotonic()
        self.run_resumed = False
        self.reported = False
        self.stage_begun = False  # a stage begun this run: a report worth asking for
        # A converted device's firmware, chosen at A3 (routes.settle_route).
        self.firmware_choice: routes.FirmwareChoice | None = None
        self.last_failure = ""  # what the last stage to fail said, for the report
        self.last_failure_class: str | None = None  # its class, where what stopped it said one (errors.Abort)
        self.running_step: str | None = None  # the step under way (runner.execute)
        self.redoing_interrupted: set[str] = set()  # steps a run before this one was cut off in, done again
        # How the person stopped the run, for its report (report_data.STOPPED_BY).
        self.stopped_by: str | None = None
        # Where in the tool's code the last stage to fail went wrong, for an
        # unexpected error (runner.crash_site).
        self.crash_site: str | None = None
        # The run's report to the project's API (report_upload.py): its id and
        # the process making its proof of work, once begun.
        self.report_id: str | None = None
        self.report_work = None
        self.last_report_id: str | None = None   # the one sent: on the screen's report, for an issue
        self.last_failed_step = ""  # and which it was: a failure may send earlier steps back
        try:
            output.private(bundle_ssid(self.manifest_path), "wifi-name")
        except (BundleError, OSError, ValueError):
            pass  # no bundle yet: its network is kept out of the log once known

    def remember_wifi(self, ssid: str, password: str, image: bytes | None) -> None:
        output.private(ssid, "wifi-name")
        output.private(password, "wifi-password")
        self._wifi, self._recovery = (ssid, password), image

    def held_wifi(self) -> tuple[str, str] | None:
        """The Wi-Fi details this run holds already - typed, or read off the
        device - asking nothing; None when it holds none."""
        return self._wifi

    def wifi(self, why: list[str] | None = None) -> tuple[str, str]:
        """The Wi-Fi name and password: read from the device where it can be.

        From D1 the device runs Tasmota connected to the home network, so the
        details are read from there rather than typed - and they are known to
        work, which typed ones are not until F2 tries them.  ESPHome gives
        them too (its Wi-Fi action); from an ESPHome or mover
        that does not, they come from the device's Sonoff backup, when this
        computer has one and the device is on that network still.  Asking is the
        fallback, with `why` saying what it is for; `--wifi` forces it.

        A Beken device's are only asked for (or given in advance): no Tasmota
        runs on it to read them from, its startup firmware is not built with
        them, and the only ones it holds - its Sonoff store's, which ESPHome
        starts with - are what it could not join with when they are needed
        (set_up_through_own_network).
        """
        forced = getattr(self.args, "wifi", False)
        espressif = kinds.of_run(self.state).family == "espressif"
        # ESPHome, or the mover, says nothing of its Wi-Fi password: only a
        # Tasmota can be read.
        readable = espressif and runs.device_runs_tasmota(self.state)
        if self._wifi is None and not forced and readable:
            found = wifi.read_wifi_from_device(self)
            if found is not None:
                self.adopt_wifi(*found)
                # A run stopped at the question earlier must not report it as typed.
                self.state.set_input("wifi_typed", False)
        if self._wifi is None and not forced and espressif and not readable:
            found = wifi.wifi_from_esphome(self) or wifi.wifi_from_backup(self)
            if found is not None:
                self.adopt_wifi(*found)
                self.state.set_input("wifi_typed", False)
        if self._wifi is None and not forced and espressif and self.esphome_api_locked and not prompts.UNATTENDED:
            # ESPHome gives them, its API unlocked: its key typed instead.
            found = unlock.ask_esphome_key(self, self.state.input("device_ip"), wifi=True) and wifi.wifi_from_esphome(self)
            if found:
                self.adopt_wifi(*found)
                self.state.set_input("wifi_typed", False)
        given = credentials.get("AUTOFLASH_WIFI_SSID")
        if self._wifi is None and not forced and given:
            # Given in advance (.env), for a run nobody sits through; no
            # password given means an open network.
            self.adopt_wifi(given, credentials.get("AUTOFLASH_WIFI_PASSWORD") or "")
            self.state.set_input("wifi_typed", False)
        if self._wifi is not None:
            return self._wifi
        output.transient_begin()  # until they are accepted, however many tries
        prompts.section("Your Wi-Fi details are needed", [
            *(["You chose to type them (--wifi)."] if forced else why or [
                "They could not be read off the device (see above).",
            ]),
            "Use the 2.4 GHz network the device stays on; kept in memory for this run.",
        ])
        output.log_only("The startup firmware rejoins the Wi-Fi by itself after the next stage, so it needs "
                        "them written in. The password is never written to disk, nor asked again in this run.")
        while self._wifi is None:
            ssid = bundle_ssid(self.manifest_path) if espressif else None
            if ssid is None:
                self.adopt_wifi(*ask_credentials())
                continue
            output.say(f"The bundle is set up for '{ssid}'.")
            password = prompts.ask_secret(f"Wi-Fi password for '{ssid}': ")
            try:
                self.remember_wifi(ssid, password, recovery_image(self.manifest_path, ssid, password))
            except BundleError as exc:
                output.tell(f"    {exc}.")
                choice = prompts.ask_choice("What now", [
                    "Type the password again",
                    "Use a different network, or a changed password - enter both",
                ])
                if choice == 1:
                    self.adopt_wifi(*ask_credentials())
        output.transient_end()
        self.state.set_input("wifi_typed", True)
        return self._wifi

    def adopt_wifi(self, ssid: str, password: str) -> None:
        """Use these details, setting the bundle up for them if it is not
        already: an Espressif device's, as a Beken device's route has none."""
        if kinds.of_run(self.state).family != "espressif":
            self.remember_wifi(ssid, password, None)
            return
        try:
            image = recovery_image(self.manifest_path, ssid, password)
        except BundleError:
            output.say(f"    Writing the Wi-Fi for '{ssid}' into the startup firmware (in memory)...")
            try:
                image = pin_wifi(self.manifest_path, ssid, password)
            except BundleError as first:
                # A bundle this checkout can no longer use: build it again.
                output.say(f"    {first}; preparing the firmware bundle again...")
                try:
                    _, image = build_bundle(ssid, password, self.manifest_path.parent, chip=chips.of(kinds.of_run(self.state).chip),
                                            release=bundle.release_on_the_device(self))
                except BundleError as exc:
                    raise Abort(f"the startup firmware could not be prepared: {exc}") from exc
        self.remember_wifi(ssid, password, image)

    def recovery_bytes(self) -> bytes:
        self.wifi()
        assert self._recovery is not None
        return self._recovery

    def ewelink_env(self) -> dict[str, str]:
        """Ask for the account password once and reuse it for this run only.

        It is held in memory, never written to the run state, and never put on a
        command line.  It reaches the reviewed tools through EWELINK_PASSWORD,
        which serve_tasmota_ota.py already supported.
        """
        # A password given in advance is tried once, and only for the account
        # it was given with when one was named too.
        given, self._given_ewelink_password = self._given_ewelink_password, None
        if self._ewelink_password is None and given and self._given_email in (None, self.email()):
            self._ewelink_password = given
            output.say("The eWeLink password is the one given in advance (.env or EWELINK_PASSWORD).")
        if self._ewelink_password is None:
            email = self.email()
            output.transient_begin()
            self._ewelink_password = prompts.ask_secret(f"eWeLink password for {email}: ")
            output.transient_end()
        return {**os.environ, "EWELINK_PASSWORD": self._ewelink_password}

    def helper_env(self) -> dict[str, str]:
        """What a helper tool that signs in to eWeLink is run with: the
        password (ewelink_env), and this run's sign-in, which the helper uses
        instead of one of its own - that would end this run's."""
        env = self.ewelink_env()
        if self.ewelink.held:
            env[SESSION_ENV] = session_env(*self.ewelink.held)
        return env

    def account_devices(self) -> list[dict[str, Any]] | None:
        """The devices in the owner's eWeLink account, or None when eWeLink
        cannot be reached.  The list, which holds each device's LAN key, stays
        in memory and is never logged."""
        try:
            devices = self.ewelink.use(lambda region, session: get_devices(region, session["at"]))
        except EwelinkError as exc:
            output.tell(f"    eWeLink is unreachable ({exc}).")
            return None
        account.keep_account_private(devices)
        signup.saw_devices(devices)
        return devices

    def _sign_in(self) -> tuple[str, dict[str, Any]]:
        """Sign in to the account (self.ewelink), in the region it was last
        found in, asking the password again while eWeLink refuses it."""
        while True:
            self.ewelink_env()
            try:
                region, session = login_session(self.email(), self._ewelink_password or "", "+44",
                                                self._last_region())
                output.private(session.get("at"), "access-token")
                self._remember_region(region)
                return region, session
            except EwelinkError as exc:
                if "login failed" not in str(exc):
                    raise
                self._ewelink_password = None
                output.transient_begin()
                output.tell(f"    {exc}.")
                choice = prompts.ask_choice("What now", [
                    "Type the password again",
                    "Use a different eWeLink account",
                ])
                output.transient_end()
                if choice == 1:
                    self.forget_account()

    def ewelink_region(self) -> str:
        """The account's region: where it signed in, or was last found."""
        return self.ewelink.held[0] if self.ewelink.held else self._last_region()

    def _last_region(self) -> str:
        settings = runs.load_settings()
        region = settings.get("ewelink_region")
        return region if settings.get("ewelink_email") == self.email() and region in API else "eu"

    def _remember_region(self, region: str) -> None:
        settings = runs.load_settings()
        if settings.get("ewelink_email") == self.email() and settings.get("ewelink_region") != region:
            settings["ewelink_region"] = region
            runs.save_settings(settings)

    def keep_sign_in(self, previous: Context) -> None:
        """The next device's run in the same sitting (cli.next_device): the
        eWeLink account the last run used, signed in to already - most
        likely the next device's too; one in another account is offered
        that at A1.  Held in memory, as the last run held it."""
        if previous.state.input("email") and not self.state.input("email"):
            self.state.set_input("email", previous.state.input("email"))
        self._ewelink_password = previous._ewelink_password
        self.ewelink.held = previous.ewelink.held

    def forget_account(self) -> None:
        """Ask again which eWeLink account, and its password."""
        self.state.set_input("email", None)
        self.ewelink.end()
        self._ewelink_password = None
        self._given_email = self._given_ewelink_password = None

    def email(self) -> str:
        """The eWeLink account, chosen once per run: its email, or the phone
        number it was made with.

        The last account used on this computer is offered first; a device in
        another account needs that account's instead.
        """
        email = self.state.input("email") or self._given_email
        if not email:
            output.transient_begin()
            last = runs.load_settings().get("ewelink_email")
            if last and prompts.ask_choice("Which eWeLink account is this device in", [
                f"The one used last time: {last}",
                "A different account - enter its email or phone number",
            ]) == 0:
                email = last
            else:
                email = prompts.ask_text(
                    "eWeLink account email or phone number",
                    help_lines=["A phone number goes with its country code, as +44 7700 900123."],
                    validate=lambda answer: (
                        is_email(answer) or phone_number(answer) is not None,
                        "That is neither an email address nor a phone number with its country code.",
                    ),
                )
            output.transient_end()
        email = account_name(email)
        self.remember_email(email)
        return email

    def remember_email(self, email: str) -> None:
        email = account_name(email)
        signup.saw_account(email)
        self.state.set_input("email", email)
        settings = runs.load_settings()
        if settings.get("ewelink_email") != email:
            settings["ewelink_email"] = email
            settings.pop("ewelink_region", None)  # the last account's
            runs.save_settings(settings)

    def file_under_device(self, mac: str) -> None:
        """Move this run under its device's MAC (records.py), once the device is
        known and before anything is written to it."""
        if records.mac_of_run(self.dir) == records.folder_name(mac).replace("-", ":"):
            return
        target = records.runs_dir(mac) / self.dir.name
        suffix = 2
        while target.exists():
            target, suffix = target.with_name(f"{self.dir.name}-{suffix}"), suffix + 1
        source = self.dir

        def move() -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            return target / "run.log"

        output.move_log(move)
        self.dir, self.evidence = target, target / "live"
        self.state.path = target / "autoflash-state.json"
        self.run = runs.run_name(target)
        runs.remember_current(self.run)
        output.say(f"This run's record is now in {runs.display_path(target)}.")

    def listen_ip(self) -> str:
        """This computer's address as the device sees it, worked out afresh each time.

        Remembering it went wrong when a cable was unplugged mid-run: the tool
        kept offering an address the computer no longer had.  --listen-ip still
        overrides it.
        """
        if self.args.listen_ip:
            return self.args.listen_ip
        device_ip = self.state.input("device_ip")
        if not device_ip:
            raise Abort("the device address is not known yet, so this computer's "
                        "address cannot be worked out; pass --listen-ip")
        try:
            derived = hostsys.local_ip_towards(device_ip)
        except OSError as exc:
            raise Abort(
                f"could not work out how this computer reaches {device_ip} ({exc}); "
                "check you are on the same network as the device, or pass --listen-ip"
            ) from exc
        address = ipaddress.ip_address(derived)
        if not (address.is_private and address.version == 4):
            raise Abort(f"this computer's address towards the device, {derived}, is not "
                        "a home-network address")
        remembered = self.state.input("listen_ip")
        if remembered != derived:
            if remembered:
                output.say(f"This computer's address towards the device is now {derived} "
                    f"(it was {remembered}).")
            else:
                output.say(f"This computer's address towards the device: {derived}")
            self.state.set_input("listen_ip", derived)
        return derived

    def set_device_ip(self, address: str) -> None:
        if address != self.state.input("device_ip"):
            output.say(f"The device is now at {address}.")
            self.state.set_input("device_ip", address)

    @property
    def steps(self) -> list[tuple[str, str, Any]]:
        """The run's route, as settled at its start (routes.steps_of)."""
        return routes.steps_of(self.state)

    @property
    def finder(self) -> join.DeviceFinder:
        if self._finder is None:
            self._finder = join.DeviceFinder(self)
        return self._finder

    @property
    def device(self) -> device.Device:
        host = self.state.input("device_ip")
        if not host:
            raise Abort("device_ip is not set; pass --device-ip")
        return device.Device(
            host,
            user=self.state.input("web_user"),
            password=self.web_password,
        )

    @property
    def manifest_path(self) -> Path:
        return self._manifest or bundle.default_manifest(self)

    @manifest_path.setter
    def manifest_path(self, path: Path) -> None:
        self._manifest = path

    def phase_runner(self, with_recovery: bool = True) -> PhaseRunner:
        """The migration server's runner, given the startup firmware - unless
        `with_recovery` is False: a restore with none to prove (step_restore)."""
        port = firewall.choose_port(self, "phase_port", firewall.PHASE_PORTS, "the migration steps")
        firewall.ensure_firewall(self)
        self.last_runner = PhaseRunner(
            repo_root=ROOT,
            manifest=self.manifest_path,
            evidence_dir=self.evidence,
            listen_ip=self.listen_ip(),
            device=self.device,
            log=phases.phase_log,
            listen_port=port,
            # Piped, never a file; and only hashes of captures that hold secrets.
            recovery=self.recovery_bytes() if with_recovery else None,
            layout=kinds.of_run(self.state).layout,
        )
        return self.last_runner
