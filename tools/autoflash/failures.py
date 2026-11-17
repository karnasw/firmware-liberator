"""What kind of failure stopped a run, for its report: one of CLASSES, from
the words the run stopped with.

A report says where a run stopped and why in words written for the person;
the class says the same in a word that can be counted across runs. Classed
here, by the words, rather than at each place a run can stop, so the message
stays the one thing each place writes. tests/autoflash/test_failures.py reads
every message the tool raises and fails on one no pattern matches.
"""

from __future__ import annotations

import re

# Each class, and what it means. "declined" and "nothing-to-do" are no fault
# of the tool's, and are counted apart from failures.
CLASSES = {
    "device-not-found": "nothing answered at the device's address, or which device is there could not be told",
    "device-no-answer": "the device stopped answering within a wait",
    "device-lost": "the connection to the device was reset, cut off or timed out mid-request",
    "device-busy": "the device turned firmware down, busy with an update cut off",
    "weak-signal": "stopped for the device's weak Wi-Fi signal",
    "network-join": "this computer or the device did not join a network in time",
    "computer-network": "this computer's address, ports or firewall kept it from the device",
    "unsupported-device": "a device this tool does not convert yet",
    "unexpected-firmware": "the device answered as firmware other than the run expected",
    "unexpected-layout": "the device's memory or partition table is not as expected",
    "upload-cut": "sending to the device stopped part way, or nothing was sent",
    "not-taken": "an image was sent but did not take, did not hold, or the device went back",
    "settings-lost": "the new firmware runs but lost its settings or template",
    "identity": "the device's Sonoff identity could not be kept",
    "backup": "the Sonoff backup is missing, unreadable or could not be written back",
    "download": "a published image, public dump or official update could not be fetched",
    "integrity": "a file's hash or pin differs, so it was refused",
    "preparation": "what is sent to the device could not be prepared on this computer",
    "ewelink-unreachable": "eWeLink could not be reached",
    "ewelink-account": "the device is not in the account, or the account could not be used",
    "ewelink-refused": "eWeLink did not take the command or the update",
    "credentials": "a password or key was needed or wrong",
    "bad-option": "options that cannot go together, or a firmware the device cannot take",
    "unsafe-state": "a safety lock, or the run's record of the device, was not as needed",
    "no-room": "this computer had no room to save what the run needs",
    "declined": "the person said no at a confirmation",
    "nothing-to-do": "the device already runs what was asked",
    "crash": "an error the tool did not anticipate",
    "other": "not classed yet",
}

# (class, pattern), tried in order: the first to match the words a run
# stopped with is its class. Specific before general.
PATTERNS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ("declined", r"^not confirmed|was not confirmed|requires an explicit confirmation|^skipped as asked"),
    ("nothing-to-do", r"already runs Sonoff's firmware: there is nothing to convert"),
    ("crash", r"^something unexpected went wrong"),
    ("no-room", r"has no room to save"),
    ("device-lost", r"connection to the device was lost"),
    ("weak-signal", r"weak Wi-Fi signal"),
    ("unsupported-device", r"which this tool does not convert yet|not set up as a device this tool converts"),
    ("device-busy", r"busy with an update"),
    ("credentials", r"encryption key|encryption: line|has a password|password|NeedsPassword"),
    ("unsafe-state", r"refusing to start from an unsafe state|another run is converting this device"
                     r"|has no record of|is not set; pass|address is not known|device id is not known"
                     r"|firmware files changed since|is on this tool's installer from an earlier run"
                     r"|is running Tasmota, not Sonoff's firmware|so the installer has to be sent again"),
    ("backup", r"backup cannot be written back|backup could not be read|no backup of|found neither with a backup"
               r"|cannot be put back without a backup|--simulate-backup-failure"),
    ("identity", r"identity cannot be kept"),
    ("integrity", r"checksums failed|do not check out|does not match the official firmware files|hash differs"
                  r"|is not the file firmware\.json pins"
                  r"|refusing to send it|rt_ota"),
    ("download", r"could not be fetched|no public dump of|no \S+ image is published|image is missing"
                 r"|public dump holds no|the download was cut off"),
    ("ewelink-unreachable", r"eWeLink could not be reached|has not reached eWeLink|but not in eWeLink"),
    ("ewelink-refused", r"official update file for the .* was refused|firmware was not sent:"
                        r"|readout firmware was not sent"),
    ("bad-option", r"--replace-own-build|needs to be told which firmware|cannot go to |cannot switch to"
                   r"|not sent from a test run|^not from a test run|eWeLink account given"
                   r"|needs its firmware: --supla"),
    ("ewelink-account", r"eWeLink account|eWeLink query failed"),
    ("computer-network", r"this computer reaches|this computer's address|are all in use on this computer"
                         r"|--listen-ip|readout could not be sent|did not fetch the \S+ instructions"
                         r"|without reaching this computer|firewall rule|^sudo |the website answered"),
    ("network-join", r"did not join|cannot join it|has not answered on your Wi-Fi|has not come back on your Wi-Fi"
                     r"|did not answer on your Wi-Fi"
                     r"|is not on your Wi-Fi"
                     r"|neither rejoined|did not come back after its settings were reset"
                     r"|did not say which Wi-Fi network|but the firmware to come would join"
                     r"|has not shown the device online again"),
    ("settings-lost", r"lost its settings|did not keep its template"),
    ("unexpected-layout", r"not laid out as this tool expects|partition table|memory could not be read"
                          r"|short read of the device's flash|bytes were read"),
    ("not-taken", r"did not take|did not hold|still running from slot|kept running its old firmware"
                  r"|keeps going back to the startup firmware"
                  r"|still answers as the firmware it was to leave|was not seen running"
                  r"|did not come back after the upload|has not started ESPHome|did not switch to its safeboot"
                  r"|would not take its new name|went back to its Sonoff firmware before"
                  r"|still on its Sonoff firmware, so nothing was read|would not fetch Sonoff's firmware back"
                  r"|refused to fetch Sonoff's firmware|after it was sent \d+ times"
                  r"|neither its own network"),
    ("upload-cut", r"stopped part way|was not uploaded|to the device failed|could not be sent to the device"
                   r"|did not complete within|did not print|nothing was armed|delivery failed"
                   r"|installer was not fully delivered|installer was not delivered"
                   r"|the device fetched \S+ of \S+ bytes"),
    ("unexpected-firmware", r"did not answer as a device this tool converts|Berry|did not say twice alike"
                            r"|did not say which slot|it is not the one this tool installs"
                            r"|metadata just written|redirected .* too many times"
                            r"|official update file for the .* is for "),
    ("preparation", r"cannot be used|could not be prepared|cannot be prepared|could not be set up for"
                    r"|firmware cannot be written"),
    ("device-not-found", r"nothing answers at|could not tell which device"),
    ("device-no-answer", r"did not answer within|did not restart within|did not answer\b"),
)]


def class_of(exc: BaseException) -> str:
    """The class of what stopped a run: the one it says (errors.Abort's
    failure), else its words'."""
    said = getattr(exc, "failure", None)
    return said if said in CLASSES else classify(str(exc))


def classify(reason: str) -> str:
    """The class of the words a run stopped with: "other" when none fits."""
    return next((name for name, pattern in PATTERNS if pattern.search(reason or "")), "other")
