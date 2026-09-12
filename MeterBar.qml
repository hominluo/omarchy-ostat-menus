import QtQuick
import qs.Commons

// Horizontal usage bar. A single `value` covers the common case; `segments`
// stacks proportional parts in one track, which is how the memory row shows
// application memory and cache sharing the same total.
Item {
  id: root

  // 0..1
  property real value: 0
  // [{ fraction: 0..1, alpha: 0..1 }], drawn left to right in order.
  property var segments: []

  property color foreground: Color.foreground
  property color warnColor: Color.urgent
  // 0..1, blends the fill toward warnColor. Callers pass Model.severity().
  property real severity: 0
  property real trackAlpha: 0.10
  property real fillAlpha: 0.80

  readonly property color fillColor: severity > 0
    ? Qt.tint(Qt.rgba(foreground.r, foreground.g, foreground.b, 1),
              Qt.rgba(warnColor.r, warnColor.g, warnColor.b, Math.min(1, severity)))
    : foreground

  implicitHeight: Math.max(2, Style.space(6))
  implicitWidth: Style.space(80)

  Rectangle {
    id: track
    anchors.fill: parent
    radius: height / 2
    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, root.trackAlpha)
  }

  // Single-value fill. Rounded on both ends, and never thinner than its own
  // corner radius so a 1% reading still paints a visible pip.
  Rectangle {
    visible: root.segments.length === 0
    height: parent.height
    width: root.value > 0 ? Math.max(height, root.value * parent.width) : 0
    radius: height / 2
    color: Qt.rgba(root.fillColor.r, root.fillColor.g, root.fillColor.b, root.fillAlpha)

    Behavior on width {
      NumberAnimation { duration: 260; easing.type: Easing.OutCubic }
    }
  }

  Row {
    visible: root.segments.length > 0
    anchors.fill: parent
    spacing: 0

    // Keyed on the count so a new segments array (one arrives with every
    // sample) updates the existing rectangles and lets the width animate,
    // instead of rebuilding them.
    Repeater {
      model: root.segments.length

      Rectangle {
        required property int index
        readonly property var segment: root.segments[index] || ({})

        height: root.height
        width: Math.max(0, (segment.fraction || 0) * root.width)
        color: Qt.rgba(root.fillColor.r, root.fillColor.g, root.fillColor.b,
                       segment.alpha === undefined ? root.fillAlpha : segment.alpha)
        // Only the outer edges of the stack are rounded; rounding every
        // segment would leave notches where they meet.
        topLeftRadius: index === 0 ? root.height / 2 : 0
        bottomLeftRadius: index === 0 ? root.height / 2 : 0
        topRightRadius: index === root.segments.length - 1 ? root.height / 2 : 0
        bottomRightRadius: index === root.segments.length - 1 ? root.height / 2 : 0

        Behavior on width {
          NumberAnimation { duration: 260; easing.type: Easing.OutCubic }
        }
      }
    }
  }
}
