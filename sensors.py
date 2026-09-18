#!/usr/bin/env python3
"""Streaming system telemetry for the Ostat Menus Omarchy bar widget.

Prints one JSON object per line on stdout, one per sampling tick, and never
exits on its own. Everything comes from /proc and /sys, so there are no
dependencies and no subprocesses: the widget pays for one Python process that
wakes on a timer, not for a fork per sample.

Two knobs arrive on stdin as bare lines, so the widget can throttle the cost
of the parts nobody is looking at:

    interval <seconds>   change the sampling period (0.25 .. 60)
    detail <0|1>         include the expensive fields (process table, mount
                         usage, per-core temperatures) or skip them

`detail 0` is the closed-panel mode. The bar itself only renders the cheap
aggregate fields, and walking every /proc/<pid> once a second to feed a panel
that is not on screen is the one genuinely wasteful thing this script could do.
"""

import errno
import json
import os
import select
import sys
import time

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
SECTOR_SIZE = 512
# Rows per process table. Panel.qml renders exactly this many.
TOP_PROCESSES = 5

# Filesystems that represent real storage the user cares about. Everything
# else in mountinfo is kernel bookkeeping (proc, sysfs, cgroup), a tmpfs that
# just mirrors RAM, or a container overlay.
REAL_FS = {
    "btrfs", "ext2", "ext3", "ext4", "xfs", "f2fs", "jfs", "reiserfs",
    "vfat", "exfat", "ntfs", "ntfs3", "zfs", "bcachefs", "nilfs2", "ubifs",
}

# Mount trees that are real filesystems but never interesting as "my disks".
SKIP_MOUNT_PREFIXES = ("/snap", "/var/lib/docker", "/var/lib/containers", "/run")

# Block devices that are not physical disks. zram is compressed RAM and
# already shows up under swap; counting its I/O as disk traffic is a lie.
SKIP_BLOCK_PREFIXES = ("loop", "ram", "zram", "dm-", "md", "sr")


def read_text(path, default=""):
    try:
        with open(path, "r") as handle:
            return handle.read()
    except OSError:
        return default


def read_int(path, default=None):
    raw = read_text(path).strip()
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------- static info

def cpu_model():
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    # ARM boards have no "model name"; fall back to the SoC compatible string.
    hardware = read_text("/sys/firmware/devicetree/base/model").strip("\x00").strip()
    return hardware or "CPU"


def host_info():
    return {
        "host": os.uname().nodename,
        "kernel": os.uname().release,
        "cpuModel": cpu_model(),
        "cpuCount": os.cpu_count() or 1,
    }


# ------------------------------------------------------------------- cpu

def read_cpu_times():
    """Return (total_jiffies, busy_jiffies) for the aggregate and each core."""
    stats = {}
    for line in read_text("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        parts = line.split()
        key = parts[0]
        values = [int(v) for v in parts[1:]]
        # user nice system idle iowait irq softirq steal guest guest_nice.
        # Idle time is idle + iowait; guest time is already counted inside
        # user/nice, so summing every field would double-count it.
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values[:8])
        stats[key] = (total, total - idle)
    return stats


def cpu_percentages(prev, cur):
    out = {}
    for key, (total, busy) in cur.items():
        if key not in prev:
            continue
        prev_total, prev_busy = prev[key]
        d_total = total - prev_total
        d_busy = busy - prev_busy
        out[key] = max(0.0, min(100.0, 100.0 * d_busy / d_total)) if d_total > 0 else 0.0
    return out


def cpu_freq_mhz():
    """Mean current core frequency, in MHz."""
    total = 0
    count = 0
    base = "/sys/devices/system/cpu"
    try:
        entries = os.listdir(base)
    except OSError:
        entries = []
    for entry in entries:
        if not entry.startswith("cpu") or not entry[3:].isdigit():
            continue
        khz = read_int(f"{base}/{entry}/cpufreq/scaling_cur_freq")
        if khz:
            total += khz
            count += 1
    if count:
        return round(total / count / 1000.0, 1)
    # No cpufreq driver (common in VMs): /proc/cpuinfo still reports a number.
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.lower().startswith("cpu mhz"):
            try:
                return round(float(line.split(":", 1)[1]), 1)
            except ValueError:
                pass
    return None


