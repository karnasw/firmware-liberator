#!/usr/bin/env python3
"""Make a copy of a stock flash dump that boots under QEMU.

Whatever the device, two things are done to every app slot holding Sonoff's
firmware: its Bluetooth start-up is skipped (BLUETOOTH, below), and with
--trust-ca the coolkit CA it pins is swapped for ours. Each slot is then
re-sealed (checksum and SHA-256) so the bootloader accepts it. The source dump
is never modified.

    python -I tools/espressif/emulator/patch_stock.py captures/emulation/flash.bin captures/emulation/run-flash.bin
"""

from __future__ import annotations

import argparse
import base64
import binascii
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.espressif import flashmap  # noqa: E402
from tools.espressif.emulator import appimage  # noqa: E402

RETURN_0_RISCV = bytes.fromhex("01458280")  # c.li a0,0 ; c.jr ra
RETURN_0_XTENSA = bytes.fromhex("0c021df0")  # movi.n a2,0 ; retw.n

PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
PEM_END = b"-----END CERTIFICATE-----"
# A certificate's EC public key, as its DER gives it: the OID id-ecPublicKey.
EC_PUBLIC_KEY = bytes.fromhex("06072a8648ce3d0201")

# QEMU (Espressif's and the lcgamboa fork) emulates no Bluetooth: the
# controller's clock setup fails, and the app aborts or faults there. Bluetooth
# is only for first-time pairing in the eWeLink app, which a device being
# converted has done already, so the app's own functions that start it are made
# to return 0 at once, as if they had: what uses it then finds it was never
# advertising. They are found by what they call (bluetooth_starters).
BT_INIT_NAME = b"esp_bt_controller_init\0"
XTENSA_ENTRY = 0x36
XTENSA_CALL8 = 0x25

# A classic ESP32 before revision 3 has a bug in reading DPORT registers, so its
# firmware stops the other core around each read: an interrupt there, and a wait
# for its answer. QEMU has no such bug, and a core asleep can take too long to
# answer, which trips the interrupt watchdog at start-up. The two functions
# return at once instead. esp_dport_access_stall_other_cpu_start is the one
# function holding the addresses of both cores' interrupt registers, and _end
# comes straight after it (dport_access_stalls).
DPORT_STALL_REGISTERS = (0x3FF000E4, 0x3FF000E8)  # DPORT_CPU_INTR_FROM_CPU_2, _3
IRAM = (0x40080000, 0x400A0000)
IPC_ISR_NAME = b"esp_ipc_isr"
RETURN_XTENSA = b"\x1d\xf0"  # retw.n


class PatchError(SystemExit):
    pass


def _coolkit_ca(pem: bytes):
    """The certificate in `pem` if it is a stock coolkit root CA, by its issuer's
    organisation: the older RSA one ("coolkit", coolkit.cn) or the newer EC one
    ("CoolKit", CoolKit Root CA ECC)."""
    from cryptography import x509  # local import: only needed for --trust-ca
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except ValueError:
        return None
    org = cert.subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)
    return cert if org and org[0].value.lower() == "coolkit" else None


def _is_coolkit_ca(pem: bytes) -> bool:
    return _coolkit_ca(pem) is not None


def holds_rsa_ca(flash: bytes) -> bool:
    """Whether the Sonoff firmware in this flash trusts eWeLink's RSA CA: the
    stub's own CA is then RSA too, for the stack that firmware gives it.
    Read from the certificates' DER, with nothing beyond the standard library,
    for the fault suite as for emu.sh."""
    for slot in flashmap.app_slots(flash):
        app = flash[slot.offset:slot.offset + slot.size]
        if holds_sonoff_firmware(app):
            for der in certificates(app):
                if b"coolkit" in der.lower():
                    return EC_PUBLIC_KEY not in der
    return False


