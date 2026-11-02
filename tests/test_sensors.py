#!/usr/bin/env python3
"""Unit tests for sensors.py.

Stdlib only, and every test drives the parsing functions through fixtures
rather than the live machine, so they pass on a box with no sensors at all:

    python3 -m unittest discover -s tests -t .

    python3 tests/test_sensors.py

Every case builds a synthetic /sys/class/hwmon tree, or answers the /proc
reads from a fixture string, so the suite does not care what chips the
machine running it happens to have.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

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


def patch_text(files):
    """Answer /proc and /sys reads from `files` instead of the real system."""
    def read(path, default=""):
        return files.get(path, default)
    return mock.patch.object(sensors, "read_text", read)


# ------------------------------------------------------------------ cpu

class CpuTimesTest(unittest.TestCase):
    def test_guest_fields_are_not_double_counted(self):
        stat = "cpu  100 20 30 800 50 1 2 3 40 40\n" \
               "cpu0 100 20 30 800 50 1 2 3 40 40\n"
        with patch_text({"/proc/stat": stat}):
            got = sensors.read_cpu_times()
        total, busy = got["cpu"]
        # first eight fields: 100+20+30+800+50+1+2+3
        self.assertEqual(total, 1006)
        # idle + iowait are not busy
        self.assertEqual(busy, 1006 - 850)
        self.assertIn("cpu0", got)

    def test_truncated_line_does_not_raise(self):
        # Used to raise IndexError out of main(), restart-looping the widget.
        with patch_text({"/proc/stat": "cpu  1 2 3\n"}):
            self.assertEqual(sensors.read_cpu_times(), {})

    def test_non_numeric_line_does_not_raise(self):
        with patch_text({"/proc/stat": "cpu 1 x 3 4\n"}):
            self.assertEqual(sensors.read_cpu_times(), {})

    def test_missing_proc_stat(self):
        with patch_text({}):
            self.assertEqual(sensors.read_cpu_times(), {})

    def test_percentages(self):
        prev = {"cpu": (1000, 500)}
        self.assertEqual(sensors.cpu_percentages(prev, {"cpu": (2000, 1500)})["cpu"], 100.0)
        # no time elapsed between the samples
        self.assertEqual(sensors.cpu_percentages(prev, prev)["cpu"], 0.0)
        # a counter that went backwards must not read negative
        self.assertEqual(sensors.cpu_percentages({"cpu": (2000, 1500)}, prev)["cpu"], 0.0)


# -------------------------------------------------------------- network

NETDEV = (
    "Inter-|   Receive                          |  Transmit\n"
    " face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n"
    "    lo: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
    "wlan0: 1000 1 0 0 0 0 0 0 2000 2 0 0 0 0 0 0\n"
    "eth0: 1 2 3\n"
    "veth0: x 1 0 0 0 0 0 0 5 1 0 0 0 0 0 0\n"
)


class NetCountersTest(unittest.TestCase):
    def test_reads_rx_and_tx_skips_lo_and_junk(self):
        with patch_text({"/proc/net/dev": NETDEV}):
            self.assertEqual(sensors.net_counters(), {"wlan0": (1000, 2000)})

    def test_default_iface(self):
        route = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
                 "wlp0s20f3\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000\n")
        with patch_text({"/proc/net/route": route}):
            self.assertEqual(sensors.default_iface(), "wlp0s20f3")

    def test_default_iface_without_route(self):
        with patch_text({}):
            self.assertIsNone(sensors.default_iface())


# ----------------------------------------------------------------- disk

DISKSTATS = (
    "259 0 nvme0n1 10 0 8 0 20 0 16 0 0 0 0\n"
    "259 1 nvme0n1p1 10 0 8 0 20 0 16 0 0 0 0\n"
    "252 0 zram0 1 0 1 0 1 0 1 0 0 0 0\n"
    "7 0 loop0 1 0 1 0 1 0 1 0 0 0 0\n"
    "8 16 sdz 1 0 x 0 1 0 1 0 0 0 0\n"
)


class DiskCountersTest(unittest.TestCase):
    def test_whole_disks_only_and_junk_lines_skipped(self):
        def isdir(path):
            return path in ("/sys/block/nvme0n1", "/sys/block/sdz")
        with patch_text({"/proc/diskstats": DISKSTATS}), \
                mock.patch.object(sensors.os.path, "isdir", isdir):
            read, written = sensors.disk_counters()
        self.assertEqual(read, 8 * sensors.SECTOR_SIZE)
        self.assertEqual(written, 16 * sensors.SECTOR_SIZE)

    def test_missing_diskstats(self):
        with patch_text({}):
            self.assertEqual(sensors.disk_counters(), (0, 0))


# --------------------------------------------------------------- mounts

SIZES = {
    "/": (1000, 4096, 500),
    "/runtime": (3000, 4096, 1000),
    "/run/media/u/USB": (2000, 4096, 1000),
    "/run/user/1000": (10, 4096, 5),
    "/mnt": (500, 4096, 100),
    "/mnt/a/long": (500, 4096, 100),
}


def statvfs(path):
    if path not in SIZES:
        raise OSError(2, "ENOENT")
    blocks, frsize, bavail = SIZES[path]
    return SimpleNamespace(f_blocks=blocks, f_frsize=frsize, f_bavail=bavail)


def mount_line(mid, target, fstype, source):
    return f"{mid} 2 0:{mid} 1 /{target.lstrip('/')} {target} rw - {fstype} {source} rw\n"


class MountsTest(unittest.TestCase):
    def read_mounts(self, lines):
        with patch_text({"/proc/self/mountinfo": "".join(lines)}), \
                mock.patch.object(sensors.os, "statvfs", statvfs):
            return sensors.mounts()

    def test_udisks_media_is_not_hidden_by_the_run_prefix(self):
        got = self.read_mounts([
            mount_line(33, "/", "btrfs", "/dev/mapper/root"),
            mount_line(40, "/run/media/u/USB", "vfat", "/dev/sdb1"),
        ])
        self.assertEqual([m["path"] for m in got], ["/", "/run/media/u/USB"])

    def test_run_bookkeeping_and_prefix_boundary(self):
        got = self.read_mounts([
            mount_line(33, "/", "btrfs", "/dev/mapper/root"),
            mount_line(41, "/run/user/1000", "ext4", "/dev/sdc1"),
            mount_line(42, "/runtime", "ext4", "/dev/sdd1"),
        ])
        # "/runtime" only shares a prefix with "/run"; it is not under it.
        self.assertEqual([m["path"] for m in got], ["/", "/runtime"])

    def test_skipped_mount_helper(self):
        self.assertTrue(sensors.skipped_mount("/snap/core/1"))
        self.assertTrue(sensors.skipped_mount("/var/lib/docker/overlay2"))
        self.assertTrue(sensors.skipped_mount("/run"))
        self.assertTrue(sensors.skipped_mount("/run/user/1000"))
        self.assertFalse(sensors.skipped_mount("/run/media/u/USB"))
        self.assertFalse(sensors.skipped_mount("/runtime"))

    def test_shortest_path_wins_whatever_the_order(self):
        long_first = [
            mount_line(50, "/mnt/a/long", "ext4", "/dev/sde1"),
            mount_line(51, "/mnt", "ext4", "/dev/sde1"),
        ]
        for lines in (long_first, list(reversed(long_first))):
            got = self.read_mounts(lines)
            self.assertEqual([m["path"] for m in got], ["/mnt"])

    def test_unreachable_mount_is_skipped(self):
        got = self.read_mounts([
            mount_line(33, "/", "btrfs", "/dev/mapper/root"),
            mount_line(60, "/gone", "ext4", "/dev/sdf1"),
        ])
        self.assertEqual([m["path"] for m in got], ["/"])

    def test_used_is_total_minus_available(self):
        got = self.read_mounts([mount_line(33, "/", "btrfs", "/dev/mapper/root")])
        self.assertEqual(got[0]["total"], 1000 * 4096)
        self.assertEqual(got[0]["free"], 500 * 4096)
        self.assertEqual(got[0]["used"], 500 * 4096)


# --------------------------------------------------------------- memory

MEMINFO = (
    "MemTotal:       1000 kB\n"
    "MemFree:         100 kB\n"
    "MemAvailable:    400 kB\n"
    "Buffers:          10 kB\n"
    "Cached:          300 kB\n"
    "SReclaimable:    50 kB\n"
    "Shmem:            20 kB\n"
    "SwapTotal:       500 kB\n"
    "SwapFree:        400 kB\n"
)


class MemoryTest(unittest.TestCase):
    def test_three_bands_partition_the_total(self):
        with patch_text({"/proc/meminfo": MEMINFO}):
            mem = sensors.memory()
        self.assertEqual(mem["total"], 1000 * 1024)
        self.assertEqual(mem["used"], 600 * 1024)
        self.assertEqual(mem["available"], 400 * 1024)
        self.assertEqual(mem["cached"], 330 * 1024)
        self.assertEqual(mem["swapUsed"], 100 * 1024)
        # what the panel meters: used + reclaimable + free <= total
        reclaimable = mem["available"] - mem["free"]
        self.assertLessEqual(mem["used"] + reclaimable + mem["free"], mem["total"])

    def test_available_falls_back_to_memfree(self):
        with patch_text({"/proc/meminfo": "MemTotal: 100 kB\nMemFree: 40 kB\n"}):
            mem = sensors.memory()
        self.assertEqual(mem["available"], 40 * 1024)
        self.assertEqual(mem["used"], 60 * 1024)

    def test_missing_meminfo_is_all_zero(self):
        with patch_text({}):
            self.assertEqual(sensors.memory()["total"], 0)


# -------------------------------------------------------------- sensors

class HwmonTest(unittest.TestCase):
    """Drives sensors() against a throwaway hwmon tree instead of /sys."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, path, name, text):
        with open(os.path.join(path, name), "w") as handle:
            handle.write(text + "\n")

    def chip(self, name, temps, labels=(), fans=()):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(path)
        self.write(path, "name", name)
        for index, milli in enumerate(temps, 1):
            self.write(path, f"temp{index}_input", str(milli))
            if index <= len(labels) and labels[index - 1]:
                self.write(path, f"temp{index}_label", labels[index - 1])
        for index, rpm in enumerate(fans, 1):
            self.write(path, f"fan{index}_input", str(rpm))
        return name, path

    def run_sensors(self, chips, detail):
        with mock.patch.object(sensors, "hwmon_chips", lambda base="/sys/class/hwmon": chips):
            return sensors.sensors(detail)

    def test_detail_off_reads_only_cpu_and_gpu_chips(self):
        slow = self.chip("dell_smm", [51000, 46000], fans=[4800])
        fast = self.chip("coretemp", [65000, 54000], ["Package id 0", "Core 0"])
        reads = []
        original = sensors.read_text

        def counting(path, default=""):
            reads.append(path)
            return original(path, default)

        with mock.patch.object(sensors, "hwmon_chips", lambda base="/sys/class/hwmon": [slow, fast]), \
                mock.patch.object(sensors, "read_text", counting):
            out = sensors.sensors(False)
        self.assertEqual(out["cpuTemp"], 65.0)
        self.assertEqual(out["cpuTempFrom"], "package")
        self.assertEqual(out["temps"], [])
        self.assertEqual(out["fans"], [])
        # The 15 ms ACPI chip is not touched at all.
        self.assertEqual([path for path in reads if "dell_smm" in path], [])

    def test_detail_on_reads_the_whole_list(self):
        slow = self.chip("dell_smm", [51000, 46000], fans=[4800])
        fast = self.chip("coretemp", [65000], ["Package id 0"])
        out = self.run_sensors([slow, fast], True)
        self.assertEqual(len(out["temps"]), 3)
        self.assertEqual([f["value"] for f in out["fans"]], [4800])

    def test_unlabelled_chips_get_distinct_labels(self):
        slow = self.chip("dell_smm", [51000, 46000, 42000])
        out = self.run_sensors([slow], True)
        labels = [reading["label"] for reading in out["temps"]]
        self.assertEqual(labels, ["dell_smm 1", "dell_smm 2", "dell_smm 3"])

    def test_fallback_temperature_is_marked_max(self):
        zones = self.chip("acpitz", [76000, 61000])
        out = self.run_sensors([zones], False)
        self.assertEqual(out["cpuTemp"], 76.0)
        self.assertEqual(out["cpuTempFrom"], "max")

    def test_out_of_range_and_missing_readings_are_dropped(self):
        chip = self.chip("coretemp", [-50000, 200000, 65000], ["Package id 0", "Core 0", ""])
        self.write(chip[1], "temp4_input", "not a number")
        out = self.run_sensors([chip], True)
        self.assertEqual([reading["value"] for reading in out["temps"]], [65.0])

    def test_no_hwmon_at_all(self):
        with mock.patch.object(sensors, "hwmon_chips", lambda base="/sys/class/hwmon": []):
            out = sensors.sensors(True)
        self.assertIsNone(out["cpuTemp"])
        self.assertEqual(out["temps"], [])


