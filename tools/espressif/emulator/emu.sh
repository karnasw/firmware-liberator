#!/bin/bash
# Start a virtual Espressif device and a "computer" to convert it from, in Docker.
#
#   tools/espressif/emulator/emu.sh            device offline: nothing reaches the internet
#   tools/espressif/emulator/emu.sh --cloud    device and tool online (real device must be unplugged)
#   tools/espressif/emulator/emu.sh --stub-cloud  a local eWeLink stub serves this instance alone, so
#                                    it comes online with no real cloud; the device stays
#                                    offline (docs/cloud-stub.md). Not with --cloud.
#   tools/espressif/emulator/emu.sh --yes      with --cloud: the real device is known to be unplugged; don't ask
#   tools/espressif/emulator/emu.sh --device MAC which backed-up device to run; asked when there are several
#   tools/espressif/emulator/emu.sh --device-dir DIR  a device with no Sonoff backup: a public one
#                                    (sanitise_device.py), whose own flash and eFuses are in DIR
#   tools/espressif/emulator/emu.sh --fresh    put the device back to its Sonoff backup, and forget the run and the account
#   tools/espressif/emulator/emu.sh --pcap     also record the device's Wi-Fi traffic (captures/emulation/state/logs)
#   tools/espressif/emulator/emu.sh --rebuild  rebuild the image first
#   tools/espressif/emulator/emu.sh --detach   start in the background; join: docker exec -it -u ubuntu autoflash-emu tmux attach -t autoflash
#   tools/espressif/emulator/emu.sh --no-firewall  without the "computer's" firewall (ufw), on by default
#   tools/espressif/emulator/emu.sh --windows  Windows is the computer, wired in by windows_link.py
#                                    (from Windows, start it all with tools/espressif/emulator/emu.ps1)
#   tools/espressif/emulator/emu.sh --no-api   readouts kept unsent: by default they go to the API's own
#                                    code (website/api/local.mjs) in a container beside the emulator
#                                    (autoflash-emu-api, or autoflash-emu-N-api), gone with it; what it
#                                    was asked and kept is in the state's api/
#   tools/espressif/emulator/emu.sh --instance N  another emulator beside the first (N = 1-99): its own
#                                    container (autoflash-emu-N), state (state-N) and browser
#                                    port (8080+N), so tests can run side by side
#
# Runs a device from its Sonoff backup, which every conversion keeps in
# captures/devices/<MAC>/sonoff-backup/ (or a dump in captures/emulation/; see
# docs/emulator.md). The backup is only read: the device runs a copy. Everything
# the emulator writes stays in captures/emulation/state/, which is private and
# Git-ignored (from a Windows checkout under WSL: ~/.local/share/autoflash-emu/state,
# on WSL's own disk); each device's flash is kept there between runs.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
# shellcheck source=/dev/null
. "$ROOT/tools/repo/emulator-room.sh"
EMU=$ROOT/captures/emulation
STATE=$EMU/state
# A Windows checkout under WSL (/mnt/c, reached through 9p) is far too slow for
# the device's flash: a write can take 100 ms, and the bridge then trips its
# interrupt watchdog while flashing Tasmota. Its state lives on WSL's own disk.
case "$ROOT" in /mnt/[a-z]/*) STATE=$HOME/.local/share/autoflash-emu/state ;; esac
PY=$ROOT/.venv/bin/python
# A Windows checkout's .venv is Windows' own: under WSL the NVS generator lives here.
[ -x "$PY" ] || PY=$HOME/.local/share/autoflash-emu/venv/bin/python
# The image tag; override to build/run an isolated image (e.g. the cloud-stub
# work) without disturbing the shared one.
IMAGE=${AUTOFLASH_EMU_IMAGE:-autoflash-emu}
NAME=autoflash-emu PORT=8080 LINK_PORT=7860

cloud=0 fresh=0 pcap=0 rebuild=0 firewall=1 windows=0 yes=0 stub=0 api=1 tty=-it device="" device_dir="" instance=""
while [ $# -gt 0 ]; do
    arg=$1
    case "$arg" in
    --device) [ $# -ge 2 ] || { echo "--device needs a MAC address" >&2; exit 2; }; device=$2; shift ;;
    --device=*) device=${arg#--device=} ;;
    --device-dir=*) device_dir=$(cd "${arg#--device-dir=}" && pwd) ;;
    --instance) [ $# -ge 2 ] || { echo "--instance needs a number, 1-99" >&2; exit 2; }; instance=$2; shift ;;
    --instance=*) instance=${arg#--instance=} ;;
    --cloud) cloud=1 ;;
    --stub-cloud) stub=1 ;;
    --yes) yes=1 ;;
    --fresh) fresh=1 ;;
    --pcap) pcap=1 ;;
    --rebuild) rebuild=1 ;;
    --detach) tty=-dt ;;
    --no-firewall) firewall=0 ;;
    --windows) windows=1 ;;
    --no-api) api=0 ;;
    *) sed -n '2,/^set -euo/{/^#/p}' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
    shift
done
if [ -n "$instance" ]; then
    case "$instance" in [1-9]|[1-9][0-9]) ;; *) echo "--instance needs a number, 1-99" >&2; exit 2 ;; esac
    STATE=$STATE-$instance NAME=autoflash-emu-$instance
    PORT=$((8080 + instance)) LINK_PORT=$((7860 + instance))
fi
# The local cloud stub stands in for eWeLink for this instance alone, so it
# cannot be combined with the real cloud (--cloud). See docs/cloud-stub.md.
# Its code, in the checkout (entrypoint.sh is told where, too).
STUB_DIR=tools/ewelink/stub_cloud
STUB_CA=$EMU/stub-cloud
if [ "$stub" = 1 ] && [ "$cloud" = 1 ]; then
    echo "--stub-cloud cannot be combined with --cloud" >&2; exit 2
fi
"$PY" -c 'import esp_idf_nvs_partition_gen' 2>/dev/null || {
    venv=$(dirname "$(dirname "$PY")")
    echo "The emulator needs ESP-IDF's NVS generator in $venv:" >&2
    echo "    python3 -m venv $venv && $PY -m pip install --require-hashes --no-deps -r $ROOT/requirements/ci.txt" >&2
    exit 1
}
# The stub's CA and the trust swap are made with `cryptography`.
[ "$stub" = 0 ] || "$PY" -c 'import cryptography' 2>/dev/null || {
    echo "--stub-cloud needs the cryptography package in $(dirname "$(dirname "$PY")"):" >&2
    echo "    $PY -m pip install --require-hashes --no-deps -r $ROOT/requirements/ci.txt" >&2
    exit 1
}
if docker ps -q --filter "name=^$NAME$" | grep -q .; then
    echo "The emulator is already running. Join it with: docker exec -it -u ubuntu $NAME tmux attach -t autoflash" >&2
    exit 1
fi

# -- which device ------------------------------------------------------------------
# shellcheck disable=SC2174  # each folder named is made 0700; its parents need not be
mkdir -p -m 0700 "$STATE" "$STATE/logs" "$STATE/devices"
efuse_mac() {
    PYTHONPATH=$ROOT "$PY" -c 'import sys; from tools.espressif.flashmap import mac_of_efuse; print(mac_of_efuse(open(sys.argv[1],"rb").read()))' "$1"
}
# The device whose flash is in $STATE/flash.bin. From before devices were chosen,
# that flash came from the dump in captures/emulation/.
# (An instance left by a checkout before 3.0.0 names it in $STATE/plug.)
if [ -f "$STATE/plug" ] && [ ! -e "$STATE/device" ]; then mv "$STATE/plug" "$STATE/device"; fi
if [ -f "$STATE/device" ]; then
    was=$(sed -n 2p "$STATE/device")
elif [ -f "$EMU/efuse.bin" ]; then
    was=$(efuse_mac "$EMU/efuse.bin")
else
    was=""
fi
if [ -n "$device_dir" ]; then
    # A public device: nothing of Sonoff's anywhere. What reads the Sonoff
    # backup - the stub's device identity, the log's redaction - is given the
    # device's own flash, in which they find nothing; it cannot go back to Sonoff.
    [ -f "$device_dir/flash.bin" ] && [ -f "$device_dir/efuse.bin" ] || {
        echo "--device-dir: $device_dir has no flash.bin and efuse.bin" >&2; exit 2; }
    [ "$fresh" = 0 ] || { echo "--fresh: a public device has no Sonoff backup to go back to" >&2; exit 2; }
    printf '%s\n%s\n' "$device_dir" "$(efuse_mac "$device_dir/efuse.bin")" > "$STATE/device"
else
    "$PY" -I "$ROOT/tools/espressif/emulator/choose_device.py" --out "$STATE/device" ${device:+--device "$device"} || exit $?
fi
SRC=$(sed -n 1p "$STATE/device")
mac=$(sed -n 2p "$STATE/device")
# The chip it is for, from its flash: which QEMU runs it.
chip=$(PYTHONPATH=$ROOT "$PY" -c 'import sys; from tools.espressif.flashmap import chip_of; print(chip_of(open(sys.argv[1],"rb").read(0x2000)))' "$SRC/flash.bin")
# Another device than last time: its own flash, kept from its last run, comes
# back, and the one that was running is put by for its next - each with the
# mode its flash was built for (below).
# (Kept in plugs/ by a checkout before 3.0.0: the same folder.)
if [ -d "$STATE/plugs" ] && [ ! -e "$STATE/devices" ]; then mv "$STATE/plugs" "$STATE/devices"; fi
if [ "$mac" != "$was" ]; then
    if [ -n "$was" ] && [ -f "$STATE/flash.bin" ]; then
        mv "$STATE/flash.bin" "$STATE/devices/${was//:/-}.bin"
        if [ -f "$STATE/.flash-mode" ]; then mv "$STATE/.flash-mode" "$STATE/devices/${was//:/-}.mode"; fi
    fi
    if [ -f "$STATE/devices/${mac//:/-}.bin" ]; then
        mv "$STATE/devices/${mac//:/-}.bin" "$STATE/flash.bin"
        rm -f "$STATE/.flash-mode"
        if [ -f "$STATE/devices/${mac//:/-}.mode" ]; then mv "$STATE/devices/${mac//:/-}.mode" "$STATE/.flash-mode"; fi
    fi
fi
# A stub run's flash differs from a cloud run's: it trusts the stub's CA and
# points dispatch at the stub. A device's flash is the device as the last run left
# it, so it is never thrown away to switch: --fresh does that, knowingly.
# (A flash from before modes were recorded is a cloud one.)
# A public device's runs nothing of Sonoff's, so it is the same either way.
mode=plain; [ "$stub" = 1 ] && mode=stub; [ -n "$device_dir" ] && mode=public
built_for=$(cat "$STATE/.flash-mode" 2>/dev/null || echo plain)
if [ "$fresh" = 0 ] && [ -f "$STATE/flash.bin" ] && [ "$built_for" != "$mode" ]; then
    want=$([ "$stub" = 1 ] && echo "--stub-cloud" || echo "a run without --stub-cloud")
    echo "This instance's device was set up for $([ "$built_for" = stub ] && echo "the local stub" || echo "the real cloud")," >&2
    echo "not for $want. Add --fresh to put it back to its Sonoff backup, or use another --instance." >&2
    exit 1
fi
# The device's product model (S60TPG UK, say) is eWeLink's name for it,
# given when it was paired, and is not in its dump. The stub reports the one
# eWeLink gave this device in the tool's own records of earlier runs (a real
# conversion's, a kept device's, an instance's), or
# AUTOFLASH_EMU_STUB_PRODUCT_MODEL; with neither, none - and the run's report
# leaves it blank rather than guess.
product_seen() {
    "$PY" -I - "$ROOT/captures" "$EMU/saved" "$(dirname "$STATE")" "${mac//:/-}" <<'EOF'
import glob, json, os, re, sys
captures, saved, states, device = sys.argv[1:]
seen = []
# A test copy of a public dump says what it is a copy of (public_dump.py).
try:
    with open(f"{captures}/devices/{device}/device.json") as f:
        model = str(json.load(f).get("product_model") or "").strip().upper()
    if model:
        seen.append((float("inf"), model))
except (OSError, ValueError):
    pass
# A device kept, or an instance left, by a checkout before 3.0.0 has its
# records in plugs/ until the tool next runs there.
for runs in (f"{captures}/devices/{device}/runs", f"{saved}/*/captures/*s/{device}/runs",
             f"{states}/state*/work/captures/*s/{device}/runs"):
    for path in glob.glob(f"{runs}/*/autoflash-state.json"):
        # Its time from the file as opened: another instance's run may
        # delete it between this opening it and asking its path.
        try:
            with open(path) as f:
                changed = os.fstat(f.fileno()).st_mtime
                model = str((json.load(f).get("inputs") or {}).get("product_model") or "").strip().upper()
        except (OSError, ValueError, AttributeError):
            continue
        if model:
            seen.append((changed, model))
