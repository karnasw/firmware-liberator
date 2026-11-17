// What drv_autoflash.c needs to know of the device it runs on: one table per
// device, in the device's own file (devices/<device>/drv_autoflash_<device>.c), which
// a build of OpenBeken carries beside the driver.
#ifndef __DRV_AUTOFLASH_H__
#define __DRV_AUTOFLASH_H__

#include "../new_common.h"

// A pin's role, and its channel: 0 leaves the pin's channel as it is.
typedef struct {
	int pin;
	int role;
	int channel;
} autoflash_pin_t;

typedef struct {
	const char *kind;          // the tool's key for it (tools/autoflash/kinds.py), for AutoflashKind
	const char *stock_model;   // what its stock firmware calls itself, as kinds.py's stock_model has it
	const char *name;          // as the log names it
	// The flash, as the stock firmware and the 4 MB table lay it out.
	uint32_t store_start;      // the stock EasyFlash V4 (EF40) store: its Wi-Fi and calibration
	uint32_t store_end;
	uint32_t download_slot;    // where the stock bootloader looks for an update
	uint32_t settings_sector;  // OpenBeken's settings (NET_PARAM)
	// Its wiring, applied on first boot, as the log describes it; and the
	// relay's channel, which restores its last state across power loss.
	const autoflash_pin_t *pins;
	int pin_count;
	const char *pins_described;
	int relay_channel;
	// Its meter's factory calibration, from the stock store into OpenBeken's
	// config (first boot only): 1 when applied.
	int (*apply_calibration)(void);
	// Whether OpenBeken's meter calibration is what apply_calibration takes
	// from the stock store now, for AutoflashCalibration: 1 it is, 0 it is
	// not, -1 the store has none. NULL for a device with no meter.
	int (*calibration_is_stock)(void);
} autoflash_device_t;

// The device this build is for, the one table its device's file defines. To
// choose among several at run time - by the stock store's model, against
// stock_model - each device's file would name its own and the driver would
// take one from a list of them; the table itself stays as it is.
extern const autoflash_device_t autoflash_device;

// The newest value of `key` in the stock store, raw (a binary value may hold
// zeros): its length, or -1 when there is none.
int Autoflash_ReadStockBlob(const char *key, uint8_t *out, int outMax);

#endif
