"""A device's relay switched through eWeLink, as the app switches it: the only
way to reach it on Sonoff's firmware without its LAN key.  check_relay()
switches it to the other state and back, each confirmed by the device's
answer to it, reading its meter after each as the app does, so a run back
to Sonoff's firmware ends with the relay proved as a run to another
firmware does (autoflash's report).  answers() asks the device for nothing it
changes, to know it is there.
"""

from __future__ import annotations

import socket
import time
from typing import Any, Callable, NamedTuple

from tools.ewelink.get_device_key import (APP_ID, TOKEN_REFUSED, Account, EwelinkError, SessionEnded, api_json,
                                          get_devices, select_device)
from tools.ewelink.probe_ota_command import WS_DISPATCH, nonce
from tools.ewelink.websocket_minimal import WebSocket, WebSocketError

# How long the device may take to report a switch it was sent (seconds).
SWITCH_WAIT = 15.0
# How long a device that dropped off eWeLink as it switched may take to come back,
# for its relay to be put back as it was (seconds; looked for every 5 s). Seen in
# the emulator: Sonoff's S60 firmware closing its connection 2 ms after it had
# confirmed a switch, back 15 s later, so the switch back reached no one.
RESTORE_WAIT = 60.0
# A device's answer to an update it does not take: error 400, which eWeLink
# passes on (the DualR3's to uiActive). eWeLink's own for a device not there is 504.
REFUSED = 400
# How long after a switch the meter is read, for it to have measured the new
# state: as autoflash's report waits before reading Tasmota's (seconds).
READING_SETTLE = 6.0
# How long a device asked for its readings may take to send them (seconds).
READING_WAIT = 10.0


class RelayCheck(NamedTuple):
    """check_relay's answer: whether each switch was confirmed, and the meter
    read with the relay on and with it off ({"Voltage", "Power"}, in volts and
    watts), each None when not read."""
    switched: bool
    restored: bool
    on: dict[str, float] | None = None
    off: dict[str, float] | None = None


def app_socket(region: str, session: dict[str, Any]) -> WebSocket:
    """The cloud's WebSocket, signed in as the app: what carries a command to
    the device and the device's reports back."""
    dispatch = api_json(WS_DISPATCH[region], headers={"Authorization": "Bearer " + session["at"]})
    ws = WebSocket.connect(dispatch["domain"], int(dispatch["port"]))
    now = time.time()
    ws.send_json({
        "action": "userOnline", "at": session["at"], "apikey": session["user"]["apikey"],
        "appid": APP_ID, "nonce": nonce(), "ts": int(now), "userAgent": "app",
        "sequence": str(int(now * 1000)), "version": 8,
    })
    handshake = ws.receive_json(timeout=10)
    if handshake.get("error", 0) != 0:
        ws.close()
        if handshake.get("error") in TOKEN_REFUSED:
            raise SessionEnded("eWeLink ended the sign-in (cloud WebSocket)")
        raise EwelinkError("Cloud WebSocket authentication failed")
    return ws


def relay_state(params: dict[str, Any]) -> bool | None:
    """Whether the relay is on, from a device's params: a single switch
    (switch), or outlet 0 of a list of them (switches, as the S61 has)."""
    switches = params.get("switches")
    if isinstance(switches, list):
        for outlet in switches:
            if isinstance(outlet, dict) and outlet.get("outlet", 0) == 0 and outlet.get("switch") in ("on", "off"):
                return outlet["switch"] == "on"
        return None
    value = params.get("switch")
    return value == "on" if value in ("on", "off") else None


def motor_mode(params: dict[str, Any]) -> bool:
    """Whether a device's params say it drives a curtain motor (a DualR3's
    workMode 2): it then reports no relay, and refuses a switch with 400."""
    return params.get("workMode") == 2


def switch_params(params: dict[str, Any], on: bool) -> dict[str, Any]:
    """The params that switch the relay, in the shape the device reports it."""
    word = "on" if on else "off"
    if isinstance(params.get("switches"), list):
        return {"switches": [{"switch": word, "outlet": 0}]}
    return {"switch": word}


def command(ws: WebSocket, device: dict[str, Any], params: dict[str, Any], *, wait: float = SWITCH_WAIT,
            taken: Callable[[dict[str, Any]], bool] = lambda msg: False,
            answers: tuple[int, ...] = (0,)) -> bool:
    """Send the device an update, as the app does; True once it has answered
    it with an error in `answers` (0, it took it), or sent what `taken` accepts."""
    seq = str(int(time.time() * 1000))
    ws.send_json({"action": "update", "deviceid": device["deviceid"], "apikey": device["apikey"],
                  "userAgent": "app", "sequence": seq, "params": params})
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            msg = ws.receive_json(timeout=max(0.1, deadline - time.monotonic()))
        except socket.timeout:
            break
        if msg.get("sequence") == seq:
            return msg.get("error", 0) in answers
        if msg.get("deviceid") == device["deviceid"] and taken(msg):
            return True
    return False


