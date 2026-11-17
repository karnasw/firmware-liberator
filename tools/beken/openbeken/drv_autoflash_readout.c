// The readout build of OpenBeken, for the conversion tool's OpenBeken driver
// (drv_autoflash.c): what a readout of a Beken device the tool does not
// convert yet installs for a few minutes, to read its whole flash and then
// put its Sonoff firmware back (tools/autoflash/readout.py).
//
// Its device is unknown, so this table sets no pins at all: no relay, button,
// LED or meter, so nothing is driven on hardware nobody has mapped. OpenBeken
// leaves every pin as the chip starts it. It still joins the device's Wi-Fi
// from the stock store, as every build of the driver does, and its revert
// command takes Sonoff's firmware back into the stock download slot.
//
// Where the stock store and download slot sit is the 4 MB table's, which the
// tool checks the device's official update file holds before it sends this.

#include "../new_common.h"
#include "../new_cfg.h"
#include "../new_pins.h"
#include "../logging/logging.h"
#include "drv_autoflash.h"

// Nothing to carry over: a device not mapped has no calibration known to it.
static int readout_no_calibration(void) {
	return 0;
}

const autoflash_device_t autoflash_device = {
	.kind = "readout",
	.stock_model = "",
	.name = "readout",
	.store_start = 0x3e0000,
	.store_end = 0x3ec000,
	.download_slot = 0x286000,
	.settings_sector = 0x3e1000,
	.pins = 0,
	.pin_count = 0,
	.pins_described = "none: a readout sets no pins",
	.relay_channel = 0,
	.apply_calibration = readout_no_calibration,
};
