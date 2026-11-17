#!/bin/bash
# The Beken emulator, BekenEmulator with this project's patches, as a container
# image - built from the source archive this repository's release
# emulator-source-beken-<commit> holds, or pulled where it is published - and
# the tools in tools/beken run in it.
#
#   tools/beken/emulator/beken-emu.sh [--instance N] [--rebuild] [--fresh] COMMAND [ARGS...]
#
#   tools/beken/emulator/beken-emu.sh python [NAME=VALUE...] ARGS...
#                             run python in the emulator's container, with this checkout
#                             at /work (the working directory) and the fetched firmware
#                             mounted in; the image is ensured first. NAME=VALUE, as env
#                             takes them, are the tool's settings (its docstring's), each
#                             set in the container, empty too:
#                             tools/beken/emulator/beken-emu.sh python SSID=MyNet SECS=60 tools/beken/emulator/seed_join.py
#   tools/beken/emulator/beken-emu.sh run [NAME=VALUE...] DUMP [ARGS...]
#                             run a BK7238 dump as the emulator's tools do: main.py DUMP
#                             --with-boot -chip BK7238 --only-uart ARGS, with settings as python's
#   tools/beken/emulator/beken-emu.sh device
#                             the public S61s dump as a device on a home Wi-Fi, on the Beken
#                             emulator's own Docker network (autoflash-beken-emu, made the first time;
#                             nothing of the Espressif emulator is on it), until docker stop: its
#                             radio bridged there through a TAP device, its router .1 - its
#                             DHCP (the device at .50-.59), DNS and eWeLink, the local stub -
#                             in its container (autoflash-beken-emu, device-net.sh). Its flash, logs
#                             and the stub's CA are kept in captures/emulation/beken/state/,
#                             so it carries on as it was left (--fresh for a new device).
#                             It returns once the device is in its stub's account
#   tools/beken/emulator/beken-emu.sh ui [PORT]
#                             the device's web page, OpenBeken's or ESPHome's, for this computer's browser,
#                             at http://127.0.0.1:PORT/ (8200, or 8200+N) through ui_proxy.py: opened
#                             straight, a browser's requests lock the emulated device up
#                             (why, in ui_proxy.py)
#   tools/beken/emulator/beken-emu.sh power off|on|cycle
#                             the device's power switch: its flash kept, as a device unplugged
#   tools/beken/emulator/beken-emu.sh router off|on [PASSWORD]
#                             the home Wi-Fi's access point switched off, or on again - with
#                             PASSWORD, its password changed, as a router's: the device's
#                             details no longer join it. `router on` alone is as it began
#   tools/beken/emulator/beken-emu.sh computer [COMMAND...]
#                             a computer on the device's Wi-Fi: python COMMAND (a shell without
#                             one) in the image, as python does, there, with eWeLink the
#                             device's stub (AUTOFLASH_EWELINK_API_BASE, its CA trusted) and
#                             the device's power switch to hand (AUTOFLASH_EMULATOR_POWER), in
#                             its own copy of this checkout (the device's state directory's
#                             work/), so its runs' captures are its own:
#                             tools/beken/emulator/beken-emu.sh computer tools/autoflash/cli.py run
#                             Its Wi-Fi is emulated: `wifi` says what it can see, and `wifi join`
#                             takes it onto the device's own network, when the device raises one
#                             (computer/bin/wifi)
#                             Its readouts go to the API's own code (website/api/local.mjs) in a
#                             container beside the device (autoflash-beken-emu-api, or
#                             autoflash-beken-emu-N-api), started with the first computer and gone
#                             with the device; what it was asked and kept is in the state's api/
#   tools/beken/emulator/beken-emu.sh test [ARGS...]
#                             upstream's own self-tests (tests/self_test.py ARGS,
#                             -k PATTERN selects by name), under the patches; they run
#                             upstream's firmwares/ (about 280 MB), fetched the first time
#   tools/beken/emulator/beken-emu.sh dump
#                             the public S61s dump (firmwares/Sonoff_S61s_..., the tools'
#                             default AUTOFLASH_BEKEN_EMU_DUMP), fetched from upstream's repository,
#                             which publishes it, into captures/emulation/beken/firmwares/: this
#                             repository, and the image, never hold Sonoff's firmware
#
#   --instance N              another emulated device beside the first (N = 1-99), for
#                             device, ui, power and computer: its own container
#                             (autoflash-beken-emu-N), network (autoflash-beken-emu-N), state
#                             (captures/emulation/beken/state-N/) and web page
#                             port (8200+N), so tests can run side by side
#   --rebuild                 rebuild the image first
#   --no-api                  with computer: its readouts kept unsent, not sent to the API
#   --fresh                   with device: the device as new - it and the computers on its
#                             Wi-Fi stopped, its state (flash, runs, captures) deleted -
#                             started again on Sonoff's firmware from the dump
#
# The image is ensured first, by python, run, device, computer and test: as it
# is, or pulled, or built where it is not published (image.sh, which says how).
# AUTOFLASH_BEKEN_EMU_IMAGE names the image (default autoflash-beken-emu),
# AUTOFLASH_BEKEN_EMU_NETWORK the device's network (default autoflash-beken-emu,
# or autoflash-beken-emu-N), AUTOFLASH_BEKEN_EMU_COMPUTER a name for the
# computer's container, AUTOFLASH_BEKEN_EMU_FLASH another whole flash under this
# checkout (rebuild_dump.py's) for a new device to start from, and image.sh's
# own (AUTOFLASH_BEKEN_EMU_PUBLISHED, AUTOFLASH_BEKEN_EMU_NO_PULL,
# AUTOFLASH_BEKEN_EMU_SOURCE_URL) where to get the image. In the container,
# AUTOFLASH_BEKEN_EMU_DUMP is the dump its tools start from (a setting given,
# AUTOFLASH_BEKEN_EMU_DUMP=/work/captures/your-dump.bin, say, overrides it), and
# nothing else of your environment is there: a variable set for something else
# (SERVER, PASS) changes no tool. Its folders are in captures/emulation/beken/
# (folders.py, which moves an earlier checkout's there). Tools run as your own
# user, so what they write in /work is yours.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
# shellcheck source=/dev/null
. "$ROOT/tools/repo/emulator-room.sh"
EMU=$ROOT/captures/emulation
FIRMWARES=$EMU/beken/firmwares
PUBLIC_DUMP=Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin
IMAGE=${AUTOFLASH_BEKEN_EMU_IMAGE:-autoflash-beken-emu}
DEVICE=autoflash-beken-emu NETWORK=autoflash-beken-emu STATE=state UI_PORT=8200

