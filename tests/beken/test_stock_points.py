"""Finding the emulator's places in a BK7238 app, in made-up apps: a few Thumb
instructions of each shape that matters, at the addresses asked for, in zeros.
No firmware is here, and nothing is run."""
import contextlib
import io
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.beken import rbl, rebuild_dump, stock_points as sp

SIZE = 0x170000
# Where things are in S61 1.0.2 and 1.1.0: what the finders must say of the same shapes there.
S61_102 = dict(after_memcmp=0x29C3C, gate_passed=0x29C82, ef_get_env_blob=0x12F438, x509_crt_parse=0x125E4C,
               appfw_task=0x319A8, appfw_loop=0x319C4, ais_cloud_start=0x35744, link_create=0x357F2,
               link_task=0x35AA8, start_cloud=0x2C204, lan_publish=0x2BF7C, ctx_pointer=0x4044AC,
               cloud_flag=0x403E31, url_offset=0x5F, dispatch_url="http://8.8.8.8:80")
S61_110 = dict(S61_102, after_memcmp=0x29FE4, gate_passed=0x2A02A, ef_get_env_blob=0x135844, x509_crt_parse=0x12C258,
               appfw_task=0x33450, appfw_loop=0x3346C, ais_cloud_start=0x36BE8, link_create=0x36C9A,
               link_task=0x379D8, start_cloud=0x2C774, lan_publish=0x2C490, ctx_pointer=0x4045B8,
               cloud_flag=0x403F03)


