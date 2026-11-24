#!/bin/sh
# Install to: /usr/lib/systemd/system-sleep/muyu-bt-resume (chmod 755)
# Xiaomi Pad 7 Pro (muyu) S3 Deep-Sleep Pre/Post Hook
#
# PRE-SUSPEND:
#   Cleanly stop Focus Pen BLE bridge and unbind 888000.serial (WCN7850 BT UART)
#   before entering deep sleep. This completely prevents hci_qca qca_suspend -110
#   timeouts, 18s suspend hangs, and SoC watchdog reboots on lid close/open.
#
# POST-RESUME:
#   Run recovery asynchronously via systemd-run --no-block (outside systemd-suspend
#   cgroup) to rebind 888000.serial, power on hci0, restart Focus Pen bridge, and
#   recover dwc3-qcom USB-NCM gadget if HS-PHY got stuck.

case "$1" in
    pre)
        # Wait briefly if a previous post-resume recovery is still binding 888000.serial
        for _i in 1 2 3 4 5; do
            systemctl is-active --quiet muyu-resume-recover.service || break
            sleep 0.5
        done
        systemctl stop muyu-focus-pen.service >/dev/null 2>&1 || true
        timeout 3 bluetoothctl power off >/dev/null 2>&1 || true
        if [ -e /sys/bus/platform/drivers/qcom_geni_serial/888000.serial ]; then
            echo "888000.serial" > /sys/bus/platform/drivers/qcom_geni_serial/unbind 2>/dev/null || true
            logger -t muyu-resume "pre-suspend: unbound 888000.serial cleanly"
        fi
        ;;
    post)
        systemd-run --no-block --unit=muyu-resume-recover /bin/sh -c '
            exec 9>/run/muyu-resume-recover.lock
            flock -n 9 || exit 0

            sleep 1

            # --- 1. WCN7850 Bluetooth + Focus Pen ---
            # Never force-unbind 888000.serial while hci0 may be mid-firmware-download
            # (that deadlocks hci_unregister_dev in D-state and blocks all future suspends).
            if [ ! -e /sys/bus/platform/drivers/qcom_geni_serial/888000.serial ]; then
                echo "888000.serial" > /sys/bus/platform/drivers/qcom_geni_serial/bind 2>/dev/null || true
            fi

            for _i in 1 2 3 4 5 6 7 8; do
                sleep 1
                timeout 2 btmgmt info 2>/dev/null | grep -q "hci0:" && break
            done

            systemctl restart bluetooth.service >/dev/null 2>&1 || true
            sleep 1
            timeout 5 bluetoothctl power on >/dev/null 2>&1 || true
            systemctl start muyu-focus-pen.service >/dev/null 2>&1 || true
            logger -t muyu-resume "post-resume: WCN7850 Bluetooth & Focus Pen ready."

            # --- 2. USB-C dwc3-qcom HS-PHY + USB Gadget ---
            if [ -d /sys/kernel/config/usb_gadget/g1 ]; then
                UDC_STATE=$(cat /sys/class/udc/a600000.usb/state 2>/dev/null || echo "")
                if [ "$UDC_STATE" != "configured" ]; then
                    logger -t muyu-resume "resetting dwc3-qcom HS-PHY (state=$UDC_STATE)..."
                    echo "" > /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null || true
                    echo "a600000.usb" > /sys/bus/platform/drivers/dwc3-qcom/unbind 2>/dev/null || true
                    sleep 1
                    echo "a600000.usb" > /sys/bus/platform/drivers/dwc3-qcom/bind 2>/dev/null || true
                    sleep 1
                    echo "a600000.usb" > /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null || true
                    sleep 1
                    ip link set usb0 up 2>/dev/null || true
                    ip addr replace 172.16.42.1/24 dev usb0 2>/dev/null || true
                    logger -t muyu-resume "USB gadget ready."
                fi
            fi
        ' >/dev/null 2>&1 || true
        ;;
esac
