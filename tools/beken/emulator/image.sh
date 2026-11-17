#!/bin/bash
# The Beken emulator's image: what it is built from, and getting it.
#
#   tools/beken/emulator/image.sh tag          the hash of what it is built from
#                                    (UPSTREAM, patches/, requirements.txt,
#                                    the Dockerfile) - so a change to a patch
#                                    rebuilds it
#   tools/beken/emulator/image.sh build IMAGE  build it, labelled with that hash, from
#                                    the archive UPSTREAM names (downloaded once
#                                    into captures/emulation/cache/ and checked
#                                    against UPSTREAM's hash)
#   tools/beken/emulator/image.sh pull IMAGE   the published one with that hash
#                                    (ghcr.io/adamsthws/autoflash-beken-emu:<hash>,
#                                    which .github/workflows/beken-emulator-image.yml
#                                    builds), tagged IMAGE - only if its label is
#                                    that hash
#   tools/beken/emulator/image.sh ensure IMAGE [--rebuild]
#                                    IMAGE labelled with that hash: as it is, or
#                                    pulled, or built where it cannot be - one
#                                    emulator at a time, so two started together
#                                    do not both build it
#   tools/beken/emulator/image.sh release      the release the archive is
#                                    downloaded from
#   tools/beken/emulator/image.sh source [DIR]
#                                    make the archive of upstream at the commit
#                                    UPSTREAM pins, in DIR (default: the current
#                                    directory): git archive, so the same commit
#                                    gives the same bytes. It writes the archive's
#                                    name and hash into UPSTREAM and says the
#                                    release to publish it as (one archive, never
#                                    changed): how the pin moves, once the new
#                                    commit is in UPSTREAM; a change to the
#                                    emulator is a patch
#
# beken-emu.sh ensures its image every time: an image from an older checkout never
# runs unseen. AUTOFLASH_BEKEN_EMU_PUBLISHED names another place to pull from,
# AUTOFLASH_BEKEN_EMU_NO_PULL=1 always builds, and
# AUTOFLASH_BEKEN_EMU_SOURCE_URL names another place to download the archive
# from.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
CACHE=$ROOT/captures/emulation/cache
PUBLISHED=${AUTOFLASH_BEKEN_EMU_PUBLISHED:-ghcr.io/adamsthws/autoflash-beken-emu}
# What the archive holds of upstream: the emulator, not its firmwares/.
ARCHIVED="src tests tools README.md requirements.txt .gitignore"

pin() { sed -n "s/^$1 //p" "$HERE/UPSTREAM"; }
UPSTREAM_REPO=$(pin repo)
UPSTREAM_COMMIT=$(pin commit)
ARCHIVE=$(pin archive)
ARCHIVE_SHA256=$(pin sha256)
[ -n "$UPSTREAM_REPO" ] && [ -n "$UPSTREAM_COMMIT" ] && [ -n "$ARCHIVE" ] && [ -n "$ARCHIVE_SHA256" ] \
    || { echo "$HERE/UPSTREAM: needs repo, commit, archive and sha256" >&2; exit 2; }
# Named after the upstream project the archive is of (its name's first word),
# and its commit, as the Espressif emulator's are. The archive pinned now was
# published as emulator-source-beken-<commit>, after the family: the release
# that holds it keeps its name, and the next pin is named by the rule
# (tests/repo/test_emulator_images.py).
release_of() { echo "emulator-source-${1%%-*}-${2:0:7}"; }
if [ "$ARCHIVE_SHA256" = f4e3169f0b240e1978febafe2bb8452c5f6f8727ebb8ff4ac36c18a890588abf ]; then
    RELEASE=emulator-source-beken-d9a3772
else
    RELEASE=$(release_of "$ARCHIVE" "$UPSTREAM_COMMIT")
