"""A stock firmware made to boot under QEMU, without assuming which firmware."""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import struct
import unittest

from tools.espressif import flashmap
from tools.espressif.emulator import appimage, patch_stock

RODATA, CODE = 0x3F400020, 0x400D0020
STARTER, INIT = 0x400D0100, 0x400D0200


def l32r(pc: int, literal: int, register: int = 2) -> bytes:
    imm = ((literal - ((pc + 3) & ~3)) >> 2) & 0xFFFF
    return bytes([(register << 4) | 1]) + struct.pack("<H", imm)


def call8(pc: int, target: int) -> bytes:
    offset = ((target - (pc & ~3) - 4) >> 2) & 0x3FFFF
    return bytes([((offset & 3) << 6) | 0x25, (offset >> 2) & 0xFF, (offset >> 10) & 0xFF])


def xtensa_app(*, with_bt: bool = True, coolkit: bytes = b"") -> bytearray:
    """A tiny ESP32 app image: a function starting the controller, as the real
    ones do, and esp_bt_controller_init, known by its name's address."""
    name = patch_stock.BT_INIT_NAME if with_bt else b"something_else\0"
    rodata = (name + b"\0" * 8 + coolkit).ljust(0x800, b"\0")
    code = bytearray(b"\0" * 0x400)
    literal = CODE + 0x40  # the literal pool, before the functions
    struct.pack_into("<I", code, literal - CODE, RODATA)

    def put(address: int, data: bytes) -> None:
        code[address - CODE:address - CODE + len(data)] = data

    put(STARTER, bytes([0x36, 0x41, 0x00]) + bytes(3))
    put(STARTER + 6, call8(STARTER + 6, INIT))
    put(INIT, bytes([0x36, 0x81, 0x00]))
    put(INIT + 3, l32r(INIT + 3, literal))
    segments = [(RODATA, bytes(rodata)), (CODE, bytes(code))]
    image = bytearray(b"\xe9" + bytes([len(segments)]) + bytes(22))
    image[23] = 1  # hash appended
    for vaddr, data in segments:
        image += struct.pack("<II", vaddr, len(data)) + data
    image += bytes(15 - len(image) % 16) + b"\0"
    image[-1] = appimage.checksum(image, appimage.parse(image + bytes(32)))
    image += hashlib.sha256(bytes(image)).digest()
    return image


class Bluetooth(unittest.TestCase):
    def test_the_function_that_starts_it_is_found_by_what_it_calls(self):
        app = xtensa_app()
        self.assertEqual(patch_stock.bluetooth_starters(app, appimage.parse(app)), [STARTER])

    def test_it_returns_at_once_keeping_its_entry(self):
        app = xtensa_app()
        image = appimage.parse(app)
        self.assertEqual(patch_stock.skip_bluetooth(app, image, "esp32"), 1)
        at = image.offset_of(STARTER)
        self.assertEqual(bytes(app[at:at + 7]), bytes([0x36, 0x41, 0x00]) + patch_stock.RETURN_0_XTENSA)

    def test_a_firmware_with_no_such_function_is_said_to_need_looking_at(self):
        app = xtensa_app(with_bt=False)
        with self.assertRaisesRegex(SystemExit, "adding-a-device"):
            patch_stock.skip_bluetooth(app, appimage.parse(app), "esp32")



def riscv_app() -> bytearray:
    """A tiny ESP32-C3 app, its controller start known by a lui and an addi."""
    name_address = 0x3C100020
    upper, lower = (name_address + 0x800) >> 12, name_address & 0xFFF
    code = bytearray(0x200)
    lui = (upper << 12) | (10 << 7) | 0x37
    addi = (lower << 20) | (10 << 15) | (10 << 7) | 0x13
    struct.pack_into("<H", code, 0x100, 0x7139)  # c.addi16sp sp,-64: the init
    struct.pack_into("<II", code, 0x104, lui, addi)
    struct.pack_into("<H", code, 0x20, 0x715D)  # c.addi16sp sp,-80: its starter
    offset = (0x42000020 + 0x100) - (0x42000020 + 0x24)
    jal = ((offset >> 20) & 1) << 31 | ((offset >> 1) & 0x3FF) << 21 | ((offset >> 11) & 1) << 20 \
        | ((offset >> 12) & 0xFF) << 12 | (1 << 7) | 0x6F
    struct.pack_into("<I", code, 0x24, jal)
    segments = [(name_address, patch_stock.BT_INIT_NAME.ljust(0x40, b"\0")), (0x42000020, bytes(code))]
    image = bytearray(b"\xe9" + bytes([len(segments)]) + bytes(22))
    for vaddr, data in segments:
        image += struct.pack("<II", vaddr, len(data)) + data
    image += bytes(16 - len(image) % 16)
    return image


