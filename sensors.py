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

def hwmon_chips():
    chips = []
    base = "/sys/class/hwmon"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return chips
    for entry in entries:
        path = f"{base}/{entry}"
        chips.append((read_text(f"{path}/name").strip(), path))
    return chips


def sensors(detail):
    temps = []
    fans = []
    cpu_temp = None
    gpu_temp = None

    # Readings that can stand in for the package temperature when no chip
    # labels one. Kept separately from `temps` so the fallback also works
    # with detail off, when `temps` is not collected at all.
    cpu_candidates = []

    for name, path in hwmon_chips():
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
                # Some chips report a placeholder 0 or an absurd value when the
                # sensor is unpopulated; neither is worth showing.
                if not (-40.0 < value < 150.0):
                    continue
                label = read_text(f"{path}/{fname[:-6]}_label").strip() or name or fname
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

    return {"cpuTemp": cpu_temp, "gpuTemp": gpu_temp, "temps": temps[:12], "fans": fans[:6]}


# ---------------------------------------------------------------- gpu

def gpu_card():
    """First DRM card that exposes a utilisation counter (amdgpu today)."""
    base = "/sys/class/drm"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for entry in entries:
        # card1-DP-1 and friends are connectors, not the device itself.
        if not entry.startswith("card") or "-" in entry:
            continue
        device = f"{base}/{entry}/device"
        if os.path.exists(f"{device}/gpu_busy_percent"):
            return device
    return None


def gpu(device):
    if not device:
        return None
    busy = read_int(f"{device}/gpu_busy_percent")
    if busy is None:
        return None
    vram_total = read_int(f"{device}/mem_info_vram_total", 0) or 0
    vram_used = read_int(f"{device}/mem_info_vram_used", 0) or 0
    power = None
    hwmon_dir = f"{device}/hwmon"
    name = "GPU"
    try:
        for entry in os.listdir(hwmon_dir):
            chip = f"{hwmon_dir}/{entry}"
            name = read_text(f"{chip}/name").strip() or name
            micro_watts = read_int(f"{chip}/power1_average") or read_int(f"{chip}/power1_input")
            if micro_watts is not None:
                power = round(micro_watts / 1_000_000.0, 1)
            break
    except OSError:
        pass
    return {
        "name": name,
        "busy": busy,
        "vramUsed": vram_used,
        "vramTotal": vram_total,
        "power": power,
    }


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
    gpu_device = gpu_card()

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
            "gpu": gpu(gpu_device),
        }
        payload.update(sensors(detail))

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