fi
ARCHIVE_URL=${AUTOFLASH_BEKEN_EMU_SOURCE_URL:-https://github.com/adamsthws/firmware-liberator/releases/download/$RELEASE/$ARCHIVE}
# Named after the image, autoflash-beken-emu, as beken-emu.sh is.
DOCKERFILE=beken-emu.Dockerfile

tag() {
    (cd "$HERE" && find UPSTREAM requirements.txt "$DOCKERFILE" patches -type f -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
}

sha256() { sha256sum < "$1" | cut -c1-64; }

# The archive, downloaded once and checked - as the Espressif emulator's
# image.sh keeps the QEMU source.
archive() {
    local cache=$CACHE/$ARCHIVE part
    mkdir -p "$CACHE"
    if [ ! -f "$cache" ] || [ "$(sha256 "$cache")" != "$ARCHIVE_SHA256" ]; then
        echo "Downloading the emulator's source ($ARCHIVE, about 300 kB)..." >&2
        part=$(mktemp "$cache.XXXXXX.part")
        curl -fsSL --retry 3 -o "$part" "$ARCHIVE_URL" \
            || { rm -f "$part"; echo "Could not download $ARCHIVE_URL" >&2; exit 1; }
        mv "$part" "$cache"
    fi
    if [ "$(sha256 "$cache")" != "$ARCHIVE_SHA256" ]; then
        echo "$cache is not the archive UPSTREAM pins; delete it and run this again" >&2
        exit 1
    fi
    echo "$cache"
}

context=""
trap '[ -z "$context" ] || rm -rf "$context"' EXIT
build() {
    local source
    source=$(archive)
    context=$(mktemp -d)
    cp -r "$HERE/$DOCKERFILE" "$HERE/patches" "$HERE/requirements.txt" "$context/"
    cp "$source" "$context/bekenemulator.tar.xz"
    echo "Building the Beken emulator image (the first time takes a minute or two)..." >&2
    docker build -q -t "$1" --build-arg SOURCE_SHA256="$ARCHIVE_SHA256" \
        --label "autoflash.built-from=$(tag)" \
        --label "org.opencontainers.image.source=https://github.com/adamsthws/firmware-liberator" \
        -f "$context/$DOCKERFILE" "$context" >/dev/null
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
    echo "Using the published Beken emulator image ($published)." >&2
}

labelled() {
    [ "$(docker image inspect -f '{{index .Config.Labels "autoflash.built-from"}}' "$1" 2>/dev/null)" = "$(tag)" ]
}

ensure() {
    local image=$1 rebuild=${2:-}
    if command -v flock >/dev/null; then
        mkdir -p "$CACHE"
        exec 9>"$CACHE/beken-image.lock"
        flock 9
    fi
    if [ "$rebuild" != --rebuild ] && labelled "$image"; then
        return 0
    fi
    if [ "$rebuild" = --rebuild ] || [ "${AUTOFLASH_BEKEN_EMU_NO_PULL:-}" = 1 ] || ! pull "$image"; then
        build "$image"
    fi
}

source_archive() {
    local out=${1:-.} clone archive sha
    # Named after upstream and the commit it is of.
    archive=bekenemulator-${UPSTREAM_COMMIT:0:7}.tar.xz
    clone=$(mktemp -d)
    trap 'rm -rf "$clone"' EXIT
    echo "Cloning $UPSTREAM_REPO at ${UPSTREAM_COMMIT:0:7}..." >&2
    git init -q "$clone"
    git -C "$clone" remote add origin "$UPSTREAM_REPO"
    git -C "$clone" fetch -q --depth 1 origin "$UPSTREAM_COMMIT"
    # shellcheck disable=SC2086  # ARCHIVED is a list of paths
    git -C "$clone" -c tar.umask=0022 archive --format=tar --prefix=BekenEmulator/ "$UPSTREAM_COMMIT" $ARCHIVED \
        | xz -9 -T1 > "$out/$archive"
    rm -rf "$clone"
    trap - EXIT
    sha=$(sha256 "$out/$archive")
    {
        echo "# The emulator the Beken work runs on: openshwprojects/BekenEmulator at this"
        echo "# commit, unmodified and without its firmwares/, as the archive this"
        echo "# repository's release $(release_of "$archive" "$UPSTREAM_COMMIT") holds (image.sh source makes it"
        echo "# from a clone, reproducibly). image.sh downloads the archive and checks it"
        echo "# against this hash, and the image applies patches/ in name order; firmware"
        echo "# dumps beken-emu.sh fetches from upstream's repository."
        echo "repo $UPSTREAM_REPO"
        echo "commit $UPSTREAM_COMMIT"
        echo "archive $archive"
        echo "sha256 $sha"
    } > "$HERE/UPSTREAM"
    echo "$sha  $out/$archive"
    echo "UPSTREAM pins it; publish it as the release $(release_of "$archive" "$UPSTREAM_COMMIT")" >&2
}

case "${1:-}" in
    tag) tag ;;
    release) echo "$RELEASE" ;;
    ensure) [ -n "${2:-}" ] || { echo "ensure needs an image name" >&2; exit 2; }; ensure "$2" "${3:-}" ;;
    build) [ -n "${2:-}" ] || { echo "build needs an image name" >&2; exit 2; }; build "$2" ;;
    pull) [ -n "${2:-}" ] || { echo "pull needs an image name" >&2; exit 2; }; pull "$2" ;;
    source) source_archive "${2:-}" ;;
    *) sed -n "2,$(($(grep -n '^set -euo pipefail' "$0" | cut -d: -f1)-1))p" "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
