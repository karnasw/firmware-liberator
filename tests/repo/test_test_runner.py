"""The unit tests' own runner (tests/run.py): a test that leaves the tool's
screen state set is reported, as it changes how the next test in the same
worker process draws."""

import unittest
from unittest import mock

from tools.autoflash import output

import run


class LeftBehindTests(unittest.TestCase):
    def test_a_screen_state_left_set_is_named_and_cleared(self):
        with mock.patch.multiple(output._s, after_blank=False, after_stage=False, waited=False):
            self.assertEqual(run.left_behind(), [])
            output._s.after_blank = True
            self.assertEqual(run.left_behind(), ["after_blank"])
            self.assertFalse(output._s.after_blank)   # the next test starts clean

    def test_every_state_named_is_one_the_screen_keeps(self):
        for name in run.SCREEN_STATE:
            self.assertIsInstance(getattr(output._s, name), bool, name)


if __name__ == "__main__":
    unittest.main()
