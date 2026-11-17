#!/usr/bin/env python3
"""The local cloud stub: one HTTPS listener that stands in for eWeLink.

This is self-hosting (see docs/cloud-stub.md): our own device, redirected to our
own domain, served our own cert under the CA we made it trust. It speaks just
enough of eWeLink for the device to come online and for the conversion tool to
drive it - nothing reaches, forges or impersonates the real cloud.

One TLS socket on `:443` wears every hat, routed by request:

  POST /dispatch/device   the device's dispatch: the IoT server to connect to -
                          ourselves, as a numeric IP (the device does no DNS here).
  GET  /api/ws            the IoT channel, a minimal RFC 6455 server. The device
                          registers, reports state and asks the date; we answer
                          error:0 (+ a heartbeat config on register) so it is
                          online. The tool's app side connects here too, and its
                          commands for the device (an OTA upgrade) are relayed.
  GET  /dispatch/app      the tool's dispatch: the same IoT server.
  /v2/user/login, /v2/device/thing, /v2/device/ota/query
                          the account API the tool reads the device's LAN key,
                          model and version from (the key is the one in the dump
                          we booted, from --device-json). Asked with a version
                          older than the device's, the OTA query offers its own
                          firmware, as eWeLink offers the latest (firmware.py).
  GET  /ota/user1.bin     that firmware, to the device's id, as eWeLink serves it.

Stdlib only, so it runs under the emulator image's plain python3. Every message
is logged with secret values redacted, so one run is also the capture that pins
the protocol. Run inside the instance container, bound to the router address
the device uses:

    python3 stub_server.py --bind 192.168.60.1 --cert server.crt --key server.key \\
        --device-json stub-device.json

With `--plain` it serves plaintext HTTP/WS instead of TLS and needs no cert. An
emulated Beken device's dispatch is plaintext (its factory dispatch URL is
`http://...:80`); its IoT WebSocket is still `wss://` and verifies the server
cert, so a run serves dispatch plaintext and the IoT channel over TLS and swaps
the firmware's trust anchor. See tools/beken/emulator/cloud_online.py.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import secrets
import socket
import socketserver
import ssl
import struct
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):  # run as a script, isolated: the repository root, for `tools.`
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 magic
# Never shown in cleartext in the log: they identify the account or device, or
# are the LAN-control key itself (devicekey/encrypt_key/apikey let one command
# the device locally).
SECRET_KEYS = {"apikey", "deviceid", "devicekey", "encrypt_key", "at", "rt",
               "selfApikey", "finalUserApikey", "flashCipher", "ssid", "bssid",
               "staMac", "password", "token"}
# A client that connects and then says nothing must not hold a thread for ever.
REQUEST_TIMEOUT = 30.0
# What eWeLink answers a token a later sign-in has ended.
TOKEN_ENDED = {"error": 401, "msg": "Access token authentication error"}


def wire(message: object) -> bytes:
    """A message as compact JSON, as eWeLink's must be: the BASICR4's firmware
    parses nothing else (a space after a colon fails its parser)."""
    return json.dumps(message, separators=(",", ":")).encode()


def log(*parts: object) -> None:
    ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(ts, *parts, flush=True)


def redacted(value: object) -> object:
    """A copy of a decoded message safe to print (secret values masked)."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in SECRET_KEYS else redacted(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


# -- WebSocket framing (server side: read masked, write unmasked) --------------

def encode_frame(payload: bytes, opcode: int = 1) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n <= 0xFFFF:
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    return bytes(header) + payload


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("peer closed")
        data += chunk
    return bytes(data)


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = recv_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = bytearray(recv_exact(sock, length))
    for i in range(len(payload)):
        payload[i] ^= mask[i % 4] if mask else 0
    return opcode, bytes(payload)


# -- app <-> device relay ------------------------------------------------------
# The real cloud passes an app's command (e.g. an OTA "upgrade") to the device
# and the device's replies back. Device and app each hold their own WebSocket to
# us, so we route between them by deviceid. This is what lets the conversion's
# serve_tasmota_ota drive the online device (verification test 4c).

