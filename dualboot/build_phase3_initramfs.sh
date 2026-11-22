#!/bin/bash
set -euo pipefail

OUT_DIR="/work/out"
ROOTFS="/work/out/initramfs_root"
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"/{bin,sbin,etc,proc,sys,dev,mnt,tmp,run,var/log,var/lib/misc,lib/firmware/ath12k/WCN7850/hw2.0,lib/firmware/qca,lib/firmware/nanosic}
mkdir -p "$OUT_DIR"

cp /work/firmware/ath12k/WCN7850/hw2.0/* "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/"
cp /work/firmware/qca/* "$ROOTFS/lib/firmware/qca/"
cp /work/firmware/nanosic/MCU_Upgrade.bin "$ROOTFS/lib/firmware/nanosic/"
cp /work/firmware/qcom/sm8635/xiaomi/muyu/* "$ROOTFS/lib/firmware/"
ln -sf . "$ROOTFS/lib/firmware/qcom"
ln -sf . "$ROOTFS/lib/firmware/sm8650"

for req in \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/amss.bin" \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/m3.bin" \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/board-2.bin" \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/board.bin" \
    "$ROOTFS/lib/firmware/qca/hmtbtfw20.tlv" \
    "$ROOTFS/lib/firmware/adsp.mdt" \
    "$ROOTFS/lib/firmware/adsp_dtb.mdt" \
    "$ROOTFS/lib/firmware/cdsp.mdt" \
    "$ROOTFS/lib/firmware/cdsp_dtb.mdt" \
    "$ROOTFS/lib/firmware/battmgr.jsn" \
    "$ROOTFS/lib/firmware/gen71100_sqe.fw" \
    "$ROOTFS/lib/firmware/gen71100_gmu.bin" \
    "$ROOTFS/lib/firmware/gen71100_zap.mbn" \
    "$ROOTFS/lib/firmware/aw88261_acf.bin" \
    "$ROOTFS/lib/firmware/Xiaomi-Pad-7-Pro-tplg.bin" \
    "$ROOTFS/lib/firmware/nanosic/MCU_Upgrade.bin"; do
    test -s "$req" || { echo "MISSING/EMPTY FIRMWARE: $req" >&2; exit 1; }
done

cp /bin/busybox "$ROOTFS/bin/busybox"
chmod +x "$ROOTFS/bin/busybox"

for cmd in sh ash mount umount mkdir mknod ls cat echo grep awk sed dmesg sleep ip ifconfig route udhcpd telnetd httpd ps kill reboot poweroff sync chmod chown ln rm cp mv touch head tail wc uname dd tr basename readlink date cut od hexdump wget nc tar gzip gunzip xz losetup switch_root chroot setsid stty clear free df top ping find stat seq; do
    ln -sf /bin/busybox "$ROOTFS/bin/$cmd"
done
ln -sf /bin/busybox "$ROOTFS/sbin/init"
ln -sf /bin/busybox "$ROOTFS/sbin/switch_root"

cat > "$ROOTFS/bin/boot_rootfs.sh" <<'BOOTROOT'
#!/bin/sh
set -e
export PATH=/bin:/sbin

URL="${1:-http://172.16.42.2:8000/rootfs.squashfs}"
IMG="/tmp/rootfs.squashfs"

echo "[muyu-p3] Fetching $URL into RAM..."
mkdir -p /tmp /mnt/ro /mnt/rw /mnt/newroot
rm -f "$IMG"
wget -O "$IMG" "$URL"
ls -lh "$IMG"

echo "[muyu-p3] Mounting SquashFS + OverlayFS in RAM..."
mount -t squashfs -o loop,ro "$IMG" /mnt/ro
mount -t tmpfs -o size=4G,mode=0755 tmpfs /mnt/rw
mkdir -p /mnt/rw/upper /mnt/rw/work
mount -t overlay overlay -o lowerdir=/mnt/ro,upperdir=/mnt/rw/upper,workdir=/mnt/rw/work /mnt/newroot

echo "[muyu-p3] Populating /lib/firmware inside newroot..."
mkdir -p /mnt/newroot/lib/firmware
cp -a /lib/firmware/. /mnt/newroot/lib/firmware/

echo "[muyu-p3] Binding runtime filesystems..."
mkdir -p /mnt/newroot/proc /mnt/newroot/sys /mnt/newroot/dev /mnt/newroot/dev/pts /mnt/newroot/run /mnt/newroot/tmp
mount --move /proc /mnt/newroot/proc 2>/dev/null || mount -t proc proc /mnt/newroot/proc
mount --move /sys /mnt/newroot/sys 2>/dev/null || mount -t sysfs sysfs /mnt/newroot/sys
mount --move /dev /mnt/newroot/dev 2>/dev/null || mount -t devtmpfs devtmpfs /mnt/newroot/dev
mkdir -p /mnt/newroot/dev/pts /mnt/newroot/dev/shm
mount -t devpts devpts /mnt/newroot/dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /mnt/newroot/dev/shm 2>/dev/null || true
mount -t tmpfs tmpfs /mnt/newroot/run 2>/dev/null || true

echo "[muyu-p3] Entering Ubuntu 24.04 ARM64 rootfs..."
exec chroot /mnt/newroot /sbin/muyu-init
BOOTROOT
chmod +x "$ROOTFS/bin/boot_rootfs.sh"

cat > "$ROOTFS/init" <<'EOF'
#!/bin/sh
export PATH=/bin:/sbin

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /sys/kernel/config /sys/kernel/debug /sys/fs/pstore /tmp /run
mount -t devpts devpts /dev/pts
mount -t configfs none /sys/kernel/config 2>/dev/null || true
mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
mount -t tmpfs -o size=4G tmpfs /tmp

setup_usb_gadget() {
    G=/sys/kernel/config/usb_gadget/g1
    mkdir -p "$G"
    echo 0x1d6b > "$G/idVendor"
    echo 0x0104 > "$G/idProduct"
    echo 0x0100 > "$G/bcdDevice"
    echo 0x0200 > "$G/bcdUSB"
    echo 0xEF > "$G/bDeviceClass"
    echo 0x02 > "$G/bDeviceSubClass"
    echo 0x01 > "$G/bDeviceProtocol"

    mkdir -p "$G/strings/0x409"
    echo "muyu-phase3" > "$G/strings/0x409/serialnumber"
    echo "Xiaomi" > "$G/strings/0x409/manufacturer"
    echo "Pad 7 Pro Mainline Linux" > "$G/strings/0x409/product"

    mkdir -p "$G/configs/c.1/strings/0x409"
    echo "CDC ACM + NCM" > "$G/configs/c.1/strings/0x409/configuration"
    echo 500 > "$G/configs/c.1/MaxPower"

    mkdir -p "$G/functions/acm.GS0"
    mkdir -p "$G/functions/ncm.usb0"
    echo "02:11:22:33:44:55" > "$G/functions/ncm.usb0/dev_addr"
    echo "02:11:22:33:44:56" > "$G/functions/ncm.usb0/host_addr"

    ln -sf "$G/functions/acm.GS0" "$G/configs/c.1/"
    ln -sf "$G/functions/ncm.usb0" "$G/configs/c.1/"

    UDC=""
    for i in 1 2 3 4 5 6 7 8 9 10; do
        UDC=$(ls /sys/class/udc 2>/dev/null | head -n 1)
        [ -n "$UDC" ] && break
        sleep 0.3 2>/dev/null || sleep 1
    done
    if [ -n "$UDC" ]; then
        echo "$UDC" > "$G/UDC"
    fi
}

setup_usb_gadget

for i in 1 2 3 4 5 6 7 8 9 10; do
    [ -e /dev/ttyGS0 ] && break
    sleep 0.3 2>/dev/null || sleep 1
done

ip link set lo up 2>/dev/null || true
if [ -d /sys/class/net/usb0 ]; then
    ip link set usb0 up 2>/dev/null || true
    ip addr add 172.16.42.1/24 dev usb0 2>/dev/null || true
    mkdir -p /var/lib/misc /var/log
    touch /var/lib/misc/udhcpd.leases
    cat > /etc/udhcpd.conf <<'DHCP'
start 172.16.42.2
end 172.16.42.10
interface usb0
lease_file /var/lib/misc/udhcpd.leases
opt subnet 255.255.255.0
opt lease 86400
DHCP
    udhcpd /etc/udhcpd.conf 2>/dev/null || true
fi

# Keep UFS link stable across s2idle/deep suspend
for f in /sys/devices/platform/soc@0/*ufshc*/spm_lvl /sys/bus/platform/devices/*ufshc*/spm_lvl; do
    [ -w "$f" ] && echo 1 > "$f" 2>/dev/null || true
