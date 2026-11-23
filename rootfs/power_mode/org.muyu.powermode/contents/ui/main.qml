import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PlasmaComponents
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    property string mode: "balance"
    property string label: "均衡模式"
    property real cpuTemp: 0.0
    property real batTemp: 0.0
    property string batStatus: "Unknown"
    property real batWatts: 0.0
    property int cpuPct: 0
    property real memUsedGb: 0.0
    property real memTotalGb: 0.0
    property bool touchMode: false
    property bool touchAuto: true
    property bool openedByTouch: false
    property bool mouseHovered: false
    property bool online: false

    readonly property bool largeUi: openedByTouch || touchMode || Kirigami.Settings.tabletMode

    function fetchStatus() {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    try {
                        var d = JSON.parse(xhr.responseText);
                        root.mode = d.mode || "balance";
                        root.label = d.label || "均衡模式";
                        root.cpuTemp = d.cpu_temp_c || 0.0;
                        root.batTemp = d.bat_temp_c || 0.0;
                        root.batStatus = d.bat_status || "Unknown";
                        root.batWatts = d.bat_watts || 0.0;
                        root.cpuPct = d.cpu_pct || 0;
                        root.memUsedGb = d.mem_used_gb || 0.0;
                        root.memTotalGb = d.mem_total_gb || 0.0;
                        root.touchMode = !!d.touch_mode;
                        root.touchAuto = !!d.touch_auto;
                        root.online = true;
                    } catch (e) {}
                } else {
                    root.online = false;
                }
            }
        };
        xhr.open("GET", "http://127.0.0.1:18732/status");
        xhr.send();
    }

    function sendApi(path) {
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE && xhr.status === 200) {
                fetchStatus();
            }
        };
        xhr.open("GET", "http://127.0.0.1:18732" + path);
        xhr.send();
    }

    function setMode(m) { sendApi("/mode?v=" + m); }
    function setTouchMode(on) { sendApi("/touch?v=" + (on ? "on" : "off")); }
    function setTouchAuto(on) { sendApi("/touch_auto?v=" + (on ? "on" : "off")); }

    function modeIcon(m) {
        if (m === "save") return "battery-profile-powersave";
        if (m === "perf") return "battery-profile-performance";
        return "battery-profile-balanced";
    }

    function shortLabel(m) {
        if (m === "save") return "省电";
        if (m === "perf") return "性能";
        return "均衡";
    }

    function powerTitle() {
        if (batStatus === "Charging") return "充电功率";
        if (batStatus === "Full") return "已充满";
        return "实时功耗";
    }

    function powerValue() {
        var w = Math.abs(batWatts).toFixed(1) + " W";
        if (batStatus === "Charging") return "+" + w;
        if (batStatus === "Discharging") return "-" + w;
        return w;
    }

    Timer {
        interval: 2000
        running: true
        repeat: true
        triggeredOnStart: true
        onTriggered: root.fetchStatus()
    }

    // Only show tooltip on real mouse/touchpad hover (prevents stuck tooltip after finger tap)
    toolTipMainText: root.mouseHovered ? ("硬件功耗模式 · " + root.label) : ""
    toolTipSubText: {
        if (!root.mouseHovered) return "";
        if (!online) return "功耗控制服务未运行";
        return powerTitle() + " " + powerValue() + " · CPU " + cpuPct + "% · 内存 " + memUsedGb.toFixed(1) + "/" + memTotalGb.toFixed(1) + " GB · SoC " + cpuTemp.toFixed(1) + "°C";
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
                source: root.modeIcon(root.mode)
                Layout.preferredWidth: Kirigami.Units.iconSizes.small
                Layout.preferredHeight: Kirigami.Units.iconSizes.small
            }

            PlasmaComponents.Label {
                text: root.online ? root.shortLabel(root.mode) : "功耗"
                font.pixelSize: Math.max(11, Kirigami.Theme.smallFont.pixelSize)
                font.bold: root.mode === "perf"
            }
        }
    }

    fullRepresentation: Item {
        Layout.minimumWidth: Kirigami.Units.gridUnit * (root.largeUi ? 20 : 17)
        Layout.minimumHeight: Kirigami.Units.gridUnit * (root.largeUi ? 19 : 16)
        Layout.preferredWidth: Layout.minimumWidth
        Layout.preferredHeight: Layout.minimumHeight

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: Kirigami.Units.largeSpacing
            spacing: root.largeUi ? Kirigami.Units.largeSpacing : Kirigami.Units.mediumSpacing

            RowLayout {
                Layout.fillWidth: true
                Kirigami.Icon {
                    source: root.modeIcon(root.mode)
                    Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                    Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                }
                ColumnLayout {
                    spacing: 2
                    Kirigami.Heading {
                        level: 4
                        text: "硬件功耗与触控"
                    }
                    PlasmaComponents.Label {
                        opacity: 0.75
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        text: root.online ? ("当前：" + root.label + " · 电池 " + root.batTemp.toFixed(1) + "°C") : "服务离线"
                    }
                }
                Item { Layout.fillWidth: true }
                PlasmaComponents.Label {
                    text: "SoC " + root.cpuTemp.toFixed(1) + "°C"
                    font.bold: true
                }
            }

            GridLayout {
                columns: 3
                Layout.fillWidth: true
                columnSpacing: Kirigami.Units.smallSpacing

                PlasmaComponents.Label {
                    text: root.powerTitle() + "\n" + root.powerValue()
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                    font.bold: true
                }
                PlasmaComponents.Label {
                    text: "CPU 负载\n" + root.cpuPct + "%"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
                PlasmaComponents.Label {
                    text: "内存占用\n" + root.memUsedGb.toFixed(1) + " / " + root.memTotalGb.toFixed(1) + " GB"
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: root.largeUi ? Kirigami.Units.mediumSpacing : Kirigami.Units.smallSpacing

                PlasmaComponents.Button {
                    text: "🔋 省电"
                    checkable: true
                    checked: root.mode === "save"
                    Layout.fillWidth: true
                    implicitHeight: root.largeUi ? 48 : 34
                    onClicked: root.setMode("save")
                }
                PlasmaComponents.Button {
                    text: "⚖️ 均衡"
                    checkable: true
                    checked: root.mode === "balance"
                    Layout.fillWidth: true
                    implicitHeight: root.largeUi ? 48 : 34
                    onClicked: root.setMode("balance")
                }
                PlasmaComponents.Button {
                    text: "🚀 性能"
                    checkable: true
                    checked: root.mode === "perf"
                    Layout.fillWidth: true
                    implicitHeight: root.largeUi ? 48 : 34
                    onClicked: root.setMode("perf")
                }
            }

            Kirigami.Separator {
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                PlasmaComponents.Label {
                    text: "👆 触控大按键模式"
                    Layout.fillWidth: true
                }
                PlasmaComponents.Switch {
                    checked: root.touchMode
                    onToggled: root.setTouchMode(checked)
                }
            }

            RowLayout {
                Layout.fillWidth: true
                PlasmaComponents.Label {
                    text: "🔄 触控/键鼠自动切换"
                    Layout.fillWidth: true
                }
                PlasmaComponents.Switch {
                    checked: root.touchAuto
                    onToggled: root.setTouchAuto(checked)
                }
            }
        }
    }
}
