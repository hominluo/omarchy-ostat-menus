<h1 align="center">Ostat Menus</h1>

<p align="center">
  A system monitor for the <a href="https://omarchy.org">Omarchy</a> bar —
  live readout up top, the whole machine one click away.
<br>
  <sub>by <a href="https://x.com/hominluo">@hominluo</a></sub>
</p>

<p align="center">
  <img src="docs/bar.png" alt="The bar readout: CPU, memory and network" height="26">
</p>

<p align="center">
  <img src="preview.png" alt="The bar readout with the Ostat Menus panel dropped down beneath it" width="400">
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#the-bar">The bar</a> ·
  <a href="#settings">Settings</a> ·
  <a href="#how-it-samples">How it samples</a> ·
  <a href="#what-it-can-read">What it can read</a>
</p>

---

CPU history and per-core load, memory composition, the top of the process
table, network and disk throughput, every GPU's utilisation, and every
temperature the machine reports — in one panel that follows your Omarchy theme.

Everything comes from `/proc` and `/sys`. No `psutil`, no `lm_sensors`, no
network, and no subprocess per sample: one small Python process streams the
numbers for the life of the widget.

## Install

```bash
omarchy plugin add https://github.com/hominluo/omarchy-ostat-menus.git --enable
omarchy bar move io.github.hominluo.ostat-menus --after omarchy.tray
```

Requires Omarchy with the Quickshell bar, and `python3` (already on every Arch
install). Nothing else.

To remove it:

```bash
omarchy plugin remove io.github.hominluo.ostat-menus
```

## The bar

Each configured metric gets a cell. Percentages (`cpu`, `mem`, `gpu`, `gpu2`)
render as an icon, an optional micro-sparkline and a number; throughput
(`net`, `disk`) renders as a two-line down/up stack, which is the only way both
directions fit in a 26px bar.

`gpu` is the first card and `gpu2` the second, which on a hybrid-graphics
laptop means the discrete GPU and then the integrated one. A cell for a card
the machine does not have takes up no room.

| Interaction | Result |
|---|---|
| left click | open the panel |
| right click | jump straight to `btop` |
| hover | CPU / memory tooltip |

Inside the panel:

| Key | Result |
|---|---|
| `c` / `m` | sort the process table by CPU or by memory |
| `t` / `Enter` | open `btop` |
| `↑` `↓` / `j` `k` | scroll |
| `Tab` | move to the next bar panel |
| `Esc` | close |

## Settings

Set these on the widget's entry in `~/.config/omarchy/shell.json`, or with
`omarchy bar set`:

| Key | Default | What it does |
|---|---|---|
| `items` | `["cpu","mem","net"]` | which metrics the bar shows, in order: `cpu`, `mem`, `net`, `disk`, `gpu`, `gpu2`, `temp` |
| `graph` | `true` | draw the micro-sparklines in the bar |
| `interval` | `2` | seconds between samples while the panel is closed |
| `activeInterval` | `1` | seconds between samples while the panel is open |
| `historySize` | `60` | samples kept, i.e. how far the graphs reach back |
| `monitorCommand` | `omarchy-launch-or-focus-tui btop` | what the footer button and right-click run |

Non-string values need `--json`:

```bash
omarchy bar set io.github.hominluo.ostat-menus graph false --json
omarchy bar set io.github.hominluo.ostat-menus interval 5 --json
omarchy bar set io.github.hominluo.ostat-menus monitorCommand "alacritty -e htop"
```

`items` has to be edited in `shell.json` by hand: Quickshell's IPC client
(`qs ipc call`, as of 0.3.1) splits every argument on commas, so a JSON array
never reaches the shell in one piece and `omarchy bar set … --json` fails with
"Too many arguments provided". The entry looks like this:

```json
{ "id": "io.github.hominluo.ostat-menus", "items": ["cpu", "mem", "gpu", "net", "disk"] }
```

The shell picks the change up on save. Settings changed while the widget is
running reach the sampler immediately; the graphs keep their history.

## How it samples

`sensors.py` runs for the life of the widget and prints one JSON object per
tick on stdout. The panel writes back on the same pipe:

```
interval <seconds>    change the sampling period
detail <0|1>          include the process table, mount usage and the full
                      temperature list, or skip them
```

`detail` is off while the panel is closed, so walking every `/proc/<pid>` only
happens when someone is looking at the result. That walk is the expensive part
of a tick — about 14ms against 1ms for everything else on a 300-process
machine — which is the whole reason for the switch.

Run it by hand to see the shape of a sample:

```bash
python3 sensors.py --once | jq
```

Two implementation notes worth knowing if you fork this:

- **Both settings go out in a single write.** Two `Process.write()` calls in one
  event-loop turn do not both reach the child; the second is silently dropped.
- **The sampler reads its own lines off the file descriptor.** A batched write
  can arrive as one chunk, and `select()` on a buffered `readline` then reports
  "nothing to read" while a complete second line sits in Python's buffer.

