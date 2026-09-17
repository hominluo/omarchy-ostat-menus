#!/usr/bin/env python3
"""GPU discovery and sampling tests for sensors.py.

Every case builds a synthetic /sys tree in a temp directory, so the suite
runs the same way on a machine with no GPU, one GPU, or four.

    python3 tests/test_gpu.py
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


def make_amd_card(base, card="card0", busy=42, vram_total=8589934592, vram_used=1073741824):
    """An amdgpu card as the kernel presents one."""
    device = f"{base}/{card}/device"
    write(f"{device}/gpu_busy_percent", busy)
    write(f"{device}/mem_info_vram_total", vram_total)
    write(f"{device}/mem_info_vram_used", vram_used)
    write(f"{device}/vendor", "0x1002")
    write(f"{device}/device", "0x73df")
    hwmon = f"{device}/hwmon/hwmon3"
    write(f"{hwmon}/name", "amdgpu")
    write(f"{hwmon}/power1_average", 35_000_000)
    write(f"{hwmon}/temp1_input", 61_000)
    os.makedirs(f"{device}/amdgpu", exist_ok=True)
    os.symlink(f"{device}/amdgpu", f"{device}/driver")
    return device


def make_intel_card(base, card="card1", rc6=1000, act=450, rp0=1150):
    """An i915 card with the modern per-GT sysfs layout."""
    device = f"{base}/{card}/device"
    write(f"{device}/vendor", "0x8086")
    write(f"{device}/device", "0x3e9b")
    gt0 = f"{device}/drm/{card}/gt/gt0"
    write(f"{gt0}/rc6_residency_ms", rc6)
    write(f"{gt0}/rps_act_freq_mhz", act)
    write(f"{gt0}/rps_RP0_freq_mhz", rp0)
    os.makedirs(f"{device}/i915", exist_ok=True)
    os.symlink(f"{device}/i915", f"{device}/driver")
    return device


def make_legacy_intel_card(base, card="card1", rc6=1000, act=450, rp0=1150):
    """An i915 card on a kernel that predates the per-GT directories."""
    device = f"{base}/{card}/device"
    write(f"{device}/vendor", "0x8086")
    write(f"{device}/device", "0x3e9b")
    write(f"{device}/drm/{card}/power/rc6_residency_ms", rc6)
    write(f"{base}/{card}/gt_act_freq_mhz", act)
    write(f"{base}/{card}/gt_RP0_freq_mhz", rp0)
    os.makedirs(f"{device}/i915", exist_ok=True)
    os.symlink(f"{device}/i915", f"{device}/driver")
    return device


class FakeNvml(object):
    """Stands in for the NVML shared library."""

    def __init__(self, devices):
        self.devices = devices
        self.shutdown_calls = 0

    def device_count(self):
        return len(self.devices)

    def sample(self, index):
        return dict(self.devices[index])

    def shutdown(self):
        self.shutdown_calls += 1


NVIDIA_SAMPLE = {
    "name": "Quadro T1000",
    "busy": 17,
    "vramUsed": 392167424,
    "vramTotal": 4294967296,
    "power": 12.5,
    "temp": 53.0,
    "freq": 1395,
    "freqMax": 1455,
}


class DrmScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_connectors_are_not_cards(self):
        make_amd_card(self.base, "card0")
        os.makedirs(f"{self.base}/card0-DP-1", exist_ok=True)
        os.makedirs(f"{self.base}/card0-eDP-1", exist_ok=True)
        self.assertEqual([name for name, _ in sensors.drm_cards(self.base)], ["card0"])

    def test_missing_base_is_not_an_error(self):
        self.assertEqual(sensors.drm_cards(f"{self.base}/absent"), [])

    def test_driver_name_reads_the_symlink(self):
        device = make_intel_card(self.base, "card1")
        self.assertEqual(sensors.driver_name(device), "i915")

    def test_driver_name_without_a_symlink(self):
        self.assertEqual(sensors.driver_name(f"{self.base}/nothing"), "")


class AmdGpuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_reports_utilisation_vram_power_and_temperature(self):
        device = make_amd_card(self.base, "card0")
        reading = sensors.AmdGpu(device, "Radeon").sample(1.0)
        self.assertEqual(reading["busy"], 42)
        self.assertEqual(reading["vramTotal"], 8589934592)
        self.assertEqual(reading["vramUsed"], 1073741824)
        self.assertEqual(reading["power"], 35.0)
        self.assertEqual(reading["temp"], 61.0)
        self.assertFalse(reading["integrated"])

    def test_missing_counter_yields_no_reading(self):
        device = make_amd_card(self.base, "card0")
        os.remove(f"{device}/gpu_busy_percent")
        self.assertIsNone(sensors.AmdGpu(device, "Radeon").sample(1.0))


class IntelGpuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_busy_is_the_share_of_the_tick_spent_out_of_rc6(self):
        device = make_intel_card(self.base, "card1", rc6=1000)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        # 400 ms of a 1 s tick in RC6 leaves 600 ms awake.
        write(f"{device}/drm/card1/gt/gt0/rc6_residency_ms", 1400)
        reading = probe.sample(1.0)
        self.assertAlmostEqual(reading["busy"], 60.0, places=1)

    def test_fully_idle_reads_zero(self):
        device = make_intel_card(self.base, "card1", rc6=1000)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        write(f"{device}/drm/card1/gt/gt0/rc6_residency_ms", 3000)
        self.assertAlmostEqual(probe.sample(2.0)["busy"], 0.0, places=1)

    def test_busy_is_clamped_when_the_counter_outruns_the_clock(self):
        device = make_intel_card(self.base, "card1", rc6=1000)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        write(f"{device}/drm/card1/gt/gt0/rc6_residency_ms", 9999)
        self.assertEqual(probe.sample(1.0)["busy"], 0.0)

    def test_counter_reset_is_skipped_rather_than_reported_backwards(self):
        device = make_intel_card(self.base, "card1", rc6=5000)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        write(f"{device}/drm/card1/gt/gt0/rc6_residency_ms", 10)
        self.assertIsNone(probe.sample(1.0))
        # The reset is absorbed, and the next tick measures normally again.
        write(f"{device}/drm/card1/gt/gt0/rc6_residency_ms", 510)
        self.assertAlmostEqual(probe.sample(1.0)["busy"], 50.0, places=1)

    def test_first_tick_has_no_previous_sample(self):
        device = make_intel_card(self.base, "card1", rc6=1000)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        # Same counter value: nothing ran, so the reading is a real zero.
        self.assertIsNotNone(probe.sample(1.0))

    def test_reports_frequency_and_no_vram(self):
        device = make_intel_card(self.base, "card1", act=450, rp0=1150)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        write(f"{device}/drm/card1/gt/gt0/rc6_residency_ms", 1500)
        reading = probe.sample(1.0)
        self.assertEqual(reading["freq"], 450)
        self.assertEqual(reading["freqMax"], 1150)
        self.assertEqual(reading["vramTotal"], 0)
        self.assertIsNone(reading["power"])
        self.assertTrue(reading["integrated"])

    def test_legacy_sysfs_layout_still_reads(self):
        device = make_legacy_intel_card(self.base, "card1", rc6=1000)
        probe = sensors.IntelGpu(device, "card1", "Intel")
        write(f"{device}/drm/card1/power/rc6_residency_ms", 1250)
        reading = probe.sample(1.0)
        self.assertAlmostEqual(reading["busy"], 75.0, places=1)
        self.assertEqual(reading["freq"], 450)

    def test_no_residency_counter_yields_no_probe(self):
        device = make_intel_card(self.base, "card1")
        os.remove(f"{device}/drm/card1/gt/gt0/rc6_residency_ms")
        self.assertIsNone(sensors.IntelGpu.residency_path(device, "card1"))


class NvidiaGpuTest(unittest.TestCase):
    def test_reading_passes_through_with_the_vendor_marked(self):
        nvml = FakeNvml([NVIDIA_SAMPLE])
        reading = sensors.NvidiaGpu(nvml, 0).sample(1.0)
        self.assertEqual(reading["name"], "Quadro T1000")
        self.assertEqual(reading["busy"], 17)
        self.assertEqual(reading["vendor"], "nvidia")
        self.assertFalse(reading["integrated"])
        self.assertEqual(reading["temp"], 53.0)

    def test_a_failed_read_yields_no_reading(self):
        nvml = FakeNvml([None])
        self.assertIsNone(sensors.NvidiaGpu(nvml, 0).sample(1.0))


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_discrete_cards_sort_ahead_of_integrated_ones(self):
        make_intel_card(self.base, "card0")
        probes = sensors.discover_gpus(self.base, FakeNvml([NVIDIA_SAMPLE]))
        readings = [probe.sample(1.0) for probe in probes]
        self.assertEqual([r["vendor"] for r in readings], ["nvidia", "intel"])

    def test_amd_and_intel_together(self):
        make_amd_card(self.base, "card0")
        make_intel_card(self.base, "card1")
        probes = sensors.discover_gpus(self.base, None)
        readings = [probe.sample(1.0) for probe in probes]
        self.assertEqual([r["vendor"] for r in readings], ["amd", "intel"])

    def test_nvidia_cards_are_left_to_nvml(self):
        device = f"{self.base}/card0/device"
        write(f"{device}/vendor", "0x10de")
        os.makedirs(f"{device}/nvidia", exist_ok=True)
        os.symlink(f"{device}/nvidia", f"{device}/driver")
        # One NVML device, and the DRM scan must not add a second entry for it.
        probes = sensors.discover_gpus(self.base, FakeNvml([NVIDIA_SAMPLE]))
        self.assertEqual(len(probes), 1)

    def test_a_machine_with_no_gpu_discovers_nothing(self):
        self.assertEqual(sensors.discover_gpus(self.base, None), [])

    def test_sample_all_drops_probes_that_read_nothing(self):
        make_amd_card(self.base, "card0")
        os.remove(f"{self.base}/card0/device/gpu_busy_percent")
        make_intel_card(self.base, "card1")
        probes = sensors.discover_gpus(self.base, None)
        self.assertEqual(len(sensors.sample_gpus(probes, 1.0)), 1)


class PciNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "pci.ids")
        self.addCleanup(self.tmp.cleanup)
        write(self.path, "\n".join([
            "# comment",
            "8086  Intel Corporation",
            "\t3e9b  UHD Graphics 630",
            "\t\t1028 0905  Precision 5540",
            "\t3e92  HD Graphics 630",
            "10de  NVIDIA Corporation",
            "\t1fb9  TU117GLM [Quadro T1000 Mobile]",
            "",
        ]))

    def test_finds_the_device_under_its_vendor(self):
        self.assertEqual(sensors.pci_device_name("8086", "3e9b", self.path),
                         "UHD Graphics 630")

    def test_subsystem_rows_are_not_devices(self):
        self.assertEqual(sensors.pci_device_name("8086", "0905", self.path), "")

    def test_a_device_id_only_matches_within_its_own_vendor(self):
        self.assertEqual(sensors.pci_device_name("10de", "3e9b", self.path), "")

    def test_missing_database_is_not_an_error(self):
        self.assertEqual(sensors.pci_device_name("8086", "3e9b", "/nonexistent"), "")


class PayloadShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_gpu_is_the_first_of_gpus(self):
        make_amd_card(self.base, "card0")
        make_intel_card(self.base, "card1")
        readings = sensors.sample_gpus(sensors.discover_gpus(self.base, None), 1.0)
        self.assertEqual(len(readings), 2)
        self.assertIs(sensors.primary_gpu(readings), readings[0])

    def test_no_gpu_leaves_the_legacy_field_null(self):
        self.assertIsNone(sensors.primary_gpu([]))

    def test_every_reading_carries_the_full_key_set(self):
        make_amd_card(self.base, "card0")
        make_intel_card(self.base, "card1")
        expected = {"name", "vendor", "busy", "vramUsed", "vramTotal",
                    "power", "temp", "freq", "freqMax", "integrated"}
        for reading in sensors.sample_gpus(sensors.discover_gpus(self.base, None), 1.0):
            self.assertEqual(set(reading), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