class Thumb:
    """Enough of an assembler: instructions from `at`, then their literal pool."""

    def __init__(self, app, at):
        self.app, self.at, self.pending = app, at, []

    def half(self, value):
        struct.pack_into("<H", self.app, self.at - sp.APP_BASE, value)
        self.at += 2
        return self

    def op(self, *values):
        for value in values:
            self.half(value)
        return self

    def bl(self, target):
        offset = target - (self.at + 4)
        return self.op(0xF000 | (offset >> 12) & 0x7FF, 0xF800 | (offset >> 1) & 0x7FF)

    def b(self, target):
        return self.half(0xE000 | ((target - (self.at + 4)) >> 1) & 0x7FF)

    def beq(self, target):
        return self.half(0xD000 | ((target - (self.at + 4)) >> 1) & 0xFF)

    def ldr(self, register, value):
        self.pending.append((self.at, value))
        return self.half(0x4800 | register << 8)

    def pool(self):
        self.at = (self.at + 3) & ~3
        for load, value in self.pending:
            half = struct.unpack_from("<H", self.app, load - sp.APP_BASE)[0]
            struct.pack_into("<H", self.app, load - sp.APP_BASE, half | (self.at - ((load + 4) & ~3)) // 4)
            struct.pack_into("<I", self.app, self.at - sp.APP_BASE, value)
            self.at += 4
        self.pending = []
        return self


PUSH, CMP_R0, LDR_R4_R6, LDRB_R3_R3, POP = 0xB5F0, 0x2800, 0x6834, 0x781B, 0xBDF0


class App:
    """A made-up app with every shape the finders read."""
    STRINGS = 0x150000

    def __init__(self, where):
        self.app = bytearray(SIZE)
        self.next_string = self.STRINGS
        self.caller = 0x20000   # calls that only make their targets functions
        self.where = where
        self.fill()

    def string(self, text):
        at = self.next_string
        self.app[at - sp.APP_BASE:at - sp.APP_BASE + len(text)] = text
        self.next_string += len(text) + 1
        return at

    def thumb(self, at):
        return Thumb(self.app, at)

    def called(self, target):
        self.thumb(self.caller).bl(target)
        self.caller += 4

    def env_function(self, at, keeps_r3):
        self.called(at)
        code = self.thumb(at).half(PUSH)
        if keeps_r3:
            code.half(0x001D)   # movs r5, r3
        code.ldr(0, self.env).half(POP).pool()

    def fill(self):
        w = self.where
        self.env = self.string(sp.ENV_NOT_READY)
        self.env_function(w["ef_get_env_blob"], True)
        self.env_function(w["ef_get_env_blob"] + 0x68, False)

        self.called(w["x509_crt_parse"])
        (self.thumb(w["x509_crt_parse"]).half(PUSH).ldr(1, self.string(sp.PEM_BEGIN))
         .ldr(2, self.string(sp.PEM_END)).half(POP).pool())

        self.called(w["after_memcmp"] - 0x20)
        gate = self.thumb(w["after_memcmp"] - 0x20).half(PUSH).ldr(1, self.string(sp.IDENTITY_FORMAT))
        gate.half(0x2220)   # movs r2, #0x20
        gate.at = w["after_memcmp"] - 4
        gate.bl(0x40000).half(CMP_R0).beq(w["gate_passed"]).half(POP)
        gate.pool()

        self.called(w["appfw_task"] - 0x80)
        (self.thumb(w["appfw_task"] - 0x80).half(PUSH).ldr(1, self.string(sp.NAME_APPFW_TASK))
         .ldr(0, w["appfw_task"] | 1).bl(0x40000).half(POP).pool())
        task = self.thumb(w["appfw_task"]).half(PUSH)
        task.at = w["appfw_loop"]
        task.op(0x2001, 0x2002).b(w["appfw_loop"]).pool()

        self.called(w["ais_cloud_start"])
        start = self.thumb(w["ais_cloud_start"]).half(PUSH).ldr(6, w["ctx_pointer"]).half(LDR_R4_R6)
        start.pool().half(0x46C0)
        start.at = w["link_create"] - 4
        start.ldr(1, self.string(sp.NAME_LINK_TASK)).ldr(0, w["link_task"] | 1).bl(0x40000).half(POP).pool()

        (self.thumb(w["start_cloud"]).half(PUSH).ldr(3, w["cloud_flag"]).half(LDRB_R3_R3)
         .bl(w["ais_cloud_start"]).half(POP).pool())
        (self.thumb(w["lan_publish"]).half(PUSH).ldr(3, w["cloud_flag"] - 1).half(LDRB_R3_R3)
         .half(POP).pool())
        self.thumb(0x21000).half(PUSH).bl(w["lan_publish"]).bl(w["start_cloud"]).half(POP)
        self.called(0x21000)
        # Another call straight before start_cloud, as on the T6: to no flag of its file.
        other = 0x70000
        self.called(other)
        self.thumb(other).half(PUSH).ldr(3, 0x41FF00).half(LDRB_R3_R3).half(POP).pool()
        self.thumb(0x21100).half(PUSH).bl(other).bl(w["start_cloud"]).half(POP)
        self.called(0x21100)

        # The set-up: its stack structure starts at sp+0x2c, the URL's field at sp+0x8b.
        setup = 0x22000
        self.called(setup)
        (self.thumb(setup).half(PUSH).half(0xA80B).ldr(5, w["cloud_flag"]).half(0x2000 | 0x2C + w["url_offset"])
         .ldr(1, self.string(w["dispatch_url"].encode() + b"\0")).half(0x0052).half(0x4468).bl(0x40000)
         .half(POP).pool())

    def bytes(self):
        return bytes(self.app)


def found(app):
    return {name: getattr(sp.find(app), name) for name in S61_102}


class KnownBuildsTests(unittest.TestCase):
    """Where the same shapes are in S61 1.0.2 and 1.1.0, the answer is the
    address those builds have, which ran in the emulator before there were finders."""

    def test_1_0_2(self):
        self.assertEqual(found(App(S61_102).bytes()), S61_102)

    def test_1_1_0(self):
        self.assertEqual(found(App(S61_110).bytes()), S61_110)

    def test_a_build_laid_out_otherwise(self):
        where = {name: (value + 0x1000 if isinstance(value, int) and value > 0x1000 else value)
                 for name, value in S61_102.items()}
        self.assertEqual(found(App(where).bytes()), where)


class RefusalTests(unittest.TestCase):
    """A point not found, or that could be more than one place, is said by name."""

    def refuses(self, app, point):
        with self.assertRaises(sp.PointError) as caught:
            sp.find(bytes(app))
        self.assertEqual(caught.exception.point, point)
        self.assertIn(point, str(caught.exception))
        return str(caught.exception)

    def test_another_firmware_has_none_of_the_tasks(self):
        made = App(S61_102)
        at = made.app.find(sp.NAME_LINK_TASK)
        made.app[at] = 0x21
        results = sp.survey(made.bytes())
        self.assertEqual(results["x509_crt_parse"], S61_102["x509_crt_parse"])    # what is there is still found
        for point in ("link_task", "ais_cloud_start", "start_cloud", "ctx_pointer", "cloud_flag", "lan_publish",
                      "dispatch_url"):
            self.assertIsInstance(results[point], sp.PointError, point)
            self.assertIn(point, str(results[point]))

    def test_a_second_certificate_parser_is_not_guessed_between(self):
        made = App(S61_102)
        twin = 0x60000
        made.called(twin)
        begin, end = made.app.find(sp.PEM_BEGIN) + sp.APP_BASE, made.app.find(sp.PEM_END) + sp.APP_BASE
        made.thumb(twin).half(PUSH).ldr(1, begin).ldr(2, end).half(POP).pool()
        self.assertIn(hex(twin), self.refuses(made.app, "x509_crt_parse"))

    def test_only_the_certificate_markers_together_make_the_parser(self):
        made = App(S61_102)
        made.app[made.app.find(sp.PEM_END)] = 0x21
        self.refuses(made.app, "x509_crt_parse")

    def test_easyflash_without_a_four_argument_function(self):
        made = App(S61_102)
        at = S61_102["ef_get_env_blob"] + 2 - sp.APP_BASE
        struct.pack_into("<H", made.app, at, 0x46C0)   # nop for its movs r5, r3
        self.assertIn("four arguments", self.refuses(made.app, "ef_get_env_blob"))

    def test_two_four_argument_functions(self):
        made = App(S61_102)
        made.env_function(S61_102["ef_get_env_blob"] + 0x68, True)
        self.refuses(made.app, "ef_get_env_blob")

    def test_the_gate_must_test_its_compare_at_once(self):
        made = App(S61_102)
        struct.pack_into("<H", made.app, S61_102["after_memcmp"] - sp.APP_BASE, 0x46C0)
        self.refuses(made.app, "after_memcmp")

    def test_the_lan_publish_is_of_start_clouds_file_only(self):
        made = App(S61_102)
        # Without the flag beside cloud_flag, nothing before start_cloud can be it.
        lan = S61_102["lan_publish"]
        made.thumb(lan).half(PUSH).ldr(3, 0x41FF00).half(LDRB_R3_R3).half(POP).pool()
        self.refuses(made.app, "lan_publish")

    def test_two_lan_publishes_beside_the_flag(self):
        made = App(S61_102)
        made.thumb(0x70000).half(PUSH).ldr(3, S61_102["cloud_flag"] - 2).half(LDRB_R3_R3).half(POP).pool()
        self.assertIn(hex(0x70000), self.refuses(made.app, "lan_publish"))

    def test_a_dispatch_url_must_be_plain(self):
        made = App(S61_102)
        at = made.app.find(b"http://8.8.8.8:80")
        made.app[at + 7] = ord("%")
        self.refuses(made.app, "dispatch_url")

    def test_a_task_must_be_given_a_function(self):
        made = App(S61_102)
        made.thumb(S61_102["link_create"] - 2).half(0x2000)   # movs r0, #0 for loading the task
        self.refuses(made.app, "link_task")


class FunctionStartTests(unittest.TestCase):
    def code(self, *shapes):
        app = bytearray(0x4000)
        for at, values in shapes:
            Thumb(app, at).op(*values)
        return app

    def test_data_looking_like_a_push_is_passed_over(self):
        app = self.code((0x10100, [PUSH, 0x2000, 0x2000]), (0x10108, [0xB510]))
        Thumb(app, 0x10000).bl(0x10100)
        self.assertEqual(sp.Code(bytes(app)).function_of(0x1010C), 0x10100)

    def test_set_up_moved_before_the_push(self):
        app = self.code((0x10100, [0x2299, PUSH, 0x2000]))
        Thumb(app, 0x10000).bl(0x10100)
        self.assertEqual(sp.Code(bytes(app)).function_of(0x10104), 0x10100)

    def test_a_function_only_jumped_to(self):
        app = self.code((0x10100, [PUSH, 0x2000]))
        Thumb(app, 0x10000).b(0x10100)
        self.assertEqual(sp.Code(bytes(app)).function_of(0x10102), 0x10100)

    def test_a_function_only_pointed_at(self):
        app = self.code((0x10100, [PUSH, 0x2000]))
        struct.pack_into("<I", app, 0x200, 0x10101)
        self.assertEqual(sp.Code(bytes(app)).function_of(0x10102), 0x10100)

    def test_no_function_is_found_where_nothing_refers(self):
        app = self.code((0x10100, [PUSH, 0x2000]))
        self.assertIsNone(sp.Code(bytes(app)).function_of(0x10102))

    def test_a_blx_goes_to_a_word_boundary(self):
        app = bytearray(0x200)
        Thumb(app, 0x10002).op(0xF000, 0xE800 | 0x10)
        self.assertEqual(sp.Code(bytes(app)).call_target(0x10002), 0x10024 & ~3)


class FilesTests(unittest.TestCase):
    APP = b"the app" * 100

    def flash(self, header_name="app"):
        table = {"bootloader": ("beken_onchip_crc", 0, 0x10000), "app": ("beken_onchip_crc", 0x10000, 0x250000),
                 "download": ("beken_onchip", 0x286000, 0x15A000)}
        entries = b""
        for name, (device, offset, length) in table.items():
            body = b"01PE" + name.encode().ljust(24, b"\0") + device.encode().ljust(24, b"\0") + struct.pack(
                "<II", offset, length)
            entries += body + struct.pack("<I", zlib.crc32(body))
        logical = bytearray(b"\xff" * (0x400000 // 34 * 32))
        logical[0x100:0x100 + len(entries)] = entries
        logical[0x10000:0x10000 + len(self.APP) + 64] = self.APP + b"stale after the end".ljust(64)
        logical[0x25FFA0:0x25FFA0 + rbl.HEADER] = rbl.pack(self.APP, name=header_name, version="1.0.0")[:rbl.HEADER]
        return bytes(rebuild_dump.stripe(bytes(logical)))

    def test_a_whole_flash_gives_the_app_to_its_headers_size(self):
        self.assertEqual(sp.app_of_file(self.flash()), self.APP)

    def test_a_flash_with_no_header_of_the_app_gives_the_partition(self):
        app = sp.app_of_flash(self.flash(header_name="other"))
        self.assertTrue(app.startswith(self.APP + b"stale"))
        self.assertEqual(len(app), 0x250000 - rbl.HEADER)

    def test_an_rbl_gives_the_app_in_it(self):
        self.assertEqual(sp.app_of_file(rbl.pack(self.APP, version="1.0.0")), self.APP)

    def test_an_app_is_itself(self):
        self.assertEqual(sp.app_of_file(self.APP), self.APP)

    def test_the_command_line_names_what_it_could_not_find(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "app.bin")
            path.write_bytes(App(S61_102).bytes())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(sp.main([str(path)]), 0)
            self.assertIn("after_memcmp     0x29c3c", out.getvalue())
            path.write_bytes(self.APP)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(sp.main([str(path)]), 1)
            self.assertIn("x509_crt_parse: no function holds", out.getvalue())


if __name__ == "__main__":
    unittest.main()
