# Changelog

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
