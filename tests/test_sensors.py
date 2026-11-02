"""Unit tests for sensors.py.

Stdlib only, and every test drives the parsing functions through fixtures
rather than the live machine, so they pass on a box with no sensors:

    python3 -m unittest discover -s tests -t .
"""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import sensors


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
        with mock.patch.object(sensors, "hwmon_chips", lambda: chips):
            return sensors.sensors(detail)

    def test_detail_off_reads_only_cpu_and_gpu_chips(self):
        slow = self.chip("dell_smm", [51000, 46000], fans=[4800])
        fast = self.chip("coretemp", [65000, 54000], ["Package id 0", "Core 0"])
        reads = []
        original = sensors.read_text

        def counting(path, default=""):
            reads.append(path)
            return original(path, default)

        with mock.patch.object(sensors, "hwmon_chips", lambda: [slow, fast]), \
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
        with mock.patch.object(sensors, "hwmon_chips", lambda: []):
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


if __name__ == "__main__":
    unittest.main()
