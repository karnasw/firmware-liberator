# Sonoff Basic Gen5: research

What is known of the Basic Gen5's own hardware. Its firmware is one of the 4 MB
eWeLink builds in [beken-research.md](../../docs/beken-research.md).

## Its flash size

**The largest open risk is a spec that disagrees.**
[CNX Software's review](https://www.cnx-software.com/2025/11/06/review-sonoff-basic-gen5-matter-switch-and-tx-gen2-matter-smart-wall-switch)
gives the Basic Gen5 "2 MB flash (SiP)", while its firmware holds only the 4 MB
table ([Their flash](../../docs/beken-research.md#their-flash)), with an update
slot at `0x286000`, beyond 2 MB. The firmware is the stronger evidence, as it
could not update itself on 2 MB, but nobody has read a real one's flash here.
Stock firmware does not tell a run its flash size: its register message gives
`romVersion`, `chipid` and `model`, and `[Flash]id:` appears only on serial.

Every source found says this of its flash:

| Evidence | Says | Kind |
|---|---|---|
| [FCC internal photos](https://fccid.io/2APN5-BASIC1GS/Internal-Photos/Internal-photos-8589146), board `BASIC-1GS-WIFI V1.2 2025.06.06` | one chip, `BEKEN / BK7238 / AU5187XA`, and no separate flash chip: the flash is inside the package | direct, but neutral: the marking does not give the size |
| [Beken's datasheet V2.1](https://cionlabs.com/wp-content/uploads/2025/06/BK7238-Datasheet_V2.1.pdf) | "SiP Flash: 2 MB or 4 MB": QH322 (QFN32, 4 MB), QH321 (QFN32, 2 MB), QH202 (QFN20, 4 MB), QH201 (QFN20, 2 MB). 4 MB was added in V2.0, December 2024, before the board's date | indirect: both sizes existed |
| Its official firmware | only the 4 MB table, its update slot above 2 MB | indirect, strong |
| Sonoff's door sensor | a 2 MB table and a 2 MB dump: Sonoff matches the table to the chip | indirect |
| The S61s | flash ID `162085`, which [BK7231GUIFlashTool](https://github.com/openshwprojects/BK7231GUIFlashTool) maps to a 4 MB Puya P25Q32HB; its dump is the only 4 MB one among about 60 BK7238 dumps in FlashDumps | direct, but a sibling, with a QFN20 package |
| [CNX Software's review](https://www.cnx-software.com/2025/11/06/review-sonoff-basic-gen5-matter-switch-and-tx-gen2-matter-smart-wall-switch) | "2 MB flash (SiP)", word for word as for the Tx Gen2 and later the BASIC-1GSP; its photos do not show the chip | indirect: it reads like the older datasheet's text |
| Generic BK7238 pages, LibreTiny's board, forum posts | 2 MB | indirect: written before 4 MB existed, and the same was wrongly said of the S61s |

So it is probably 4 MB, but nobody has measured one. Sonoff's pages, the CSA
listing, the Elektroda thread and GitHub say nothing on it. One read-only test
settles it. On a real unit, run BK7231GUIFlashTool in BK7238 mode, "Firmware
backup (read) only", over TX1 and RX1: its `Flash MID loaded:` line says `162085`
for 4 MB, and `152085` or `154285` for 2 MB. Stock's serial boot log also prints
`[Flash]id:`.