class Conn:
    """A WebSocket peer with a lock, so either thread can send to it safely."""
    def __init__(self, sock: socket.socket, peer: str):
        self.sock = sock
        self.peer = peer
        self.lock = threading.Lock()

    def send(self, payload: bytes, opcode: int = 1) -> None:
        with self.lock:
            self.sock.sendall(encode_frame(payload, opcode=opcode))


_relay_lock = threading.Lock()
_devices: dict[str, Conn] = {}          # deviceid -> the device's WebSocket
_subscribers: dict[str, set[Conn]] = {}  # deviceid -> app WebSockets watching it


def _register_device(deviceid: str, conn: Conn) -> None:
    with _relay_lock:
        _devices[deviceid] = conn
        _subscribers.setdefault(deviceid, set())


def _drop_device(deviceid: str, conn: Conn) -> set[Conn]:
    with _relay_lock:
        if _devices.get(deviceid) is conn:
            _devices.pop(deviceid, None)
        return set(_subscribers.get(deviceid, ()))


def _subscribe(deviceid: str, app: Conn) -> Conn | None:
    with _relay_lock:
        _subscribers.setdefault(deviceid, set()).add(app)
        return _devices.get(deviceid)


def _subscribers_of(deviceid: str) -> set[Conn]:
    with _relay_lock:
        return set(_subscribers.get(deviceid, ()))


def _unsubscribe(app: Conn) -> None:
    with _relay_lock:
        for watchers in _subscribers.values():
            watchers.discard(app)


def _tell_watchers(deviceid: str, online: bool) -> None:
    """The apps watching a device told it came online or went offline, as
    eWeLink tells them (sysmsg): a sender waits on that to send again a command
    its device lost (tools/ewelink/ota_push.Resending)."""
    for app in _subscribers_of(deviceid):
        try:
            app.send(wire({"action": "sysmsg", "deviceid": deviceid, "params": {"online": online}}))
        except OSError:
            pass


def _online(deviceid: str) -> bool:
    """Whether the device holds its IoT WebSocket now: what eWeLink calls online."""
    with _relay_lock:
        return deviceid in _devices



def send_to_device(deviceid: str, msg: dict) -> bool:
    """Send `msg` to the device over its IoT WebSocket, as the cloud passes on an
    app's command (an OTA `upgrade`, say). False if it is not online."""
    with _relay_lock:
        conn = _devices.get(deviceid)
    if conn is None:
        return False
    conn.send(wire(msg))
    return True

# -- the server ----------------------------------------------------------------

