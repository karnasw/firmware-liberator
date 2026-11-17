# Sonoff NSPanel: research

What was found out about the NSPanel's own firmware. What it shares with the other
Espressif devices is in [espressif-research.md](../../docs/espressif-research.md).

## The NSPanel wants three records

The NSPanel boots from its whole dump, then turns the bridge's file down as soon
as it has read the 24-byte frame header, before any payload. The cause is in its
stock updater, read from the dump's app: the parser is around `0x4014ad04`, and
the command it answers is built around `0x400dca90`.

The header carries, in order, a format byte (`03`), a one-byte record count, an
8-byte target version, reserved bytes and, at offset `0x14`, a big-endian CRC-32
over the first 20 bytes. The updater checks three things in it, in this order,
and answers `ota: fail` (its error 6) on the first that does not hold:

1. **The CRC-32.** The bridge computes it the same way, so this passes.
2. **The version.** It must sort above the running firmware's. The bridge sends
   `9.9.9`, so this passes.
3. **The record count.** It must equal a number the firmware builds for itself,
   which for the NSPanel is the constant **3** (`movi.n a3, 3` at `0x400dcd00`).
   The cloud's upgrade message does not carry it, so the stub cloud cannot change
   it. The three records are the ESP32 app and two HMI screen images, matching
   the public update file's `ESP32D0WDV3 + HMIX120 + HMIX86` names. The bridge
   sends one record, so the file is rejected here. A raw image, with no header
   at all, is rejected sooner still.

So neither the CRC nor the version is the problem, and no cleverer wrapper byte
fixes it. To pass, the file has to present a three-record manifest: record 0 the
ESP32 app, whose 32-byte model name the updater compares with
`FWPNL-E32NSP-NSPANEL-ESP32D0WDV3` (the bridge already sends it), and records 1
and 2 named `HMIX120` and `HMIX86`.

The real blocker is that a three-record file cannot be made safe for the screen
in software. The per-record checks and routing (parser state 2 at `0x4014ae6c`,
writer at `0x4014ac24`) were traced to confirm it:

- All three 76-byte records are validated up front: a CRC-32 over each record's
  first 60 metadata bytes, record 0's model name, and a version field. The CRC
  covers the **metadata only, not the payload**, so a dummy record passes even
  with a declared payload length of 0. The manifest check does not stop this.
- The routing does. The handler for records 1 and 2 sends
  `whmi-wri ...,921600,0` to the display MCU over UART **at the start of the
  record, before any HMI payload is examined**. A zero-length or garbage HMI
  record still commands the physical screen into Nextion firmware-upload mode.
  The emulator has no display MCU, so this cannot be tried there; on a real
  panel it risks leaving the screen unusable.
- The ESP32 app (record 0) is activated, its boot partition set and the panel
  restarted, only after the **whole** three-record update completes. Failing
  record 1 or 2 aborts everything (error 6, app never activated), and letting
  them succeed means really streaming firmware to the display. The app cannot
  land while the screen is spared.

Reaching Tasmota or ESPHome on the NSPanel therefore needs genuine `HMIX120` and
`HMIX86` images and a real panel to prove and recover the display path, or a
different way in. One lead was not pursued and needs hardware to test: the stock
handler builds the manifest conditionally on two HMI-present flags, which it
reads from run-time state and not from the update file. If the firmware could be
made to report no screen, the count might drop below 3 and an ESP32-only file
might match. The builder (`tools/espressif/build_vendor_ota.py`) still emits a
single record: an NSPanel branch gains nothing until one of these routes is
proven, so none was added.

## Its room temperature

The NSPanel's firmware reads its room thermistor (GPIO38, ADC1 channel 2) through
`esp_adc_cal` and reports it (`intTemp`, `tempCorrection`, then
`{"temperature":...}` to eWeLink and its screen). As far as its strings show,
nothing in it refuses an update or switches anything on that reading. In the
emulator every classic-ESP32 ADC channel reads 0, which with no eFuse calibration
comes out as the attenuation's offset, and the panel sends `"temperature": -2`.
That is the only effect seen. A believable reading would need a patch like the
ESP32-C3's 0005 for the classic ADC (`misc.esp32.sens`, whose `ADC_values` is
zeroed and set only by PICSimLab); the conversion does not need one.
