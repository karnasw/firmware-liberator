#!/bin/bash
# A computer on the emulated Beken device's home Wi-Fi (beken-emu.sh computer): this
# system's roots and the device's stub's CA trusted, so trusting the stub takes
# nothing away from any other site; then python ARGS, or a shell.
#
# Started as root, it runs them as your user (HOST_UID, HOST_GID) with the
# right to move this computer's own network links (NET_ADMIN, and NET_RAW for
# DHCP), as `wifi join` does to take it onto the device's own network
# (computer/bin/wifi, first on the PATH with the nmcli stand-in).
set -euo pipefail
if [ "$(id -u)" = 0 ] && [ -n "${HOST_UID:-}" ]; then
    : "${HOST_GID:?}"
    # Your user, made here where the image has none of your id - a CI runner's
    # is 1001, the image's own user 1000 - as the Espressif emulator's
    # entrypoint does: getent failing would end this script unsaid (set -e).
    if ! getent passwd "$HOST_UID" > /dev/null; then
        getent group "$HOST_GID" > /dev/null || groupadd -g "$HOST_GID" you
        useradd -m -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash you
    fi
    home=$(getent passwd "$HOST_UID" | cut -d: -f6)
    exec setpriv --reuid="$HOST_UID" --regid="$HOST_GID" --clear-groups \
        --inh-caps +net_admin,+net_raw --ambient-caps +net_admin,+net_raw \
        env HOME="${home:-/}" "$0" "$@"
fi
export PATH=/work/tools/beken/emulator/computer/bin:$PATH
cat /etc/ssl/certs/ca-certificates.crt /device/ca.crt > /tmp/ca-bundle.pem
export SSL_CERT_FILE=/tmp/ca-bundle.pem
[ $# -gt 0 ] || exec bash
exec python "$@"
