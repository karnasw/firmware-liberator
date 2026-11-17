#!/bin/bash
# Runs as root when the container starts (see tools/espressif/emulator/emu.sh), then
# hands over to you, as your own user id, in a tmux session.
set -euo pipefail

: "${HOST_UID:?}" "${HOST_GID:?}" "${DEVICE_MAC:?}" "${CLOUD:=0}" "${PCAP:=0}" "${FIREWALL:=1}" "${WINDOWS:=0}" "${STUB_CLOUD:=0}" "${STUB_PRODUCT_MODEL:=}"
RUN=/run/autoflash-emu
# The home network: the device's tap on its own, or with emu.sh --windows a
# bridge of it and the wire to Windows, which is then the computer on it.
LAN=tap0
[ "$WINDOWS" = 1 ] && LAN=br0 && cp /opt/autoflash-emu/motd.windows /opt/autoflash-emu/motd
mkdir -p "$RUN" /state/logs

# -- you, with sudo that asks for your password, as on a real computer ----------
# The password is autoflash: the welcome message says so, and the conversion tool
# says so where it warns that sudo may ask. Only the emulator's own
# Wi-Fi switching runs without it: on a real computer that is the desktop's
# Wi-Fi menu, which asks for nothing.
user=$(getent passwd "$HOST_UID" | cut -d: -f1 || true)
if [ -z "$user" ]; then
    getent group "$HOST_GID" >/dev/null || groupadd -g "$HOST_GID" you
    useradd -m -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash you
    user=you
fi
SUDO_PASSWORD=autoflash
echo "$user:$SUDO_PASSWORD" | chpasswd
printf '%s\n' "Defaults lecture=never" \
    "$user ALL=(root) NOPASSWD: /opt/autoflash-emu/bin/wifi-root" \
    "$user ALL=(ALL) ALL" > /etc/sudoers.d/autoflash-emu
chmod 0440 /etc/sudoers.d/autoflash-emu
home=$(getent passwd "$user" | cut -d: -f6)

# -- a firewall, on, as on a typical Linux desktop ---------------------------
# ufw blocking incoming connections, so the conversion has to add its
# temporary rule for the device, through sudo, and remove it again. Allowed
# here is only what the emulator itself needs: the device's DHCP and DNS from
# this "computer" (its router), and your browser's port 8080. Turned on
# before the rules below, so it cannot disturb them. emu.sh --no-firewall
# leaves it off.
if [ "$FIREWALL" = 1 ]; then
    # A container may not change kernel settings, and has no IPv6 here.
    : > /etc/ufw/sysctl-none.conf
    sed -i -e 's|^IPT_SYSCTL=.*|IPT_SYSCTL=/etc/ufw/sysctl-none.conf|' \
           -e 's|^IPV6=.*|IPV6=no|' /etc/default/ufw
    ufw --force reset >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow in on "$LAN" to any port 67 proto udp comment 'emulator: DHCP for the device' >/dev/null
    ufw allow in on "$LAN" to any port 53 comment 'emulator: DNS for the device' >/dev/null
    [ "$STUB_CLOUD" = 1 ] && ufw allow in on "$LAN" to any port 443 proto tcp comment 'emulator: the local cloud stub' >/dev/null
    ufw allow in on eth0 to any port 8080 proto tcp comment 'emulator: your browser' >/dev/null
    if [ "$WINDOWS" = 1 ]; then
        ufw allow in on eth0 to any port 7860 proto tcp comment 'emulator: the wire to Windows' >/dev/null
    fi
    ufw logging off >/dev/null
    ufw --force enable >/dev/null
else
    # The welcome message says the firewall is on: not this time.
    on="This computer's firewall (ufw) is on, as on a typical Linux desktop."
    off=$(printf '%-*s' "${#on}" "This computer's firewall is off (emu.sh --no-firewall).")
    sed -i "s|$on|$off|" /opt/autoflash-emu/motd
fi

# -- internet: only with --cloud, or for this computer with --stub-cloud -------
# Without either nothing in here reaches the internet - neither the device nor the
# conversion tool - so the device's shared eWeLink identity cannot be used by
# mistake. Replies to your browser (published port 8080) still get out.
iptables -P FORWARD DROP
if [ "$CLOUD" = 1 ]; then
    iptables -A FORWARD -i "$LAN" -o eth0 -j ACCEPT
    iptables -A FORWARD -i eth0 -o "$LAN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -t nat -A POSTROUTING -s 192.168.60.0/24 -o eth0 -j MASQUERADE