class BluetoothRiscV(unittest.TestCase):
    def test_the_function_that_starts_it_is_found_by_what_it_calls(self):
        app = riscv_app()
        self.assertEqual(patch_stock.bluetooth_starters(app, appimage.parse(app), "esp32c3"), [0x42000040])

    def test_it_returns_at_once(self):
        app = riscv_app()
        image = appimage.parse(app)
        self.assertEqual(patch_stock.skip_bluetooth(app, image, "esp32c3"), 1)
        at = image.offset_of(0x42000040)
        self.assertEqual(bytes(app[at:at + 4]), patch_stock.RETURN_0_RISCV)


def iram_app(*, registers=patch_stock.DPORT_STALL_REGISTERS) -> bytearray:
    """A tiny ESP32 app whose IRAM holds a literal pool with the two interrupt
    registers, a function using both, and the function after it."""
    base, start, end = 0x40080400, 0x40080440, 0x40080480
    code = bytearray(0x100)
    for index, register in enumerate(registers):
        struct.pack_into("<I", code, 4 * index, register)
    code[start - base:start - base + 3] = bytes([0x36, 0x41, 0x00])
    for index in range(len(registers)):
        at = start + 3 + 3 * index
        code[at - base:at - base + 3] = l32r(at, base + 4 * index)
    code[end - base:end - base + 3] = bytes([0x36, 0x41, 0x00])
    image = bytearray(b"\xe9\x01" + bytes(22))
    image[23] = 1
    image += struct.pack("<II", base, len(code)) + code
    image += bytes(15 - len(image) % 16) + b"\0"
    image[-1] = appimage.checksum(image, appimage.parse(image + bytes(32)))
    image += hashlib.sha256(bytes(image)).digest()
    return image


class DportStall(unittest.TestCase):
    def test_the_two_functions_are_found_by_the_registers_the_first_holds(self):
        app = iram_app()
        self.assertEqual(patch_stock.dport_stalls(app, appimage.parse(app)), [0x40080440, 0x40080480])

    def test_they_return_at_once_keeping_their_entries(self):
        app = iram_app()
        image = appimage.parse(app)
        self.assertEqual(patch_stock.skip_dport_stall(app, image, "esp32"), 2)
        for address in (0x40080440, 0x40080480):
            at = image.offset_of(address)
            self.assertEqual(bytes(app[at:at + 5]), bytes([0x36, 0x41, 0x00]) + patch_stock.RETURN_XTENSA)

    def test_a_firmware_without_them_is_left_alone(self):
        app = iram_app(registers=patch_stock.DPORT_STALL_REGISTERS[:1])
        before = bytes(app)
        self.assertEqual(patch_stock.skip_dport_stall(app, appimage.parse(app), "esp32"), 0)
        self.assertEqual(bytes(app), before)

    def test_an_esp32_c3_has_no_such_bug(self):
        app = iram_app()
        self.assertEqual(patch_stock.skip_dport_stall(app, appimage.parse(app), "esp32c3"), 0)


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the cryptography package")
class CoolkitCa(unittest.TestCase):
    @staticmethod
    def pem(organisation: str, key) -> bytes:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        name = x509.Name([x509.NameAttribute(x509.oid.NameOID.ORGANIZATION_NAME, organisation)])
        start = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(1).not_valid_before(start).not_valid_after(start + datetime.timedelta(days=1))
                .sign(key, hashes.SHA256()))
        return cert.public_bytes(serialization.Encoding.PEM)

    def test_both_generations_of_eWeLinks_ca_are_found(self):
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        self.assertTrue(patch_stock._is_coolkit_ca(self.pem("coolkit", rsa.generate_private_key(65537, 1024))))
        self.assertTrue(patch_stock._is_coolkit_ca(self.pem("CoolKit", ec.generate_private_key(ec.SECP256R1()))))
        self.assertFalse(patch_stock._is_coolkit_ca(self.pem("Another", ec.generate_private_key(ec.SECP256R1()))))