def certificates(app: bytes) -> list[bytes]:
    """The DER of each PEM certificate in `app`."""
    found, at = [], app.find(PEM_BEGIN)
    while at >= 0:
        end = app.find(PEM_END, at)
        if end < 0:
            break
        try:
            found.append(base64.b64decode(b"".join(app[at + len(PEM_BEGIN):end].split())))
        except (binascii.Error, ValueError):
            pass
        at = app.find(PEM_BEGIN, end)
    return found


def coolkit_slots(app: bytes) -> list[tuple[int, int]]:
    """Each place the app holds the coolkit CA, as (offset, bytes up to its NUL)."""
    slots, at = [], app.find(PEM_BEGIN)
    while at >= 0:
        end = app.find(PEM_END, at)
        if end < 0:
            break
        if _is_coolkit_ca(bytes(app[at:end + len(PEM_END)])):
            slots.append((at, app.index(b"\0", at) - at))
        at = app.find(PEM_BEGIN, at + 1)
    return slots


def swap_trust_anchor(app: bytearray, ca_pem: bytes) -> None:
    """Replace every coolkit CA the app pins with our own, NUL-padded.

    Each is a NUL-terminated string in a slot of its own, so every later offset
    (and the unrelated data right after each) is left untouched, and mbedTLS
    reads only up to the first NUL.
    """
    if _is_coolkit_ca(ca_pem):  # must be our own CA, never coolkit's
        raise SystemExit("replacement CA is the coolkit CA; expected our own CA")
    slots = coolkit_slots(app)
    if not slots:
        raise SystemExit("the app holds no coolkit CA to swap")
    for at, size in slots:
        if len(ca_pem) >= size:
            raise SystemExit(f"replacement CA PEM is {len(ca_pem)} bytes; must be < "
                             f"{size} to fit the trust slot (use an EC or RSA-1024 CA)")
        app[at:at + size] = ca_pem + b"\0" * (size - len(ca_pem))


def _address_of(image: appimage.Image, offset: int) -> int | None:
    for segment in image.segments:
        if segment.offset <= offset < segment.offset + segment.size:
            return segment.vaddr + offset - segment.offset
    return None


def _xtensa_function_of(code: bytes, base: int, pc: int) -> int | None:
    """The start of the function holding `pc`: windowed-ABI functions start on
    a 4-byte boundary with `entry a1, n`."""
    at = pc & ~3
    while at >= base:
        opcode = code[at - base:at - base + 2]
        if opcode[0] == XTENSA_ENTRY and opcode[1] & 0x0F == 1:
            return at
        at -= 4
    return None


def _xtensa_starters(code: bytes, base: int, literals: set[int | None]) -> list[int]:
    inits = set()
    for at in range(len(code) - 2):
        if code[at] & 0x0F != 1:  # l32r
            continue
        imm = struct.unpack_from("<H", code, at + 1)[0]
        if ((base + at + 3) & ~3) + ((imm - 0x10000) << 2) in literals:
            function = _xtensa_function_of(code, base, base + at)
            if function is not None:
                inits.add(function)
    starters = set()
    for at in range(len(code) - 2):
        if code[at] & 0x3F != XTENSA_CALL8:
            continue
        offset = (code[at] >> 6) | (code[at + 1] << 2) | (code[at + 2] << 10)
        offset -= 0x40000 if offset & 0x20000 else 0
        if ((base + at) & ~3) + (offset << 2) + 4 in inits:
            function = _xtensa_function_of(code, base, base + at)
            if function is not None:
                starters.add(function)
    return sorted(starters)


def _riscv_function_of(code: bytes, base: int, pc: int) -> int | None:
    """The start of the function holding `pc`: the instruction nearest before
    it that makes room on the stack (c.addi16sp, c.addi sp or addi sp, by a
    negative amount)."""
    at = pc
    while at >= base:
        half = struct.unpack_from("<H", code, at - base)[0]
        if half & 0x1000 and half & 0xEF83 in (0x6101, 0x0101):
            return at
        if at - base + 4 <= len(code):
            word = struct.unpack_from("<I", code, at - base)[0]
            if word & 0xFFFFF == 0x10113 and word >> 31:
                return at
        at -= 2
    return None