elif [ "$STUB_CLOUD" = 1 ]; then
    # The local stub is the device's cloud, so the device stays on its own network.
    # This computer may fetch what a new user's run downloads (the official
    # firmware files), but not eWeLink: the tool is pointed at the stub, and
    # eWeLink's own addresses lead nowhere from here.
    for host in {eu,us,as}-apia.coolkit.cc cn-apia.coolkit.cn {eu,us,as}-dispa.coolkit.cc cn-dispa.coolkit.cn; do
        echo "0.0.0.0 $host" >> /etc/hosts
    done
else
    iptables -A OUTPUT -o eth0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -o eth0 -j DROP
fi

# -- this computer to the device, as on the same Wi-Fi --------------------------
# windows-test.sh routes 192.168.60.0/24 on the computer you started it from
# through here, so a browser there reaches the device at its own address, as the
# conversion's last link gives it. Only that computer (this container's
# gateway) to the device, and the device's replies: nothing else is forwarded, and
# without --cloud the device reaches no internet still.
gateway=$(ip route show default | awk '{print $3; exit}')
if [ -n "$gateway" ]; then
    iptables -A FORWARD -i eth0 -o "$LAN" -s "$gateway" -d 192.168.60.50 -j ACCEPT
    iptables -A FORWARD -i "$LAN" -o eth0 -s 192.168.60.50 -d "$gateway" \
        -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
fi

# -- the device's radio: a tap device QEMU's emulated Wi-Fi is wired to ---------
ip tuntap add dev tap0 mode tap user "$HOST_UID" group "$HOST_GID"
ip link set tap0 up
# A burst of ARP requests - the conversion tool's sweep for the device sends one
# to every address - stops the fork's emulated Wi-Fi receiving for good. A
# device ignores requests for other addresses anyway, so only its own reach it.
if ! { tc qdisc add dev tap0 clsact &&
       tc filter add dev tap0 egress pref 1 protocol arp flower arp_op request arp_tip 192.168.60.50 action pass &&
       tc filter add dev tap0 egress pref 2 protocol arp flower arp_op request arp_tip 192.168.60.0/24 action drop; }; then
    echo "warning: no ARP filter for the device; a sweep for it may stop its Wi-Fi" >&2
fi
if [ "$WINDOWS" = 1 ]; then
    # Windows' wire (tap1, carried by windows-link) and the device's radio, on one
    # segment: Windows sees the device's MAC, its mDNS and its connections, as
    # a computer on the same Wi-Fi does. Docker loads br_netfilter, so frames
    # crossing the bridge meet iptables' FORWARD (policy DROP, below): let
    # them through, as a home access point does. Snooping off: the device never
    # joins a multicast group, and mDNS must still reach it.
    ip link add br0 type bridge mcast_snooping 0
    ip tuntap add dev tap1 mode tap
    ip link set tap0 master br0
    ip link set tap1 master br0
    ip link set tap1 up
    ip link set br0 up
    iptables -I FORWARD 1 -i br0 -o br0 -j ACCEPT
fi
cat > "$RUN/env" <<EOF
LAN=$LAN
WINDOWS=$WINDOWS
DEVICE_MAC=$DEVICE_MAC
CHIP=${CHIP:-esp32c3}
PSRAM=${PSRAM:-}
KIND=${DEVICE_KIND:-}
ADC=${ADC:-}
CLOUD=$CLOUD
PCAP=$PCAP
EOF
echo on > "$RUN/power"
if [ "$CLOUD" = 1 ]; then echo "internet: ON (cloud)" > "$RUN/cloud-label"
elif [ "$STUB_CLOUD" = 1 ]; then echo "cloud: local stub" > "$RUN/cloud-label"
else echo "internet: off" > "$RUN/cloud-label"; fi
: > "$RUN/softap"
chown -R "$HOST_UID:$HOST_GID" "$RUN"
# The local cloud stub answers on the router's own address, under names only
# this instance's dnsmasq resolves (it reads /etc/hosts). Added before the
# router starts, so the device resolves them from its first dispatch lookup. The
# MINIR4's firmware puts -v1 into the dispatch host it was given ("-v1%s").
if [ "$STUB_CLOUD" = 1 ]; then
    grep -q autoflash-stub.test /etc/hosts 2>/dev/null \
        || echo "192.168.60.1 disp.autoflash-stub.test disp-v1.autoflash-stub.test iot.autoflash-stub.test" \
                "apia.autoflash-stub.test" >> /etc/hosts
