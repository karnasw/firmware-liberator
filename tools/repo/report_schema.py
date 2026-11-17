#!/usr/bin/env python3
"""The schema every report sent to the API is held to, written from the
tool's own constants (tools/autoflash/report_data.py and those it draws on):
a run's, the file's own, and a readout's, under "readout", which a report
with "kind": "readout" is held to instead.

    python3 tools/repo/report_schema.py          write website/api/report-schema.json
    python3 tools/repo/report_schema.py check    say whether it is as written here

The Worker turns away any report it does not fit (website/api/report-schema.js);
tests/repo/test_report_schema.py fails while the file is not current. A list
of words only ever grows: a word the tool no longer sends stays allowed,
as an earlier version of the tool still in use may send it.

JSON Schema's words, but only these, which both validate() here and the
Worker's know: type, enum, pattern (the whole value), maxLength, minimum,
maximum, properties, required, additionalProperties, propertyNames,
maxProperties.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import failures, join, kinds, records, report, report_data, report_upload, routes  # noqa: E402

PATH = ROOT / "website" / "api" / "report-schema.json"
MAX_BYTES = 8192


def words(*values: Any) -> dict[str, Any]:
    return {"type": "string", "enum": list(dict.fromkeys(values))}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    kind = schema["type"]
    return {**schema, "type": [*(kind if isinstance(kind, list) else [kind]), "null"],
            **({"enum": [*schema["enum"], None]} if "enum" in schema else {})}


def string(pattern: str, longest: int) -> dict[str, Any]:
    return {"type": "string", "pattern": pattern, "maxLength": longest}


def integer(lowest: int, highest: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": lowest, "maximum": highest}


def record(properties: dict[str, Any]) -> dict[str, Any]:
    """An object with exactly these properties, each present."""
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def schema() -> dict[str, Any]:
    """The schema as the tool's constants make it today."""
    sources = [*report.CONVERTED_FROM, *(family.DIRECT_FROM_TASMOTA for family in routes.FAMILIES.values()
                                         if hasattr(family, "DIRECT_FROM_TASMOTA"))]
    step = "^[A-Z][0-9]{1,2}$"
    shared = {
        "format": {"type": "integer", "enum": [report_data.FORMAT]},
        "id": string(f"^[0-9a-f]{{{2 * report_upload.REPORT_ID_BYTES}}}$", 2 * report_upload.REPORT_ID_BYTES),
        "proof": string("^[0-9]{1,12}:[0-9]{1,20}$", 33),
        "tag": words("user", "dev"),
        "tool": string("^[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}$", 11),
    }
    machine = {
        "os": record({"family": words(*report_data.OS_FAMILIES),
                      "version": string(f"^{report_data.OS_VERSION.pattern}$", 48),
                      "arch": words(*report_data.ARCHES)}),
        "python": record({"version": string("^([0-9]{1,4}(\\.[0-9]{1,4}){1,3}|unknown)$", 19),
                          "source": words(*report_data.PYTHON_WORDS)}),
        "seconds": integer(0, 7 * 86400),
    }
    model = nullable(string(f"^{report_data.MODEL.pattern}$", 24))
    return {
        "$comment": "Written by tools/repo/report_schema.py: change that, not this.",
        "maxBytes": MAX_BYTES,
        # How the tool classes a failure's words (failures.PATTERNS), in its
        # order, for the API to check a report's words read as the tool's own
        # (website/api/review.js): matched ignoring case, as there.
        "failurePatterns": [[name, pattern.pattern] for name, pattern in failures.PATTERNS],
        # What the stats page and admin view call each failure class and each
        # device kind, in the tool's own words (failures.CLASSES, kinds).
        "failureClasses": dict(failures.CLASSES),
        # How the person stopped a cancelled run, its stopped "error" (and
        # only a cancelled run's), for the API to hold any other pairing.
        "stoppedBy": list(report_data.STOPPED_BY),
        "deviceNames": {kind.key: kind.name for kind in kinds.EVERY},
        # One device's readout's report (report_data.readout_payload): held
        # to this instead where its "kind" is "readout".
        "readout": record({
            **shared,
            "kind": words("readout"),
            "result": words(*report_data.READOUT_RESULTS),
            # How a failed one failed, in the tool's own classes.
            "error": nullable(words(*failures.CLASSES)),
            "family": nullable(words(*routes.FAMILIES)),
            "device": nullable(words(*(kind.key for kind in kinds.EVERY))),
            "model": model,
            "stock_model": model,
            "stock_version": nullable(string(f"^{report_data.VERSION.pattern}$", 19)),
            "whole": {"type": ["boolean", "null"]},
            "device_hash": nullable(string("^[0-9a-f]{64}$", 64)),
            **machine,
            # How many devices this computer has converted: bounded only to keep it sane.
            "converted_here": integer(0, report_data.NTH_DEVICE_LIMIT),
        }),
        **record({
            **shared,
            "kind": words("run"),
            "result": words(*report_data.RESULTS),
            "stopped": nullable(record({
                "step": nullable(string(step, 3)),
                # How a run failed, or for one the person stopped, how they did.
                "error": words(*failures.CLASSES, *report_data.STOPPED_BY),
                "text": string("^[ -~]*$", report_data.TEXT_LIMIT),
                # Where in the tool's code an unexpected error came from.
                "crash_site": nullable(string(f"^{report_data.CRASH_SITE.pattern}$", 200)),
            })),
            "family": nullable(words(*routes.FAMILIES)),
            "device": nullable(words(*(kind.key for kind in kinds.EVERY))),
            "model": model,
            "stock_version": nullable(string(f"^{report_data.VERSION.pattern}$", 19)),
            "device_hash": nullable(string("^[0-9a-f]{64}$", 64)),
            "firmware": nullable(words(*routes.FIRMWARES)),
            "source": words(*sources),
            "backup": words(*records.BACKUP_OUTCOMES),
            "identity": nullable(words("backup", "kept", "account")),
            **machine,
            "resumed": {"type": "boolean"},
            "stages": {"type": "object", "maxProperties": 64, "propertyNames": string(step, 3),
                       "additionalProperties": record({"seconds": integer(0, 86400), "busy": integer(0, 86400),
                                                       "tries": integer(1, 99),
                                                       "wait": integer(0, report_data.WAIT_LIMIT),
                                                       "setup_network": nullable(words(*join.OWN_NETWORK_REASONS)),
                                                       "power_cycles": integer(0, 99)})},
            "signal": nullable(integer(-127, 0)),
            "wifi_typed": {"type": ["boolean", "null"]},
            "setup_network": {"type": ["boolean", "null"]},
            "power_cycled": {"type": ["boolean", "null"]},
            "checks": words(*report_data.CHECKS),
            "meter": nullable(words(*report_data.METER_WORDS)),
            # Not capped: the bound only keeps the number a sane one.
            "nth_device": nullable(integer(1, report_data.NTH_DEVICE_LIMIT)),
            "chip": record({"revision": nullable(string("^[0-9]{1,3}\\.[0-9]{1,3}$", 7)),
                            "flash_id": nullable(string("^[0-9a-f]{6}$", 6)),
                            "flash_mb": nullable(integer(1, 128))}),
            "computer": record({"connection": nullable(words(*report_data.CONNECTIONS)),
                                "vpn": {"type": ["boolean", "null"]},
                                "networks": integer(0, report_data.NETWORKS_LIMIT),
                                "security_note": {"type": "boolean"},
                                "firewall": nullable(words(*report_data.FIREWALL_OUTCOMES)),
                                "admin": {"type": "boolean"}}),
            "ewelink_region": nullable(words(*report_data.EWELINK_REGIONS)),
            "ctrl_c_carried_on": integer(0, report_data.CTRL_C_LIMIT),
            "launcher": words(*report_data.LAUNCHERS),
            "install": words(*report_data.INSTALLS),
            # The name the device's own updater checks, and whether it is the tool's for its kind.
            "firmware_name": nullable(string(f"^{report_data.A_NAME.pattern}$", report_data.FIRMWARE_NAME_LIMIT)),
            "firmware_name_expected": {"type": ["boolean", "null"]},
            "begun_on": nullable(string(f"^{report_data.TOOL_VERSION.pattern}$", 11)),
            "back_after_power_cycle": nullable(integer(0, report_data.WAIT_LIMIT)),
        }),
    }


