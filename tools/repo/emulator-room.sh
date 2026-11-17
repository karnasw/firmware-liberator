# shellcheck shell=bash
# What every emulator script shares, sourced by tools/espressif/emulator/emu.sh,
# tools/espressif/emulator/unattended-test.sh, tools/beken/emulator/beken-emu.sh
# and tools/espressif/emulator/windows-test.sh; and by website/scripts/preview.sh,
# for checkout_files and LOCAL_API_IMAGE.

# checkout_files SRC - the files git tracks or would in SRC, NUL-separated: what an
# emulated computer's copy of the checkout holds.
checkout_files() {
    git -C "$1" ls-files -z --cached --others --exclude-standard
}

# copy_checkout SRC DEST - the files git tracks or would (git ls-files: ignored ones
# left out, wherever the rule is: .gitignore, .git/info/exclude, the user's own), as
# the emulated computer's copy of the checkout. What SRC no longer has goes from
# DEST, but for the copy's own captures/, .venv/ and .python/.
copy_checkout() {
    local src=$1 dest=$2 list
    list=$(mktemp)
    # A list git could not give, or an empty one, would empty the copy: stop instead.
    if ! checkout_files "$src" > "$list" || [ ! -s "$list" ]; then
        echo "git could not list $src's files: its copy is left as it was" >&2
        rm -f "$list"
        return 1
    fi
    mkdir -p "$dest"
    rsync -a --from0 --files-from="$list" --ignore-missing-args "$src/" "$dest/"
    LC_ALL=C sort -z "$list" > "$list.src"
    # The checkout's top-level names, in a file: grep -q on a pipe stops its writer
    # early, which under pipefail reads as no match, and a folder the checkout has went.
    sed -z 's#/.*##' "$list.src" | LC_ALL=C sort -zu > "$list.tops"
    # A top-level folder the checkout has nothing in goes whole, made writable first:
    # an old copy's read-only toolchain would refuse a file at a time.
    local top
    while IFS= read -r -d '' top; do
        case $top in captures | .venv | .python) continue ;; esac
        if ! grep -qzxF -- "$top" "$list.tops"; then
            chmod -R u+w "$dest/$top" 2>/dev/null || true
            rm -rf "${dest:?}/$top" || echo "Could not remove $dest/$top: remove it by hand" >&2
        fi
    done < <(find "$dest" -mindepth 1 -maxdepth 1 -printf '%f\0')
    (cd "$dest" && find . \( -path ./captures -o -path ./.venv -o -path ./.python \) -prune \
        -o \( -type f -o -type l \) -print0) | sed -z 's#^\./##' | LC_ALL=C sort -z > "$list.dest"
    LC_ALL=C comm -z -23 "$list.dest" "$list.src" | (cd "$dest" && xargs -0 -r rm -f --) || true
    rm -f "$list" "$list.src" "$list.dest" "$list.tops"
}

# wait_for_room MB - until this computer has MB free and the cores for one more
# emulator: one started on a starved computer fails for that alone, its device
# stopping mid-download. MB is what that emulator was measured to need, plus
# 100 (docs/emulator.md); AUTOFLASH_EMULATOR_MIN_FREE_MB stands for it. After
# AUTOFLASH_EMULATOR_ROOM_WAIT seconds (default 600) it starts anyway, saying
# so. Where /proc says nothing (macOS), it does not wait. Both emulators wait
# here, so its variables are AUTOFLASH_EMULATOR_, neither one's own.
wait_for_room() {
    local meminfo=${AUTOFLASH_EMULATOR_MEMINFO:-/proc/meminfo} loadavg=${AUTOFLASH_EMULATOR_LOADAVG:-/proc/loadavg}
    local need=${AUTOFLASH_EMULATOR_MIN_FREE_MB:-${1:?wait_for_room needs the MB this emulator needs}} limit=${AUTOFLASH_EMULATOR_ROOM_WAIT:-600} waited=0 cores free load
    [ -r "$meminfo" ] && [ -r "$loadavg" ] || return 0
    cores=$(nproc 2>/dev/null || echo 1)
    while :; do
        free=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' "$meminfo")
        load=$(cut -d' ' -f1 "$loadavg")
        if [ "${free:-0}" -ge "$need" ] && awk -v now="$load" -v cores="$cores" 'BEGIN {exit !(now < 2 * cores)}'; then
            return 0
        fi
        if [ "$waited" -ge "$limit" ]; then
            echo "Starting with ${free} MB free and a load of $load on $cores cores: a failure may be the computer's." >&2
            return 0
        fi
        [ "$waited" -gt 0 ] || echo "Waiting for room for another emulator: ${free} MB free (wanted $need), a load of $load on $cores cores..." >&2
        sleep 10
        waited=$((waited + 10))
    done
}

# The project's API, its own code (website/api/local.mjs), where an emulated
# computer sends its readouts: in a container beside it, on its Docker network,
# so nothing of this computer's - its firewall, its Node - comes between them.
# local.mjs needs Node 22.23 or newer. Each emulator starts it unless told
# --no-api, and gives its computer AUTOFLASH_READOUT_URL (readout.upload_url).
LOCAL_API_IMAGE=node:22.23.4-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

# local_api_start NAME NETWORK FOLDER [ENOUGH] - the API as container NAME on
# NETWORK, what it is asked and keeps in FOLDER; ENOUGH, the products it has
# enough readouts of (wrangler.jsonc's ENOUGH). One NAME already running goes
# first. Its readout address in LOCAL_API_READOUT_URL. ROOT: the checkout whose
# website/ it runs.
local_api_start() {
    local name=$1 network=$2 folder=$3 enough=${4:-} waited=0
    docker rm -f "$name" >/dev/null 2>&1 || true
    mkdir -p "$folder"
    docker run -d --name "$name" --network "$network" --user "$(id -u):$(id -g)" \
        -v "$ROOT/website:/website:ro" -v "$folder:/api" -e AUTOFLASH_LOCAL_PROOF_BITS -e AUTOFLASH_LOCAL_READOUT_LIMIT \
        "$LOCAL_API_IMAGE" node /website/api/local.mjs 0.0.0.0 8080 /api "$enough" >/dev/null
    until docker logs "$name" 2>&1 | grep -q '^API on'; do
        [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ] && [ $((waited += 1)) -le 60 ] || {
            echo "The API's container did not start:" >&2; docker logs "$name" >&2 2>&1; return 1; }
        sleep 1
    done
    # shellcheck disable=SC2034  # read by the script that sourced this
    LOCAL_API_READOUT_URL=$(local_api_readout_url "$name")
}

# local_api_readout_url NAME - where the running API NAME takes readouts.
local_api_readout_url() {
    echo "http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"):8080/v1/readout"
}

# local_api_stop NAME LOG - the API NAME gone, what it said kept in LOG.
local_api_stop() {
    docker logs "$1" > "$2" 2>&1 || true
    docker rm -f "$1" >/dev/null 2>&1 || true
}

# local_api_goes_with CONTAINER NAME - the API NAME gone once CONTAINER stops,
# by a watcher of its own: the container outlives the script that started it
# (exec'd, or in the background). One never started is waited for two minutes.
local_api_goes_with() {
    # shellcheck disable=SC2016  # expanded by the watcher's own shell, from its arguments
    nohup sh -c 'n=0; until docker inspect "$1" >/dev/null 2>&1 || [ $((n += 1)) -gt 120 ]; do sleep 1; done
                 docker wait "$1" >/dev/null 2>&1; docker rm -f "$2" >/dev/null 2>&1' _ "$1" "$2" >/dev/null 2>&1 &
}
