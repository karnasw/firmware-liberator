#!/bin/bash
# The emulator's image: what it is built from, and getting it.
#
#   tools/espressif/emulator/image.sh tag          the hash of what it is built from
#                                        (emu.Dockerfile, patches/, container/)
#                                        - so a change to a patch rebuilds it
#   tools/espressif/emulator/image.sh build IMAGE  build it, labelled with that hash
#   tools/espressif/emulator/image.sh pull IMAGE   the published one with that hash
#                                        (ghcr.io/adamsthws/autoflash-emu:<hash>,
#                                        which .github/workflows/emulator-image.yml
#                                        builds), tagged IMAGE - only if its
#                                        label is that hash
#   tools/espressif/emulator/image.sh ensure IMAGE [--rebuild]
#                                        IMAGE labelled with that hash: as it is,
#                                        or pulled, or built where it cannot be -
#                                        one emulator at a time, so two started
#                                        together do not both build it
#   tools/espressif/emulator/image.sh release  the release the QEMU source is
#                                        downloaded from
#   tools/espressif/emulator/image.sh pin ARCHIVE COMMIT
#                                        move the pin to the fork's source at
#                                        COMMIT (its full hash), ARCHIVE made
#                                        from it: written into UPSTREAM, as the
#                                        Beken emulator's pin is, and the
#                                        release to publish ARCHIVE as said
#
# emu.sh ensures its image every time: an image from an older checkout never
# runs unseen. AUTOFLASH_EMU_PUBLISHED names another place to pull from,
# AUTOFLASH_EMU_NO_PULL=1 always builds, and AUTOFLASH_EMU_SOURCE_URL names
# another place to download the QEMU source from.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
EMU=$ROOT/captures/emulation
PUBLISHED=${AUTOFLASH_EMU_PUBLISHED:-ghcr.io/adamsthws/autoflash-emu}

HERE=$ROOT/tools/espressif/emulator

