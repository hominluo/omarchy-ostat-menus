# Ostat Menus — system monitor for the Omarchy bar

A live resource readout in the bar, and a dense panel behind it: CPU history
and per-core load, memory composition, the top of the process table, network
and disk throughput, GPU utilisation, and every temperature the machine
reports. Everything is read straight from `/proc` and `/sys` — no dependencies,
no network, no subprocess per sample.

```bash
omarchy plugin add https://github.com/hominluo/omarchy-ostat-menus.git --enable
omarchy bar move io.github.hominluo.ostat-menus --after omarchy.tray
```

## The bar

Each configured metric gets a cell. Percentages (`cpu`, `mem`, `gpu`) render as
an icon, an optional micro-sparkline, and a number; throughput (`net`, `disk`)
renders as a two-line down/up stack, which is the only way both directions fit
in a 26px bar.

| Interaction | Result |
|---|---|
| left click | open the panel |
| right click | jump straight to `btop` |
| hover | CPU/memory tooltip |

Inside the panel: `c` / `m` switch the process table between CPU and memory
order, `t` (or Enter) opens `btop`, arrows and `j`/`k` scroll, `Tab` moves to
the next bar panel, `Esc` closes.

## Settings

Set these on the widget's entry in `~/.config/omarchy/shell.json`, or with
`omarchy bar set io.github.hominluo.ostat-menus <key> <value>`.

| Key | Default | What it does |
|---|---|---|
| `items` | `["cpu","mem","net"]` | which metrics the bar shows, in order: `cpu`, `mem`, `net`, `disk`, `gpu`, `temp` |
| `graph` | `true` | draw the micro-sparklines in the bar |
| `interval` | `2` | seconds between samples while the panel is closed |
| `activeInterval` | `1` | seconds between samples while the panel is open |
| `historySize` | `60` | samples kept, i.e. how far the graphs reach back |
| `monitorCommand` | `omarchy-launch-or-focus-tui btop` | what the footer button and right-click run |

`omarchy bar set` writes JSON with `--json`, which is how `items` gets set:

```bash
omarchy bar set io.github.hominluo.ostat-menus items '["cpu","mem","gpu","net","disk"]' --json
omarchy bar set io.github.hominluo.ostat-menus graph false --json
```

## How it samples

`sensors.py` runs for the life of the widget and prints one JSON object per
tick on stdout. Everything comes from `/proc` and `/sys` — no `psutil`, no
`lm_sensors`, no subprocess per sample. The panel writes back on the same pipe:

```
interval <seconds>    change the sampling period
detail <0|1>          include the process table, mount usage and the full
                      temperature list, or skip them
```

`detail` is off while the panel is closed, so walking every `/proc/<pid>` only
happens when someone is looking at the result — that walk is the expensive part
of a tick (about 14ms against 1ms for everything else on a 300-process
machine), which is the whole reason for the switch.

Both settings go out in a single write. Two `Process.write()` calls in one
event-loop turn do not both reach the child, and a batched write can arrive as
one chunk, so the sampler reads its own lines off the file descriptor rather
than through a buffered `readline`. Run it by hand to see the shape
of a sample:

```bash
python3 ~/.config/omarchy/plugins/io.github.hominluo.ostat-menus/sensors.py --once | jq
```

## What it can and cannot read

- **CPU, memory, swap, load, network, disk I/O, filesystem usage** — everywhere.
- **Temperatures and fans** — whatever `hwmon` exposes. A machine with no
  sensor drivers loaded simply has no SENSORS section.
- **GPU** — utilisation, VRAM, temperature and power come from the amdgpu
  sysfs interface. Intel and NVIDIA do not expose an equivalent
  `gpu_busy_percent`, so on those machines the GRAPHICS section hides itself
  and only the GPU temperature (if any) shows up under SENSORS.
- **Btrfs subvolumes and bind mounts** collapse to one row per pool. Listing
  `/`, `/home` and `/var/log` with three identical bars says nothing.

## Notes

- The bar instantiates one widget per monitor, so a multi-monitor setup runs
  one `sensors.py` per screen. At the default 2s idle interval that is a
  rounding error, but it is why the sampler is careful about what it does per
  tick.
- Process CPU is per-core, the scale `top` and `btop` use: one fully busy
  thread reads 100% regardless of core count.
- Memory "used" is `MemTotal - MemAvailable` — the figure `free -h` calls used,
  not the resident-set sum, which double-counts shared pages. The three bands
  partition the total exactly: used, the reclaimable part of what is available,
  and the untouched remainder. `/proc/meminfo`'s own `Cached` is not used for
  the middle band because it already sits inside `MemAvailable`.