# ---------------------------------------------------------------- memory

def memory():
    fields = {}
    for line in read_text("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        try:
            fields[name] = int(rest.split()[0]) * 1024
        except (IndexError, ValueError):
            continue

    total = fields.get("MemTotal", 0)
    available = fields.get("MemAvailable", fields.get("MemFree", 0))
    free = fields.get("MemFree", 0)
    buffers = fields.get("Buffers", 0)
    # Reclaimable slab is cache in every sense that matters to a reader, and
    # leaving it out makes used+cache+free fall short of total by a GB or more.
    cached = fields.get("Cached", 0) + fields.get("SReclaimable", 0) - fields.get("Shmem", 0)
    cached = max(0, cached)
    swap_total = fields.get("SwapTotal", 0)
    swap_used = swap_total - fields.get("SwapFree", 0)

    return {
        "total": total,
        # "Used" here is the app-memory figure: what would not come back if
        # every cache were dropped. This is the number `free -h` calls used.
        "used": max(0, total - available),
        "available": available,
        "free": free,
        "buffers": buffers,
        "cached": cached,
        "swapTotal": swap_total,
        "swapUsed": max(0, swap_used),
    }


# ---------------------------------------------------------------- network

def default_iface():
    for line in read_text("/proc/net/route").splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000":
            return parts[0]
    return None


def net_counters():
    per_iface = {}
    for line in read_text("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not name or name == "lo":
            continue
        parts = rest.split()
        if len(parts) < 9:
            continue
        per_iface[name] = (int(parts[0]), int(parts[8]))
    return per_iface


# ---------------------------------------------------------------- disk

def disk_counters():
    """Aggregate sectors read/written across physical block devices."""
    read_sectors = 0
    write_sectors = 0
    for line in read_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 10:
            continue
        name = parts[2]
        if name.startswith(SKIP_BLOCK_PREFIXES):
            continue
        # Partitions duplicate their parent disk's traffic. Only whole disks
        # have a /sys/block entry of their own.
        if not os.path.isdir(f"/sys/block/{name}"):
            continue
        read_sectors += int(parts[5])
        write_sectors += int(parts[9])
    return read_sectors * SECTOR_SIZE, write_sectors * SECTOR_SIZE


def mounts():
    seen = set()
    seen_pools = set()
    out = []
    for line in read_text("/proc/self/mountinfo").splitlines():
        head, _, tail = line.partition(" - ")
        if not tail:
            continue
        head_parts = head.split()
        tail_parts = tail.split()
        if len(head_parts) < 5 or len(tail_parts) < 2:
            continue
        target = head_parts[4].replace("\\040", " ")
        fstype = tail_parts[0]
        source = tail_parts[1]
        if fstype not in REAL_FS:
            continue
        if target.startswith(SKIP_MOUNT_PREFIXES):
            continue
        if target in seen:
            continue
        try:
            st = os.statvfs(target)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue
        total = st.f_blocks * st.f_frsize
        # f_bavail excludes the root reserve, so used = total - available
        # matches what df prints rather than overstating free space.
        free = st.f_bavail * st.f_frsize
        # Btrfs subvolumes and bind mounts are separate mountinfo entries that
        # all report the same pool. Listing /, /home, /var/log and
        # /var/cache/pacman/pkg with four identical bars says nothing; keep the
        # shortest path per pool, which mountinfo order makes the first seen.
        pool = (source, total, free)
        if pool in seen_pools:
            continue
        seen_pools.add(pool)
        seen.add(target)
        out.append({
            "path": target,
            "device": source,
            "fstype": fstype,
            "total": total,
            "used": max(0, total - free),
            "free": free,
        })
    out.sort(key=lambda m: (len(m["path"]), m["path"]))
    return out[:6]


# ---------------------------------------------------------------- sensors

def hwmon_chips(base="/sys/class/hwmon"):
    chips = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return chips
    for entry in entries:
        path = f"{base}/{entry}"
        chips.append((read_text(f"{path}/name").strip(), path))
    return chips


def sensors(detail, hwmon_base="/sys/class/hwmon"):
    temps = []
    fans = []
    cpu_temp = None
    gpu_temp = None

    # Readings that can stand in for the package temperature when no chip
    # labels one. Kept separately from `temps` so the fallback also works
    # with detail off, when `temps` is not collected at all.
    cpu_candidates = []

    for name, path in hwmon_chips(hwmon_base):
        try:
            files = os.listdir(path)
        except OSError:
            continue

        for fname in sorted(files):
            if fname.startswith("temp") and fname.endswith("_input"):
                milli = read_int(f"{path}/{fname}")
                if milli is None:
                    continue
                value = round(milli / 1000.0, 1)
                # Some chips report an absurd value when the sensor is
                # unpopulated.
                if not (-40.0 < value < 150.0):
                    continue
                label = read_text(f"{path}/{fname[:-6]}_label").strip()
                # Others report a placeholder 0 instead. A ThinkPad exposes
                # eight ACPI temperature inputs and populates two; the other
                # six read exactly zero and carry no label, and printing them
                # both says nothing and costs six of the rows below. A chip
                # that names a sensor is reporting a real one, so a labelled
                # zero is believed — a machine really can sit at freezing.
                if value == 0.0 and not label:
                    continue
                label = label or name or fname
                is_package = label.lower().startswith(("package", "tctl", "tdie", "cpu"))
                if name in ("coretemp", "k10temp", "zenpower") and cpu_temp is None and is_package:
                    cpu_temp = value
                if name in ("amdgpu", "nouveau", "nvidia", "i915", "xe") and gpu_temp is None:
                    gpu_temp = value
                if name in ("coretemp", "k10temp", "zenpower", "acpitz"):
                    cpu_candidates.append(value)
                if detail:
                    temps.append({"chip": name, "label": label, "value": value})

            elif fname.startswith("fan") and fname.endswith("_input") and detail:
                rpm = read_int(f"{path}/{fname}")
                if rpm is None or rpm <= 0:
                    continue
                label = read_text(f"{path}/{fname[:-6]}_label").strip() or fname.split("_")[0]
                fans.append({"chip": name, "label": label, "value": rpm})

    # No labelled package sensor: fall back to the hottest core-ish reading so
    # the hero still has a temperature to show.
    if cpu_temp is None and cpu_candidates:
        cpu_temp = max(cpu_candidates)

    return {"cpuTemp": cpu_temp, "gpuTemp": gpu_temp, "temps": temps[:16], "fans": fans[:6]}


# ---------------------------------------------------------------- gpu

# A machine can hold more than one GPU, and on a laptop the interesting pair
# is usually a discrete card next to the integrated one that drives the
# display. Each vendor publishes its utilisation somewhere different:
#
#   amdgpu   gpu_busy_percent, straight out of sysfs
#   i915/xe  no busy counter at all; RC6 residency says how long the GPU sat
#            in its sleep state, and the rest of the tick is work
#   nvidia   nothing in sysfs under the proprietary driver; NVML is the only
#            source, reached through ctypes so there is still no subprocess
#
# Each probe object holds whatever state its counter needs between ticks and
# returns one reading per sample, or None when the device stops answering.

PCI_IDS_PATHS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids")

# Drivers whose cards are an integrated GPU. Everything else sorts ahead of
# them, so gpus[0] is the discrete card on a hybrid-graphics laptop.
INTEGRATED_DRIVERS = ("i915", "xe")


def drm_cards(base="/sys/class/drm"):
    """Every real DRM device, without the connectors that share the prefix."""
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    cards = []
    for entry in entries:
        # card1-DP-1 and friends are connectors, not the device itself.
        if not entry.startswith("card") or "-" in entry:
            continue
        cards.append((entry, f"{base}/{entry}/device"))
    return cards


def driver_name(device):
    try:
        return os.path.basename(os.readlink(f"{device}/driver"))
    except OSError:
        return ""


def pci_device_name(vendor, device, path=None):
    """Marketing name for a PCI id, from the hwdata database if it is present.

    The kernel never spells out "UHD Graphics 630" for an i915 card, and a
    panel row reading "Intel 8086:3e9b" helps nobody. The file is a couple of
    megabytes, so the scan stops at the end of the vendor's block rather than
    reading the rest.
    """
    candidates = (path,) if path else PCI_IDS_PATHS
    vendor = vendor.lower()
    device = device.lower()
    for candidate in candidates:
        try:
            with open(candidate, "r", errors="replace") as handle:
                in_vendor = False
                for line in handle:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        # A vendor row. Reaching the next one means the
                        # device is not listed under ours.
                        if in_vendor:
                            return ""
                        in_vendor = line.split()[0].lower() == vendor
                        continue
                    if not in_vendor:
                        continue
                    # One tab is a device; two is a subsystem, which is a
                    # different id space and must not match.
                    if line.startswith("\t\t"):
                        continue
                    fields = line.strip().split(None, 1)
                    if len(fields) == 2 and fields[0].lower() == device:
                        return fields[1].strip()
        except OSError:
            continue
    return ""


def pci_name(device, fallback):
    vendor = read_text(f"{device}/vendor").strip().replace("0x", "")
    ident = read_text(f"{device}/device").strip().replace("0x", "")
    if vendor and ident:
        name = pci_device_name(vendor, ident)
        if name:
            return name
    return fallback


def reading(name, vendor, busy, integrated,
            vram_used=0, vram_total=0, power=None, temp=None,
            freq=None, freq_max=None):
    """The one shape every probe returns, so the panel can render them alike."""
    return {
        "name": name,
        "vendor": vendor,
        "busy": round(float(busy), 1),
        "vramUsed": int(vram_used),
        "vramTotal": int(vram_total),
        "power": power,
        "temp": temp,
        "freq": freq,
        "freqMax": freq_max,
        "integrated": bool(integrated),
    }


class AmdGpu(object):
    """An amdgpu card, which publishes everything we need in sysfs."""

    vendor = "amd"
    integrated = False

    def __init__(self, device, name):
        self.device = device
        self.name = name

    @staticmethod
    def counter_path(device):
        path = f"{device}/gpu_busy_percent"
        return path if os.path.exists(path) else None

    def sample(self, elapsed):
        busy = read_int(f"{self.device}/gpu_busy_percent")
        if busy is None:
            return None
        power = None
        temp = None
        hwmon_dir = f"{self.device}/hwmon"
        try:
            for entry in sorted(os.listdir(hwmon_dir)):
                chip = f"{hwmon_dir}/{entry}"
                micro_watts = read_int(f"{chip}/power1_average") or read_int(f"{chip}/power1_input")
                if micro_watts is not None:
                    power = round(micro_watts / 1_000_000.0, 1)
                milli = read_int(f"{chip}/temp1_input")
                if milli is not None:
                    temp = round(milli / 1000.0, 1)
                break
        except OSError:
            pass
        return reading(
            self.name, self.vendor, busy, self.integrated,
            vram_used=read_int(f"{self.device}/mem_info_vram_used", 0) or 0,
            vram_total=read_int(f"{self.device}/mem_info_vram_total", 0) or 0,
            power=power,
            temp=temp,
            freq=(read_int(f"{self.device}/pp_dpm_sclk") or None),
        )


class IntelGpu(object):
    """An i915 or xe card, measured through RC6 residency.

    The render engine parks in RC6 whenever it has nothing to do, and the
    kernel counts the milliseconds it spends there. Time not spent asleep is
    time spent working, so the share of the tick outside RC6 is the same
    number `intel_gpu_top` and `btop` report as utilisation, without needing
    the perf permissions the i915 PMU asks for.
    """

    vendor = "intel"
    integrated = True

    def __init__(self, device, card, name):
        self.device = device
        self.name = name
        self.residency = self.residency_path(device, card)
        self.freq_paths = self.frequency_paths(device, card)
        self.previous = read_int(self.residency) if self.residency else None

    @staticmethod
    def residency_path(device, card):
        candidates = (
            # Modern kernels expose one directory per graphics tile.
            f"{device}/drm/{card}/gt/gt0/rc6_residency_ms",
            f"{device}/drm/{card}/power/rc6_residency_ms",
        )
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def frequency_paths(device, card):
        tile = f"{device}/drm/{card}/gt/gt0"
        if os.path.exists(f"{tile}/rps_act_freq_mhz"):
            return (f"{tile}/rps_act_freq_mhz", f"{tile}/rps_RP0_freq_mhz")
        legacy = os.path.dirname(os.path.dirname(device))
        return (f"{legacy}/{card}/gt_act_freq_mhz", f"{legacy}/{card}/gt_RP0_freq_mhz")

    def sample(self, elapsed):
        if not self.residency:
            return None
        current = read_int(self.residency)
        if current is None:
            return None
        previous = self.previous
        self.previous = current
        if previous is None:
            return None
        asleep_ms = current - previous
        if asleep_ms < 0:
            # The counter restarted, most likely across a suspend. One tick of
            # nonsense is worse than one tick of nothing.
            return None
        window_ms = max(1.0, elapsed * 1000.0)
        busy = 100.0 * (1.0 - asleep_ms / window_ms)
        # Residency is sampled a moment after the clock, so a fully idle GPU
        # can report a hair more sleep than the tick was long.
        busy = max(0.0, min(100.0, busy))
        return reading(
            self.name, self.vendor, busy, self.integrated,
            freq=read_int(self.freq_paths[0]),
            freq_max=read_int(self.freq_paths[1]),
        )


class Nvml(object):
    """The NVIDIA management library, bound directly with ctypes.

    The proprietary driver puts no utilisation counter in sysfs and registers
    no hwmon chip, so NVML is the only way to read one. Loading the library
    keeps the widget's promise of a single process: this is a function call,
    not a fork of `nvidia-smi` every tick.
    """

    TEMPERATURE_GPU = 0
    CLOCK_GRAPHICS = 0

    def __init__(self, library):
        self.library = library
        self.handles = []

    @classmethod
    def load(cls):
        try:
            import ctypes
        except ImportError:
            return None
        for soname in ("libnvidia-ml.so.1", "libnvidia-ml.so"):
            try:
                library = ctypes.CDLL(soname)
            except OSError:
                continue
            if library.nvmlInit_v2() != 0:
                continue
            instance = cls(library)
            if instance.open_devices():
                return instance
            instance.shutdown()
        return None

    def open_devices(self):
        import ctypes
        count = ctypes.c_uint()
        if self.library.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
            return False
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if self.library.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) == 0:
                self.handles.append(handle)
        return bool(self.handles)

    def device_count(self):
        return len(self.handles)

    def sample(self, index):
        import ctypes

        class Utilisation(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        class Memory(ctypes.Structure):
            _fields_ = [("total", ctypes.c_ulonglong),
                        ("free", ctypes.c_ulonglong),
                        ("used", ctypes.c_ulonglong)]

        handle = self.handles[index]
        used = Utilisation()
        if self.library.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(used)) != 0:
            return None

        name = ctypes.create_string_buffer(96)
        if self.library.nvmlDeviceGetName(handle, name, ctypes.c_uint(96)) == 0:
            label = name.value.decode("utf-8", "replace").strip() or "NVIDIA GPU"
        else:
            label = "NVIDIA GPU"

        memory = Memory()
        vram_used = vram_total = 0
        if self.library.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) == 0:
            vram_used = memory.used
            vram_total = memory.total

        celsius = ctypes.c_uint()
        temp = None
        if self.library.nvmlDeviceGetTemperature(
                handle, self.TEMPERATURE_GPU, ctypes.byref(celsius)) == 0:
            temp = float(celsius.value)

        milli_watts = ctypes.c_uint()
        power = None
        if self.library.nvmlDeviceGetPowerUsage(handle, ctypes.byref(milli_watts)) == 0:
            power = round(milli_watts.value / 1000.0, 1)

        megahertz = ctypes.c_uint()
        freq = None
        if self.library.nvmlDeviceGetClockInfo(
                handle, self.CLOCK_GRAPHICS, ctypes.byref(megahertz)) == 0:
            freq = megahertz.value

        return {
            "name": label,
            "busy": used.gpu,
            "vramUsed": vram_used,
            "vramTotal": vram_total,
            "power": power,
            "temp": temp,
            "freq": freq,
            "freqMax": None,
        }

    def shutdown(self):
        try:
            self.library.nvmlShutdown()
        except Exception:
            pass