# ------------------------------------------------------------ primitives

class ReadTextTest(unittest.TestCase):
    def test_undecodable_bytes_return_the_default(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"\xff\xfe\x00garbage")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(sensors.read_text(path, "fallback"), "fallback")

    def test_missing_file_returns_the_default(self):
        self.assertEqual(sensors.read_text("/nonexistent/ostat", "fallback"), "fallback")

    def test_normal_file_is_returned(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("65000\n")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(sensors.read_text(path).strip(), "65000")


class ControlTest(unittest.TestCase):
    def test_clamp_interval(self):
        self.assertEqual(sensors.clamp_interval("0.1"), 0.25)
        self.assertEqual(sensors.clamp_interval("120"), 60.0)
        self.assertEqual(sensors.clamp_interval("2"), 2.0)
        self.assertIsNone(sensors.clamp_interval("nope"))

    def test_handle_command(self):
        state = {"interval": 2.0, "detail": False, "wake": False}
        sensors.handle_command("interval 3.5", state)
        self.assertEqual(state["interval"], 3.5)
        self.assertTrue(state["wake"])
        state["wake"] = False
        sensors.handle_command("detail 1", state)
        self.assertTrue(state["detail"])
        self.assertTrue(state["wake"])
        state["wake"] = False
        sensors.handle_command("detail 1", state)
        self.assertFalse(state["wake"])          # unchanged: no early sample
        sensors.handle_command("nonsense", state)
        sensors.handle_command("detail", state)
        self.assertTrue(state["detail"])


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
    unittest.main()