fi
/opt/autoflash-emu/bin/wifi-root home >/dev/null
ln -sfn /state/work /work
# Start the stub once the router address (192.168.60.1) is up. It serves both
# the device (dispatch + IoT) and the tool (account API) from the repo copy at
# /state/work, using the CA/leaf mounted at /stub-cloud and this device's identity
# from its own dump. Logs (device values redacted) go to the instance's log dir.
# Kept non-fatal: a stub problem must not bring the whole emulator down.
# Where the stub is in the checkout comes from emu.sh (STUB_CLOUD_DIR), so
# moving it needs no new image.
if [ "$STUB_CLOUD" = 1 ]; then
    stub=/state/work/${STUB_CLOUD_DIR:-tools/ewelink/stub_cloud}
    # The tool's trust: this system's roots and the stub's CA, so trusting the
    # stub takes nothing away from any other site.
    cat /etc/ssl/certs/ca-certificates.crt /stub-cloud/ca.crt > "$RUN/stub-ca-bundle.pem" 2>/dev/null \
        || cp /stub-cloud/ca.crt "$RUN/stub-ca-bundle.pem"
    chmod 0644 "$RUN/stub-ca-bundle.pem"
    # Run as root, so -B: no bytecode left in your state directory that only
    # root could delete.
    # Its product model, when eWeLink gave it one before (emu.sh looks it up),
    # and its MAC, both of which eWeLink's record of it carries.
    if /opt/venv/bin/python3 -I -B "$stub/device_identity.py" /secrets/stock-flash.bin \
            ${STUB_PRODUCT_MODEL:+--product-model "$STUB_PRODUCT_MODEL"} --sta-mac "$DEVICE_MAC" \
            --out "$RUN/stub-device.json" 2>>/state/logs/stub.log; then
        /opt/venv/bin/python3 -I -B "$stub/stub_server.py" \
            --bind 192.168.60.1 --port 443 --cert /stub-cloud/server.crt --key /stub-cloud/server.key \
            --iot-host 192.168.60.1 --iot-domain iot.autoflash-stub.test --iot-port 443 \
            --device-json "$RUN/stub-device.json" \
            >> /state/logs/stub.log 2>&1 &
    else
        echo "stub: could not read the device identity; stub not started" >> /state/logs/stub.log
    fi
fi
cp /opt/autoflash-emu/tmux.conf "$home/.tmux.conf"
grep -q autoflash-emu-motd "$home/.bashrc" 2>/dev/null || cat >> "$home/.bashrc" <<'EOF'
# autoflash-emu-motd
[ -n "${TMUX:-}" ] && [ "$(tmux display -p '#W')" = conversion ] && cat /opt/autoflash-emu/motd
EOF
# In stub mode the conversion tool talks to the local stub, not coolkit, and
# trusts the stub's CA. Exported for any conversion the user starts in here.
if [ "$STUB_CLOUD" = 1 ] && ! grep -q autoflash-emu-stub "$home/.bashrc" 2>/dev/null; then
    cat >> "$home/.bashrc" <<'EOF'
# autoflash-emu-stub
export AUTOFLASH_EWELINK_API_BASE=https://apia.autoflash-stub.test
export SSL_CERT_FILE=/run/autoflash-emu/stub-ca-bundle.pem
export OPENSSL_CONF=/stub-cloud/openssl.cnf
EOF
fi
chown "$HOST_UID:$HOST_GID" "$home/.tmux.conf" "$home/.bashrc"

as_you() { runuser -u "$user" -- env HOME="$home" PATH="$PATH" AUTOFLASH_EMULATOR_URL="${AUTOFLASH_EMULATOR_URL:-}" \
    AUTOFLASH_EMULATOR_SUDO_PASSWORD="$SUDO_PASSWORD" "$@"; }
as_you /opt/autoflash-emu/bin/device-supervisor >/dev/null 2>&1 &
as_you /opt/autoflash-emu/bin/browser-forward >"$RUN/browser-forward.log" 2>&1 &
[ "$WINDOWS" = 1 ] && /opt/autoflash-emu/bin/windows-link tap1 >"$RUN/windows-link.log" 2>&1 &

cd /work
as_you tmux new-session -d -s autoflash -n conversion -c /work
as_you tmux new-window -d -t autoflash -n device "device log"
exec runuser -u "$user" -- env HOME="$home" PATH="$PATH" AUTOFLASH_EMULATOR_URL="${AUTOFLASH_EMULATOR_URL:-}" \
    AUTOFLASH_EMULATOR_SUDO_PASSWORD="$SUDO_PASSWORD" tmux attach -t autoflash