class NvidiaGpu(object):
    vendor = "nvidia"
    integrated = False

    def __init__(self, nvml, index):
        self.nvml = nvml
        self.index = index

    def sample(self, elapsed):
        try:
            values = self.nvml.sample(self.index)
        except Exception:
            return None
        if not values:
            return None
        return reading(
            values["name"], self.vendor, values["busy"], self.integrated,
            vram_used=values["vramUsed"], vram_total=values["vramTotal"],
            power=values["power"], temp=values["temp"],
            freq=values["freq"], freq_max=values["freqMax"],
        )


def discover_gpus(base="/sys/class/drm", nvml=None):
    """Every GPU this machine can report on, discrete cards first."""
    discrete = []
    integrated = []

    if nvml is not None:
        for index in range(nvml.device_count()):
            discrete.append(NvidiaGpu(nvml, index))

    for card, device in drm_cards(base):
        driver = driver_name(device)
        # NVML already covers these, and nouveau publishes no counter at all.
        if driver in ("nvidia", "nouveau"):
            continue
        if driver in INTEGRATED_DRIVERS or (not driver and IntelGpu.residency_path(device, card)):
            if IntelGpu.residency_path(device, card):
                integrated.append(IntelGpu(device, card, pci_name(device, "Integrated Graphics")))
            continue
        if AmdGpu.counter_path(device):
            discrete.append(AmdGpu(device, pci_name(device, "GPU")))

    return discrete + integrated