done

# Auto-boot persistent Ubuntu rootfs from UFS PARTNAME=linux if present
LINUX_DEV=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    for uev in /sys/class/block/sd*/uevent; do
        if grep -q '^PARTNAME=linux$' "$uev" 2>/dev/null; then
            LINUX_DEV=$(basename "$(dirname "$uev")")
            break
        fi
    done
    [ -n "$LINUX_DEV" ] && [ -b "/dev/$LINUX_DEV" ] && break
    sleep 0.3 2>/dev/null || sleep 1
done

if [ -n "$LINUX_DEV" ] && [ -b "/dev/$LINUX_DEV" ]; then
    mkdir -p /mnt/newroot
    if mount -t ext4 -o rw,noatime "/dev/$LINUX_DEV" /mnt/newroot 2>/dev/null; then
        if [ ! -f /mnt/newroot/lib/firmware/adsp.mdt ] && [ ! -f /mnt/newroot/usr/lib/firmware/adsp.mdt ]; then
            mkdir -p /mnt/newroot/lib/firmware
            cp -a /lib/firmware/. /mnt/newroot/lib/firmware/ 2>/dev/null || true
        fi
        if [ -x /mnt/newroot/usr/lib/systemd/systemd ] || [ -L /mnt/newroot/sbin/init ]; then
            mkdir -p /mnt/newroot/proc /mnt/newroot/sys /mnt/newroot/dev /mnt/newroot/run
            mount --move /proc /mnt/newroot/proc 2>/dev/null || true
            mount --move /sys /mnt/newroot/sys 2>/dev/null || true
            mount --move /dev /mnt/newroot/dev 2>/dev/null || true
            exec /sbin/switch_root /mnt/newroot /sbin/init
        elif [ -x /mnt/newroot/sbin/muyu-init ]; then
            mkdir -p /mnt/newroot/proc /mnt/newroot/sys /mnt/newroot/dev /mnt/newroot/dev/pts /mnt/newroot/dev/shm /mnt/newroot/run /mnt/newroot/tmp
            mount --bind /proc /mnt/newroot/proc
            mount --bind /sys /mnt/newroot/sys
            mount --bind /dev /mnt/newroot/dev
            mount -t devpts devpts /mnt/newroot/dev/pts 2>/dev/null || true
            mount -t tmpfs tmpfs /mnt/newroot/dev/shm 2>/dev/null || true
            mount -t tmpfs tmpfs /mnt/newroot/run 2>/dev/null || true
            chroot /mnt/newroot /sbin/muyu-init --auto-desktop >/mnt/newroot/tmp/muyu-init.log 2>&1 &
            if [ -e /dev/ttyGS0 ]; then
                (
                    exec </dev/ttyGS0 >/dev/ttyGS0 2>&1
                    echo "=== MUYU DUALBOOT LINUX (/dev/$LINUX_DEV) ==="
                    while true; do
                        chroot /mnt/newroot /bin/bash -i
                    done
                ) &
            fi
            while true; do
                sleep 3600
            done
        fi
        umount /mnt/newroot 2>/dev/null || true
    fi
fi

if [ -e /dev/ttyGS0 ]; then
    (
        exec </dev/ttyGS0 >/dev/ttyGS0 2>&1
        echo "=== MUYU PHASE3 INITRAMFS READY (ttyGS0 + usb0=172.16.42.1) ==="
        while true; do
            /bin/sh
        done
    ) &
fi

# Keep PID 1 alive indefinitely (no 65s auto-reboot in Phase 3)
while true; do
    sleep 3600
done
EOF
chmod +x "$ROOTFS/init"

echo "Phase-3 initramfs root prepared at $ROOTFS"
du -sh "$ROOTFS"