def switch(ws: WebSocket, device: dict[str, Any], on: bool, *, wait: float = SWITCH_WAIT) -> bool:
    """Switch the relay on or off; True once the device has taken it: its
    answer to the command (error 0), which the S61 gives without reporting
    its new state, or a report of that state."""
    return command(ws, device, switch_params(device.get("params") or {}, on), wait=wait,
                   taken=lambda msg: isinstance(msg.get("params"), dict) and relay_state(msg["params"]) is on)


def reading(params: dict[str, Any]) -> dict[str, float] | None:
    """A device's readings from a report of them, in volts and watts: Sonoff's
    S60 and S61 firmware send voltage and power as whole hundredths (23022 is
    230.22 V).  None for a report without them."""
    values = {name: params[key] / 100 for name, key in (("Voltage", "voltage"), ("Power", "power"))
              if isinstance(params.get(key), int) and not isinstance(params.get(key), bool)}
    return values or None


def read_meter(ws: WebSocket, device: dict[str, Any], *, wait: float = READING_WAIT) -> dict[str, float] | None:
    """The device's readings, asked for as the app asks as its page opens
    (uiActive), which the device answers by sending them.  None when it sends
    none in `wait`: a device with no meter, or one refusing it (the DualR3)."""
    seq = str(int(time.time() * 1000))
    ws.send_json({"action": "update", "deviceid": device["deviceid"], "apikey": device["apikey"],
                  "userAgent": "app", "sequence": seq, "params": {"uiActive": 60}})
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            msg = ws.receive_json(timeout=max(0.1, deadline - time.monotonic()))
        except socket.timeout:
            break
        if msg.get("sequence") == seq and msg.get("error", 0) != 0:
            return None
        found = reading(msg["params"]) if (msg.get("deviceid") == device["deviceid"]
                                            and isinstance(msg.get("params"), dict)) else None
        if found:
            return found
    return None


def answers(*, account: Account, device_id: str) -> bool | None:
    """Whether the device answers through eWeLink now, or None when eWeLink
    cannot be reached.  The account's "online" does not say it: eWeLink
    keeps a device that went silent online until it has missed its
    heartbeats, minutes on, and passes it what is sent meanwhile to no one.
    Asked as the app asks for a device's readings as its page opens
    (uiActive), which changes nothing on the device: refusing it is answering
    too. Firmware with no meter does not know that and says nothing, so then
    asked to keep its LED's setting as it last reported it, which changes
    nothing either."""
    def ask(region: str, session: dict[str, Any]) -> bool:
        device = select_device(get_devices(region, session["at"]), device_id)
        ws = app_socket(region, session)
        try:
            if command(ws, device, {"uiActive": 60}, answers=(0, REFUSED)):
                return True
            led = (device.get("params") or {}).get("sledOnline")
            return command(ws, device, {"sledOnline": led}) if led in ("on", "off") else False
        finally:
            ws.close()

    try:
        return account.use(ask)
    except (EwelinkError, WebSocketError, OSError, KeyError, ValueError):
        return None


def check_relay(*, account: Account, device_id: str,
                say: Callable[[str], None] = print) -> RelayCheck | None:
    """Switch the device's relay to the other state and back, reading its meter
    after each confirmed switch: whether each switch was confirmed, and the
    readings, or None when it could not be tried (eWeLink not reached, the
    device offline, or no relay state in its record)."""
    try:
        device = account.use(lambda region, session: select_device(get_devices(region, session["at"]), device_id))
        region, session = account.session()
        was_on = relay_state(device.get("params") or {})
        if was_on is None or not device.get("online"):
            say(f"    The device's relay cannot be switched through eWeLink "
                f"({'it is offline' if was_on is not None else 'its record gives no relay state'}).")
            return None
        ws = app_socket(region, session)

        def switched_and_read(on: bool) -> tuple[bool, dict[str, float] | None]:
            if not switch(ws, device, on):
                return False, None
            time.sleep(READING_SETTLE)
            return True, read_meter(ws, device)
        try:
            switched, first = switched_and_read(not was_on)
            restored, second = switched_and_read(was_on)
        finally:
            ws.close()
        if not restored:
            restored = switch_back(region, session, device, was_on, say)
    except (EwelinkError, WebSocketError, OSError, KeyError, ValueError) as exc:
        say(f"    The relay could not be switched through eWeLink: {exc}")
        return None
    on, off = (second, first) if was_on else (first, second)
    return RelayCheck(switched, restored, on, off)


def switch_back(region: str, session: dict[str, Any], device: dict[str, Any], on: bool,
                say: Callable[[str], None]) -> bool:
    """The relay put back as it was, once the device answers through eWeLink again,
    within RESTORE_WAIT: a check leaves the device as it found it.  Sent again on a
    new connection each time, as the device's own may have gone: a switch to the
    state it is in changes nothing."""
    say("    The switch back was not confirmed: sending it again as the device comes back...")
    deadline = time.monotonic() + RESTORE_WAIT
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            ws = app_socket(region, session)
            try:
                if switch(ws, device, on, wait=min(SWITCH_WAIT, max(1.0, deadline - time.monotonic()))):
                    say("    The relay is back as it was.")
                    return True
            finally:
                ws.close()
        except (EwelinkError, WebSocketError, OSError):
            continue
    say("    The relay could not be put back as it was: check it in the eWeLink app.")
    return False