def sample_gpus(probes, elapsed):
    readings = []
    for probe in probes:
        try:
            value = probe.sample(elapsed)
        except Exception:
            value = None
        if value is not None:
            readings.append(value)
    return readings


def primary_gpu(readings):
    """The card the bar cell and the legacy `gpu` field refer to."""
    return readings[0] if readings else None

# ---------------------------------------------------------------- processes

def process_table(prev_times, elapsed, ncpu):
    """Top processes by CPU and by RSS.

    CPU percentage is per-core, the same scale top and btop use, so a fully
    busy thread reads 100% no matter how many cores the machine has.
    """
    cur_times = {}
    rows = []
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return {}, [], []

    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        # comm sits in parens and may itself contain spaces or parens, so the
        # only safe split point is the LAST ')'.
        close = raw.rfind(b")")
        open_paren = raw.find(b"(")
        if close < 0 or open_paren < 0:
            continue
        name = raw[open_paren + 1:close].decode("utf-8", "replace")
        fields = raw[close + 2:].split()
        if len(fields) < 20:
            continue
        try:
            utime = int(fields[11])
            stime = int(fields[12])
            start_time = int(fields[19])
        except ValueError:
            continue

        key = (pid, start_time)
        ticks = utime + stime
        cur_times[key] = ticks

        cpu_pct = 0.0
        if key in prev_times and elapsed > 0:
            delta = ticks - prev_times[key]
            if delta > 0:
                cpu_pct = 100.0 * delta / (elapsed * CLK_TCK)

        statm = read_text(f"/proc/{pid}/statm").split()
        rss = int(statm[1]) * PAGE_SIZE if len(statm) > 1 and statm[1].isdigit() else 0

        rows.append({
            "pid": int(pid),
            "name": name,
            "cpu": round(min(cpu_pct, 100.0 * ncpu), 1),
            "mem": rss,
        })

    by_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:TOP_PROCESSES]
    by_mem = sorted(rows, key=lambda r: r["mem"], reverse=True)[:TOP_PROCESSES]
    return cur_times, by_cpu, by_mem


