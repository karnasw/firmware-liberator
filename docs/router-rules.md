# Router rules for a stock device (optional)

None of this is needed to convert a device: the tool delivers its firmware
straight from your computer over your own network, never through your router.
A router *can* do two things for a device still running Sonoff's firmware:

- **stop it installing Sonoff updates**, so that a future Sonoff firmware
  cannot close the way this tool gets in before you have converted the device;
- **send its update download to your computer instead**, which earlier
  versions of the tool required whenever Sonoff was offering the device an
  update.

Both are for someone comfortable in their router's admin pages. Neither is a
normal "port forward": they deal with traffic leaving a device at home, not
traffic arriving from the internet.

## How a Sonoff update reaches the device

This comes from the reverse engineering and an 18-hour watch of a stock S60
on 1.2.0, both in [ota-findings.md](../archive/docs/reference/ota-findings.md):

- The device keeps one encrypted connection open to eWeLink's servers, with
  a tiny heartbeat every 145 seconds. Over 18 hours nothing else happened but
  a nightly energy-statistics sync, unless someone used the device or had its
  page open in the app.
- When it connects (at power-on, or after losing Wi-Fi) it reports its
  firmware version, and Sonoff's cloud, not the device, decides whether to
  offer an update. A power cycle showed no download and no plain-HTTP traffic
  at all.
- The device downloads firmware only when sent an **upgrade command**, which
  names the download address. Sonoff's own downloads have always been
  **plain HTTP on port 8088**.

Sonoff's Beken firmware takes the same upgrade command and downloads from
whatever address it names ([Beken research](beken-research.md), under the
update path); the port of Sonoff's own downloads for it has not been watched.

**Not known yet:** whether Sonoff ever sends that command by itself, without
anyone tapping Update in the app. The watched device had no update on offer,
so could not show it; a device Sonoff *is* offering one to would, such as a
stock S60 on 1.1.1, which is offered 1.2.0.

## Blocking Sonoff updates

> **Not needed today.** Every Sonoff firmware tested so far can be converted,
> the S60's up to and including 1.2.0, whether or not Sonoff is offering the
> device an update. This step is insurance: if Sonoff ever releases a firmware
> that closes the way in (by checking a signature on downloads, say, or
> refusing an upgrade command that names another address), a device that
> installs it could no longer be converted, while one that never receives it
> stays convertible. Then this step becomes necessary for new devices.

The rule: **reject TCP from every device on the network the device will join,
to port 8088 anywhere outside your home network.** It covers the whole
network because a new device's address and MAC address are not known until it
has joined, by when it may already have been offered an update. So the rule
goes in **before the device is unboxed**, before it ever joins your Wi-Fi or
the eWeLink app. For a new device:

1. Add the rule (below).
2. Unbox the device, power it up and pair it in the eWeLink app as usual.
3. Convert it with the tool.
4. Once no device on your network is still on Sonoff's firmware, remove the
   rule, or narrow it to a stock device you are keeping: reserve its address,
   known by then, in DHCP and use it as the source.

**Why this is expected to work.** In the research, with the device's route
to Sonoff's download server blocked at the router, a genuine upgrade command
left it unable to download and on its version. The one side effect: it then
showed as offline in eWeLink until switched off and on.

**It does not get in the way of converting the device**, whose firmware
never passes through the router's firewall, nor of a converted one: Tasmota's
own updates do not use port 8088.

**What it costs.** While the rule is in place, no device on that network can
download from port 8088 on the internet. Very little uses that port, but check
that nothing of yours does, or put the rule on a separate network for
smart-home devices if you have one.

**What it does not cover.** If Sonoff ever moved its downloads off port
8088, or to HTTPS on 443, this rule would not see them. The only certain block
is cutting the device off from the internet, which also ends control through
the eWeLink cloud, and the pairing the tool relies on.

### On OPNsense

1. **Firewall → Rules → LAN** (or the interface of the network the device's
   Wi-Fi is on), then **Add**.
2. Set:

   | Field | Value |
   |---|---|
   | Action | Reject (the device gives up straight away, instead of waiting) |
   | Interface | LAN |
   | Direction | in |
   | Protocol | TCP |
   | Source | LAN net - every device, since the device's address isn't known yet |
   | Destination | LAN net, with **Invert** ticked - meaning "anywhere except my network" |
   | Destination port | 8088 |
   | Log | ticked, so the firewall log shows every blocked attempt |
   | Description | Block Sonoff firmware downloads |

3. Move it **above** the "allow LAN to any" rule: the first match from the
   top wins.
4. **Apply**.

On OpenWrt it is under *Firewall → Traffic Rules*, on pfSense *Firewall →
Rules → LAN*: a reject or drop rule for outbound TCP from the home network to
port 8088 outside it.

To check it: whenever a device tries to download from port 8088, the
firewall log shows a rejected connection, and the update watcher
(`tools/ewelink/watch_sonoff_updates.py`) shows `device opened a
connection to the firmware port` at the same moment. To remove it, delete the
rule and apply.

## Redirecting the download to your computer

Earlier versions of the tool used this only when Sonoff was offering the
device an update; the tool's own firmware always came straight from your
computer. It guarded against the device also fetching and installing Sonoff's
genuine update in the same few seconds, from a one-time signed address only
the device can produce. It was dropped because the owner's upgrade command
already decides what is installed, and many routers cannot do it. To block
updates, the rule above is simpler and needs none of this.

The rule is a **destination NAT** (DNAT) rule for traffic *from* the device,
with values from the update information the tool reads at step A2:

| Field | Value |
|---|---|
| Protocol | TCP |
| Source address | the device's address |
| Destination address | the Sonoff download address in the update information |
| Destination port | its port (8088 so far) |
| Redirect to address | your computer's address |
| Redirect to port | the port the tool serves the bridge on (8088, or 8090–8099 if 8088 was busy) |

With the device and your computer on the same network, the reply must also
go back through the router, or the device sees it come from the wrong address
and drops the connection. Turn on **NAT reflection** / **hairpin
NAT** / **masquerading**, or add an outbound NAT rule translating traffic from
the device to your computer on that port to the router's own address.

Where it lives on common routers:

- **OPNsense / pfSense:** Firewall → NAT → Port Forward, on the **LAN**
  interface.
- **OpenWrt:** Network → Firewall → Port Forwards, source zone LAN.
- **UniFi:** the graphical "Destination NAT" policy applies only to traffic
  arriving from the internet, so the research added the two rules with
  `iptables` over SSH; the exact commands are in
  [ota-findings.md](../archive/docs/reference/ota-findings.md).

Common mistakes: source and destination swapped, a plain port forward
instead of DNAT, no NAT reflection, and the device's address changing after
the rule was made (reserve it in DHCP).

### Testing it, and putting it back into the tool

The old step tested the rule before anything was written to the device: with
the device switched off, your computer borrowed its address for a few seconds
and checked that a connection from it to Sonoff's download server was
redirected, and one from your computer's own address was not. That test still
exists, and the suite tests it:

- `tools/autoflash/redirect_test.py` - the test itself, with the
  address-borrowing helpers in `tools/autoflash/hostsys.py`;
- `tools/espressif/check_ota_firewall.sh` - the research's command-line version.

The step that asked for the rule and ran the test was **B2**,
`step_intercept`, in `tools/autoflash/cli.py`, in git history. Putting it back
means restoring it and passing it the Sonoff address from step A2. Today's
`B2` is another step: a Beken device's first firmware, sent through eWeLink.