def _riscv_starters(code: bytes, base: int, name_address: int) -> list[int]:
    upper = (name_address + 0x800) >> 12
    lower = name_address - (upper << 12)
    inits = set()
    for at in range(0, len(code) - 4, 2):
        word = struct.unpack_from("<I", code, at)[0]
        if word & 0x7F != 0x37 or word >> 12 != upper:  # lui rd, upper
            continue
        register = (word >> 7) & 0x1F
        for later in range(at + 2, min(at + 40, len(code) - 4), 2):
            next_word = struct.unpack_from("<I", code, later)[0]
            immediate = next_word >> 20
            immediate -= 4096 if immediate & 0x800 else 0
            if next_word & 0x707F == 0x13 and (next_word >> 15) & 0x1F == register and immediate == lower:
                function = _riscv_function_of(code, base, base + at)
                if function is not None:
                    inits.add(function)
                break
    starters = set()
    for at in range(0, len(code) - 4, 2):
        word = struct.unpack_from("<I", code, at)[0]
        if word & 0xFFF != 0x0EF:  # jal ra, offset
            continue
        offset = ((word >> 31) << 20) | (((word >> 12) & 0xFF) << 12) | (((word >> 20) & 1) << 11) \
            | (((word >> 21) & 0x3FF) << 1)
        offset -= 0x200000 if offset & 0x100000 else 0
        if base + at + offset in inits:
            function = _riscv_function_of(code, base, base + at)
            if function is not None:
                starters.add(function)
    return sorted(starters)


def bluetooth_starters(app: bytes, image: appimage.Image, chip: str = "esp32") -> list[int]:
    """The functions of the app that call esp_bt_controller_init, by load
    address. That is the one function holding the address of its own name (its
    asserts' __func__): on an ESP32 by an l32r from a literal pool, on an
    ESP32-C3 by a lui and an addi."""
    named = app.find(BT_INIT_NAME)
    name_address = _address_of(image, named) if named >= 0 else None
    if name_address is None:
        return []
    low, high = (0x400D0000, 0x40400000) if chip == "esp32" else (0x42000000, 0x42800000)
    code_segment = next((s for s in image.segments if low <= s.vaddr < high), None)
    if code_segment is None:
        return []
    code, base = bytes(app[code_segment.offset:code_segment.offset + code_segment.size]), code_segment.vaddr
    if chip != "esp32":
        return _riscv_starters(code, base, name_address)
    literals, at, packed = set(), app.find(struct.pack("<I", name_address)), struct.pack("<I", name_address)
    while at >= 0:
        literals.add(_address_of(image, at))
        at = app.find(packed, at + 1)
    return _xtensa_starters(code, base, literals)


def dport_stalls(app: bytes, image: appimage.Image) -> list[int]:
    """The addresses of esp_dport_access_stall_other_cpu_start and _end, or none
    in a firmware built without them."""
    for segment in image.segments:
        if not IRAM[0] <= segment.vaddr < IRAM[1]:
            continue
        code, base = bytes(app[segment.offset:segment.offset + segment.size]), segment.vaddr
        holding: dict[int, set[int]] = {}
        for at in range(len(code) - 2):
            if code[at] & 0x0F != 1:  # l32r
                continue
            imm = struct.unpack_from("<H", code, at + 1)[0]
            literal = ((base + at + 3) & ~3) + ((imm - 0x10000) << 2)
            word = _word_at(app, image, literal)
            if word in DPORT_STALL_REGISTERS:
                function = _xtensa_function_of(code, base, base + at)
                if function is not None:
                    holding.setdefault(function, set()).add(word)
        for function, words in sorted(holding.items()):
            if len(words) == len(DPORT_STALL_REGISTERS):
                for after in range(function + 4 - base, len(code) - 2, 4):
                    if code[after] == XTENSA_ENTRY and code[after + 1] & 0x0F == 1:
                        return [function, base + after]
    return []