print(max(seen)[1] if seen else "")
EOF
}
product=""
if [ "$stub" = 1 ]; then
    product=$(printf '%s' "${AUTOFLASH_EMU_STUB_PRODUCT_MODEL:-}" | tr '[:lower:]' '[:upper:]')
    [ -n "$product" ] || product=$(product_seen)
    case "$product" in
    ""|[A-Z0-9]*) ;;
    *) echo "AUTOFLASH_EMU_STUB_PRODUCT_MODEL is a product model such as S60TPG, not '$product'" >&2; exit 2 ;;
    esac
fi

# -- the image -----------------------------------------------------------------
# The image, labelled with a hash of what it is built from: the published one
# with that hash, or built here (tools/espressif/emulator/image.sh). An image from an
# older checkout never runs unseen. AUTOFLASH_EMU_NO_PULL=1 always builds.
ensure=(ensure "$IMAGE")
[ "$rebuild" = 0 ] || ensure+=(--rebuild)
"$ROOT/tools/espressif/emulator/image.sh" "${ensure[@]}"

# -- the device's flash and eFuses ---------------------------------------------
if [ "$fresh" = 1 ]; then
    rm -rf "$STATE/flash.bin" "$STATE/work/captures" "$STATE/.flash-mode" "$STATE/stub-reported.json"
    echo "The device is back to its Sonoff backup; the previous run and eWeLink account are forgotten."