def _timestamp(when: dt.datetime) -> str:
    """A time as eWeLink writes one: 2026-10-30T20:00:00.000Z."""
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Stub:
    def __init__(self, iot_host: str, iot_port: int, hb_interval: int, device: dict | None = None,
                 update=None, iot_domain: str = "", others: list[dict] | None = None,
                 reported_file: Path | None = None):
        self.iot_host = iot_host
        self.iot_domain = iot_domain or iot_host
        self.update = update  # firmware.Update the OTA query offers, or None
        self.iot_port = iot_port
        self.hb_interval = hb_interval
        self.device = device  # the account-API view of this device, or None
        # The account's other devices, none of them emulated: listed as eWeLink
        # lists an account's every device, offered no firmware, never online.
        self.others = list(others or [])
        # What the device says of itself (firmware version, model, the params of
        # its updates) in its own dispatch, register and update messages: the
        # account API reports that, as the real cloud does, rather than
        # anything assumed about the dump.
        self.reported: dict[str, dict] = {}
        # Kept in `reported_file` as eWeLink keeps its record of a device: a
        # device converted since, which never speaks to the cloud again, is still
        # listed with the firmware version and params it last gave.
        self.reported_file = reported_file
        self._reported_lock = threading.Lock()
        if reported_file is not None and reported_file.is_file():
            try:
                self.reported = json.loads(reported_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.reported = {}
        self.access_token: str | None = None  # the last sign-in's, which ended the one before

    def silence_limit(self) -> float:
        """How long a device may say nothing, not even a ping, and be online."""
        return 2.0 * self.hb_interval + 15.0

    def heard(self, msg: dict) -> None:
        deviceid = msg.get("deviceid")
        if not deviceid:
            return
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        version = msg.get("fwVersion") or msg.get("romVersion") or params.get("fwVersion")
        seen = self.reported.setdefault(deviceid, {})
        if msg.get("action") == "update":
            # Its state as it reports it (its network, its version): eWeLink's
            # record of a device carries the params it last sent.
            seen.setdefault("params", {}).update(params)
        if version:
            seen["fwVersion"] = version
        if msg.get("model"):
            seen["model"] = msg["model"]
        if msg.get("action") == "register" and msg.get("apikey"):
            seen["apikey"] = msg["apikey"]
        self._keep_reported()

    def _keep_reported(self) -> None:
        if self.reported_file is None:
            return
        with self._reported_lock:
            temporary = self.reported_file.with_name(f".{self.reported_file.name}.tmp")
            try:
                temporary.write_text(json.dumps(self.reported, sort_keys=True), encoding="utf-8")
                temporary.replace(self.reported_file)
            except OSError as exc:  # the stub serves on without it
                log("what the device reported could not be kept:", exc)

    def dispatch_response(self) -> dict:
        # The device connects to this host by numeric IP without DNS (it
        # inet_aton()s the field), so hand back an address, not a hostname.
        # The S60 reads `IP`; the S61 reads `domain` (the same numeric address,
        # unless --iot-domain gives a name) - include both so either firmware
        # finds the field it parses. eWeLink's `domain` is a hostname, which the
        # MINIR4 connects by and checks the certificate against: its TLS cannot
        # match an IP address in one.
        return {"error": 0, "reason": "", "IP": self.iot_host, "domain": self.iot_domain,
                "port": self.iot_port}

    def app_dispatch_response(self) -> dict:
        # The tool's app-side dispatch (serve_tasmota_ota). It connects by TLS
        # with server_hostname set to this value, which the leaf's IP SAN covers.
        return {"error": 0, "reason": "", "domain": self.iot_host, "IP": self.iot_host,
                "port": self.iot_port}

    # -- tool-facing account API (touchpoint A) ---------------------------------
    # The tool already has this device's key because it is in the flash dump we
    # booted; the stub just hands back that same local value. Nothing is fetched
    # from or sent to the real cloud.

    def account_login(self) -> dict:
        apikey = (self.account_device() or {}).get("apikey", "stub-user-apikey")
        self.access_token = f"stub-access-token-{secrets.token_hex(4)}"
        return {"error": 0, "data": {"at": self.access_token, "rt": "stub-refresh-token",
                                     "region": "eu", "user": {"apikey": apikey}}}

    def signed_in(self, token: str | None) -> bool:
        return token is not None and token == self.access_token

    def account_device(self) -> dict | None:
        """This device's account record: --device-json's, its identity filled in
        from the device's own register where that does not give it - as the
        S61's, whose identity is sealed in its store, does not."""
        d = dict(self.device or {})
        if not d.get("deviceid"):
            registered = [(i, seen) for i, seen in self.reported.items() if seen.get("apikey")]
            if not registered:
                return None
            d["deviceid"], seen = registered[-1]
            d.setdefault("apikey", seen["apikey"])
            d.setdefault("devicekey", seen["apikey"])
        return d

    def device_thing(self) -> dict:
        d = self.account_device()
        others = [self.other_item(other) for other in self.others]
        if not d:
            if others:
                return {"error": 0, "data": {"thingList": others}}
            return {"error": 404, "msg": "no device configured in the stub"}
        seen = self.reported.get(d["deviceid"], {})
        model = seen.get("model") or d.get("model")
        # eWeLink's record holds the relay's state from the device's earlier
        # reports; this one starts each run with none, and the POW Elite's and
        # POWCT's firmware reports it only when it changes. So until the device
        # reports it, it is off, in the shape those two take. Once reported it
        # is only the device's own: the TH Elite's 1.0.2 reports one switch, and
        # answers a list with 400.
        params = {**seen.get("params", {}), "model": model}
        if "switch" not in params and "switches" not in params:
            params["switches"] = [{"switch": "off", "outlet": 0}]
        if seen.get("fwVersion"):  # unknown until the device has said, as at eWeLink
            params["fwVersion"] = seen["fwVersion"]
        if d.get("staMac"):  # eWeLink has it from the device; this one never says it here
            params.setdefault("staMac", d["staMac"])
        item = {"itemData": {
            "deviceid": d["deviceid"], "devicekey": d["devicekey"],
            "apikey": d.get("apikey", d["devicekey"]),
            "name": d.get("name", "S60 (stub)"),
            "online": _online(d["deviceid"]),
            "extra": {"uiid": d.get("uiid", 190), "model": model},
            "params": params,
        }}
        # The product model (S60TPG/S60TPF) only when known: eWeLink names it,
        # the dump does not, so it is what eWeLink gave this device before (see
        # emu.sh). Without it the tool leaves the report's device model blank and
        # names the Tasmota template plainly "Sonoff S60".
        if d.get("productModel"):
            item["itemData"]["productModel"] = d["productModel"]
        return {"error": 0, "data": {"thingList": [item, *others]}}

    @staticmethod
    def other_item(other: dict) -> dict:
        """eWeLink's record of one of the account's other devices (--others-json):
        {"deviceid", "name", "productModel", "model", "uiid", "fwVersion"}, with
        a made-up key, as the record carries one."""
        item = {"itemData": {
            "deviceid": other["deviceid"], "devicekey": "stub-other-devicekey", "apikey": "stub-user-apikey",
            "name": other.get("name", other["deviceid"]), "online": bool(other.get("online", False)),
            "extra": {"uiid": other.get("uiid", 0), "model": other.get("model", "")},
            "params": {"fwVersion": other["fwVersion"]} if other.get("fwVersion") else {},
        }}
        if other.get("productModel"):
            item["itemData"]["productModel"] = other["productModel"]
        return item

    def ota_query(self, body: bytes = b"") -> dict:
        """What eWeLink offers: nothing to a device asking with its own version
        (the conversion only reads this; it delivers firmware over LAN), and the
        device's own firmware to one asking with an older one, as query_ota's
        latest_offer does."""
        from tools.ewelink.stub_cloud.firmware import FILE_NAME, version_tuple
        try:
            asked = json.loads(body).get("deviceInfoList") or [] if body else []
        except (ValueError, AttributeError):
            asked = []
        offers = []
        mine = (self.account_device() or {}).get("deviceid")
        for info in asked if isinstance(asked, list) else []:
            if (self.update and isinstance(info, dict) and info.get("deviceid") == mine
                    and version_tuple(str(info.get("version", ""))) < version_tuple(self.update.version)):
                offers.append({"deviceid": mine, "model": info.get("model"), "version": self.update.version,
                               "binList": [{"name": FILE_NAME, "downloadUrl":
                                            f"https://{self.iot_host}/ota/{FILE_NAME}",
                                            "digest": hashlib.sha256(self.update.data).hexdigest()}]})
        return {"error": 0, "data": {"otaInfoList": offers}}

    def ota_file(self, query: str) -> bytes | None:
        """The offered file, to the device's own id, as Sonoff's server asks."""
        import urllib.parse
        asked = urllib.parse.parse_qs(query).get("deviceid", [""])[0]
        if self.update is None or not asked or asked != (self.account_device() or {}).get("deviceid"):
            return None
        return self.update.data

    def ws_reply(self, msg: dict) -> dict | None:
        """The IoT-channel answer to one device message, or None to stay quiet."""
        action = msg.get("action")
        base = {"error": 0}
        # Echo back the identifiers and whichever sequence id the device used to
        # correlate the reply: the S60 register uses `d_seq` (a number), other
        # messages may use `sequence`.
        for key in ("deviceid", "apikey", "sequence", "d_seq"):
            if key in msg:
                base[key] = msg[key]
        if action in ("register", "userOnline"):
            # The heartbeat config the device waits for before it is "online".
            base["config"] = {"hb": 1, "hbInterval": self.hb_interval}
            return base
        if action == "date":
            base["date"] = _timestamp(dt.datetime.now(dt.timezone.utc))
            return base
        if action == "query" and isinstance(msg.get("params"), list) and "TZ" in msg["params"]:
            # The S61 asks its time zone after it registers, and closes the
            # channel when it is not told: UTC, no daylight saving to come.
            now = dt.datetime.now(dt.timezone.utc)
            base["params"] = {"TZ": {"utc": _timestamp(now), "dst": 0, "offset": 0,
                                     "nextDst": _timestamp(now + dt.timedelta(days=365)), "nextOffset": 0}}
            return base
        if action in ("update", "query", "upgrade", "sysmsg"):
            return base
        if action is None:
            return None  # our own keep-alive being acked
        return base  # anything else: a bare success


class Handler(socketserver.BaseRequestHandler):
    stub: Stub  # set on the server

    def handle(self) -> None:
        sock = self.request
        peer = "%s:%s" % self.client_address
        # The handshake is made here, in this connection's own thread, and not
        # while accepting: one client that stalls in it must not stop the rest.
        sock.settimeout(REQUEST_TIMEOUT)
        try:
            if isinstance(sock, ssl.SSLSocket):
                sock.do_handshake()
            header = self._read_headers(sock)
        except (ConnectionError, ssl.SSLError, OSError) as exc:
            log(peer, "connection ended before a request:", exc)
            return
        if header is None:
            return
        request_line, headers, body = header
        log(peer, "->", request_line)
        parts = request_line.split(" ")
        path, _, query = (parts[1] if len(parts) > 1 else "").partition("?")
        if request_line.startswith("GET /api/ws"):
            self._serve_ws(sock, headers, peer)
        elif path == "/dispatch/device":
            self._serve_dispatch(sock, body, peer)
        elif path == "/dispatch/app":
            self._serve_json(sock, self.stub.app_dispatch_response(), peer, "app dispatch")
        elif path == "/v2/user/login":
            self._serve_json(sock, self.stub.account_login(), peer, "account login")
        elif path == "/v2/device/thing":
            self._serve_signed_in(sock, headers, self.stub.device_thing, peer, "device list")
        elif path == "/v2/device/ota/query":
            self._serve_signed_in(sock, headers, lambda: self.stub.ota_query(body), peer, "OTA query")
        elif path.startswith("/ota/"):
            data = self.stub.ota_file(query)
            log(peer, "OTA file ->", f"{len(data)} bytes" if data else "refused")
            self._send_http(sock, 200 if data else 404, data or b"", "application/octet-stream")
        else:
            self._send_http(sock, 404, b"", "text/plain")

    def _read_headers(self, sock):
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
            if len(buf) > 64 * 1024:
                return None
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        request_line = lines[0].decode("latin1")
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                name, value = line.split(b":", 1)
                headers[name.strip().lower().decode("latin1")] = value.strip().decode("latin1")
        body = bytearray(rest)
        want = int(headers.get("content-length", 0) or 0)
        while len(body) < want:
            chunk = sock.recv(min(4096, want - len(body)))
            if not chunk:
                break
            body += chunk
        return request_line, headers, bytes(body)

    def _send_http(self, sock, status: int, body: bytes, ctype: str) -> None:
        reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
        head = (f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n").encode("latin1")
        sock.sendall(head + body)

    def _serve_json(self, sock, response: dict, peer: str, what: str) -> None:
        log(peer, f"{what} ->", json.dumps(redacted(response)))
        self._send_http(sock, 200, wire(response), "application/json")

    def _serve_signed_in(self, sock, headers, answer, peer: str, what: str) -> None:
        token = headers.get("authorization", "").removeprefix("Bearer ")
        self._serve_json(sock, answer() if self.stub.signed_in(token) else TOKEN_ENDED, peer, what)

    def _serve_dispatch(self, sock, body: bytes, peer: str) -> None:
        try:
            request = json.loads(body) if body else {}
            log(peer, "dispatch request:", json.dumps(redacted(request)))
            if isinstance(request, dict):
                self.stub.heard(request)
        except ValueError:
            log(peer, "dispatch request (non-JSON):", body[:200])
        response = self.stub.dispatch_response()
        log(peer, "dispatch ->", json.dumps(response))
        self._send_http(sock, 200, wire(response), "application/json")

    def _serve_ws(self, sock, headers, peer: str) -> None:
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        sock.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("latin1"))
        conn = Conn(sock, peer)
        role: str | None = None   # "device" or "app", from the first message
        deviceid: str | None = None
        sock.settimeout(None)  # long-lived: the device speaks every heartbeat interval
        log(peer, "websocket open")
        try:
            while True:
                try:
                    opcode, payload = read_frame(sock)
                except (ConnectionError, ssl.SSLError, OSError) as exc:
                    log(peer, "websocket closed:", exc)
                    return
                if opcode == 0x8:
                    log(peer, "websocket close frame")
                    return
                if opcode == 0x9:  # ping -> pong
                    conn.send(payload, opcode=0xA)
                    continue
                if opcode == 0xA:
                    continue
                try:
                    msg = json.loads(payload)
                except ValueError:
                    log(peer, "ws <- (non-JSON)", payload[:200])
                    continue
                log(peer, "ws <-", json.dumps(redacted(msg)))
                if role is None:
                    role = "device" if msg.get("userAgent") == "device" or msg.get("action") == "register" else "app"
                    log(peer, f"role: {role}")
                    if role == "device":
                        # A device that misses its heartbeats is offline, as at
                        # eWeLink: one switched off sends nothing, not a close.
                        sock.settimeout(self.stub.silence_limit())
                if role == "device":
                    deviceid = self._device_message(conn, msg, deviceid)
                else:
                    self._app_message(conn, msg)
        finally:
            if role == "app":
                _unsubscribe(conn)
            if role == "device" and deviceid:
                _drop_device(deviceid, conn)
                _tell_watchers(deviceid, False)

    def _device_message(self, conn: "Conn", msg: dict, deviceid: str | None) -> str | None:
        if msg.get("action") == "register" and msg.get("deviceid"):
            deviceid = msg["deviceid"]
            _register_device(deviceid, conn)
            _tell_watchers(deviceid, True)
            # Whether it is the account's device - the device's own id and key -
            # without logging either: a device put back on Sonoff's firmware
            # with an identity rebuilt is seen to be itself.
            account = self.stub.device or {}
            if account.get("deviceid"):
                log(conn.peer, "register: the account's own device id",
                    "yes" if deviceid == account["deviceid"] else "NO",
                    "| its key", "yes" if msg.get("apikey") in (account.get("apikey"), account.get("devicekey"))
                    else "NO")
        self.stub.heard(msg)
        reply = self.stub.ws_reply(msg)   # keep the device online (error:0 + config)
        if reply is not None:
            log(conn.peer, "ws ->", json.dumps(redacted(reply)))
            conn.send(wire(reply))
        # Pass the device's own reports (incl. its reply to a relayed command) to
        # any app watching it.
        if deviceid:
            for app in _subscribers_of(deviceid):
                try:
                    app.send(wire(msg))
                except OSError:
                    pass
        return deviceid

    def _app_message(self, conn: "Conn", msg: dict) -> None:
        target_id = msg.get("deviceid")
        if target_id and msg.get("action") not in (None, "userOnline"):
            # A command for a device: hand it to that device and watch for its
            # replies. The device's answer (relayed back) is the app's ack.
            device = _subscribe(target_id, conn)
            if device is not None:
                log(conn.peer, f"relay app->device ({msg.get('action')})")
                device.send(wire(msg))
            else:
                log(conn.peer, "relay: no such device online")
                conn.send(wire({"error": 404, "deviceid": target_id, "sequence": msg.get("sequence")}))
            return
        if msg.get("action") == "userOnline" and not self.stub.signed_in(msg.get("at")):
            log(conn.peer, "ws -> refused: a token a later sign-in ended")
            conn.send(wire(TOKEN_ENDED))
            return
        reply = self.stub.ws_reply(msg)   # userOnline handshake, keep-alives
        if reply is not None:
            log(conn.peer, "ws ->", json.dumps(redacted(reply)))
            conn.send(wire(reply))


class TLSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, context: ssl.SSLContext):
        super().__init__(address, handler)
        self.context = context

    def get_request(self):
        raw, addr = super().get_request()
        try:
            # No I/O here: the handshake happens in the connection's thread.
            return self.context.wrap_socket(raw, server_side=True,
                                            do_handshake_on_connect=False), addr
        except (ssl.SSLError, OSError):
            raw.close()
            raise