tag() {
    # UPSTREAM is among them once the pin is there (pin_source).
    local inputs=(emu.Dockerfile patches container)
    [ ! -f "$HERE/UPSTREAM" ] || inputs+=(UPSTREAM)
    (cd "$HERE" && find "${inputs[@]}" -type f -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
}

# The QEMU fork's source: from this repository's release
# emulator-source-<upstream>-<commit>, which holds that one archive and never
# changes (a new pin is a new release), downloaded once into
# captures/emulation/cache and checked, so the image builds from nothing but
# this repository - and again without a network once it is there. UPSTREAM
# pins it (repo, commit, archive, sha256), as the Beken emulator's does; until
# the pin first moves, emu.Dockerfile's QEMU_SOURCE_SHA256 and the archive
# named below do (tests/repo/test_emulator_images.py: the next image moves them).
if [ -f "$HERE/UPSTREAM" ]; then
    pin() { sed -n "s/^$1 //p" "$HERE/UPSTREAM"; }
    QEMU_SOURCE=$(pin archive)
    QEMU_COMMIT=$(pin commit); QEMU_COMMIT=${QEMU_COMMIT:0:7}
    QEMU_SHA256=$(pin sha256)
else
    QEMU_SOURCE=qemu-lcgamboa-64aed50.tar.xz
    QEMU_COMMIT=64aed50
    QEMU_SHA256=$(sed -n 's/^ARG QEMU_SOURCE_SHA256=//p' "$HERE/emu.Dockerfile")
fi
# Named after the upstream project the archive is of (its name's first word), and its commit.
RELEASE=emulator-source-${QEMU_SOURCE%%-*}-$QEMU_COMMIT
QEMU_SOURCE_URL=${AUTOFLASH_EMU_SOURCE_URL:-https://github.com/adamsthws/firmware-liberator/releases/download/$RELEASE/$QEMU_SOURCE}
qemu_source() {
    local want=$QEMU_SHA256 cache=$EMU/cache/$QEMU_SOURCE
    [ -n "$want" ] || return 0   # a Dockerfile that fetches the source itself
    mkdir -p "$EMU/cache"
    if [ ! -f "$cache" ] || [ "$(sha256sum < "$cache" | cut -c1-64)" != "$want" ]; then
        echo "Downloading the emulator's QEMU source ($QEMU_SOURCE, about 33 MB)..." >&2
        local part
        part=$(mktemp "$cache.XXXXXX.part")
        curl -fsSL --retry 3 -o "$part" "$QEMU_SOURCE_URL" \
            || { rm -f "$part"; echo "Could not download $QEMU_SOURCE_URL" >&2; exit 1; }
        mv "$part" "$cache"
    fi
    if [ "$(sha256sum < "$cache" | cut -c1-64)" != "$want" ]; then
        echo "$cache is not the source the emulator pins; delete it and run this again" >&2
        exit 1
    fi
    cp "$cache" "$1"
}

context=""
trap '[ -z "$context" ] || rm -rf "$context"' EXIT
build() {
    context=$(mktemp -d)
    cp -r "$ROOT/tools/espressif/emulator/emu.Dockerfile" "$ROOT/tools/espressif/emulator/patches" \
          "$ROOT/tools/espressif/emulator/container" "$context/"
    qemu_source "$context/qemu-source.tar.xz"
    echo "Building the emulator image (the first time takes a few minutes)..." >&2
    docker build -q -t "$1" --build-arg QEMU_SOURCE_SHA256="$QEMU_SHA256" --label "autoflash.built-from=$(tag)" \
        --label "org.opencontainers.image.source=https://github.com/adamsthws/firmware-liberator" \
        -f "$context/emu.Dockerfile" "$context" >/dev/null
}

pull() {
    local want published
    want=$(tag)
    published=$PUBLISHED:$want
    docker pull -q "$published" >/dev/null 2>&1 || return 1
    if [ "$(docker image inspect -f '{{index .Config.Labels "autoflash.built-from"}}' "$published")" != "$want" ]; then
        echo "$published is not labelled $want: not used" >&2
        return 1
    fi
    docker tag "$published" "$1"
    echo "Using the published emulator image ($published)." >&2
}

labelled() {
    [ "$(docker image inspect -f '{{index .Config.Labels "autoflash.built-from"}}' "$1" 2>/dev/null)" = "$(tag)" ]
}

ensure() {
    local image=$1 rebuild=${2:-}
    if command -v flock >/dev/null; then
        mkdir -p "$EMU/cache"
        exec 9>"$EMU/cache/image.lock"
        flock 9
    fi
    if [ "$rebuild" != --rebuild ] && labelled "$image"; then
        return 0
    fi
    if [ "$rebuild" = --rebuild ] || [ "${AUTOFLASH_EMU_NO_PULL:-}" = 1 ] || ! pull "$image"; then
        build "$image"
    fi
}

# The pin moved, in the one form both emulators keep it in: UPSTREAM written,
# and emu.Dockerfile left to take the hash image.sh passes it.
pin_source() {
    local archive=${1:-} commit=${2:-} name sha
    [ -f "$archive" ] && [[ $commit =~ ^[0-9a-f]{40}$ ]] \
        || { echo "pin needs the archive made and the fork's full commit hash" >&2; exit 2; }
    name=$(basename "$archive")
    sha=$(sha256sum < "$archive" | cut -c1-64)
    {
        echo "# The QEMU fork the emulator is built from: lcgamboa/qemu at this commit, with"
        echo "# the subprojects its build fetches, as the archive this repository's release"
        echo "# emulator-source-${name%%-*}-<commit> holds. image.sh downloads the archive,"
        echo "# checks it against this hash, and emu.Dockerfile applies patches/ in name order."
        echo "repo https://github.com/lcgamboa/qemu.git"
        echo "commit $commit"
        echo "archive $name"
        echo "sha256 $sha"
    } > "$HERE/UPSTREAM"
    sed 's/^ARG QEMU_SOURCE_SHA256=.*/ARG QEMU_SOURCE_SHA256/' "$HERE/emu.Dockerfile" > "$HERE/emu.Dockerfile.new"
    mv "$HERE/emu.Dockerfile.new" "$HERE/emu.Dockerfile"
    echo "UPSTREAM pins $name; publish it as the release emulator-source-${name%%-*}-${commit:0:7}" >&2
}

case "${1:-}" in
    tag) tag ;;
    release) echo "$RELEASE" ;;
    pin) pin_source "${2:-}" "${3:-}" ;;
    ensure) [ -n "${2:-}" ] || { echo "ensure needs an image name" >&2; exit 2; }; ensure "$2" "${3:-}" ;;
    build) [ -n "${2:-}" ] || { echo "build needs an image name" >&2; exit 2; }; build "$2" ;;
    pull) [ -n "${2:-}" ] || { echo "pull needs an image name" >&2; exit 2; }; pull "$2" ;;
    *) sed -n "2,$(($(grep -n '^set -euo pipefail' "$0" | cut -d: -f1)-1))p" "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
