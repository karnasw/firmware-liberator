#!/usr/bin/env python3
"""Complete the eWeLink cloud cycle: the emulated S61 comes ONLINE against a stub.

Builds on cloud_dial.py (which gets the device to dial GET /dispatch/device) by
standing up the local CoolKit stub (tools/ewelink/stub_cloud/stub_server.py) on
loopback and bridging the emulator's netstack to it, then driving the whole
handshake:

    GET /dispatch/device   -> stub answers {error:0, IP, domain, port} (itself)
    wss:// connect, GET /api/ws, TLS handshake  -> WebSocket upgrade (RFC 6455)
    {"action":"register", ...}   -> stub answers {error:0, config:{hb,...}}
    => the device is ONLINE on the eWeLink IoT channel (then heartbeats / TZ query).

Dispatch is plaintext (the S61's factory dispatch URL is http://...:80), but the
IoT WebSocket is wss:// and the firmware verifies the server certificate against
a baked-in CoolKit root CA (authmode REQUIRED), so a self-signed stub cert is
rejected (bad_certificate). We clear that the same honest way the S60 does
(tools/espressif/emulator/patch_stock.py --trust-ca): a *trust-anchor swap*. The S60 swaps
the CA PEM in its flash image; the S61 does not keep its CA as a static flash
blob (it is assembled into RAM at TLS-setup time), so the swap is applied at the
one point the firmware installs it - mbedtls_x509_crt_parse: when the
firmware loads the CoolKit root CA into its trust store, we substitute our own
CA. Nothing in the verification path is weakened; only which anchor is trusted
changes, and the stub serves a leaf signed by that CA. Our CA/leaf are EC P-256
(made with openssl); the leaf carries a DNS and an IP SAN for the dialled IoT
address and serverAuth EKU, which the firmware's mbedTLS requires.

The bridge is the netstack's own relay (attach_wifi_peer(net_out=True)): the device
dials a placeholder public IP (its factory dispatch host, off its own subnet so
it routes through the gateway), which we redirect to the loopback stub -
plaintext dispatch on one port, TLS IoT on another. Everything stays on this
host; nothing reaches the real cloud.

Two synthesised pieces, as in cloud_dial.py: Wi-Fi creds seeded at
ef_get_env_blob, and the pairing-success trigger once Wi-Fi is up: what the
firmware runs then, its LAN service published (the mDNS record the tool's local
discovery finds) and the cloud started. Where all of this is in the firmware is
found in the firmware itself (stock_points.py).

  tools/beken/emulator/beken-emu.sh python tools/beken/emulator/cloud_online.py

The set-up is a CloudSession, which cloud_push.py builds on to push an
`upgrade` once the device is online.
"""

