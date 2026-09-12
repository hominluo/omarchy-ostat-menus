import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// iStat-Menus-style system monitor: a live readout in the bar, and a popup
// with the full picture — CPU history and per-core load, memory composition,
// the process table, network and disk throughput, GPU, and temperatures.
//
// All of it comes from one long-running sensors.py that streams NDJSON. The
// panel talks back over the same pipe to raise the sample rate and turn on the
// expensive fields only while it is on screen.
Panel {
  id: root
  moduleName: "io.github.hominluo.ostat-menus"
  ipcTarget: "io.github.hominluo.ostat-menus"
  manageIpc: false

  // ------------------------------------------------------------- settings

  readonly property var cells: {
    var configured = setting("items", ["cpu", "mem", "net"])
    return (configured instanceof Array) && configured.length > 0 ? configured : ["cpu", "mem"]
  }
  readonly property bool showGraph: setting("graph", true) === true
  readonly property real idleInterval: Math.max(0.5, Number(setting("interval", 2)) || 2)
  readonly property real activeInterval: Math.max(0.25, Number(setting("activeInterval", 1)) || 1)
  readonly property int historySize: Math.max(12, Math.min(240, Number(setting("historySize", 60)) || 60))
  readonly property string terminalCommand: String(setting("monitorCommand", "omarchy-launch-or-focus-tui btop"))

  // ------------------------------------------------------------- state

  property var sample: ({})
  property bool connected: false
  property string sensorError: ""
  property string processSort: "cpu"
  property int cursorRow: -1

  property var cpuHistory: []
  property var memHistory: []
  property var rxHistory: []
  property var txHistory: []
  property var gpuHistory: []

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color faint: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool vertical: bar ? bar.vertical : false
  readonly property int barSize: bar ? bar.barSize : Style.bar.sizeHorizontal

  readonly property real cpuValue: Model.num(sample.cpu)
  readonly property var memInfo: sample.mem || {}
  readonly property real memValue: Model.fraction(memInfo.used, memInfo.total) * 100
  // Cache the kernel would hand back under pressure: what is available minus
  // what is already untouched.
  readonly property real reclaimable: Math.max(0, Model.num(memInfo.available) - Model.num(memInfo.free))
  readonly property var netInfo: sample.net || {}
  readonly property var diskInfo: sample.disk || {}
  readonly property var gpuInfo: sample.gpu || null
  readonly property var cores: sample.cores || []
  readonly property var mounts: sample.mounts || []
  readonly property var temps: sample.temps || []
  readonly property var fans: sample.fans || []
  readonly property var processRows: (processSort === "mem" ? sample.procMem : sample.procCpu) || []
  readonly property real cpuTemp: sample.cpuTemp === null || sample.cpuTemp === undefined ? -1 : Number(sample.cpuTemp)

  // ------------------------------------------------------------- sampling

  // Absolute path to the sibling script. Deriving it from this file's own URL
  // means the plugin works from any directory — a git clone, a hand-made
  // folder, or a `omarchy plugin clone` copy under another name.
  readonly property string sensorScript: String(Qt.resolvedUrl("sensors.py")).replace(/^file:\/\//, "")

  // Both settings go out in ONE write. Two Process.write() calls in the same
  // event-loop turn do not both reach the child — the second is dropped — so
  // splitting them silently left the sampler on whatever detail level it
  // started with.
  function pushSensorConfig() {
    if (!sensors.running) return
    sensors.write("interval " + (opened ? activeInterval : idleInterval).toFixed(2)
      + "\ndetail " + (opened ? "1" : "0") + "\n")
  }

  function ingest(line) {
    var text = String(line).trim()
    if (text === "") return
    var payload
    try {
      payload = JSON.parse(text)
    } catch (error) {
      // A malformed line is a bug in the sampler, not a reason to blank the
      // panel; keep showing the last good sample.
      return
    }

    connected = true
    sensorError = ""
    sample = payload

    cpuHistory = Model.pushSample(cpuHistory, payload.cpu, historySize)
    memHistory = Model.pushSample(memHistory, Model.fraction(payload.mem ? payload.mem.used : 0,
                                                             payload.mem ? payload.mem.total : 0) * 100, historySize)
    rxHistory = Model.pushSample(rxHistory, payload.net ? payload.net.rx : 0, historySize)
    txHistory = Model.pushSample(txHistory, payload.net ? payload.net.tx : 0, historySize)
    if (payload.gpu) gpuHistory = Model.pushSample(gpuHistory, payload.gpu.busy, historySize)
  }

  // Started from Component.onCompleted rather than `running: true` here:
  // setting running during property assignment can launch the child before
  // stdinEnabled is applied, which leaves the command pipe unconnected and the
  // panel stuck on whatever detail level the sampler defaulted to.
  Process {
    id: sensors
    command: ["python3", root.sensorScript, "--interval", root.idleInterval.toFixed(2)]
    stdinEnabled: true
    onStarted: root.pushSensorConfig()
    stdout: SplitParser { onRead: function(line) { root.ingest(line) } }
    stderr: SplitParser {
      onRead: function(line) {
        var text = String(line).trim()
        if (text !== "") root.sensorError = text
      }
    }
    onExited: function(exitCode) {
      root.connected = false
      if (root.sensorError === "") root.sensorError = "sensors.py exited (" + exitCode + ")"
      // The sampler should never stop on its own. If it does — a broken
      // Python, a deleted script — come back for it rather than leaving the
      // widget dead until the next shell restart.
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 5000
    onTriggered: if (!sensors.running) sensors.running = true
  }

  Component.onCompleted: sensors.running = true

  onOpenedChanged: {
    pushSensorConfig()
    if (opened) {
      cursorRow = -1
      if (panelFlick) panelFlick.contentY = 0
      Qt.callLater(function() { keyCatcher.forceActiveFocus() })
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function cpu(): string { return Model.percent(root.cpuValue, 1) }
    function memory(): string { return Model.percent(root.memValue, 1) }
    function json(): string { return JSON.stringify(root.sample) }
  }

  // ------------------------------------------------------------- bar button

  function scrollBy(steps) {
    if (!panelFlick) return
    var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
    panelFlick.contentY = Math.max(0, Math.min(maxY, panelFlick.contentY + steps * Style.space(48)))
  }

  function openMonitor() {
    if (bar) bar.run(terminalCommand)
    close()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    horizontalMargin: root.vertical ? 0 : 7
    fixedWidth: root.vertical ? -1 : readout.implicitWidth + Style.spaceReal(14)
    fixedHeight: root.vertical ? readout.implicitHeight + Style.spaceReal(10) : -1
    tooltipText: root.connected
      ? "CPU " + Model.percent(root.cpuValue) + " · MEM " + Model.percent(root.memValue)
      : "System monitor (starting…)"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.openMonitor()
      else root.toggle()
    }

    Item {
      id: readout
      anchors.centerIn: parent
      implicitWidth: root.vertical ? verticalCells.implicitWidth : horizontalCells.implicitWidth
      implicitHeight: root.vertical ? verticalCells.implicitHeight : horizontalCells.implicitHeight

      Row {
        id: horizontalCells
        visible: !root.vertical
        anchors.centerIn: parent
        spacing: Style.space(9)

        Repeater {
          model: root.cells
          BarCell {
            required property var modelData
            kind: String(modelData)
          }
        }
      }

      Column {
        id: verticalCells
        visible: root.vertical
        anchors.centerIn: parent
        spacing: Style.space(3)

        Repeater {
          model: root.cells
          // A vertical bar is one glyph wide, so each metric collapses to its
          // icon stacked over a bare number.
          Column {
            required property var modelData
            spacing: 0

            Text {
              anchors.horizontalCenter: parent.horizontalCenter
              textFormat: Text.PlainText
              text: root.cellGlyph(String(modelData))
              color: root.barForeground
              font.family: root.fontFamily
              font.pixelSize: Style.bar.iconFont
              renderType: Text.NativeRendering
            }

            Text {
              anchors.horizontalCenter: parent.horizontalCenter
              textFormat: Text.PlainText
              text: root.cellCompactValue(String(modelData))
              color: root.barForeground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }
    }
  }

  function cellGlyph(kind) {
    switch (kind) {
      case "cpu": return "󰻠"
      case "mem": return "󰍛"
      case "net": return "󰓅"
      case "disk": return "󰋊"
      case "gpu": return "󰢮"
      case "temp": return "󰔏"
      default: return "󰇅"
    }
  }

  // One short string per metric for the vertical bar and the tooltip, where
  // there is no room for the two-line up/down stacks the horizontal bar uses.
  function cellCompactValue(kind) {
    switch (kind) {
      case "cpu": return Math.round(cpuValue) + ""
      case "mem": return Math.round(memValue) + ""
      case "net": return Model.rateCompact(Model.num(netInfo.rx) + Model.num(netInfo.tx))
      case "disk": return Model.rateCompact(Model.num(diskInfo.read) + Model.num(diskInfo.write))
      case "gpu": return gpuInfo ? Math.round(Model.num(gpuInfo.busy)) + "" : "–"
      case "temp": return cpuTemp >= 0 ? Math.round(cpuTemp) + "" : "–"
      default: return ""
    }
  }

  // A bar cell: icon, value, and for the percentage metrics an optional
  // micro-sparkline that turns a number into a trend at a glance.
  component BarCell: Row {
    id: cell
    property string kind: "cpu"

    readonly property bool isRatePair: kind === "net" || kind === "disk"
    readonly property real downRate: kind === "net" ? Model.num(root.netInfo.rx) : Model.num(root.diskInfo.read)
    readonly property real upRate: kind === "net" ? Model.num(root.netInfo.tx) : Model.num(root.diskInfo.write)
    readonly property real percentValue: {
      if (kind === "cpu") return root.cpuValue
      if (kind === "mem") return root.memValue
      if (kind === "gpu") return root.gpuInfo ? Model.num(root.gpuInfo.busy) : 0
      return 0
    }
    readonly property var graphValues: {
      if (kind === "cpu") return root.cpuHistory
      if (kind === "mem") return root.memHistory
      if (kind === "gpu") return root.gpuHistory
      return []
    }
    readonly property bool graphable: root.showGraph && graphValues.length >= 5

    anchors.verticalCenter: parent.verticalCenter
    spacing: Style.space(4)
    visible: kind !== "gpu" || !!root.gpuInfo

    // Same size and weight as the stock bar icons (BarIconButton uses
    // Style.bar.iconFont at full opacity), so the readout reads as part of
    // the bar rather than a dimmed annex of it.
    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: root.cellGlyph(cell.kind)
      color: root.barForeground
      font.family: root.fontFamily
      font.pixelSize: Style.bar.iconFont
      renderType: Text.NativeRendering
    }

    Sparkline {
      anchors.verticalCenter: parent.verticalCenter
      visible: cell.graphable && !cell.isRatePair
      width: visible ? Style.space(22) : 0
      height: Math.round(root.barSize * 0.5)
      capacity: root.historySize
      values: cell.graphValues
      stroke: root.barForeground
      // CPU and GPU sit near zero most of the time, where a 0-100 scale paints
      // a flat line along the bottom edge that reads as an underscore rather
      // than a graph; the floor keeps a quiet minute quiet while still showing
      // its shape. Memory is the opposite — a large, slow-moving figure that
      // auto-scaling flattens into a solid filled block — so it keeps the
      // absolute scale.
      autoScale: cell.kind !== "mem"
      autoScaleFloor: 25
      maxValue: 100
      gridLines: 0
      // Stroke only. In a 13px cell an area fill is mush, and for a figure
      // that barely moves — memory sits at one level all day — filling under
      // a flat line paints a solid block instead of a graph.
      fillAlpha: 0
      strokeAlpha: 0.85
      lineWidth: 1
    }

    // Percentage metrics: one line, right-aligned to a fixed width so the bar
    // does not jitter as the number crosses 9 → 10 → 100.
    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: !cell.isRatePair
      textFormat: Text.PlainText
      text: root.connected ? Math.round(cell.percentValue) + "%" : "··"
      color: root.barForeground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignRight
      width: Math.max(implicitWidth, rateMetrics.percentWidth)
    }

    // Throughput metrics get the two-line down/up stack: the shape iStat uses,
    // and the only way to fit both directions in a 26px bar.
    Column {
      anchors.verticalCenter: parent.verticalCenter
      visible: cell.isRatePair
      spacing: -Style.space(2)

      Text {
        textFormat: Text.PlainText
        text: "󰇚 " + Model.rateCompact(cell.downRate)
        color: root.barForeground
        opacity: cell.downRate > 0 ? 1.0 : 0.55
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignLeft
        width: rateMetrics.rateWidth
      }

      Text {
        textFormat: Text.PlainText
        text: "󰕒 " + Model.rateCompact(cell.upRate)
        color: root.barForeground
        opacity: cell.upRate > 0 ? 1.0 : 0.55
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignLeft
        width: rateMetrics.rateWidth
      }
    }
  }

  // Reserve the width of the widest string each cell type can produce, so a
  // rate ticking from "0B" to "12.3M" never shoves its neighbours sideways.
  Item {
    id: rateMetrics
    visible: false
    readonly property real percentWidth: percentProbe.implicitWidth
    readonly property real rateWidth: rateProbe.implicitWidth

    Text {
      id: percentProbe
      text: "100%"
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      id: rateProbe
      text: "󰇚 999M"
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // ------------------------------------------------------------- popup

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(408))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(760))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.scrollBy(dy) }
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.openMonitor()
      onTextKey: function(t) {
        var key = t.toLowerCase()
        if (key === "c") root.processSort = "cpu"
        else if (key === "m") root.processSort = "mem"
        else if (key === "t") root.openMonitor()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------------------------------------------------------- hero

          PanelHero {
            id: hero
            width: parent.width
            title: String(root.sample.host || "System")
            meta: root.connected
              ? "UP " + Model.duration(root.sample.uptime) + "   LOAD " + Model.loadText(root.sample.load)
              : "STARTING SENSORS"
            detail: root.cpuTemp >= 0 ? Model.temperature(root.cpuTemp) : ""
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: "󰘚"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.sensorError !== ""
            width: parent.width
            text: root.sensorError
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          // ---------------------------------------------------------- cpu

          PanelSeparator { foreground: root.foreground }

          SectionRow {
            label: "PROCESSOR"
            value: Model.shortCpuModel(root.sample.cpuModel)
          }

          // Readings sit above the graph, never on it. A trace that crosses
          // its own label is unreadable exactly when it matters — during a
          // spike.
          Item {
            width: parent.width
            implicitHeight: cpuValueText.implicitHeight

            Text {
              id: cpuValueText
              anchors.left: parent.left
              anchors.bottom: parent.bottom
              textFormat: Text.PlainText
              text: Model.percent(root.cpuValue)
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
              font.bold: true
            }

            Text {
              anchors.right: parent.right
              anchors.baseline: cpuValueText.baseline
              textFormat: Text.PlainText
              text: Model.frequency(root.sample.freq)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
            }
          }

          Sparkline {
            width: parent.width
            height: Style.space(48)
            capacity: root.historySize
            values: root.cpuHistory
            maxValue: 100
            stroke: root.foreground
            fillAlpha: 0.14
            gridLines: 3
          }

          // Per-core columns, filled from the bottom, each with its index
          // underneath. The cluster is capped at part of the panel width so a
          // 4-core machine gets readable bars rather than four fat slabs; the
          // space it leaves carries the core count and package temperature.
          Item {
            id: coreCluster
            width: parent.width
            visible: root.cores.length > 0
            implicitHeight: Math.max(coreRow.implicitHeight, coreMeta.implicitHeight)

            readonly property int gap: Style.space(3)
            readonly property bool labelled: root.cores.length <= 16
            readonly property real columnWidth: {
              var count = Math.max(1, root.cores.length)
              var budget = width * 0.56
              return Math.max(2, Math.min(Style.space(20), (budget - gap * (count - 1)) / count))
            }

            Row {
              id: coreRow
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              spacing: coreCluster.gap

              Repeater {
                model: root.cores

                Column {
                  required property var modelData
                  required property int index

                  spacing: Style.space(3)

                  Rectangle {
                    width: coreCluster.columnWidth
                    height: Style.space(26)
                    radius: Style.cornerRadius
                    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.07)

                    Rectangle {
                      anchors.bottom: parent.bottom
                      width: parent.width
                      // A busy core must never read as an empty box, so keep a
                      // 1px pip once there is any load at all.
                      height: Math.max(modelData > 0 ? 1 : 0,
                                       parent.height * Model.clamp(modelData / 100, 0, 1))
                      radius: parent.radius
                      color: Qt.tint(root.foreground,
                                     Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, Model.severity(modelData)))
                      opacity: 0.9

                      Behavior on height {
                        NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                      }
                    }
                  }

                  Text {
                    visible: coreCluster.labelled
                    width: coreCluster.columnWidth
                    textFormat: Text.PlainText
                    text: index
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    horizontalAlignment: Text.AlignHCenter
                  }
                }
              }
            }

            Column {
              id: coreMeta
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                anchors.right: parent.right
                textFormat: Text.PlainText
                text: Model.num(root.sample.cpuCount) + " cores"
                color: root.foreground
                opacity: 0.6
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                anchors.right: parent.right
                visible: root.cpuTemp >= 0
                textFormat: Text.PlainText
                text: Model.temperature(root.cpuTemp) + " package"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          // ---------------------------------------------------------- memory

          PanelSeparator { foreground: root.foreground }

          SectionRow {
            label: "MEMORY"
            value: Model.percent(root.memValue) + " of " + Model.bytes(root.memInfo.total)
          }

          MeterBar {
            width: parent.width
            height: Style.space(8)
            foreground: root.foreground
            warnColor: root.urgent
            severity: Model.severity(root.memValue)
            // The three bands partition the total exactly: application memory
            // (total - available), the reclaimable part of what is available,
            // and the untouched remainder. Using MemInfo's own Cached figure
            // here would double-count — it is already inside `available` — and
            // push the bar past 100%.
            segments: [
              { fraction: Model.fraction(root.memInfo.used, root.memInfo.total), alpha: 0.85 },
              { fraction: Model.fraction(root.reclaimable, root.memInfo.total), alpha: 0.28 }
            ]
          }

          Row {
            width: parent.width
            spacing: Style.space(14)

            Legend { swatchAlpha: 0.85; label: "Used"; value: Model.bytes(root.memInfo.used) }
            Legend { swatchAlpha: 0.28; label: "Cache"; value: Model.bytes(root.reclaimable) }
            Legend { swatchAlpha: 0.10; label: "Free"; value: Model.bytes(root.memInfo.free) }
          }

          // "Free" above is MemFree — genuinely untouched pages, which on a
          // warm machine is almost nothing. Available is the number a reader
          // actually wants: free plus everything the kernel would reclaim.
          InfoLine {
            label: "Available"
            value: Model.bytes(root.memInfo.available)
          }

          InfoLine {
            visible: Model.num(root.memInfo.swapTotal) > 0
            label: "Swap"
            value: Model.bytes(root.memInfo.swapUsed) + " of " + Model.bytes(root.memInfo.swapTotal)
          }

          // ---------------------------------------------------------- processes

          PanelSeparator { foreground: root.foreground }

          Item {
            width: parent.width
            implicitHeight: Math.max(processHeader.implicitHeight, sortTabs.implicitHeight)

            PanelSectionHeader {
              id: processHeader
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: "TOP PROCESSES"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Row {
              id: sortTabs
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(4)

              SortTab { mode: "cpu"; label: "CPU" }
              SortTab { mode: "mem"; label: "MEM" }
            }
          }

          Column {
            width: parent.width
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              visible: root.processRows.length === 0
              width: parent.width
              text: root.connected ? "Sampling…" : "Waiting for sensors"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              horizontalAlignment: Text.AlignHCenter
              topPadding: Style.space(6)
              bottomPadding: Style.space(6)
            }

            Repeater {
              model: root.processRows.slice(0, 5)

              ProcessRow {
                required property var modelData
                required property int index
                width: parent.width
                row: modelData
                rank: index
              }
            }
          }

          // ---------------------------------------------------------- network

          PanelSeparator { foreground: root.foreground }

          SectionRow {
            label: "NETWORK"
            value: String(root.netInfo.iface || "—")
          }

          Row {
            width: parent.width
            spacing: Style.space(18)

            RateLabel { glyph: "󰇚"; value: Model.num(root.netInfo.rx) }
            RateLabel { glyph: "󰕒"; value: Model.num(root.netInfo.tx); labelOpacity: 0.7 }
          }

          Sparkline {
            width: parent.width
            height: Style.space(46)
            capacity: root.historySize
            values: root.rxHistory
            values2: root.txHistory
            autoScale: true
            autoScaleFloor: 16384
            stroke: root.foreground
            stroke2: root.foreground
            fillAlpha: 0.13
            fillAlpha2: 0.0
            strokeAlpha: 0.85
            strokeAlpha2: 0.45
            gridLines: 2
          }

          InfoLine {
            label: "Session"
            value: "󰇚 " + Model.bytes(root.netInfo.rxTotal) + "   󰕒 " + Model.bytes(root.netInfo.txTotal)
          }

          // ---------------------------------------------------------- disk

          PanelSeparator { foreground: root.foreground }

          SectionRow {
            label: "DISK"
            value: "󰇚 " + Model.rate(root.diskInfo.read) + "   󰕒 " + Model.rate(root.diskInfo.write)
          }

          Column {
            width: parent.width
            spacing: Style.space(8)

            Repeater {
              model: root.mounts

              Column {
                required property var modelData
                width: parent.width
                spacing: Style.space(3)

                readonly property real usedFraction: Model.fraction(modelData.used, modelData.total)

                Item {
                  width: parent.width
                  implicitHeight: mountPath.implicitHeight

                  Text {
                    id: mountPath
                    anchors.left: parent.left
                    textFormat: Text.PlainText
                    text: String(modelData.path)
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }

                  Text {
                    anchors.right: parent.right
                    textFormat: Text.PlainText
                    text: Model.bytes(modelData.used) + " of " + Model.bytes(modelData.total)
                      + "   " + Model.percent(parent.parent.usedFraction * 100)
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                MeterBar {
                  width: parent.width
                  height: Style.space(5)
                  foreground: root.foreground
                  warnColor: root.urgent
                  value: parent.usedFraction
                  severity: Model.severity(parent.usedFraction * 100)
                }
              }
            }
          }

          // ---------------------------------------------------------- gpu

          PanelSeparator {
            foreground: root.foreground
            visible: !!root.gpuInfo
          }

          SectionRow {
            visible: !!root.gpuInfo
            label: "GRAPHICS"
            value: root.gpuInfo ? String(root.gpuInfo.name) : ""
          }

          Column {
            width: parent.width
            visible: !!root.gpuInfo
            spacing: Style.space(6)

            InfoLine {
              label: "Utilisation"
              value: root.gpuInfo ? Model.percent(root.gpuInfo.busy) : ""
            }

            MeterBar {
              width: parent.width
              height: Style.space(5)
              foreground: root.foreground
              warnColor: root.urgent
              value: root.gpuInfo ? Model.clamp(Model.num(root.gpuInfo.busy) / 100, 0, 1) : 0
              severity: root.gpuInfo ? Model.severity(root.gpuInfo.busy) : 0
            }

            InfoLine {
              visible: !!root.gpuInfo && Model.num(root.gpuInfo.vramTotal) > 0
              label: "VRAM"
              value: root.gpuInfo
                ? Model.bytes(root.gpuInfo.vramUsed) + " of " + Model.bytes(root.gpuInfo.vramTotal)
                : ""
            }

            MeterBar {
              width: parent.width
              height: Style.space(5)
              visible: !!root.gpuInfo && Model.num(root.gpuInfo.vramTotal) > 0
              foreground: root.foreground
              warnColor: root.urgent
              value: root.gpuInfo ? Model.fraction(root.gpuInfo.vramUsed, root.gpuInfo.vramTotal) : 0
              severity: root.gpuInfo ? Model.severity(Model.fraction(root.gpuInfo.vramUsed, root.gpuInfo.vramTotal) * 100) : 0
            }

            InfoLine {
              label: "Temperature"
              value: {
                if (!root.gpuInfo) return ""
                var parts = []
                if (root.sample.gpuTemp !== null && root.sample.gpuTemp !== undefined)
                  parts.push(Model.temperature(root.sample.gpuTemp))
                if (root.gpuInfo.power !== null && root.gpuInfo.power !== undefined)
                  parts.push(Number(root.gpuInfo.power).toFixed(1) + " W")
                return parts.join("   ")
              }
            }
          }

          // ---------------------------------------------------------- sensors

          PanelSeparator {
            foreground: root.foreground
            visible: root.temps.length > 0 || root.fans.length > 0
          }

          PanelSectionHeader {
            visible: root.temps.length > 0 || root.fans.length > 0
            text: "SENSORS"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          // Two columns: a dozen thermal readings in one column would push
          // everything below it off the bottom of the panel.
          Grid {
            width: parent.width
            visible: root.temps.length > 0 || root.fans.length > 0
            columns: 2
            columnSpacing: Style.space(14)
            rowSpacing: Style.space(3)

            Repeater {
              model: root.temps

              SensorCell {
                required property var modelData
                width: (parent.width - Style.space(14)) / 2
                label: String(modelData.label)
                value: Model.temperature(modelData.value)
                severity: Model.tempSeverity(modelData.value)
              }
            }

            Repeater {
              model: root.fans

              SensorCell {
                required property var modelData
                width: (parent.width - Style.space(14)) / 2
                label: String(modelData.label)
                value: Math.round(Model.num(modelData.value)) + " RPM"
              }
            }
          }

          // ---------------------------------------------------------- footer

          PanelSeparator { foreground: root.foreground }

          FooterAction {
            width: parent.width
          }
        }
      }
    }
  }

  // ------------------------------------------------------------- components

  // Section header with a right-aligned value, e.g. "MEMORY … 52% of 16 GB".
  component SectionRow: Item {
    property string label: ""
    property string value: ""

    width: parent.width
    implicitHeight: headerText.implicitHeight

    PanelSectionHeader {
      id: headerText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      text: parent.label
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Text {
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.left: headerText.right
      anchors.leftMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: parent.value
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideRight
    }
  }

  // Label on the left, value on the right, both dim. The workhorse row.
  component InfoLine: Item {
    property string label: ""
    property string value: ""

    width: parent.width
    implicitHeight: value === "" && label === "" ? 0 : lineLabel.implicitHeight
    visible: implicitHeight > 0

    Text {
      id: lineLabel
      textFormat: Text.PlainText
      anchors.left: parent.left
      text: parent.label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.left: lineLabel.right
      anchors.leftMargin: Style.space(10)
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      horizontalAlignment: Text.AlignRight
      elide: Text.ElideRight
    }
  }

  // Swatch + name + figure, matching one band of the memory meter.
  component Legend: Row {
    property real swatchAlpha: 0.8
    property string label: ""
    property string value: ""

    spacing: Style.space(5)

    Rectangle {
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(7)
      height: Style.space(7)
      radius: width / 2
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, parent.swatchAlpha)
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: parent.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: parent.value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }
  }

  component RateLabel: Row {
    property string glyph: ""
    property real value: 0
    property real labelOpacity: 1.0

    spacing: Style.space(5)

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: parent.glyph
      color: root.foreground
      opacity: 0.55
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: Model.rate(parent.value)
      color: root.foreground
      opacity: parent.labelOpacity
      font.family: root.fontFamily
      font.pixelSize: Style.font.subtitle
      font.bold: true
    }
  }

  component SensorCell: Item {
    property string label: ""
    property string value: ""
    property real severity: 0

    implicitHeight: sensorLabel.implicitHeight

    Text {
      id: sensorLabel
      textFormat: Text.PlainText
      anchors.left: parent.left
      width: Math.max(0, parent.width - sensorValue.implicitWidth - Style.space(8))
      text: parent.label
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      elide: Text.ElideRight
    }

    Text {
      id: sensorValue
      textFormat: Text.PlainText
      anchors.right: parent.right
      text: parent.value
      color: parent.severity > 0
        ? Qt.tint(root.foreground, Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, parent.severity))
        : root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }
  }

  component SortTab: Item {
    property string mode: "cpu"
    property string label: ""

    readonly property bool selected: root.processSort === mode

    implicitWidth: tabText.implicitWidth + Style.space(12)
    implicitHeight: tabText.implicitHeight + Style.space(4)

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: parent.selected
        ? Style.selectedFillFor(root.foreground, Color.accent)
        : (tabMouse.containsMouse ? Style.hoverFillFor(root.foreground, Color.accent) : "transparent")

      Behavior on color { ColorAnimation { duration: 80 } }
    }

    Text {
      id: tabText
      anchors.centerIn: parent
      textFormat: Text.PlainText
      text: parent.label
      color: parent.selected ? root.foreground : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
    }

    MouseArea {
      id: tabMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.processSort = parent.mode
    }
  }

  // One process. The row's own fill is proportional to its share of the
  // sorted column, so the table reads as a bar chart without adding a
  // separate meter to every line.
  component ProcessRow: Item {
    property var row: null
    property int rank: 0

    readonly property real metric: row ? (root.processSort === "mem" ? Model.num(row.mem) : Model.num(row.cpu)) : 0
    readonly property real topMetric: {
      var rows = root.processRows
      if (!rows || rows.length === 0) return 1
      var head = rows[0]
      var value = root.processSort === "mem" ? Model.num(head.mem) : Model.num(head.cpu)
      return value > 0 ? value : 1
    }

    implicitHeight: procName.implicitHeight + Style.space(7)

    Rectangle {
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(0, Model.clamp(parent.metric / parent.topMetric, 0, 1) * parent.width)
      height: parent.height
      radius: Style.cornerRadius
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.07)

      Behavior on width { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
    }

    Text {
      id: procName
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.leftMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      width: Math.max(0, parent.width - procValue.implicitWidth - procPid.implicitWidth - Style.space(24))
      text: Model.processName(parent.row ? parent.row.name : "")
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }

    Text {
      id: procPid
      textFormat: Text.PlainText
      anchors.right: procValue.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: parent.row ? parent.row.pid : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      id: procValue
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      text: {
        if (!parent.row) return ""
        return root.processSort === "mem"
          ? Model.bytes(parent.row.mem)
          : Model.percent(parent.row.cpu, 1)
      }
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }
  }

  component FooterAction: CursorSurface {
    id: footer

    hasCursor: footerMouse.containsMouse
    foreground: root.foreground
    implicitHeight: footerLabel.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: footerMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.openMonitor()
    }

    Row {
      anchors.centerIn: parent
      spacing: Style.space(8)

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: ""
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
      }

      Text {
        id: footerLabel
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: "Open btop"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: "T"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }
}