def uptime_seconds():
    parts = read_text("/proc/uptime").split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return 0.0


# ---------------------------------------------------------------- main loop

def clamp_interval(value):
    try:
        return max(0.25, min(60.0, float(value)))
    except (TypeError, ValueError):
        return None


def drain_stdin(state):
    """Apply every complete command line currently readable on stdin.

    Returns False when the pipe has closed, so the caller can stop polling a
    descriptor that will be permanently ready.
    """
    try:
        chunk = os.read(0, 4096)
    except (BlockingIOError, InterruptedError):
        return True
    except OSError:
        return False
    if not chunk:
        return False

    state["buffer"] += chunk
    while b"\n" in state["buffer"]:
        line, _, state["buffer"] = state["buffer"].partition(b"\n")
        handle_command(line.decode("utf-8", "replace"), state)
    return True


def handle_command(line, state):
    parts = line.strip().split()
    if len(parts) != 2:
        return
    key, value = parts
    if key == "interval":
        interval = clamp_interval(value)
        if interval is not None:
            state["interval"] = interval
            state["wake"] = True
    elif key == "detail":
        was = state["detail"]
        state["detail"] = value not in ("0", "false", "off", "no")
        if state["detail"] != was:
            state["wake"] = True


def main():
    detail_default = "--detail" in sys.argv
    once = "--once" in sys.argv
    interval = 2.0
    for index, arg in enumerate(sys.argv):
        if arg == "--interval" and index + 1 < len(sys.argv):
            interval = clamp_interval(sys.argv[index + 1]) or interval

    state = {"interval": interval, "detail": detail_default or once, "stdinOpen": True, "wake": False, "buffer": b""}
    static = host_info()
    ncpu = static["cpuCount"]
    # NVML is opened once and kept for the life of the process; the NVIDIA
    # probes below hold handles into it.
    gpu_probes = discover_gpus(nvml=Nvml.load())

    prev_cpu = read_cpu_times()
    prev_net = net_counters()
    prev_disk = disk_counters()
    prev_proc = {}
    prev_time = time.monotonic()

    # The first tick has no previous sample to diff against, so every rate
    # would read zero. Take a short first nap instead of the full interval so
    # the panel fills in quickly rather than sitting blank.
    time.sleep(min(0.35, state["interval"]))

    while True:
        now = time.monotonic()
        elapsed = max(1e-6, now - prev_time)
        prev_time = now
        detail = state["detail"]

        cur_cpu = read_cpu_times()
        pct = cpu_percentages(prev_cpu, cur_cpu)
        prev_cpu = cur_cpu
        cores = []
        index = 0
        while f"cpu{index}" in pct:
            cores.append(round(pct[f"cpu{index}"], 1))
            index += 1

        cur_net = net_counters()
        iface = default_iface()
        if iface not in cur_net:
            # No default route (or it points at a tunnel with no counters):
            # fall back to whichever interface has moved the most bytes.
            iface = max(cur_net, key=lambda k: cur_net[k][0] + cur_net[k][1], default=None)
        rx_rate = tx_rate = 0.0
        rx_total = tx_total = 0
        if iface and iface in cur_net:
            rx_total, tx_total = cur_net[iface]
            if iface in prev_net:
                prev_rx, prev_tx = prev_net[iface]
                # Counters reset when an interface goes down and back up;
                # max(0, ...) keeps that from rendering as a negative rate.
                rx_rate = max(0.0, (rx_total - prev_rx) / elapsed)
                tx_rate = max(0.0, (tx_total - prev_tx) / elapsed)
        prev_net = cur_net

        cur_disk = disk_counters()
        read_rate = max(0.0, (cur_disk[0] - prev_disk[0]) / elapsed)
        write_rate = max(0.0, (cur_disk[1] - prev_disk[1]) / elapsed)
        prev_disk = cur_disk

        gpu_readings = sample_gpus(gpu_probes, elapsed)

        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            load1 = load5 = load15 = 0.0

        payload = {
            "t": time.time(),
            "detail": detail,
            "host": static["host"],
            "kernel": static["kernel"],
            "cpuModel": static["cpuModel"],
            "cpuCount": ncpu,
            "uptime": uptime_seconds(),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
            "cpu": round(pct.get("cpu", 0.0), 1),
            "cores": cores,
            "freq": cpu_freq_mhz(),
            "mem": memory(),
            "net": {
                "iface": iface or "",
                "rx": round(rx_rate),
                "tx": round(tx_rate),
                "rxTotal": rx_total,
                "txTotal": tx_total,
            },
            "disk": {"read": round(read_rate), "write": round(write_rate)},
            "gpus": gpu_readings,
            # Kept so a panel built against the single-GPU payload keeps
            # rendering: it is simply the first card.
            "gpu": primary_gpu(gpu_readings),
        }
        payload.update(sensors(detail))

        # The proprietary NVIDIA driver registers no hwmon chip, so the
        # sensor sweep finds no GPU temperature on those machines. The card
        # reports its own, so fall back to that rather than showing none.
        if payload.get("gpuTemp") is None:
            for value in gpu_readings:
                if value["temp"] is not None:
                    payload["gpuTemp"] = value["temp"]
                    break

        if detail:
            prev_proc, by_cpu, by_mem = process_table(prev_proc, elapsed, ncpu)
            payload["procCpu"] = by_cpu
            payload["procMem"] = by_mem
            payload["mounts"] = mounts()
        else:
            # Drop the accumulated per-pid tick counts: they would be stale
            # whenever detail comes back on, producing one tick of nonsense.
            prev_proc = {}

        try:
            sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except OSError as exc:
            if exc.errno == errno.EPIPE:
                # The reader went away. Point stdout at /dev/null before
                # returning, or the interpreter's exit-time flush trips over
                # the same broken pipe and prints a traceback nobody asked for.
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                return 0
            raise

        if once:
            return 0

        # select() doubles as the sleep and as the stdin command channel, so a
        # request to change interval or detail takes effect on the next tick
        # instead of after the current sleep would have ended.
        deadline = time.monotonic() + state["interval"]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not state["stdinOpen"]:
                time.sleep(remaining)
                break
            ready, _, _ = select.select([0], [], [], remaining)
            if not ready:
                break
            if not drain_stdin(state):
                # EOF. A caller that never wired up stdin (a shell pipeline, a
                # host that leaves it closed) is not asking us to quit — it
                # just has no commands to send. Keep sampling, but stop
                # select()ing on a descriptor that is permanently ready.
                state["stdinOpen"] = False
                continue
            if state.pop("wake", False):
                # A panel that just opened should not wait out the idle
                # interval before its first detailed sample. Seed the per-pid
                # tick counts now and emit after a short delay, so the first
                # process table already carries real percentages instead of a
                # column of zeros. Every other counter is re-snapshotted with
                # it: moving prev_time alone would divide up to a whole idle
                # interval of network and disk bytes by 0.35 s, and that spike
                # then owns the auto-scaled graphs for the next minute.
                if state["detail"] and not prev_proc:
                    prev_proc, _, _ = process_table({}, 1.0, ncpu)
                    prev_cpu = read_cpu_times()
                    prev_net = net_counters()
                    prev_disk = disk_counters()
                    prev_time = time.monotonic()
                deadline = time.monotonic() + 0.35
            else:
                deadline = min(deadline, time.monotonic() + state["interval"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