class PlainServer(socketserver.ThreadingTCPServer):
    """Plaintext variant for an emulated Beken device, which dials http://...:80."""
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--plain", action="store_true",
                        help="serve plaintext HTTP/WS, no TLS (an emulated Beken device dials "
                             "http://...:80 with the factory dispatch URL)")
    parser.add_argument("--iot-host", default="192.168.60.1",
                        help="IoT server handed back by dispatch: a numeric IP (the device "
                             "does no DNS for it) that the cert's IP SAN covers")
    parser.add_argument("--iot-port", type=int, default=443)
    parser.add_argument("--iot-domain", default="",
                        help="the hostname dispatch gives as `domain` (eWeLink gives one there), for a device "
                             "that connects by it (default: --iot-host)")
    parser.add_argument("--hb-interval", type=int, default=145)
    parser.add_argument("--device-json", type=Path, default=None,
                        help="JSON of this device's account-API view (deviceid, "
                             "devicekey, model, ...) for the tool-facing API")
    parser.add_argument("--firmware", type=Path, default=Path("/state/stub-firmware.bin"),
                        help="the update file the OTA query offers, as emu.sh makes it (firmware.py)")
    parser.add_argument("--others-json", type=Path, default=Path("/state/stub-others.json"),
                        help="the account's other devices, none emulated, as emu.sh copies them from "
                             "AUTOFLASH_EMU_STUB_OTHERS: a JSON list (Stub.other_item); none if missing")
    parser.add_argument("--reported-json", type=Path, default=Path("/state/stub-reported.json"),
                        help="what each device has reported, kept as eWeLink keeps its record of it "
                             "(unattended-test.sh keeps it with a device it saves)")
    args = parser.parse_args(argv)

    device = json.loads(args.device_json.read_text()) if args.device_json else None
    others = json.loads(args.others_json.read_text(encoding="utf-8")) if args.others_json.is_file() else []
    if others:
        log(f"the account's other devices: {len(others)}, none emulated")
    try:
        from tools.ewelink.stub_cloud.firmware import read
        update = read(args.firmware)
    except Exception as exc:  # noqa: BLE001 - the stub serves the rest without it
        log("the firmware to offer could not be read:", exc)
        update = None
    log(f"offering the device's own firmware, {update.version}, to a query with an older version" if update
        else "no firmware to offer")
    Handler.stub = Stub(args.iot_host, args.iot_port, args.hb_interval, device, update, args.iot_domain, others,
                        args.reported_json)
    if args.plain:
        server = PlainServer((args.bind, args.port), Handler)
        scheme = "plaintext"
    else:
        if not args.cert or not args.key:
            parser.error("--cert and --key are required unless --plain is given")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server = TLSServer((args.bind, args.port), Handler, context)
        scheme = "TLS"
    log(f"stub listening on {args.bind}:{args.port} ({scheme}); "
        f"dispatch -> {args.iot_host}:{args.iot_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