usage() { sed -n "2,$(($(grep -n '^set -euo pipefail' "$0" | cut -d: -f1)-1))p" "$0" | sed 's/^# \{0,1\}//'; exit 2; }
rebuild="" fresh=0 api=1 instance=""
while [ $# -gt 0 ]; do
    case "$1" in
    --instance) [ $# -ge 2 ] || { echo "--instance needs a number, 1-99" >&2; exit 2; }; instance=$2; shift ;;
    --instance=*) instance=${1#--instance=} ;;
    --rebuild) rebuild=--rebuild ;;
    --fresh) fresh=1 ;;
    --no-api) api=0 ;;
    --*) usage ;;
    *) break ;;
    esac
    shift
done
if [ -n "$instance" ]; then
    case "$instance" in [1-9]|[1-9][0-9]) ;; *) echo "--instance needs a number, 1-99" >&2; exit 2 ;; esac
    DEVICE=$DEVICE-$instance NETWORK=$NETWORK-$instance STATE=$STATE-$instance UI_PORT=$((UI_PORT + instance))
fi
[ "$fresh" = 0 ] || [ "${1:-}" = device ] || { echo "--fresh is for device" >&2; exit 2; }
NETWORK=${AUTOFLASH_BEKEN_EMU_NETWORK:-$NETWORK}
DEVICE_STATE=$EMU/beken/$STATE
# An earlier checkout's folders, named after the S61, moved to these once.
case "${1:-}" in dump|python|run|device|ui|power|router|computer|test) python3 -I "$HERE/folders.py" migrate ;; esac

