import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    property bool attached: false
    property bool charging: false
    property int penSoc: -1
    property int txVout: 0
    property int txIout: 0
    property int powerMw: 0
    property int placeErr: 0
    property int reverseChgMode: 0
    property string chargeMode: "auto"
    property bool openedByTouch: false
    property bool mouseHovered: false
    property bool online: false

    readonly property bool largeUi: openedByTouch || Kirigami.Settings.tabletMode

    function fetchStatus() {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    try {
                        var d = JSON.parse(xhr.responseText);
                        root.attached = !!d.attached;
                        root.charging = !!d.charging;
                        root.penSoc = d.pen_soc;
                        root.txVout = d.tx_vout;
                        root.txIout = d.tx_iout;
                        root.powerMw = d.power_mw;
                        root.placeErr = d.place_err;
                        root.reverseChgMode = d.reverse_chg_mode;
                        root.chargeMode = d.mode || "auto";
                        root.online = true;
                    } catch (e) {}
                } else {
                    root.online = false;
                }
            }
        };
        xhr.open("GET", "http://127.0.0.1:18731/status");
        xhr.send();
    }

    function setMode(m) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE && xhr.status === 200) {
                fetchStatus();
            }
        };
        xhr.open("GET", "http://127.0.0.1:18731/mode?v=" + m);
        xhr.send();
    }

    Timer {
        interval: 2000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.fetchStatus()
    }

    // Only show tooltip on real mouse/touchpad hover (prevents stuck tooltip after finger tap)
    toolTipMainText: root.mouseHovered ? "小米焦点触控笔" : ""
    toolTipSubText: {
        if (!root.mouseHovered) return "";
        if (!online) return "磁吸充电服务未运行";
        if (placeErr !== 0) return "摆放偏移，请重新对齐";
        var soc = penSoc >= 0 ? (penSoc + "%") : "--";
        if (attached && reverseChgMode === 1)
            return "磁吸无线充电中 · " + soc + " (" + (powerMw / 1000).toFixed(2) + "W)";
        if (attached && penSoc >= 100)
            return "已磁吸（已充满）· 100%";
        if (attached)
            return "已磁吸（待机/已暂停）· " + soc;
        return "已取下 · 上次电量 " + soc;
    }

    compactRepresentation: Item {
        Layout.minimumWidth: row.implicitWidth + Kirigami.Units.smallSpacing * 2
        Layout.preferredWidth: Layout.minimumWidth

        HoverHandler {
            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
            onHoveredChanged: root.mouseHovered = hovered
        }

        TapHandler {
            onTapped: function(eventPoint, button) {
                root.openedByTouch = (point.device.type === PointerDevice.TouchScreen);
                if (root.openedByTouch) {
                    root.mouseHovered = false;
                }
                root.expanded = !root.expanded;
            }
        }

        RowLayout {
            id: row
            anchors.centerIn: parent
            spacing: 4

            Kirigami.Icon {
                source: root.placeErr !== 0 ? "dialog-warning"
                      : (root.attached && root.reverseChgMode === 1 ? "battery-charging" : "draw-freehand")
                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                Layout.preferredHeight: Kirigami.Units.iconSizes.small
            }

            PlasmaComponents.Label {
                text: {
                    if (!root.online) return "笔";
                    if (root.placeErr !== 0) return "偏移";
                    var p = root.penSoc >= 0 ? (root.penSoc + "%") : (root.attached ? "充" : "笔");
                    return (root.attached && root.reverseChgMode === 1) ? ("⚡" + p) : p;
                }
                font.pixelSize: Math.max(11, Kirigami.Theme.smallFont.pixelSize)
                font.bold: root.attached && root.reverseChgMode === 1
            }
        }
    }

    fullRepresentation: Item {
        Layout.minimumWidth: Kirigami.Units.gridUnit * (root.largeUi ? 19 : 16)
        Layout.minimumHeight: Kirigami.Units.gridUnit * (root.largeUi ? 15 : 13)
        Layout.preferredWidth: Layout.minimumWidth
        Layout.preferredHeight: Layout.minimumHeight

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: root.largeUi ? Kirigami.Units.largeSpacing : Kirigami.Units.mediumSpacing

            RowLayout {
                Layout.fillWidth: true
                Kirigami.Icon {
                    source: root.attached && root.reverseChgMode === 1 ? "battery-charging" : "draw-freehand"
                    Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                }
                ColumnLayout {
                    spacing: 2
                    Kirigami.Heading {
                        level: 4
                        text: "小米焦点触控笔"
                    }
                    PlasmaComponents.Label {
                        opacity: 0.75
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        text: {
                            if (!root.online) return "服务离线";
                            if (root.placeErr !== 0) return "⚠️ 磁吸摆放偏移";
                            if (root.attached && root.reverseChgMode === 1) return "⚡ 磁吸无线充电中";
                            if (root.attached && root.penSoc >= 100) return "✅ 已磁吸（已充满待机）";
                            if (root.attached) return "🧲 已磁吸（待机/已暂停）";
                            return "✏️ 已取下使用中";
                        }
                    }
                }
                Item { Layout.fillWidth: true }
                Kirigami.Heading {
                    level: 2
                    text: root.penSoc >= 0 ? (root.penSoc + "%") : "--%"
                }
            }

            QQC2.ProgressBar {
                Layout.fillWidth: true
                from: 0
                to: 100
                value: root.penSoc >= 0 ? root.penSoc : 0
            }

            GridLayout {
                columns: 3
                Layout.fillWidth: true
                columnSpacing: Kirigami.Units.smallSpacing

                PlasmaComponents.Label {
                    text: "电压\n" + (root.txVout / 1000).toFixed(2) + " V"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
                PlasmaComponents.Label {
                    text: "电流\n" + root.txIout + " mA"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
                PlasmaComponents.Label {
                    text: "功率\n" + (root.powerMw / 1000).toFixed(2) + " W"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.largeUi ? Kirigami.Units.mediumSpacing : Kirigami.Units.smallSpacing

                PlasmaComponents.Button {
                    text: "自动磁吸"
                    checkable: true
                    checked: root.chargeMode === "auto"
                    Layout.fillWidth: true
                    implicitHeight: root.largeUi ? 48 : 34
                    onClicked: root.setMode("auto")
                }
                PlasmaComponents.Button {
                    text: "强制充电"
                    checkable: true
                    checked: root.chargeMode === "on"
                    Layout.fillWidth: true
                    implicitHeight: root.largeUi ? 48 : 34
                    onClicked: root.setMode("on")
                }
                PlasmaComponents.Button {
                    text: "暂停"
                    checkable: true
                    checked: root.chargeMode === "off"
                    Layout.fillWidth: true
                    implicitHeight: root.largeUi ? 48 : 34
                    onClicked: root.setMode("off")
                }
            }
        }
    }
}
