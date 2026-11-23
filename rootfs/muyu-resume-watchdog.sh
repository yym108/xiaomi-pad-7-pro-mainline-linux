#!/bin/sh
# Install to: /usr/lib/systemd/system-sleep/muyu-bt-resume (chmod 755)
# Xiaomi Pad 7 Pro (muyu) S3 Deep-Sleep Resume Watchdog
# Uses systemd-run --no-block so systemd-suspend cgroup cleanup never kills the recovery job.

[ "$1" = "post" ] || exit 0

systemd-run --no-block --unit=muyu-resume-recover /bin/sh -c '
    sleep 2

    # --- 1. WCN7850 Bluetooth + Focus Pen ---
    if ! (timeout 3 btmgmt info 2>/dev/null | grep -q "hci0:" && \
          timeout 3 bluetoothctl show 2>/dev/null | grep -q "Powered: yes"); then
        logger -t muyu-resume "hci0 unhealthy after S3 resume; rebinding 888000.serial..."
        echo "888000.serial" > /sys/bus/platform/drivers/qcom_geni_serial/unbind 2>/dev/null || true
        sleep 1
        echo "888000.serial" > /sys/bus/platform/drivers/qcom_geni_serial/bind 2>/dev/null || true
        for _i in 1 2 3 4 5 6; do
            sleep 1
            timeout 2 btmgmt info 2>/dev/null | grep -q "hci0:" && break
        done
        systemctl restart bluetooth.service >/dev/null 2>&1 || true
        sleep 2
        timeout 5 bluetoothctl power on >/dev/null 2>&1 || true
        systemctl restart muyu-focus-pen.service >/dev/null 2>&1 || true
        logger -t muyu-resume "WCN7850 Bluetooth & Focus Pen recovered."
    fi

    # --- 2. USB-C dwc3-qcom HS-PHY + USB-NCM Gadget ---
    if [ -d /sys/kernel/config/usb_gadget/g1 ]; then
        UDC_STATE=$(cat /sys/class/udc/a600000.usb/state 2>/dev/null || echo "")
        USB_ONLINE=$(cat /sys/class/power_supply/qcom-battmgr-usb/online 2>/dev/null || echo "0")
        if [ "$UDC_STATE" != "configured" ] && [ "$USB_ONLINE" = "1" ]; then
            logger -t muyu-resume "dwc3-qcom HS-PHY stuck (state=$UDC_STATE); rebinding a600000.usb..."
            echo "" > /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null || true
            echo "a600000.usb" > /sys/bus/platform/drivers/dwc3-qcom/unbind 2>/dev/null || true
            sleep 1
            echo "a600000.usb" > /sys/bus/platform/drivers/dwc3-qcom/bind 2>/dev/null || true
            sleep 1
            echo "a600000.usb" > /sys/kernel/config/usb_gadget/g1/UDC 2>/dev/null || true
            sleep 1
            ip link set usb0 up 2>/dev/null || true
            ip addr add 172.16.42.1/24 dev usb0 2>/dev/null || true
            logger -t muyu-resume "USB-NCM gadget recovered."
        fi
    fi
' >/dev/null 2>&1 || true