fi
if [ "$stub" = 1 ]; then
    # Our own CA + leaf for the stub domain (once; kept stable - it is flashed in).
    # The device's own identity is read from its dump inside the container, so the
    # LAN key stays on that network (see entrypoint.sh).
    # A firmware trusting eWeLink's RSA CA is given an RSA one: a classic ESP32's has
    # the stack only for that, and the BASICR4's turns down an EC one (unknown_ca).
    if PYTHONPATH=$ROOT "$PY" -c 'import sys
from tools.espressif.emulator.patch_stock import holds_rsa_ca
sys.exit(not holds_rsa_ca(open(sys.argv[1], "rb").read()))' "$SRC/flash.bin"; then
        STUB_CA=$EMU/stub-cloud-rsa
        "$PY" -I "$ROOT/$STUB_DIR/make_ca.py" --out-dir "$STUB_CA" --rsa >/dev/null
    else
        "$PY" -I "$ROOT/$STUB_DIR/make_ca.py" --out-dir "$STUB_CA" >/dev/null
    fi
fi
if [ ! -f "$STATE/flash.bin" ]; then
    # Home Wi-Fi rewritten to the emulator's open network, Bluetooth start-up
    # skipped (the emulator has no Bluetooth), re-sealed for the bootloader. In
    # stub mode, dispatch is also redirected to the stub and our CA is trusted.
    nvs_args=() patch_args=()
    if [ "$stub" = 1 ]; then
        nvs_args=(--dis-server disp.autoflash-stub.test --dis-port 443)
        patch_args=(--trust-ca "$STUB_CA/ca.crt")
    fi
    "$PY" -I "$ROOT/tools/espressif/emulator/rewrite_nvs.py" "$SRC/flash.bin" "$STATE/wifi-flash.bin" "${nvs_args[@]}" \
        2>&1 | grep -v 'absolute path given' >/dev/null
    "$PY" -I "$ROOT/tools/espressif/emulator/patch_stock.py" "$STATE/wifi-flash.bin" "$STATE/flash.bin" "${patch_args[@]}" >/dev/null
    rm -f "$STATE/wifi-flash.bin"
    echo "$mode" > "$STATE/.flash-mode"