import base64
import json
import os
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "beken"))   # the Beken tools
ROOT = Path(os.environ.get("BK_EMU_ROOT", "/opt/beken-emulator"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools" / "ewelink" / "stub_cloud"))
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (  # noqa: E402
    UC_ARM_REG_CPSR,
    UC_ARM_REG_LR,
    UC_ARM_REG_PC,
    UC_ARM_REG_R0,
    UC_ARM_REG_R1,
    UC_ARM_REG_R10,
    UC_ARM_REG_R11,
    UC_ARM_REG_R12,
    UC_ARM_REG_R2,
    UC_ARM_REG_R3,
    UC_ARM_REG_R4,
    UC_ARM_REG_R5,
    UC_ARM_REG_R6,
    UC_ARM_REG_R7,
    UC_ARM_REG_R8,
    UC_ARM_REG_R9,
    UC_ARM_REG_SP)
import stock_points  # noqa: E402
import stub_server as ss  # noqa: E402


class _Compact:   # the S61's embedded JSON parser rejects whitespace between tokens
    @staticmethod
    def dumps(obj, *a, **k): return json.dumps(obj, separators=(",", ":"))
    loads = staticmethod(json.loads)


ss.json = _Compact()

DUMP = os.environ.get("AUTOFLASH_BEKEN_EMU_DUMP", str(ROOT/"firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"))
SECS = float(os.environ.get("SECS", "85")); FORCE_AT = float(os.environ.get("FORCE_AT", "34"))
HB = int(os.environ.get("HB", "10"))
SSID = os.environ.get("SSID", "AutoflashNet").encode(); PW = os.environ.get("PASS", "autoflash1234").encode()
TIMER5 = 1 << 9
COOLKIT_CN = b"CoolKit Root CA ECC"   # the baked-in trust anchor's subject CN, inside its DER

# The dispatch server the firmware dials by default (Points.dispatch_url): a placeholder
# public IP, so it routes off the device's subnet to the gateway (not loopback,
# which the device's own stack would keep). We leave the context untouched and
# redirect this target to the stub below, so nothing reaches the real address.
DISPATCH_IP, DISPATCH_PORT = "8.8.8.8", 80
WS_PORT = 443   # device-facing port dispatch hands back for the IoT WebSocket
REGS = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3, UC_ARM_REG_R4, UC_ARM_REG_R5,
        UC_ARM_REG_R6, UC_ARM_REG_R7, UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
        UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC, UC_ARM_REG_CPSR]
_PEM_B, _PEM_E = b"-----BEGIN CERTIFICATE-----", b"-----END CERTIFICATE-----"


# --- our own CA + the stub's leaf: the trust-anchor swap material (EC P-256) ---
# Regenerated each run into a temp dir: the swap is applied at runtime, so the CA
# need not persist (unlike the S60, whose firmware is patched once to trust it).
def _make_certs(d, ip):
    ca_key, ca_crt = d/"ca.key", d/"ca.crt"
    lf_key, lf_crt, lf_csr = d/"leaf.key", d/"leaf.crt", d/"leaf.csr"
    ext = d/"leaf.ext"; ext.write_text(
        "subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth\n"
        "basicConstraints=critical,CA:FALSE\n" % (ip, ip), encoding="utf-8")
    run = lambda *a: subprocess.run(a, check=True, capture_output=True)
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(ca_key))
    run("openssl", "req", "-x509", "-new", "-key", str(ca_key), "-sha256", "-days", "36500",
        "-subj", "/O=Autoflash stub/CN=Autoflash stub root CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", str(ca_crt))
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(lf_key))
    run("openssl", "req", "-new", "-key", str(lf_key), "-subj", "/CN=%s" % ip, "-out", str(lf_csr))
    run("openssl", "x509", "-req", "-in", str(lf_csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
        "-CAcreateserial", "-days", "36500", "-sha256", "-extfile", str(ext), "-out", str(lf_crt))
    return ca_crt.read_bytes(), str(lf_crt), str(lf_key)


def _is_coolkit(buf):
    i = buf.find(_PEM_B); j = buf.find(_PEM_E, i)
    if i < 0 or j < 0:
        return False
    try:
        der = base64.b64decode(buf[i+len(_PEM_B):j].replace(b"\r", b"").replace(b"\n", b""))
    except Exception:
        return False
    return COOLKIT_CN in der


def dialling_here(points):
    """`points`, if the firmware's dispatch URL is the address the relay redirects."""
    dialled = urlsplit(points.dispatch_url)
    if (dialled.hostname, dialled.port) != (DISPATCH_IP, DISPATCH_PORT):
        raise stock_points.PointError("dispatch_url", "the firmware dials %s, and the relay here redirects %s:%d"
                                      % (points.dispatch_url, DISPATCH_IP, DISPATCH_PORT))
    return points


class StockHooks:
    """What the stock firmware needs of the emulator to come online, put on
    `emu`: the integrity gate passed, the Wi-Fi credentials answered as a
    provisioned store gives them, the trust-anchor swap to `ca_pem`, the
    pairing-success trigger `force_at` seconds into a boot, and Timer 5.

    They go in at `points` (stock_points.find), given here or to `attach`
    before that app runs. Each acts only while `active`: another firmware the
    device is converted to runs other code there (tools/beken/emulator/device.py turns them
    off when it starts one). `boot()` starts the clock for the trigger again,
    as each boot of the device needs."""

    def __init__(self, emu, ssid, password, ca_pem, force_at, netlog, points=None):
        self.emu, self.netlog = emu, netlog
        self._ssid, self._password, self._our_ca, self._force_at = ssid, password, ca_pem, force_at
        self.active = True
        self.swap = {"n": 0, "skip": []}
        self.dial = {"ais_cloud_start": 0}
        self._started = time.time()
        self._st = {"forced": False, "calling": False, "prepped": False, "next": []}
        self.points = None
        self._install_timer()
        if points is not None:
            self.attach(points)

    def boot(self):
        self._started = time.time()
        self._st.update(forced=False, calling=False, prepped=False, next=[])

    def _read(self, address, n):
        try:
            return bytes(self.emu.mu.mem_read(address, n))
        except Exception:
            return b""

    def _string(self, address, n=40):
        b = self._read(address, n); z = b.find(0); return b[:z if z >= 0 else n]

    def attach(self, points):
        """Put the hooks in, at the places given. Once: a device's next boot is a new emulator."""
        self.points = points
        mu = self.emu.mu
        on = lambda f: (lambda m, a, s, u: f(m, a, s, u) if self.active else None)  # noqa: E731
        mu.hook_add(UC_HOOK_CODE, on(lambda m, a, s, u: mu.reg_write(UC_ARM_REG_R0, 0)), None,
                    points.after_memcmp, points.after_memcmp)

        def ef_get(m, a, s, u):   # the Wi-Fi credentials, as a provisioned device's store gives them
            key = self._string(mu.reg_read(UC_ARM_REG_R0)); buf = mu.reg_read(UC_ARM_REG_R1)
            outp = mu.reg_read(UC_ARM_REG_R3)
            val = self._ssid if key == b"wifi-ssid" else (self._password if key == b"wifi-pass" else None)
            if val is None:
                return
            try:
                mu.mem_write(buf, val)
                if outp:
                    mu.mem_write(outp, struct.pack("<I", len(val)))
            except Exception:
                pass
            mu.reg_write(UC_ARM_REG_R0, len(val)); mu.reg_write(UC_ARM_REG_PC, mu.reg_read(UC_ARM_REG_LR))
        mu.hook_add(UC_HOOK_CODE, on(ef_get), None, points.ef_get_env_blob, points.ef_get_env_blob)

        # --- the faithful trust swap (the S60's --trust-ca, applied at the parse point) ---
        # When the firmware loads the CoolKit root CA into a trust store, replace the PEM
        # it is about to parse with our own CA's PEM. Identified by the CoolKit CN inside
        # the decoded DER, so only the trust anchor is touched; the server leaf and any
        # other cert parse untouched. The firmware passes len incl. the NUL terminator.
        def trust(m, a, s, u):
            r1 = mu.reg_read(UC_ARM_REG_R1); r2 = mu.reg_read(UC_ARM_REG_R2)
            if r1 == 0 or not (0 < r2 <= 8192):
                return
            if not _is_coolkit(self._read(r1, min(r2, 4096))):
                return
            repl = (self._our_ca if self._our_ca.endswith(b"\n") else self._our_ca + b"\n") + b"\x00"
            if len(repl) > r2:   # must fit the firmware's buffer; our EC CA PEM does
                self.swap["skip"].append("%d>%d" % (len(repl), r2)); return
            try:
                mu.mem_write(r1, repl); mu.reg_write(UC_ARM_REG_R2, len(repl)); self.swap["n"] += 1
            except Exception as e:
                self.swap["skip"].append(str(e))
        mu.hook_add(UC_HOOK_CODE, on(trust), None, points.x509_crt_parse, points.x509_crt_parse)
        mu.hook_add(UC_HOOK_CODE, on(lambda m, a, s, u: self.dial.__setitem__(
            "ais_cloud_start", self.dial["ais_cloud_start"] + 1)), None, points.ais_cloud_start, points.ais_cloud_start)

        # The pairing-success trigger: force-call the start-cloud handler once Wi-Fi is up.
        st = self._st

        def trap(m, a, s, u):
            if st["calling"]:
                for r, v in zip(REGS, st["saved"]):
                    mu.reg_write(r, v)
                self.emu._exc_epoch += 1; st["calling"] = False
                if st["next"]:
                    force_thumb(st["next"].pop(0))
        mu.hook_add(UC_HOOK_CODE, trap, None, 0x4, 0x4)

        def force_thumb(pc):
            st["saved"] = [mu.reg_read(x) for x in REGS]; cpsr = mu.reg_read(UC_ARM_REG_CPSR)
            mu.reg_write(UC_ARM_REG_LR, 0x5); mu.reg_write(UC_ARM_REG_CPSR, cpsr | 0x20)
            mu.reg_write(UC_ARM_REG_PC, pc | 1)
            self.emu._exc_epoch += 1; st["calling"] = True

        def appfw(m, a, s, u):
            if st["forced"] or st["calling"] or time.time() - self._started < self._force_at:
                return
            if not st["prepped"]:
                st["prepped"] = True
                p = struct.unpack("<I", self._read(points.ctx_pointer, 4))[0]
                self.netlog.append("[dispatch url (factory) = %r -> redirected to the stub]"
                                   % self._string(p + points.url_offset, 32))
                try:
                    mu.mem_write(points.cloud_flag, b"\x01")
                except Exception:
                    pass
                return
            st["forced"] = True
            self.netlog.append("[pairing-success trigger @%.1f]" % (time.time() - self._started))
            # What the firmware runs once a joined device's network is up: its LAN
            # service published, then the cloud started.
            st["next"] = [points.start_cloud]
            force_thumb(points.lan_publish)
        mu.hook_add(UC_HOOK_CODE, on(appfw), None, points.appfw_loop, points.appfw_loop)

    def _install_timer(self):
        mac = self.emu.wifimac
        tick = mac.tick

        def hooked_tick(_self, insns):
            r = tick(insns)
            if self.active:
                self.emu.state.timer3_5_ctl |= TIMER5
            return r
        mac.tick = types.MethodType(hooked_tick, mac)


class CloudSession:
    """The emulated S61 and its eWeLink stub, set up and not yet running.

    `routes` maps more (address, port) pairs the device dials to loopback ports:
    cloud_push.py adds its firmware server so. `flash_writethrough` models the
    flash writes the device makes of itself (an OTA download, the bootloader's
    apply). `stub.heard` can be wrapped before `run` to act on what the device sends."""

    def __init__(self, dump=DUMP, *, ssid=SSID, password=PW, heartbeat=HB, force_at=FORCE_AT,
                 routes=None, flash_writethrough=False):
        self.stublog, self.netlog = [], []
        self.points = dialling_here(stock_points.find(stock_points.app_of_file(Path(dump).read_bytes())))
        self._ssid, self._password, self._force_at = ssid, password, force_at
        self._certs = Path(tempfile.mkdtemp(prefix="s61stub-"))
        self._our_ca, leaf_crt, leaf_key = _make_certs(self._certs, DISPATCH_IP)

        ss.log = lambda *p: self.stublog.append(" ".join(str(x) for x in p))
        self.stub = ss.Handler.stub = ss.Stub(DISPATCH_IP, WS_PORT, heartbeat)
        # The S61's dispatch parser reads keys "error","domain","IP","port", formats the
        # IoT URL as `wss://%.*s:%.*s` (PORT is a STRING; the S60 took a number), and
        # connects to `domain`. Hand back all of it, compact (see _Compact above).
        self.stub.dispatch_response = lambda: {
            "error": 0, "reason": "", "IP": DISPATCH_IP, "domain": DISPATCH_IP, "port": str(WS_PORT)}
        plain = ss.PlainServer(("127.0.0.1", 0), ss.Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(leaf_crt, leaf_key)
        tls = ss.TLSServer(("127.0.0.1", 0), ss.Handler, context)
        self.dispatch_port, self.ws_port = plain.server_address[1], tls.server_address[1]
        for server in (plain, tls):
            threading.Thread(target=server.serve_forever, daemon=True).start()

        self.emu = build_emulator(dump, chip="BK7238", with_boot=True, only_uart=True, save_decrypted=False,
                                  wifi=True, log=lambda m: self.netlog.append(str(m)))
        self.emu.flash_writethrough = flash_writethrough
        self.emu.setup()
        self.emu.net_sink = lambda m: self.netlog.append(str(m))
        self.emu.attach_wifi_peer(role="ap", ssid=ssid.decode(), passphrase=password.decode(), channel=1,
                                  peer_mac=b"\x02\x00\x00\x00\x00\x01", net_out=True)
        self._route(dict(routes or {}))
        self._hooks()

    # Keep "net_out" fully contained: the relay's outbound connects go to the stub
    # for the dispatch IP (plaintext dispatch on :80, TLS IoT otherwise), to a
    # route given, and nowhere for anything else, so nothing leaves the host.
    def _route(self, routes):
        io = self.emu.wifi_peer.net.io
        connect = io.connect

        def redirect(conn, host, port):
            if (host, port) in routes:
                return connect(conn, "127.0.0.1", routes[(host, port)])
            if host == DISPATCH_IP and port == DISPATCH_PORT:
                return connect(conn, "127.0.0.1", self.dispatch_port)   # plaintext dispatch
            if host == DISPATCH_IP:
                return connect(conn, "127.0.0.1", self.ws_port)         # TLS IoT WebSocket
            return connect(conn, "127.0.0.1", 1)                        # refuse anything else
        io.connect = redirect

    def _hooks(self):
        self.hooks = StockHooks(self.emu, self._ssid, self._password, self._our_ca, self._force_at,
                                self.netlog, self.points)
        self.swap, self.dial = self.hooks.swap, self.hooks.dial

    def run(self, secs, until=None):
        """Run the device for `secs` seconds, or until `until()` is true (asked
        every 2 s), then stop it."""
        self.hooks.boot()
        self._started = time.time()
        threading.Thread(target=self.emu.run, daemon=True).start()
        while time.time() - self._started < secs and not (until and until()):
            time.sleep(min(2.0, max(0.0, secs - (time.time() - self._started))))
        try:
            self.emu.stop()
        except Exception:   # the emulator may already have stopped by itself
            pass
        time.sleep(0.5)

    def online(self):
        """dispatch answered, WebSocket open, registered, ONLINE (error 0 with a config)."""
        log = self.stublog
        return (any("dispatch ->" in ln for ln in log), any("websocket open" in ln for ln in log),
                any("register" in ln for ln in log),
                any("ws ->" in ln and '"config"' in ln and '"error":0' in ln for ln in log))

    def flash(self):
        """The flash as the device left it: its writes included, with flash_writethrough."""
        emu = self.emu
        return bytes(emu.physical_flash if emu.physical_flash is not None else emu.raw_flash)


def main():
    session = CloudSession()
    session.run(SECS)
    p = session.emu.wifi_peer; nst = getattr(p, "net", None); ip = getattr(nst, "device_ip", None)

    def ips(b): return ".".join(str(x) for x in b) if isinstance(b, (bytes, bytearray)) and len(b) == 4 else str(b)
    print("=== BK7238 CLOUD ONLINE (dispatch :%d plaintext, ws :%d TLS, trust-swapped) ==="
          % (session.dispatch_port, session.ws_port), flush=True)
    print("  device IP = %s   dial-chain = %s   CA trust-swaps = %d%s"
          % (ips(ip), dict(session.dial), session.swap["n"],
             (" skipped=%s" % session.swap["skip"] if session.swap["skip"] else "")), flush=True)
    print("  --- stub (eWeLink CoolKit) log ---", flush=True)
    for ln in session.stublog:
        print("    " + re.sub(r"^\S+\s", "", ln)[:150], flush=True)
    dispatched, wsopen, reg, online = session.online()
    print("  --- verdict ---", flush=True)
    print("  dispatch answered: %s | websocket open: %s | register: %s | online (error:0+config): %s"
          % (dispatched, wsopen, reg, online), flush=True)
    print("  %s" % ("PASS: the device trusts our CA, completed the eWeLink cloud handshake and is ONLINE."
                    if (wsopen and reg and online) else
                    "INCOMPLETE: see the stub log + netstack trace below."), flush=True)
    if not (wsopen and reg and online):
        print("    [raw netlog tail, last 30 of %d] ---" % len(session.netlog), flush=True)
        for ln in session.netlog[-30:]:
            print("    net: " + str(ln)[:150], flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
