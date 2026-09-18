#!/usr/bin/env python3
"""hwmon temperature and fan tests for sensors.py.

Each case builds a synthetic /sys/class/hwmon tree, so the suite does not
care what chips the machine running it happens to have.

    python3 tests/test_sensors.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sensors  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(str(text))


def make_chip(base, index, name, temps=(), fans=()):
    """One hwmon chip. `temps` is a list of (label, millidegrees)."""
    path = f"{base}/hwmon{index}"
    write(f"{path}/name", name)
    for number, (label, milli) in enumerate(temps, start=1):
        write(f"{path}/temp{number}_input", milli)
        if label is not None:
            write(f"{path}/temp{number}_label", label)
    for number, rpm in enumerate(fans, start=1):
        write(f"{path}/fan{number}_input", rpm)
    return path


# A ThinkPad P1: the thinkpad ACPI chip exposes eight temperature inputs and
# populates two of them. The rest read a placeholder zero and carry no label.
THINKPAD_TEMPS = [("CPU", 59000), ("GPU", 52000)] + [(None, 0)] * 6

CORETEMP_TEMPS = [
    ("Package id 0", 60000),
    ("Core 0", 57000), ("Core 1", 61000), ("Core 2", 55000),
    ("Core 3", 56000), ("Core 4", 56000), ("Core 5", 56000),
]


class PlaceholderTest(unittest.TestCase):
    """An unpopulated sensor reads zero; it is not a reading of zero."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def read(self):
        return sensors.sensors(True, self.base)

    def test_unlabelled_zeros_are_dropped(self):
        make_chip(self.base, 0, "thinkpad", THINKPAD_TEMPS)
        labels = [t["label"] for t in self.read()["temps"]]
        self.assertEqual(labels, ["CPU", "GPU"])

    def test_the_chip_name_is_not_repeated_once_per_empty_sensor(self):
        make_chip(self.base, 0, "thinkpad", THINKPAD_TEMPS)
        temps = self.read()["temps"]
        self.assertEqual(sum(1 for t in temps if t["label"] == "thinkpad"), 0)

    def test_an_unlabelled_sensor_with_a_real_reading_survives(self):
        # acpitz and the chipset sensor label nothing and still mean something.
        make_chip(self.base, 0, "acpitz", [(None, 59000)])
        make_chip(self.base, 1, "pch_cannonlake", [(None, 61000)])
        temps = self.read()["temps"]
        self.assertEqual([t["value"] for t in temps], [59.0, 61.0])
        self.assertEqual([t["label"] for t in temps], ["acpitz", "pch_cannonlake"])

    def test_a_labelled_zero_is_believed(self):
        # A chip that names a sensor is reporting a real one, and a machine
        # really can sit at freezing.
        make_chip(self.base, 0, "outdoor", [("Ambient", 0)])
        self.assertEqual([t["value"] for t in self.read()["temps"]], [0.0])

    def test_absurd_readings_are_still_rejected(self):
        make_chip(self.base, 0, "flaky", [("Hot", 200000), ("Cold", -60000)])
        self.assertEqual(self.read()["temps"], [])


class CapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_a_laptops_full_sensor_set_fits(self):
        # The machine this was found on: 13 real sensors once the six
        # placeholders are gone.
        make_chip(self.base, 0, "acpitz", [(None, 59000)])
        make_chip(self.base, 1, "coretemp", CORETEMP_TEMPS)
        make_chip(self.base, 2, "iwlwifi_1", [(None, 52000)])
        make_chip(self.base, 3, "nvme", [("Composite", 46850)])
        make_chip(self.base, 4, "pch_cannonlake", [(None, 61000)])
        make_chip(self.base, 5, "thinkpad", THINKPAD_TEMPS, fans=(2475, 2159))
        temps = sensors.sensors(True, self.base)["temps"]
        self.assertEqual(len(temps), 13)
        # The per-core readings are the ones the placeholders used to displace.
        self.assertIn("Core 5", [t["label"] for t in temps])
        self.assertIn("Package id 0", [t["label"] for t in temps])

    def test_a_machine_with_many_sensors_is_still_bounded(self):
        make_chip(self.base, 0, "huge", [("S%d" % i, 40000 + i) for i in range(40)])
        self.assertEqual(len(sensors.sensors(True, self.base)["temps"]), 16)

    def test_fans_stay_bounded(self):
        make_chip(self.base, 0, "noisy", [], fans=tuple(range(1000, 1012)))
        self.assertEqual(len(sensors.sensors(True, self.base)["fans"]), 6)

    def test_a_stopped_fan_is_not_listed(self):
        make_chip(self.base, 0, "thinkpad", [], fans=(2475, 0))
        self.assertEqual(len(sensors.sensors(True, self.base)["fans"]), 1)


class HeroTemperatureTest(unittest.TestCase):
    """cpuTemp and gpuTemp drive the hero readout and the bar's temp cell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_the_package_sensor_is_preferred(self):
        make_chip(self.base, 0, "coretemp", CORETEMP_TEMPS)
        self.assertEqual(sensors.sensors(True, self.base)["cpuTemp"], 60.0)

    def test_an_unlabelled_chip_falls_back_to_its_hottest_core(self):
        make_chip(self.base, 0, "coretemp", [(None, 55000), (None, 61000)])
        self.assertEqual(sensors.sensors(True, self.base)["cpuTemp"], 61.0)

    def test_placeholder_zeros_cannot_become_the_cpu_temperature(self):
        make_chip(self.base, 0, "acpitz", [(None, 0)])
        make_chip(self.base, 1, "coretemp", [("Package id 0", 60000)])
        self.assertEqual(sensors.sensors(True, self.base)["cpuTemp"], 60.0)

    def test_a_gpu_chip_supplies_the_gpu_temperature(self):
        make_chip(self.base, 0, "amdgpu", [("edge", 61000)])
        self.assertEqual(sensors.sensors(True, self.base)["gpuTemp"], 61.0)

    def test_detail_off_still_reports_the_hero_figures(self):
        make_chip(self.base, 0, "coretemp", CORETEMP_TEMPS)
        reading = sensors.sensors(False, self.base)
        self.assertEqual(reading["cpuTemp"], 60.0)
        self.assertEqual(reading["temps"], [])

    def test_no_chips_at_all(self):
        reading = sensors.sensors(True, f"{self.base}/absent")
        self.assertIsNone(reading["cpuTemp"])
        self.assertEqual(reading["temps"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
