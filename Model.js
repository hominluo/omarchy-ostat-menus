.pragma library

// Formatting helpers for the iStat panel. Kept out of the QML so the panel
// body stays layout, and so the unit rules live in exactly one place.

var KIB = 1024

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value))
}

function num(value, fallback) {
  var n = Number(value)
  return isFinite(n) ? n : (fallback === undefined ? 0 : fallback)
}

// Byte sizes use binary units with the short SI-looking labels every Linux
// tool prints (KB/MB/GB meaning KiB/MiB/GiB). Precision drops as the number
// grows so a column of these stays the same width: 9.87 GB, 98.7 GB, 987 GB.
function bytes(value, unitSuffix) {
  var n = num(value)
  var suffix = unitSuffix === undefined ? "" : unitSuffix
  if (n < 1) return "0 B" + suffix
  var units = ["B", "KB", "MB", "GB", "TB", "PB"]
  var index = 0
  while (n >= KIB && index < units.length - 1) {
    n /= KIB
    index++
  }
  var digits = index === 0 ? 0 : (n < 10 ? 2 : (n < 100 ? 1 : 0))
  return n.toFixed(digits) + " " + units[index] + suffix
}

function rate(value) {
  return bytes(value, "/s")
}

// The bar has no room for "1.23 MB/s". One significant figure and a bare
// unit letter keeps every rate three or four characters wide.
function rateCompact(value) {
  var n = num(value)
  if (n < 1000) return Math.round(n) + "B"
  var units = ["K", "M", "G", "T"]
  var index = -1
  while (n >= KIB && index < units.length - 1) {
    n /= KIB
    index++
  }
  return (n < 10 ? n.toFixed(1) : Math.round(n).toString()) + units[index]
}

function percent(value, digits) {
  var n = num(value)
  return n.toFixed(digits === undefined ? 0 : digits) + "%"
}

function temperature(value) {
  if (value === null || value === undefined) return ""
  return Math.round(num(value)) + "°"
}

function frequency(mhz) {
  var n = num(mhz, -1)
  if (n <= 0) return ""
  return n >= 1000 ? (n / 1000).toFixed(2) + " GHz" : Math.round(n) + " MHz"
}

// Uptime reads as two units at most: "4d 7h", "7h 12m", "12m". A third unit
// is noise at every scale this is displayed at.
function duration(seconds) {
  var total = Math.max(0, Math.floor(num(seconds)))
  var days = Math.floor(total / 86400)
  var hours = Math.floor((total % 86400) / 3600)
  var minutes = Math.floor((total % 3600) / 60)
  if (days > 0) return days + "d " + hours + "h"
  if (hours > 0) return hours + "h " + minutes + "m"
  if (minutes > 0) return minutes + "m"
  return total + "s"
}

function loadText(load) {
  if (!load || load.length < 3) return ""
  return load[0].toFixed(2) + "  " + load[1].toFixed(2) + "  " + load[2].toFixed(2)
}

// Marketing strings ("Intel(R) Core(TM) i5-7400 CPU @ 3.00GHz") do not fit
// in a right-aligned section header. Strip the noise the trademark symbols
// and the clock suffix add, since the panel prints the live clock anyway.
function shortCpuModel(model) {
  var text = String(model || "").trim()
  if (text === "") return ""
  text = text.replace(/\((R|TM|r|tm)\)/g, "")
  text = text.replace(/\s+CPU\s*/g, " ")
  text = text.replace(/\s*@.*$/, "")
  text = text.replace(/\s+Processor\s*$/, "")
  text = text.replace(/\s+/g, " ").trim()
  return text
}

// A kernel comm is capped at 15 characters, so long binaries arrive already
// truncated; this only has to catch the kernel-thread noise and keep the
// column from stretching.
function processName(name) {
  var text = String(name || "").trim()
  if (text === "") return "?"
  return text.length > 22 ? text.slice(0, 21) + "…" : text
}

function fraction(part, whole) {
  var w = num(whole)
  if (w <= 0) return 0
  return clamp(num(part) / w, 0, 1)
}

// Shared threshold rule: everything that can run hot (load, usage, temp)
// warms toward the urgent color past 75% and sits there past 90%.
function severity(value) {
  var n = num(value)
  if (n >= 90) return 1.0
  if (n <= 75) return 0.0
  return (n - 75) / 15
}

function tempSeverity(celsius) {
  var n = num(celsius, -1)
  if (n < 0) return 0.0
  if (n >= 90) return 1.0
  if (n <= 70) return 0.0
  return (n - 70) / 20
}

// Push a sample into a fixed-length ring used by the sparklines. Returns a
// new array because QML only re-evaluates bindings on assignment, not on
// in-place mutation of the array a property already holds.
function pushSample(history, value, capacity) {
  var next = (history || []).slice()
  next.push(num(value))
  var limit = capacity || 60
  if (next.length > limit) next = next.slice(next.length - limit)
  return next
}

function maxOf(values, floor) {
  var top = num(floor)
  for (var i = 0; i < (values || []).length; i++) {
    var v = num(values[i])
    if (v > top) top = v
  }
  return top
}