def _word_at(app: bytes, image: appimage.Image, address: int) -> int | None:
    for segment in image.segments:
        if segment.vaddr <= address < segment.vaddr + segment.size - 3:
            return struct.unpack_from("<I", app, segment.offset + address - segment.vaddr)[0]
    return None


def skip_dport_stall(app: bytearray, image: appimage.Image, chip: str) -> int:
    """Patch out a classic ESP32's stopping of the other core; how many functions were."""
    # ESP-IDF 5's esp_ipc_isr uses the same registers, for stopping a core it needs stopped.
    if chip != "esp32" or IPC_ISR_NAME in app:
        return 0
    stalls = dport_stalls(app, image)
    for address in stalls:  # after the `entry`, which the return needs
        at = image.offset_of(address) + 3
        app[at:at + len(RETURN_XTENSA)] = RETURN_XTENSA
    return len(stalls)


def skip_bluetooth(app: bytearray, image: appimage.Image, chip: str) -> int:
    """Patch the app's Bluetooth start-up; how many functions were patched."""
    starters = bluetooth_starters(app, image, chip)
    if not starters:
        raise PatchError("no function calling esp_bt_controller_init found: see docs/adding-a-device.md")
    for address in starters:
        if chip == "esp32":  # after the `entry`, which the return needs
            at = image.offset_of(address) + 3
            app[at:at + len(RETURN_0_XTENSA)] = RETURN_0_XTENSA
        else:  # at the start, before the stack changes
            at = image.offset_of(address)
            app[at:at + len(RETURN_0_RISCV)] = RETURN_0_RISCV
    return len(starters)


def trusts(flash: bytes, ca_pem: bytes | None) -> bool | None:
    """Whether every Sonoff firmware in this flash holds `ca_pem`, as
    swap_trust_anchor puts it there; None when the flash holds none."""
    found = None
    for slot in flashmap.app_slots(flash):
        app = flash[slot.offset:slot.offset + slot.size]
        if holds_sonoff_firmware(app):
            found = found is not False and ca_pem is not None and ca_pem in app
    return found


def holds_sonoff_firmware(app: bytes) -> bool:
    """Whether an app slot holds Sonoff's firmware, which uses Bluetooth - not
    the conversion's installer in the other slot of a device backed up on it."""
    return app[0] == appimage.MAGIC and BT_INIT_NAME in app


def patch(flash: bytes, ca_pem: bytes | None = None) -> bytes:
    out = bytearray(flash)
    chip = flashmap.chip_of(flash)
    patched = 0
    for slot in flashmap.app_slots(flash):
        app = bytearray(out[slot.offset:slot.offset + slot.size])
        if not holds_sonoff_firmware(app):
            continue
        image = appimage.parse(app)
        if not appimage.verify(app, image):
            raise SystemExit(f"{slot.label} does not verify before patching")
        skip_bluetooth(app, image, chip)
        skip_dport_stall(app, image, chip)
        if ca_pem is not None:
            swap_trust_anchor(app, ca_pem)
        appimage.reseal(app, image)
        out[slot.offset:slot.offset + slot.size] = app
        patched += 1
    if not patched:
        raise SystemExit("no app slot holds Sonoff's firmware")
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--trust-ca", type=Path, default=None,
                        help="PEM of our own CA to swap in for the coolkit root "
                             "(cloud-stub mode); omit for a plain boot image")
    args = parser.parse_args(argv)
    if args.source.resolve() == args.output.resolve():
        raise SystemExit("refusing to overwrite the source dump")
    ca_pem = args.trust_ca.read_bytes() if args.trust_ca else None
    args.output.write_bytes(patch(args.source.read_bytes(), ca_pem))
    args.output.chmod(0o600)
    extra = " + trust-anchor swap" if ca_pem else ""
    print(f"Wrote {args.output}: Bluetooth start-up skipped{extra}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
