// The Sonoff Basic Gen5, BASIC-1GS (BK7238), for the conversion tool's OpenBeken
// driver (tools/beken/openbeken/drv_autoflash.c): where its stock store and
// download slot sit, and its wiring. It has no meter, so no calibration.

#include "../new_common.h"
#include "../new_cfg.h"
#include "../new_pins.h"
#include "../logging/logging.h"
#include "drv_autoflash.h"

static int basic_1gs_no_calibration(void) {
	return 0;
}

// Its wiring, from the OpenBeken device database's entry for the BASIC-1GS,
// board BASIC-1GS-WIFI V1.2 (elektroda.com topic 4167932): one person's
// template, not checked here on a real one.
static const autoflash_pin_t basic_1gs_pins[] = {
	{26, IOR_Relay, 1},
	{6, IOR_Button, 1},
	{1, IOR_Button_n, 1},
	{8, IOR_LED_WIFI, 2},
};

const autoflash_device_t autoflash_device = {
	.kind = "sonoff-basic-1gs",
	.stock_model = "SN-BK7238-BASICR5-01",
	.name = "BASIC-1GS",
	.store_start = 0x3e0000,
	.store_end = 0x3ec000,
	.download_slot = 0x286000,
	.settings_sector = 0x3e1000,
	.pins = basic_1gs_pins,
	.pin_count = sizeof(basic_1gs_pins) / sizeof(basic_1gs_pins[0]),
	.pins_described = "Rel P26, Btn P6, Btn_n P1, WifiLED P8",
	.relay_channel = 1,
	.apply_calibration = basic_1gs_no_calibration,
};