class Flash(unittest.TestCase):
    def flash(self) -> bytes:
        data = bytearray(b"\xff" * 0x100000)
        data[0x1000] = 0xE9
        struct.pack_into("<H", data, 0x1000 + flashmap.CHIP_ID_AT, 0)
        table = b"".join(b"\xaa\x50\x00" + bytes([subtype]) + struct.pack("<II", offset, 0x40000)
                         + label.ljust(16, b"\0") + bytes(4)
                         for label, subtype, offset in ((b"ota_0", 0x10, 0x20000), (b"ota_1", 0x11, 0x60000)))
        data[0x8000:0x8000 + len(table)] = table
        return bytes(data)

    def test_only_a_slot_holding_sonoffs_firmware_is_patched_and_it_still_verifies(self):
        flash = bytearray(self.flash())
        sonoff = xtensa_app()
        other = xtensa_app(with_bt=False)
        flash[0x20000:0x20000 + len(sonoff)] = sonoff
        flash[0x60000:0x60000 + len(other)] = other
        out = patch_stock.patch(bytes(flash))
        patched = bytearray(out[0x20000:0x60000])
        self.assertNotEqual(bytes(patched[:len(sonoff)]), bytes(sonoff))
        self.assertTrue(appimage.verify(patched, appimage.parse(patched)))
        self.assertEqual(out[0x60000:0x60000 + len(other)], bytes(other))

    def test_a_flash_with_no_sonoff_firmware_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "no app slot"):
            patch_stock.patch(self.flash())

    def holding(self, coolkit: bytes, *, with_bt: bool = True) -> bytes:
        flash = bytearray(self.flash())
        app = xtensa_app(with_bt=with_bt, coolkit=coolkit)
        flash[0x20000:0x20000 + len(app)] = app
        return bytes(flash)

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the cryptography package")
    def test_the_kind_of_ca_the_firmware_trusts_is_read_from_its_der(self):
        # Read with nothing beyond the standard library, for the fault suite.
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        rsa_ca = CoolkitCa.pem("coolkit", rsa.generate_private_key(65537, 1024))
        ec_ca = CoolkitCa.pem("CoolKit", ec.generate_private_key(ec.SECP256R1()))
        ours = CoolkitCa.pem("Autoflash stub", ec.generate_private_key(ec.SECP256R1()))
        self.assertTrue(patch_stock.holds_rsa_ca(self.holding(rsa_ca + b"\0")))
        self.assertFalse(patch_stock.holds_rsa_ca(self.holding(ec_ca + b"\0")))
        self.assertFalse(patch_stock.holds_rsa_ca(self.holding(ours + b"\0" + rsa_ca + b"\0", with_bt=False)))
        self.assertTrue(patch_stock.holds_rsa_ca(self.holding(ours + b"\0" + rsa_ca + b"\0")))

    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the cryptography package")
    def test_whether_sonoffs_firmware_trusts_the_stubs_ca_as_it_is_now(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        made, again = (CoolkitCa.pem("Autoflash stub", ec.generate_private_key(ec.SECP256R1())) for _ in range(2))
        self.assertIs(patch_stock.trusts(self.holding(made + b"\0"), made), True)
        self.assertIs(patch_stock.trusts(self.holding(made + b"\0"), again), False)
        self.assertIs(patch_stock.trusts(self.holding(made + b"\0"), None), False)   # not made yet
        self.assertIsNone(patch_stock.trusts(self.holding(made + b"\0", with_bt=False), again))   # not Sonoff's


if __name__ == "__main__":
    unittest.main()