def grown(new: Any, old: Any) -> Any:
    """`new`, with every word `old` allowed still allowed: a list of words
    only grows."""
    if isinstance(new, dict) and isinstance(old, dict):
        merged = {key: grown(value, old.get(key)) for key, value in new.items()}
        if isinstance(new.get("enum"), list) and isinstance(old.get("enum"), list):
            merged["enum"] = [*new["enum"], *(word for word in old["enum"] if word not in new["enum"])]
        return merged
    return new


def current() -> dict[str, Any]:
    """The schema to write: today's, grown by what the file allowed before."""
    try:
        old = json.loads(PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        old = {}
    return grown(schema(), old)


def text(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2) + "\n"


TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "null": type(None)}


def is_type(value: Any, name: str) -> bool:
    if name == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, TYPES[name])


def validate(rule: dict[str, Any], value: Any, where: str = "report") -> list[str]:
    """What is wrong with `value` by `rule`: [] when it fits."""
    kinds_allowed = rule.get("type")
    if kinds_allowed is not None:
        allowed = kinds_allowed if isinstance(kinds_allowed, list) else [kinds_allowed]
        if not any(is_type(value, name) for name in allowed):
            return [f"{where}: not {' or '.join(allowed)}"]
    if "enum" in rule and value not in rule["enum"]:
        return [f"{where}: not one of the allowed words"]
    problems = []
    if isinstance(value, str):
        if len(value) > rule.get("maxLength", len(value)):
            problems.append(f"{where}: too long")
        # The whole value, as JavaScript's anchors mean it: Python's $ would
        # also let a newline at the end through.
        if "pattern" in rule and not re.fullmatch(rule["pattern"].removeprefix("^").removesuffix("$"), value):
            problems.append(f"{where}: not as it should be")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < rule.get("minimum", value) or value > rule.get("maximum", value):
            problems.append(f"{where}: out of range")
    if isinstance(value, dict):
        if len(value) > rule.get("maxProperties", len(value)):
            problems.append(f"{where}: too many")
        properties = rule.get("properties", {})
        problems += [f"{where}.{key}: missing" for key in rule.get("required", []) if key not in value]
        for key, item in value.items():
            if "propertyNames" in rule:
                problems += validate(rule["propertyNames"], key, f"{where} name {key[:16]}")
            if key in properties:
                problems += validate(properties[key], item, f"{where}.{key}")
            elif rule.get("additionalProperties") is False:
                problems.append(f"{where}.{key[:16]}: not allowed")
            elif isinstance(rule.get("additionalProperties"), dict):
                problems += validate(rule["additionalProperties"], item, f"{where}.{key}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["check"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    wanted = text(current())
    have = PATH.read_text(encoding="utf-8") if PATH.exists() else ""
    if args == ["check"]:
        if have != wanted:
            print(f"{PATH.relative_to(ROOT)} is not current: run python3 tools/repo/report_schema.py")
            return 1
        print(f"{PATH.relative_to(ROOT)} is current.")
        return 0
    PATH.write_text(wanted, encoding="utf-8")
    print(f"Wrote {PATH.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
