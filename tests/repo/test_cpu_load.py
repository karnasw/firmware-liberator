import unittest
from unittest import mock

from tools.repo import cpu_load


class OverloadTests(unittest.TestCase):
    def test_overloaded_is_well_above_one_task_a_core(self):
        # An emulator on each core keeps the load near one a core: no wait for that.
        for load, expected in ((6.0, False), (9.0, False), (9.5, True), (28.0, True)):
            with mock.patch.object(cpu_load.os, "getloadavg", return_value=(load, 0.0, 0.0)):
                self.assertIs(cpu_load.overloaded(cores=6), expected, load)

    def test_a_computer_that_keeps_no_load_is_never_held_back(self):
        with mock.patch.object(cpu_load.os, "getloadavg", side_effect=OSError):
            self.assertFalse(cpu_load.overloaded(cores=1))


class WaitTests(unittest.TestCase):
    def wait(self, loads):
        clock = [0.0]
        said = []
        found = iter(loads)
        last = [loads[-1]]

        def load():
            last[0] = next(found, last[0])
            return (last[0], 0.0, 0.0)

        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.object(cpu_load.os, "getloadavg", side_effect=load), \
                mock.patch.object(cpu_load.os, "cpu_count", return_value=6), \
                mock.patch.object(cpu_load.time, "sleep", side_effect=sleep), \
                mock.patch.object(cpu_load.time, "monotonic", side_effect=lambda: clock[0]):
            return cpu_load.wait_for_room(say=said.append), said

    def test_a_machine_with_room_starts_at_once_saying_nothing(self):
        self.assertEqual(self.wait([4.0]), (0.0, []))

    def test_an_overloaded_one_waits_for_the_load_to_fall(self):
        waited, said = self.wait([28.0, 28.0, 20.0, 12.0, 6.0])
        self.assertEqual(waited, 3 * cpu_load.POLL)
        self.assertEqual(len(said), 1)

    def test_load_that_never_falls_holds_a_scenario_back_only_so_long(self):
        waited, said = self.wait([28.0])
        self.assertEqual(waited, cpu_load.LONGEST)
        self.assertIn("starting the next scenario anyway", said[-1])


if __name__ == "__main__":
    unittest.main()