# A tool's settings: the NAME=VALUE arguments python and run are given before
# anything else, as env takes them, each passed into the container (-e
# NAME=VALUE, empty ones empty). TOOL_SETTINGS holds them; shift past them by
# its length over two.
TOOL_SETTINGS=()
settings() {
    local arg
    for arg; do
        [[ $arg =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || break
        TOOL_SETTINGS+=(-e "$arg")
    done
}

pin() { sed -n "s/^$1 //p" "$HERE/UPSTREAM"; }
UPSTREAM_REPO=$(pin repo)
UPSTREAM_COMMIT=$(pin commit)

# The image, labelled with the hash of what it is built from (image.sh). Its
# own process, so its lock is let go once the image is there: what runs next
# (exec docker run) would otherwise hold it for as long as it runs, and every
# other emulator wait.
ensure() {
    "$HERE/image.sh" ensure "$IMAGE" ${rebuild:+"$rebuild"}
}

# Firmware dumps come from upstream's repository, which publishes them, never
# from this one: the paths given (sparse-checkout patterns) from its firmwares/,
# at the pinned commit, into captures/emulation/beken/firmwares/.
fetch_firmwares() {
    local work f
    mkdir -p "$FIRMWARES"
    work=$(mktemp -d "$FIRMWARES.XXXXXX.part")
    trap 'rm -rf "$work"' EXIT
    git init -q "$work"
    git -C "$work" remote add origin "$UPSTREAM_REPO"
    git -C "$work" sparse-checkout set --no-cone "$@"
    git -C "$work" fetch -q --depth 1 --filter=blob:none origin "$UPSTREAM_COMMIT"
    git -C "$work" -c advice.detachedHead=false checkout -q "$UPSTREAM_COMMIT"
    for f in "$work"/firmwares/*; do [ -e "$FIRMWARES/$(basename "$f")" ] || cp "$f" "$FIRMWARES/"; done
    rm -rf "$work"
    trap - EXIT
}

dump() {
    [ -f "$FIRMWARES/$PUBLIC_DUMP" ] && return 0
    echo "Fetching the public S61s dump from $UPSTREAM_REPO (4 MB)..." >&2
    fetch_firmwares "/firmwares/$PUBLIC_DUMP"
}

# Upstream's self-tests run every firmware in firmwares/ (about 280 MB).
firmwares() {
    [ -f "$FIRMWARES/OpenBK7238_QIO_1.18.300.bin" ] && return 0
    echo "Fetching upstream's firmwares/ for its self-tests (about 280 MB)..." >&2
    fetch_firmwares /firmwares/
}

# python in the container: this checkout at /work, the dumps at the emulator's
# firmwares/, as your own user; a terminal when there is one.
container() {
    local tty=() workdir="/work${PWD#"$ROOT"}"
    [ -t 0 ] && [ -t 1 ] && tty=(-t)
    # WORK: another tree at /work (computer's copy), worked in at its top.
    [ -n "${WORK:-}" ] && workdir=/work
    mkdir -p "$FIRMWARES"
    # exec: a signal to this script (timeout, Ctrl-C) reaches the docker client,
    # which passes it to the container; a child docker would run on, holding the
    # image lock, with the container to be stopped by hand.
    # CONTAINER_OPTIONS: what a caller adds (computer's network and settings).
    # CONTAINER_USER: another user to start as (the computer's root, which hands over to yours).
    exec docker run --rm -i "${tty[@]}" ${CONTAINER_OPTIONS[@]+"${CONTAINER_OPTIONS[@]}"} \
        --user "${CONTAINER_USER:-$(id -u):$(id -g)}" \
        -v "${WORK:-$ROOT}:/work" -v "$FIRMWARES:/opt/beken-emulator/firmwares:ro" \
        -e AUTOFLASH_BEKEN_EMU_DUMP="/opt/beken-emulator/firmwares/$PUBLIC_DUMP" \
        ${TOOL_SETTINGS[@]+"${TOOL_SETTINGS[@]}"} -w "$workdir" "$IMAGE" "$@"
}

# The whole flash a new device starts from, as its container sees it.
device_dump() {
    local own=${AUTOFLASH_BEKEN_EMU_FLASH:-}
    if [ -z "$own" ]; then echo "/opt/beken-emulator/firmwares/$PUBLIC_DUMP"; return; fi
    own=$(realpath "$own")
    case "$own" in "$ROOT"/*) echo "/work${own#"$ROOT"}" ;;
        *) echo "$own is outside this checkout: put it under captures/" >&2; exit 2 ;; esac
}

# The device's network, a /24 of its own: the tool sweeps the network it is on
# at its real size, and Docker's default is a /16. Its gateway is .254 and
# Docker hands out .128 and up, so .1-.127 are the device's home network's own:
# its router .1 and the device's pool (device-net.sh). Made once, on a subnet
# neither this computer's routes nor another Docker network use, and kept.
network() {
    if docker network inspect "$NETWORK" >/dev/null 2>&1; then
        [ "$(docker network inspect -f '{{range .IPAM.Config}}{{.IPRange}}{{end}}' "$NETWORK")" = "$(subnet).128/25" ] && return 0
        echo "The Docker network $NETWORK is not laid out as the device's home network:" >&2
        echo "remove it (docker network rm $NETWORK) and run this again" >&2
        exit 1
    fi
    local subnet attempt
    for attempt in 1 2 3 4 5 6 7 8; do
        subnet=$( { ip -4 route 2>/dev/null | awk '{print $1}'
                    docker network ls -q | xargs docker network inspect \
                        -f '{{range .IPAM.Config}}{{println .Subnet}}{{end}}' 2>/dev/null; } | python3 -I -c '
import ipaddress, sys
taken = [ipaddress.ip_network(line.strip(), strict=False) for line in sys.stdin
         if line.strip() and line.strip() != "default" and ":" not in line]
# One a device: the fault suite runs several side by side, each on its own.
for candidate in [f"198.18.{n}.0/24" for n in range(61, 128)] + [f"172.31.{n}.0/24" for n in range(61, 128)]:
    if not any(ipaddress.ip_network(candidate).overlaps(network) for network in taken):
        print(candidate)
        break')
        [ -n "$subnet" ] || { echo "No free /24 for the device's Docker network: make one, and name it with AUTOFLASH_BEKEN_EMU_NETWORK" >&2; exit 1; }
        docker network create --subnet "$subnet" --ip-range "${subnet%.*}.128/25" --gateway "${subnet%.*}.254" \
            --label autoflash.emulator=1 "$NETWORK" >/dev/null 2>&1 && return 0
        # Made by another run starting at the same moment - or that subnet taken
        # by another device's network made since it was chosen: choose again.
        docker network inspect "$NETWORK" >/dev/null 2>&1 && return 0
        sleep "$attempt"
    done
    echo "Could not make the Docker network $NETWORK" >&2
    exit 1
}

# The device's network's first three numbers (198.18.61, say).
subnet() {
    local cidr
    cidr=$(docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$NETWORK")
    echo "${cidr%.*}"
}

case "${1:-}" in
    dump) dump ;;
    python)
        shift; settings "$@"; shift $((${#TOOL_SETTINGS[@]} / 2))
        ensure; dump || echo "No public dump: give the tool a dump of your own." >&2
        container "$@" ;;
    run)
        shift; settings "$@"; shift $((${#TOOL_SETTINGS[@]} / 2))
        [ -n "${1:-}" ] || { echo "run needs a dump" >&2; exit 2; }
        ensure
        dump_path=$(realpath "$1"); shift
        case "$dump_path" in "$ROOT"/*) dump_path="/work${dump_path#"$ROOT"}" ;;
            *) echo "$dump_path is outside this checkout: put it under captures/" >&2; exit 2 ;; esac
        container /opt/beken-emulator/src/main.py "$dump_path" --with-boot -chip BK7238 --only-uart "$@" ;;
    device)
        if [ "$fresh" = 1 ]; then
            # Everything on its Wi-Fi first - the device and any computer's run - so
            # nothing writes its state while it goes.
            if docker network inspect "$NETWORK" >/dev/null 2>&1; then
                mapfile -t running < <(docker ps -q --filter "network=$NETWORK")
                [ ${#running[@]} -eq 0 ] || docker stop -t 30 "${running[@]}" >/dev/null
            fi
            case "$DEVICE_STATE" in "$EMU"/beken/state*) rm -rf "$DEVICE_STATE" ;; esac
            echo "$DEVICE is new: its state is gone; it starts again from the dump." >&2
        fi
        ensure; dump; network
        mkdir -p "$DEVICE_STATE/air"
        starts_from=$(device_dump)
        # Measured: the device 152 MB and its computer 97, plus 100.
        wait_for_room 350
        docker run -d --rm --name "$DEVICE" --network "$NETWORK" --cap-add NET_ADMIN --device /dev/net/tun \
            -v "$ROOT:/work:ro" -v "$FIRMWARES:/opt/beken-emulator/firmwares:ro" -v "$DEVICE_STATE:/state" \
            -e AUTOFLASH_BEKEN_EMU_DUMP="$starts_from" \
            -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" -e DEVICE_TRACE="${DEVICE_TRACE:-}" \
            --entrypoint bash "$IMAGE" /work/tools/beken/emulator/device-net.sh >/dev/null
        echo "The device is starting as $DEVICE on $NETWORK, its router at $(subnet).1; its serial output is in" >&2
        echo "${DEVICE_STATE#"$ROOT"/}/serial.log; docker stop $DEVICE ends it." >&2
        # A run needs the device in its account, which the stub only has once
        # the device has registered with it: a minute or two from a new device.
        for _ in $(seq 120); do
            grep -qs '"apikey"' "$DEVICE_STATE/stub-reported.json" && { echo "$DEVICE is online." >&2; exit 0; }
            docker ps -q --filter "name=^$DEVICE\$" | grep -q . || { echo "$DEVICE stopped: see its serial.log" >&2; exit 1; }
            sleep 5
        done
        echo "$DEVICE has not reached its stub after 10 minutes: see its serial.log" >&2; exit 1 ;;
    ui)
        # The device's address as its router gave it last (its firmware may have
        # changed, and with it its lease).
        read -r _ _ lease named _ < <(docker exec "$DEVICE" sh -c 'sort -n /run/autoflash-beken-emu/leases | tail -1' 2>/dev/null)
        # Released while still in use (ESPHome, rejoining after its network went):
        # the last its router acknowledged.
        [ -n "${lease:-}" ] || read -r lease named < <(docker exec "$DEVICE" sh -c \
            'sed -n "s/.*DHCPACK([^)]*) \([^ ]*\) [^ ]* \([^ ]*\).*/\1 \2/p" /run/autoflash-beken-emu/dnsmasq.log | tail -1' 2>/dev/null)
        [ -n "${lease:-}" ] || { echo "No device on its Wi-Fi yet: start it (beken-emu.sh device) and give it a minute" >&2; exit 1; }
        # Which firmware, by the name it gave its router: esphome-abcdef, or
        # openbeken-abcdef (OpenBK7238_ABCDEF before its first start's naming;
        # autoflash-recovery-CDEF as the interim firmware).
        case "${named:-}" in
            esphome*) firmware=esphome ;;
            openbeken-*|OpenBK*|autoflash-recovery-*) firmware=openbeken ;;
            *) echo "The device at $lease runs neither OpenBeken nor ESPHome (it calls itself ${named:-nothing}): Sonoff's firmware has no page" >&2; exit 1 ;;
        esac
        exec python3 -I "$HERE/ui_proxy.py" --device "$lease" --firmware "$firmware" --listen "127.0.0.1:${2:-$UI_PORT}" ;;
    power)
        case "${2:-}" in
            off|on) echo "$2" > "$DEVICE_STATE/power" ;;
            cycle) echo off > "$DEVICE_STATE/power"; sleep 2; echo on > "$DEVICE_STATE/power" ;;
            *) echo "power off, on or cycle" >&2; exit 2 ;;
        esac ;;
    router)
        case "${2:-}" in
            off) echo off > "$DEVICE_STATE/router" ;;
            on) echo "on${3:+ $3}" > "$DEVICE_STATE/router" ;;
            *) echo "router off, or on [PASSWORD]" >&2; exit 2 ;;
        esac ;;
    computer)
        shift; ensure; network
        [ -f "$DEVICE_STATE/ca.crt" ] || { echo "No device yet: start it first (beken-emu.sh device)" >&2; exit 1; }
        # The computer's own copy of this checkout, refreshed each time, its
        # captures kept: a device's runs are filed apart from every other's. Only
        # what git tracks or would, as emu.sh's (copy_checkout).
        WORK=$DEVICE_STATE/work
        mkdir -p "$WORK/captures"
        copy_checkout "$ROOT" "$WORK"
        # The device's power switch too, so a run can offer to switch it off and on;
        # and which terminal it is shown in (VS Code's: output.start_at_top).
        [ -f "$DEVICE_STATE/power" ] || echo on > "$DEVICE_STATE/power"
        [ -f "$DEVICE_STATE/router" ] || echo on > "$DEVICE_STATE/router"
        # A computer starts on the home network: the station hears the device's
        # own network, and joins it only once this one does (device.py follow_air).
        : > "$DEVICE_STATE/joins"
        mkdir -p "$DEVICE_STATE/air"
        # The API's own code for its readouts (emulator-room.sh), one a device,
        # kept as long as the device runs, so a second computer's readout finds
        # the first's; none with --no-api, the readouts kept unsent.
        readout_url=""
        if [ "$api" = 1 ]; then
            if docker ps -q --filter "name=^$DEVICE-api\$" | grep -q .; then
                readout_url=$(local_api_readout_url "$DEVICE-api")
            else
                local_api_start "$DEVICE-api" "$NETWORK" "$DEVICE_STATE/api" "${AUTOFLASH_API_ENOUGH:-}"
                local_api_goes_with "$DEVICE" "$DEVICE-api"
                readout_url=$LOCAL_API_READOUT_URL
            fi
        fi
        CONTAINER_OPTIONS=(${AUTOFLASH_BEKEN_EMU_COMPUTER:+--name "$AUTOFLASH_BEKEN_EMU_COMPUTER"}
                           --network "$NETWORK" -v "$DEVICE_STATE/ca.crt:/device/ca.crt:ro"
                           -v "$DEVICE_STATE/power:/device/power" -e AUTOFLASH_EMULATOR_POWER=/device/power
                           -v "$DEVICE_STATE/router:/device/router" -e AUTOFLASH_EMULATOR_ROUTER=/device/router
                           -v "$DEVICE_STATE/joins:/device/joins"
                           -v "$DEVICE_STATE/air:/device/air:ro" --cap-add NET_ADMIN
                           -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)"
                           -e TERM_PROGRAM -e AUTOFLASH_READOUT_URL="$readout_url"
                           -e AUTOFLASH_EWELINK_API_BASE="https://$(subnet).1" --entrypoint bash)
        CONTAINER_USER=0:0 container /work/tools/beken/emulator/computer.sh "$@" ;;
    test) shift; ensure; firmwares; container /opt/beken-emulator/tests/self_test.py "$@" ;;
    *) usage ;;
esac
