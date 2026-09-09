import QtQuick
import qs.Commons

// Right-anchored history graph: an area fill under a stroked line, drawn on a
// fixed time base so samples scroll in from the right edge instead of
// rescaling horizontally as the buffer fills.
//
// A second optional series shares the vertical scale, which is what lets the
// network graph put receive and transmit in one box and stay comparable.
Canvas {
  id: root

  property var values: []
  property var values2: []

  // Vertical scale. `autoScale` grows the axis to fit the tallest sample in
  // view (network rates, which have no ceiling); a fixed maxValue keeps
  // percentage graphs anchored at 100 so a quiet minute reads as quiet
  // rather than being stretched to fill the box.
  property real maxValue: 100
  property bool autoScale: false
  property real autoScaleFloor: 1

  // Samples the box is wide enough to hold. Points are placed against this,
  // not against values.length, so early samples do not stretch to fill.
  property int capacity: 60

  property color stroke: Color.foreground
  property color stroke2: Color.foreground
  property real lineWidth: Math.max(1, Math.round(Style.spaceReal(1.5)))
  property real fillAlpha: 0.13
  property real fillAlpha2: 0.0
  property real strokeAlpha: 0.85
  property real strokeAlpha2: 0.55
  property int gridLines: 3
  property real gridAlpha: 0.07
  property color gridColor: stroke

  readonly property real scaleTop: {
    if (!autoScale) return Math.max(1, maxValue)
    var top = Math.max(autoScaleFloor, 1)
    for (var i = 0; i < (values || []).length; i++) top = Math.max(top, values[i])
    for (var j = 0; j < (values2 || []).length; j++) top = Math.max(top, values2[j])
    // A little headroom keeps the peak off the top border, where the line
    // would otherwise be clipped by the stroke width.
    return top * 1.12
  }

  onValuesChanged: requestPaint()
  onValues2Changed: requestPaint()
  onStrokeChanged: requestPaint()
  onStroke2Changed: requestPaint()
  onScaleTopChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()

  function seriesPath(ctx, data, close) {
    var count = (data || []).length
    if (count < 1) return false
    var step = capacity > 1 ? width / (capacity - 1) : width
    // Anchor the newest sample at the right edge and walk backwards, so a
    // half-full buffer draws a short trace on the right rather than a
    // stretched one across the whole box.
    var firstX = width - (count - 1) * step

    ctx.beginPath()
    for (var i = 0; i < count; i++) {
      var x = firstX + i * step
      var y = height - (Math.max(0, data[i]) / root.scaleTop) * height
      y = Math.max(root.lineWidth / 2, Math.min(height - root.lineWidth / 2, y))
      if (i === 0) ctx.moveTo(x, y)
      else ctx.lineTo(x, y)
    }
    if (close) {
      ctx.lineTo(firstX + (count - 1) * step, height)
      ctx.lineTo(firstX, height)
      ctx.closePath()
    }
    return true
  }

  function paintSeries(ctx, data, color, fill, alpha) {
    if (!data || data.length === 0) return
    if (fill > 0 && data.length > 1) {
      seriesPath(ctx, data, true)
      ctx.fillStyle = Qt.rgba(color.r, color.g, color.b, fill)
      ctx.fill()
    }
    if (!seriesPath(ctx, data, false)) return
    ctx.strokeStyle = Qt.rgba(color.r, color.g, color.b, alpha)
    ctx.lineWidth = root.lineWidth
    ctx.lineJoin = "round"
    ctx.lineCap = "round"
    // A single sample has no segment to stroke; give it a dot so a
    // just-opened panel is not an empty box.
    if (data.length === 1) {
      ctx.lineTo(width, height - (Math.max(0, data[0]) / root.scaleTop) * height)
    }
    ctx.stroke()
  }

  onPaint: {
    var ctx = getContext("2d")
    ctx.reset()
    ctx.clearRect(0, 0, width, height)

    if (gridLines > 0) {
      ctx.strokeStyle = Qt.rgba(gridColor.r, gridColor.g, gridColor.b, gridAlpha)
      ctx.lineWidth = 1
      for (var g = 1; g <= gridLines; g++) {
        // Half-pixel offset so a 1px rule lands on a device pixel instead of
        // straddling two and painting as a 2px smear.
        var y = Math.round(height * g / (gridLines + 1)) + 0.5
        ctx.beginPath()
        ctx.moveTo(0, y)
        ctx.lineTo(width, y)
        ctx.stroke()
      }
    }

    paintSeries(ctx, values2, stroke2, fillAlpha2, strokeAlpha2)
    paintSeries(ctx, values, stroke, fillAlpha, strokeAlpha)
  }
}
