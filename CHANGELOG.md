# Changelog

## 1.2.0 — 2026-11-01

Every GPU the machine has, not just the one the panel happened to notice.

- Discovery used to look for `gpu_busy_percent`, an amdgpu-only sysfs file, so
  on an NVIDIA or Intel machine the sample carried `"gpu": null` and every GPU
  element hid itself. The card's driver now decides which probe to use:
  - **AMD** reads `gpu_busy_percent` (unchanged), and its clock from the
    active DPM state in `pp_dpm_sclk`; that file is a table, not a number, so
    `read_int()` had always returned nothing there.
  - **NVIDIA** reads NVML through `ctypes` — utilisation, VRAM, temperature,
    power, clock — in-process, with no `nvidia-smi` fork per tick. Entry
    points are looked up with their `_v2` names and the older short names, so
    a driver from before the rename loads too, and a missing optional counter
    leaves that field empty instead of failing the whole card.
  - **Intel** reads RC6 residency (the time the render engine slept) and
    reports the rest of the tick as utilisation, plus the current and maximum
    frequency.
- A hybrid machine reports one reading per card. `gpus` is the list,
  discrete first; the old `gpu` field is its first entry, so a panel built
  against the single-GPU payload still renders. The bar gains a `gpu2` cell
  for the second card, alongside `gpu`.
- Each reading carries an `id`, and the panel keys its sparkline history on
  it rather than on the card's position: a probe that skips a tick (an RC6
  counter reset across a suspend) no longer hands its history to the card that
  moved up a slot.
- On an NVIDIA machine the driver registers no `hwmon` chip, so the sensor
  sweep finds no GPU temperature; the card's own reading fills that in.
- New `tests/test_gpu.py`: discovery, each vendor's probe, NVML symbol
  fallback, and the payload shape, all against synthetic `/sys` trees.

## 1.1.1 — 2026-11-01

Correctness and cost fixes from a full review of the sampler and the panel.

- With the panel closed, only the hwmon chips that can supply a CPU or GPU
  temperature are read. The expensive ACPI/SMM chips (`dell_smm`, `BAT0`,
  `nvme`, the USB-C ports) are skipped: 32 ms per tick became 0.7 ms on a
  Dell laptop.
- `/run/media/<user>/<label>` (where udisks mounts removable media) is no
  longer hidden by the `/run` skip rule. Mount prefixes now match on a path
  boundary, so `/run` still hides `/run` and `/run/user`.
- The "Session" traffic figures are measured from a baseline taken when the
  panel started (or when the interface changed), instead of showing the
  counters since boot.
- A malformed `/proc` line (short, non-numeric, or not valid UTF-8) is skipped
  instead of raising out of `main()` — which made the widget restart the
  sampler on every tick.
- Mount rows dedupe by pool to the shortest path regardless of mountinfo
  order, rather than keeping whichever entry happened to come first.
- Unlabelled temperature inputs get a distinct `chip index` label instead of
  several identical rows, and the hero reads "max" when the temperature is a
  fallback rather than a real package sensor.
- The bar tooltip tracks the live CPU/MEM figure while it is open.
- New `check` script and `tests/`: fixture-driven tests for `sensors.py` and
  the `Model.js` helpers, run with `unittest` and `node:test`.