fi
# The firmware the stub offers as eWeLink's latest, for a run going back to
# Sonoff's with no backup: the device's own, made as the device's is (firmware.py).
if [ "$stub" = 1 ]; then
    "$PY" -I "$ROOT/$STUB_DIR/firmware.py" "$SRC/flash.bin" "$STATE/stub-firmware.bin" \
        --trust-ca "$STUB_CA/ca.crt" --product "$product" || rm -f "$STATE/stub-firmware.bin"
fi
# The account's other devices, which the stub lists beside this one, none of
# them emulated: AUTOFLASH_EMU_STUB_OTHERS names a JSON list of them
# (docs/cloud-stub.md). None unless it is set, whatever an earlier start left.
rm -f "$STATE/stub-others.json"
if [ "$stub" = 1 ] && [ -n "${AUTOFLASH_EMU_STUB_OTHERS:-}" ]; then
    cp "$AUTOFLASH_EMU_STUB_OTHERS" "$STATE/stub-others.json"
fi
# Copied by content: the backup's files are read-only, and a copy would be too.
# An ESP32-C3's file is padded to what the fork expects of it; an ESP32's is whole.
rm -f "$STATE/efuse.bin" && cat "$SRC/efuse.bin" > "$STATE/efuse.bin"
[ "$chip" = esp32 ] || truncate -s 1024 "$STATE/efuse.bin"
chmod 0600 "$STATE"/*.bin

# -- the "computer": a copy of this repository ---------------------------------
# A copy, so a run's lock renames and captures never touch the real checkout.
# The code is refreshed on every start, and the run's captures are kept.
# Only what git tracks or would (copy_checkout): never .env, which a run you sit
# through asks for as a real one does (unattended-test.sh hands it to its run
# alone), nor captures, .venv or anything else ignored.
mkdir -p "$STATE/work/captures"
copy_checkout "$ROOT" "$STATE/work"
# Not the firmware bundle in captures/safeboot-recovery-migration: a real
# bundle is set up for the real home Wi-Fi. The run builds its own, as a new
# user's would (it downloads the official files: --cloud or --stub-cloud).
# The eWeLink account email last used here (captures/settings.json) is offered
# to save typing it - but not with --fresh, which starts as a new user would.
# Not to a public device's run, which is kept as public (--device-dir).
if [ "$fresh" = 0 ] && [ -z "$device_dir" ] && [ ! -f "$STATE/work/captures/settings.json" ] \
        && [ -f "$ROOT/captures/settings.json" ]; then
    cp "$ROOT/captures/settings.json" "$STATE/work/captures/"
fi

# -- the cloud ----------------------------------------------------------------
if [ "$cloud" = 1 ] && [ "$yes" = 1 ]; then
    echo "  --yes: the real device is taken to be unplugged, as it must be while this runs."
elif [ "$cloud" = 1 ]; then
    cat <<EOF

  The emulated device is a clone of your real device: same eWeLink device id,
  keys and MAC. Online at the same time, the two fight over
  one device in your eWeLink account, and an upgrade the conversion sends
  could reach the real device instead.

  Unplug the real device now, and keep it unplugged while this runs.

EOF
    read -r -p "  Type 'unplugged' once it is: " answer
    [ "$answer" = unplugged ] || { echo "Not confirmed; start without --cloud for an offline device."; exit 1; }
fi

# Your browser, and with --windows the wire windows_link.py connects to.
publish=(-p "127.0.0.1:$PORT:8080")
[ "$windows" = 1 ] && publish+=(-p "127.0.0.1:$LINK_PORT:7860")
# Developing the QEMU fork: AUTOFLASH_EMU_QEMU=path/to/qemu-system-riscv32 (or
# -xtensa, for a classic ESP32) runs that build in place of the image's
# (tools/espressif/emulator/qemu-build.Dockerfile builds one).
dev=()
qemu_binary=qemu-system-riscv32; [ "$chip" = esp32 ] && qemu_binary=qemu-system-xtensa
[ -n "${AUTOFLASH_EMU_QEMU:-}" ] && dev=(-v "$(realpath "$AUTOFLASH_EMU_QEMU"):/opt/qemu/bin/$qemu_binary:ro")
# The stub's CA and leaf cert, read-only, for the in-container stub server.
stubvol=()
[ "$stub" = 1 ] && stubvol=(-v "$STUB_CA:/stub-cloud:ro")
# Awake while you are in it, so a conversion is not cut off by sleep: a lock
# held only while this runs (systemd-inhibit), changing no setting.
awake=()
if [ "$tty" = -it ] && systemd-inhibit --what=sleep:idle true 2>/dev/null; then
    awake=(systemd-inhibit --what=sleep:idle --who="Autoflash emulator" --why="the emulated device's conversion" --mode=block)
fi
# Capped (--memory, below): through a whole conversion on an ESP32-C3 the
# device, its router and the tool peaked at about 150 MB between them.
# The emulator's own Docker network, a /24 (AUTOFLASH_EMU_NETWORK names another).
# The conversion tool in the container sweeps the network it is on at its real
# size, and Docker's default bridge is a /16: a /22 swept, some 4 s, every run.
# Made once, on a subnet neither this computer's routes nor another Docker
# network use, and kept, as the image is; every instance shares it, as they
# shared the default bridge, and Windows' container joins it (windows-test.sh).
NETWORK=${AUTOFLASH_EMU_NETWORK:-autoflash-emu}
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    subnet=$( { ip -4 route 2>/dev/null | awk '{print $1}'
                docker network ls -q | xargs docker network inspect \
                    -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' 2>/dev/null; } | "$PY" -I -c '
import ipaddress, sys
taken = [ipaddress.ip_network(line.strip(), strict=False) for line in sys.stdin
         if line.strip() and line.strip() != "default" and ":" not in line]
for candidate in ("198.18.60.0/24", "172.31.60.0/24", "10.231.60.0/24"):
    if not any(ipaddress.ip_network(candidate).overlaps(network) for network in taken):
        print(candidate)
        break')
    if [ -z "$subnet" ]; then
        echo "No free /24 for the emulator's Docker network: make one, and name it with AUTOFLASH_EMU_NETWORK" >&2
        exit 1
    fi
    # Another instance starting at the same moment may have made it first.
    docker network create --subnet "$subnet" --label autoflash.emulator=1 "$NETWORK" >/dev/null 2>&1 ||
        docker network inspect "$NETWORK" >/dev/null
fi
model=${product:-$(product_seen)}
# Some stock firmware stops at start-up without the external RAM its device has.
psram=$(PYTHONPATH=$ROOT "$PY" -c 'import sys
from tools.autoflash import kinds
print(next((k.psram_mb for k in kinds.EVERY if k.psram_mb and k.product_of(sys.argv[1])), ""))' "$model")
# Its kind, whose wiring the device's board (board.py) takes its meter from.
kind=$(PYTHONPATH=$ROOT "$PY" -c 'import sys
from tools.autoflash import kinds
print(next((k.key for k in kinds.EVERY if k.product_of(sys.argv[1])), ""))' "$model")
# And some read a sensor on the board that the emulated ADC would read as 0.
adc=$(PYTHONPATH=$ROOT "$PY" -c 'import sys
from tools.autoflash import kinds
kind = next((k for k in kinds.EVERY if k.adc_readings and k.product_of(sys.argv[1])), None)
print(" ".join(f"{name}={value}" for name, value in kind.adc_readings) if kind else "")' "$model")
# QEMU for the dual-core ESP32 takes more than the 256 MB the ESP32-C3's stays
# in: over it, the kernel kills QEMU part way through a download, which looks
# like the device restarting. Measured through every classic ESP32's conversions it
# peaked at 250 MB, so 350 MB is its limit.
memory=256m; [ "$chip" = esp32 ] && memory=350m
# What one was measured to use with its computer (C3 146 MB, classic ESP32 249 MB), plus 100.
room=250; [ "$chip" = esp32 ] && room=350
wait_for_room "$room"
# The API's own code beside it, for the tool's readouts (emulator-room.sh), as
# long as the emulator runs. Not for Windows as the computer: the tool runs
# there, out of this environment's reach.
api_env=()
if [ "$api" = 1 ] && [ "$windows" = 0 ]; then
    local_api_start "$NAME-api" "$NETWORK" "$STATE/api" "${AUTOFLASH_API_ENOUGH:-}"
    local_api_goes_with "$NAME" "$NAME-api"
    api_env=(-e AUTOFLASH_READOUT_URL="$LOCAL_API_READOUT_URL")
fi
exec "${awake[@]}" docker run --rm $tty --name "$NAME" --hostname autoflash-emu-computer \
    --memory "${AUTOFLASH_EMU_MEMORY:-$memory}" --cpus "${AUTOFLASH_EMU_CPUS:-2}" \
    --network "$NETWORK" --cap-add NET_ADMIN --device /dev/net/tun \
    --sysctl net.ipv4.ip_forward="$(( cloud || windows ))" \
    "${publish[@]}" "${dev[@]}" \
    -v "$STATE:/state" -v "$SRC/flash.bin:/secrets/stock-flash.bin:ro" "${stubvol[@]}" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e DEVICE_MAC="$mac" -e CHIP="$chip" -e PSRAM="$psram" -e ADC="$adc" \
    -e DEVICE_KIND="$kind" -e CLOUD="$cloud" -e PCAP="$pcap" -e FIREWALL="$firewall" -e WINDOWS="$windows" \
    -e STUB_CLOUD="$stub" -e STUB_CLOUD_DIR="$STUB_DIR" -e STUB_PRODUCT_MODEL="$product" \
    -e AUTOFLASH_EMULATOR_URL="http://localhost:$PORT" "${api_env[@]}" \
    "$IMAGE"
