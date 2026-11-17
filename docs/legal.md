# Staying on the right side of copyright

This project lets you back up the Sonoff firmware on a device you own, and put
it back later. Sonoff's firmware is Sonoff's copyright, so this page sets out
what the project does, and deliberately does not, to keep that use lawful. It
is not legal advice.

## The short version

- The backup is of **your device**, is kept **on your computer**, and is only
  ever written back to **the same device**.
- A **readout**, sent only if you choose, is the one copy that leaves your
  computer: anonymised, kept privately, used only to add support for the
  device ([Readouts](#readouts)).
- The project **never distributes Sonoff's firmware**, in whole or in part.
- The tool updates through the device's **ordinary update mechanism**, started
  from **your own eWeLink account**. It breaks no encryption, signature or other
  protection.

## What the law allows

Copyright law in most places lets the lawful user of a program make a backup
copy of it, and study how it works so that other software can work with it:

- **UK:** the Copyright, Designs and Patents Act 1988, s.50A (backup copies),
  s.50BA (observing, studying and testing) and s.50B (decompiling for
  interoperability).
- **EU:** the Software Directive 2009/24/EC, Art. 5(2), 5(3) and 6. The backup
  right cannot be removed by contract.
- **US:** 17 U.S.C. §117, which lets the owner of a copy of a program make an
  archival copy. Firmware that comes with a device you bought is generally
  treated as a copy you own.

The backup right does not cover giving the copy to anyone else, and that is
where the line is drawn below. The study and interoperability rights are what
the project relies on for a published dump or a readout.

Right-to-repair laws mostly require manufacturers to provide parts, tools and
information. They are **background support** for what this project does, not
the grant: they give no copyright permissions, so the project rests on the
interoperability and study exceptions above, for every device it works with,
now or later.

## What the project does

### The backup stays with you and your device

- The backup is saved during a conversion under
  `captures/devices/<MAC>/sonoff-backup/`
  ([Your Sonoff backup](technical-details.md#your-sonoff-backup)), which Git
  ignores, so it cannot be committed by accident. It is filed under the MAC
  address read from the device's own memory (an Espressif chip's eFuses, a
  Beken device's factory record), not one reported over the network.
- **Back to Sonoff's firmware** writes a device's own backup only to that
  device, never one device's firmware onto another. Each backup also holds
  that device's eWeLink device id and keys, which belong to it alone. Without
  a backup, the tool uses Sonoff's published firmware (as eWeLink offers it,
  or the public copy of eWeLink's update file), fetched at run time, with the
  device's own identity.
- The backup is written once and kept read-only. Converting a device again
  keeps its first backup.

### No Sonoff firmware is distributed

- No Sonoff firmware image, and none of Sonoff's code, is in the repository
  or its history. The firmware files committed are this project's own builds,
  builds of Tasmota, ESPHome and OpenBeken, and a few data files
  ([NOTICES.md](../NOTICES.md) lists their sources and licences).
- Three of those data files are Sonoff's partition tables, each the standard
  ESP-IDF table a model ships with: a short list of partition names, offsets
  and sizes, with no code or settings in it. The S60's,
  `artifacts/sonoff-s60-partition-table.bin`, was read from a stock device;
  the NSPanel's and the TH Elite's come from public flash dumps
  ([the artifact notes](../artifacts/README.md#sonoffs-partition-tables)). The
  tool needs them to check a device's layout, and to put it back when the
  device returns to Sonoff's firmware.
- The emulator ([emulator.md](emulator.md)) boots **your own** backup, or a
  public dump it fetches at run time, and its image contains neither.
  `tools/espressif/emulator/patch_stock.py` changes a copy to run under QEMU,
  checking the bytes it replaces, carrying none of Sonoff's code beyond those
  few instructions, and never changing the backup. Its **public devices** hold
  nothing of Sonoff's: they are made from devices whose Sonoff firmware was
  already replaced, and `tools/espressif/emulator/sanitise_device.py` shows
  each holds no part of its backup ([emulator.md](emulator.md#public-devices)).
- The maintainer keeps their own backup off their computer, as any owner may
  back up their own: encrypted, in a private package only they can reach,
  never in the repository, a release or anything public.

### Nothing is circumvented

The tool does not 'get around' technological protection measures.

- It sends the device the same kind of upgrade command Sonoff's servers send,
  from the owner's own eWeLink account, and the device runs every check it
  normally does, including the SHA-256 check on the firmware it downloads
  ([How it works](what-happens.md)).
- The device's firmware is not encrypted and does not check signatures, and
  the tool does not disable or work around any such feature.
- The optional router rules ([router-rules.md](router-rules.md)) keep a stock
  device as it is by stopping it from installing updates. They defeat
  nothing.

### Studying and emulating the firmware

Some of the project's work is research: reading a device's firmware to learn
how it stores Wi-Fi credentials and pairs, so a bridge firmware can keep the
owner's Wi-Fi through a conversion. This is the observing, testing
and decompiling-for-interoperability the exceptions above allow, for any
device the project adds.

- The dumps studied are **the owner's own devices**, read from hardware the
  owner bought, and dumps others have published, as in
  [FlashDumps](https://github.com/openshwprojects/FlashDumps), which the
  tooling fetches at run time. No vendor firmware, decompiled listing, or data
  or key taken from one is published: the docs describe the firmware in
  prose, tables, addresses and offsets only, facts and ideas rather than the
  vendor's code or data.
- Where a device is studied under emulation, because the ordinary update
  route does not yet cover it, it runs from a copy of such a dump. A
  change the emulator needs to boot that copy, such as standing in for a
  hardware check it cannot reproduce, is made **only inside the emulator, on
  that copy, and is never written to or run on a real device**. It removes no
  protection from a device, and distributes nothing.
- Any exchange with the vendor's cloud uses the owner's own account and
  device; the published tooling points at a local stub by default.

### Readouts

A readout ([readouts.md](readouts.md)) sends the project an anonymised copy
of a device's memory, Sonoff's firmware included, so that support for the
device can be built: the observing and studying for interoperability the
exceptions above allow.

- It is sent only when you agree to it, with your Wi-Fi details and the
  device's eWeLink identity taken out first.
- It is stored privately on the project's server and never published or
  passed on; the docs describe what it shows as for any dump.

### Signing in to eWeLink

- The tool signs in to eWeLink as you, with your own account, to talk to your
  own devices. It does not keep your password or the access token
  ([How your eWeLink account is handled](technical-details.md#how-your-ewelink-account-is-handled)).
- The application id it signs in with is the public one used by the
  open-source SonoffLAN integration, not one taken from Sonoff's own app.

### Names

Sonoff and eWeLink are trademarks of their owners. The project names them only
to say which devices and services it works with, and states that it is
independent and not an official installer ([NOTICES.md](../NOTICES.md)).

## What this project asks of you

- **Do not share your backup.** It is Sonoff's firmware, and it also holds
  your Wi-Fi password and your device's eWeLink keys in plain text. A readout
  is the way to send the project a device's firmware: the tool anonymises it.
- **Do not attach a Sonoff backup, or any part of one, to an issue or pull
  request.** Describe what you see instead, such as the offsets, sizes, Sonoff
  version and log lines. A pull request that adds Sonoff firmware or code
  taken from it will not be accepted.
- **Use a backup only on the device it came from.**