## What it can read

- **CPU, memory, swap, load, network, disk I/O, filesystem usage** — everywhere.
- **Temperatures and fans** — whatever `hwmon` exposes, up to 16 temperatures
  and 6 fans. A machine with no sensor drivers loaded simply has no SENSORS
  section. Sensors a chip exposes but does not populate are skipped: a ThinkPad
  offers eight ACPI temperature inputs and fills two, and the rest read exactly
  zero with no label. A *labelled* zero is shown, because a chip that names a
  sensor is reporting a real one.
- **GPU** — every card the machine has, discrete first. Each vendor keeps its
  utilisation somewhere different:

  | Vendor | Source | What you get |
  |---|---|---|
  | AMD | `gpu_busy_percent` in sysfs | utilisation, VRAM, temperature, power |
  | NVIDIA | NVML, called through `ctypes` | utilisation, VRAM, temperature, power, clock |
  | Intel | RC6 residency in sysfs | utilisation, clock |

  The proprietary NVIDIA driver publishes no utilisation counter in sysfs and
  registers no `hwmon` chip, so NVML is the only source; it is loaded as a
  shared library, which keeps the promise of one process and no forks. A
  machine without the driver installed simply has no NVIDIA section.

  Intel's render engine parks in RC6 whenever it is idle and the kernel counts
  the milliseconds it spends there, so the share of each tick spent outside
  RC6 is the utilisation figure. It matches what `btop` reports. An integrated
  GPU has no VRAM of its own, and neither a temperature nor a power reading, so
  those rows give way to its clock.

A few numbers are computed rather than copied, and the definitions matter:

- **Process CPU is per-core**, the scale `top` and `btop` use: one fully busy
  thread reads 100% regardless of core count.
- **Memory "used" is `MemTotal - MemAvailable`** — the figure `free -h` calls
  used, not the resident-set sum, which double-counts shared pages. The three
  bands partition the total exactly: used, the reclaimable part of what is
  available, and the untouched remainder. `/proc/meminfo`'s own `Cached` is not
  used for the middle band because it already sits inside `MemAvailable`.
  "Free" is therefore `MemFree`, which on a warm machine is nearly nothing —
  the number you actually want is on the **Available** row.
- **Btrfs subvolumes and bind mounts collapse to one row per pool.** Listing
  `/`, `/home` and `/var/log` with three identical bars says nothing.

## Notes

- The bar instantiates one widget per monitor, so a multi-monitor setup runs one
  `sensors.py` per screen. At the default 2s idle interval that is a rounding
  error, but it is why the sampler is careful about what it does per tick.
- Plugins run unsandboxed inside `omarchy-shell`. This one reads `/proc` and
  `/sys`, opens no sockets, and spawns no processes other than the command
  behind the footer button. It is a few hundred lines — read them. On a machine
  with the NVIDIA driver installed it also loads `libnvidia-ml.so`, the same
  library `nvidia-smi` uses, and calls it in-process.

## Development

```bash
git clone https://github.com/hominluo/omarchy-ostat-menus.git \
  ~/.config/omarchy/plugins/io.github.hominluo.ostat-menus
omarchy-shell shell rescanPlugins
omarchy plugin enable io.github.hominluo.ostat-menus
```

`python3 tests/test_gpu.py` and `python3 tests/test_sensors.py` run the
suites; both build their own `/sys` trees in a temp directory, so they pass on
a machine with no GPU and no sensor drivers at all.
`omarchy plugin validate .` checks the manifest. Saving a file under
`~/.config/omarchy/plugins/` makes the shell reload the plugin's code, but the
bar keeps the widget instance it already has — `Component.onCompleted` does
not run again and the sampler is not restarted — so after editing `Panel.qml`
run `omarchy restart shell` to see the change.

| File | What it is |
|---|---|
| `manifest.json` | plugin declaration and the settings schema |
| `sensors.py` | the sampler: `/proc` and `/sys` in, NDJSON out (`--once`, `--detail`, `--interval N`) |
| `Panel.qml` | bar widget and popup panel |
| `Sparkline.qml` | history graph — area fill, stroked line, fixed time base |
| `MeterBar.qml` | single-value and segmented usage bars |
| `Model.js` | unit formatting and threshold helpers |
| `tests/test_gpu.py` | GPU discovery and sampling, against synthetic `/sys` trees |
| `tests/test_sensors.py` | hwmon temperatures and fans, against synthetic `/sys` trees |

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Bjango or iStat Menus; the name is a nod, the code is not.

---

<p align="center">
  Built by <a href="https://x.com/hominluo">@hominluo</a> ·
  <a href="https://github.com/hominluo">GitHub</a> ·
  <a href="https://github.com/hominluo/omarchy-ostat-menus/issues">Issues</a> ·
  <a href="https://github.com/hominluo/omarchy-ostat-menus/releases">Releases</a>
</p>
