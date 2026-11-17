"""This computer's firewall (tools/autoflash/firewall.py): the temporary rule is
removed however the tool exits."""

import types
import unittest
from unittest import mock

from tools.autoflash import firewall, hostsys, output, prompts


class FakeState:
    def __init__(self, rules):
        self.inputs = {"firewall_rules": [rule.to_json() for rule in rules]}

    def input(self, key):
        return self.inputs.get(key)

    def set_input(self, key, value):
        self.inputs[key] = value


class ReleaseFirewallTests(unittest.TestCase):
    RULE = hostsys.FirewallRule("ufw", "192.0.2.50", [8088, 8089])

    def ctx(self, rules=(RULE,)):
        return types.SimpleNamespace(state=FakeState(list(rules)))

    def test_a_recorded_rule_is_removed(self):
        ctx = self.ctx()
        with mock.patch.object(hostsys, "close_firewall", return_value=True) as close, \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            firewall.release_firewall(ctx)
        close.assert_called_once_with(self.RULE)
        self.assertEqual(firewall.recorded_rules(ctx), [])

    def test_nothing_is_asked_when_no_rule_was_added(self):
        with mock.patch.object(hostsys, "close_firewall") as close, mock.patch.object(output, "tell") as told:
            firewall.release_firewall(self.ctx(rules=()))
        close.assert_not_called()
        told.assert_not_called()

    def test_ctrl_c_at_the_password_prompt_says_how_to_remove_it(self):
        ctx = self.ctx()
        lines = []
        with mock.patch.object(hostsys, "close_firewall", side_effect=KeyboardInterrupt), \
             mock.patch.object(output, "tell", side_effect=lines.append):
            self.assertFalse(firewall.release_firewall(ctx))  # does not raise; a rule is left
        screen = "\n".join(lines)
        self.assertIn("was not removed (Ctrl-C); remove it with:", screen)
        self.assertIn("192.0.2.50", screen)
        self.assertEqual(firewall.recorded_rules(ctx), [self.RULE])  # still recorded for cleanup


class PortTests(unittest.TestCase):
    def test_the_image_and_the_phases_are_never_served_on_the_same_port(self):
        self.assertEqual(set(firewall.DELIVERY_PORTS) & set(firewall.PHASE_PORTS), set())


class QuitTests(unittest.TestCase):
    RULE = ReleaseFirewallTests.RULE

    def quit_screen(self, rules, removed=True):
        ctx = types.SimpleNamespace(state=FakeState(rules))
        lines = []
        with mock.patch.object(hostsys, "close_firewall", return_value=removed), \
             mock.patch.object(output, "tell", side_effect=lines.append), mock.patch.object(output, "say"), \
             mock.patch.object(prompts, "section"):
            firewall.say_quit(ctx)
        return lines

    QUIT = "Stopped as asked. Run this again to carry on."

    def test_quitting_ends_with_how_to_carry_on(self):
        for rules in ([], [self.RULE]):
            self.assertEqual(self.quit_screen(rules)[-1], self.QUIT)

    def test_quitting_claims_nothing_of_the_device(self):
        # A quit can leave the device part way - on the installer, after its
        # backup failed, say - so "nothing was left half done" was untrue.
        self.assertNotIn("half done", " ".join(self.quit_screen([])))

    def test_a_rule_left_behind_is_not_followed_by_the_quit_line(self):
        self.assertNotIn(self.QUIT, self.quit_screen([self.RULE], removed=False))


class EmulatorPasswordTests(unittest.TestCase):
    def test_the_emulator_password_is_given_only_inside_the_emulator(self):
        import os
        with mock.patch.dict(os.environ, {"AUTOFLASH_EMULATOR_SUDO_PASSWORD": "autoflash"}), \
             mock.patch.object(hostsys, "IS_WINDOWS", False):
            self.assertEqual(firewall.emulator_password_hint(),
                             ["In the emulator, the password is: autoflash"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTOFLASH_EMULATOR_SUDO_PASSWORD", None)
            self.assertEqual(firewall.emulator_password_hint(), [])

    def test_the_removal_note_gives_it_too(self):
        import os
        ctx = types.SimpleNamespace(state=FakeState([ReleaseFirewallTests.RULE]))
        lines = []
        with mock.patch.dict(os.environ, {"AUTOFLASH_EMULATOR_SUDO_PASSWORD": "autoflash"}), \
             mock.patch.object(hostsys, "IS_WINDOWS", False), \
             mock.patch.object(hostsys, "close_firewall", return_value=True), \
             mock.patch.object(output, "tell", side_effect=lines.append), mock.patch.object(output, "say"):
            firewall.close_firewall_rules(ctx)
        self.assertIn("In the emulator, the password is: autoflash", lines)


class FirewallOutcomeTests(unittest.TestCase):
    """How letting the device through went is kept for the run's report."""

    def outcome(self, *, active="ufw", ignored=False, opened=None, refused=False):
        state = FakeState([])
        state.inputs["device_ip"] = "192.0.2.50"
        ctx = types.SimpleNamespace(state=state)
        rule = opened or hostsys.FirewallRule("ufw", "192.0.2.50", [8088, 8089])
        with mock.patch.object(firewall, "firewall_ports", return_value=[8088, 8089]), \
                mock.patch.object(hostsys, "firewall_advice", return_value=[]), \
                mock.patch.object(hostsys, "local_rules_ignored", return_value=ignored), \
                mock.patch.object(hostsys, "active_firewall", return_value=active), \
                mock.patch.object(hostsys, "firewall_commands", return_value=(["add"], ["remove"])), \
                mock.patch.object(hostsys, "open_firewall", side_effect=OSError("no") if refused else None,
                                  return_value=rule), \
                mock.patch.object(prompts, "section"), mock.patch.object(output, "say"), \
                mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"):
            firewall.ensure_firewall(ctx)
        return state.input("firewall_outcome")

    def test_each_way_it_can_go_is_named(self):
        there = hostsys.FirewallRule("ufw", "192.0.2.50", [8088, 8089], {"preexisting": True})
        self.assertEqual([self.outcome(active=None), self.outcome(), self.outcome(opened=there),
                          self.outcome(refused=True), self.outcome(ignored=True)],
                         ["none", "added", "there", "failed", "managed"])

if __name__ == "__main__":
    unittest.main()

