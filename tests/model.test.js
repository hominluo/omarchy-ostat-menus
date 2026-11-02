// Model.js is imported by Panel.qml and by this file: the helper module ends
// with a guarded module.exports so node can require it. No dependencies.

const test = require("node:test")
const assert = require("node:assert/strict")
const M = require("../Model.js")

test("bytes uses binary units and drops precision as it grows", () => {
  assert.equal(M.bytes(0), "0 B")
  assert.equal(M.bytes(-5), "0 B")
  assert.equal(M.bytes(512), "512 B")
  assert.equal(M.bytes(1024), "1.00 KB")
  assert.equal(M.bytes(1023), "1023 B")
  assert.equal(M.bytes(10 * 1024 ** 3), "10.0 GB")
  assert.equal(M.bytes(9.99 * 1024 ** 3), "9.99 GB")
  assert.equal(M.bytes(150 * 1024 ** 3), "150 GB")
})

test("rate appends /s and rateCompact stays four characters", () => {
  assert.equal(M.rate(0), "0 B/s")
  assert.equal(M.rate(2048), "2.00 KB/s")
  assert.equal(M.rateCompact(0), "0B")
  assert.equal(M.rateCompact(999), "999B")
  assert.equal(M.rateCompact(1000), "1.0K")
  assert.equal(M.rateCompact(999 * 1024), "999K")
  assert.equal(M.rateCompact(1024 * 1024), "1.0M")
  assert.equal(M.rateCompact(12.3 * 1024 * 1024), "12M")
})

test("percent, fraction and severity handle junk", () => {
  assert.equal(M.percent(42.4, 1), "42.4%")
  assert.equal(M.percent(undefined), "0%")
  assert.equal(M.fraction(1, 0), 0)
  assert.equal(M.fraction(2, 4), 0.5)
  assert.equal(M.fraction(9, 4), 1)
  assert.equal(M.severity(50), 0)
  assert.equal(M.severity(90), 1)
  assert.equal(M.severity(82.5), 0.5)
  assert.equal(M.tempSeverity(-1), 0)
  assert.equal(M.tempSeverity(80), 0.5)
  assert.equal(M.tempSeverity(95), 1)
})

test("duration keeps two units", () => {
  assert.equal(M.duration(0), "0s")
  assert.equal(M.duration(90), "1m")
  assert.equal(M.duration(3600 + 720), "1h 12m")
  assert.equal(M.duration(4 * 86400 + 7 * 3600), "4d 7h")
  assert.equal(M.duration(-10), "0s")
})

test("temperature and frequency", () => {
  assert.equal(M.temperature(null), "")
  assert.equal(M.temperature(64.6), "65°")
  assert.equal(M.frequency(null), "")
  assert.equal(M.frequency(0), "")
  assert.equal(M.frequency(800), "800 MHz")
  assert.equal(M.frequency(3000), "3.00 GHz")
})

test("shortCpuModel strips marketing noise", () => {
  assert.equal(M.shortCpuModel("11th Gen Intel(R) Core(TM) i7-1185G7 @ 3.00GHz"),
    "11th Gen Intel Core i7-1185G7")
  assert.equal(M.shortCpuModel("AMD Ryzen 7 5800X 8-Core Processor"),
    "AMD Ryzen 7 5800X 8-Core")
  assert.equal(M.shortCpuModel(""), "")
})

test("processName elides and names the unnamed", () => {
  assert.equal(M.processName(""), "?")
  assert.equal(M.processName("  chrome  "), "chrome")
  assert.equal(M.processName("x".repeat(30)), "x".repeat(21) + "…")
})

test("monitorName is the last non-flag token", () => {
  assert.equal(M.monitorName("omarchy-launch-or-focus-tui btop"), "btop")
  assert.equal(M.monitorName("alacritty -e htop"), "htop")
  assert.equal(M.monitorName("/usr/bin/kitty"), "kitty")
  assert.equal(M.monitorName(""), "monitor")
  assert.equal(M.monitorName("-x"), "monitor")
})

test("pushSample keeps a fixed-length ring without mutating the input", () => {
  const start = [1, 2, 3]
  const next = M.pushSample(start, 4, 3)
  assert.deepEqual(start, [1, 2, 3])
  assert.deepEqual(next, [2, 3, 4])
  assert.deepEqual(M.pushSample([], 1, 0), [1])
  assert.deepEqual(M.pushSample(null, "x", 2), [0])
})

test("maxOf never returns below the floor", () => {
  assert.equal(M.maxOf([], 5), 5)
  assert.equal(M.maxOf([1, 9, 2], 5), 9)
  assert.equal(M.maxOf([1, NaN, 2], 0), 2)
})

test("sessionBase re-bases on an interface change only", () => {
  const first = M.sessionBase(null, { iface: "wlan0", rxTotal: 100, txTotal: 50 })
  assert.deepEqual(first, { iface: "wlan0", rx: 100, tx: 50 })
  // Same interface: the baseline survives, or the session figure resets every tick.
  const same = M.sessionBase(first, { iface: "wlan0", rxTotal: 900, txTotal: 90 })
  assert.equal(same, first)
  // Wi-Fi -> Ethernet swaps in different counters.
  assert.deepEqual(M.sessionBase(first, { iface: "eth0", rxTotal: 5, txTotal: 6 }),
    { iface: "eth0", rx: 5, tx: 6 })
  assert.equal(M.sessionBase(null, null), null)
  assert.equal(M.sessionBase(first, {}), first)
})

test("sessionTotals measures from the baseline and clamps resets", () => {
  const base = { iface: "wlan0", rx: 1000, tx: 500 }
  assert.deepEqual(M.sessionTotals(base, { rxTotal: 1500, txTotal: 900 }), { rx: 500, tx: 400 })
  assert.deepEqual(M.sessionTotals(base, { rxTotal: 10, txTotal: 10 }), { rx: 0, tx: 0 })
  assert.deepEqual(M.sessionTotals(null, { rxTotal: 10 }), { rx: 0, tx: 0 })
  assert.deepEqual(M.sessionTotals(base, null), { rx: 0, tx: 0 })
})
