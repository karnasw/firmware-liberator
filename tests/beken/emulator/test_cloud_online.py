"""cloud_online.py, the help Sonoff's firmware is given to come online in the emulator."""

import sys
import unittest
from types import SimpleNamespace
from unittest import mock


class StockHooksTests(unittest.TestCase):
    """The help Sonoff's firmware is given to come online is at the places found
    in it: with another firmware started there (device.py turns it off), none of
    it may touch the CPU, or it would corrupt that firmware's run."""

    # As found in S61 1.0.2.
    POINTS = SimpleNamespace(after_memcmp=0x29C3C, gate_passed=0x29C82, ef_get_env_blob=0x12F438,
                             x509_crt_parse=0x125E4C, appfw_task=0x319A8, appfw_loop=0x319C4,
                             ais_cloud_start=0x35744, link_create=0x357F2, link_task=0x35AA8, start_cloud=0x2C204,
                             lan_publish=0x2BF7C, ctx_pointer=0x4044AC, cloud_flag=0x403E31, url_offset=0x5F,
                             dispatch_url="http://8.8.8.8:80")

    def hooks(self):
        regs = SimpleNamespace(**{name: index for index, name in enumerate(
            ("UC_ARM_REG_R0 UC_ARM_REG_R1 UC_ARM_REG_R2 UC_ARM_REG_R3 UC_ARM_REG_R4 UC_ARM_REG_R5 UC_ARM_REG_R6 "
             "UC_ARM_REG_R7 UC_ARM_REG_R8 UC_ARM_REG_R9 UC_ARM_REG_R10 UC_ARM_REG_R11 UC_ARM_REG_R12 UC_ARM_REG_SP "
             "UC_ARM_REG_LR UC_ARM_REG_PC UC_ARM_REG_CPSR").split())})
        fakes = {"src": SimpleNamespace(), "src.main": SimpleNamespace(build_emulator=None),
                 "unicorn": SimpleNamespace(UC_HOOK_CODE=4), "unicorn.arm_const": regs}
        with mock.patch.dict(sys.modules, fakes):
            sys.modules.pop("cloud_online", None)
            import cloud_online
        self.addCleanup(sys.modules.pop, "cloud_online", None)
        touched, installed = [], []
        mu = SimpleNamespace(
            hook_add=lambda kind, callback, user, begin, end: installed.append((begin, callback)),
            reg_read=lambda reg: 0x404000, reg_write=lambda reg, value: touched.append(("reg", reg, value)),
            mem_read=lambda address, size: (b"wifi-ssid\0" + bytes(size))[:size],
            mem_write=lambda address, data: touched.append(("mem", address)))
        state = SimpleNamespace(timer3_5_ctl=0)
        emu = SimpleNamespace(mu=mu, wifimac=SimpleNamespace(tick=lambda insns: 0), state=state, _exc_epoch=0)
        hooks = cloud_online.StockHooks(emu, b"net", b"password", b"CA", 0.0, [], self.POINTS)
        hooks.module = cloud_online
        return hooks, emu, installed, touched

    def test_nothing_is_touched_while_off(self):
        hooks, emu, installed, touched = self.hooks()
        hooks.active = False
        hooks._started = 0.0
        for address, callback in installed:
            callback(emu.mu, address, 2, None)
        emu.wifimac.tick(1)
        self.assertEqual(touched, [])
        self.assertEqual(emu.state.timer3_5_ctl, 0)

    def test_on_they_act(self):
        hooks, emu, installed, touched = self.hooks()
        gate = dict(installed)[0x29C3C]   # the integrity gate: its compare made "equal"
        gate(emu.mu, 0x29C3C, 2, None)
        self.assertIn(("reg", 0, 0), touched)
        emu.wifimac.tick(1)
        self.assertTrue(emu.state.timer3_5_ctl & (1 << 9))

    def test_every_place_the_help_goes_is_a_point_given(self):
        _, _, installed, _ = self.hooks()
        places = {begin for begin, _ in installed} - {0x4}   # the return from a forced call, in no firmware
        self.assertEqual(places, {self.POINTS.after_memcmp, self.POINTS.ef_get_env_blob, self.POINTS.x509_crt_parse,
                                  self.POINTS.ais_cloud_start, self.POINTS.appfw_loop})

    def test_the_trigger_publishes_the_lan_service_and_then_starts_the_cloud(self):
        hooks, emu, installed, touched = self.hooks()
        loop = dict(installed)[self.POINTS.appfw_loop]
        hooks._started = 0.0
        loop(emu.mu, self.POINTS.appfw_loop, 2, None)   # first it readies the context
        self.assertIn(("mem", self.POINTS.cloud_flag), touched)
        loop(emu.mu, self.POINTS.appfw_loop, 2, None)
        self.assertIn(("reg", 15, self.POINTS.lan_publish | 1), touched)
        dict(installed)[0x4](emu.mu, 0x4, 2, None)      # it returns
        self.assertIn(("reg", 15, self.POINTS.start_cloud | 1), touched)

    def test_nothing_goes_in_until_there_are_points(self):
        hooks, emu, installed, _ = self.hooks()
        before = len(installed)
        hooks.module.StockHooks(emu, b"net", b"password", b"CA", 0.0, [])
        self.assertEqual(len(installed), before)

    def test_a_firmware_dialling_elsewhere_is_refused(self):
        hooks, _, _, _ = self.hooks()
        self.assertIs(hooks.module.dialling_here(self.POINTS), self.POINTS)
        with self.assertRaisesRegex(ValueError, "dispatch_url.*http://192.0.2.1"):
            hooks.module.dialling_here(SimpleNamespace(dispatch_url="http://192.0.2.1:80"))


if __name__ == "__main__":
    unittest.main()
