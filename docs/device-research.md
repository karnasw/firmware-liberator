# Device research

What is known about the devices the tool converts or might, and how it was
found out, so you need not repeat the work. To add a device, see
[adding-a-device.md](adding-a-device.md); what holds particular devices up is
in [devices/](../devices/README.md).

## By chip family

Both pages have the same sections, where the family has something to say: its
devices, their flash and layouts, a device's identity and stores, the cloud,
the update path, what a backup logs, the stock firmware in the emulator, tools
and methods, and questions still open.

- [Espressif research](espressif-research.md): the ESP32 and ESP32-C3 devices,
  the locked ones among them, their partition layouts, a device's Sonoff
  identity and where the tool keeps it, Sonoff's newer ais SDK, how a device's
  firmware name is learnt, and what a backup logs.
- [Beken research](beken-research.md): the eWeLink firmware on the BK7238
  devices: its flash and stores, its cloud and update format, its pairing in
  the emulator, and the route to OpenBeken and ESPHome.

## By device

What was found out about one device is kept with its files, in its
`research.md`, and listed here:

- [Sonoff NSPanel](../devices/sonoff-nspanel/research.md): why its updater
  refuses the bridge's file, and its room temperature.
- [Sonoff S61](../devices/sonoff-s61/research.md): its energy meter's
  calibration, its wiring, and what OpenBeken did on it in the emulator.
- [Sonoff Basic Gen5](../devices/sonoff-basic-1gs/research.md): its flash size.

The emulators most of it was found with are described in
[emulator.md](emulator.md) and
[tools/beken/emulator/](../tools/beken/emulator/README.md); the eWeLink stand-in
they run against is in [cloud-stub.md](cloud-stub.md).
