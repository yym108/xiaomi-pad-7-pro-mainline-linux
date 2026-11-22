#!/bin/bash
set -euo pipefail

ARCH_DIR="/arch"
WORK_DIR="/work"
PHASE3_DIR="/phase3"
ROOT="/tmp/arch_root"
OUT="$ARCH_DIR/out"
mkdir -p "$OUT"
rm -rf "$ROOT"
mkdir -p "$ROOT"

echo "=== [1/5] Extracting ArchLinuxARM-aarch64-latest.tar.gz ==="
bsdtar -xpf "$ARCH_DIR/ArchLinuxARM-aarch64-latest.tar.gz" -C "$ROOT" 2>/dev/null || \
    tar --warning=no-unknown-keyword -xpf "$ARCH_DIR/ArchLinuxARM-aarch64-latest.tar.gz" -C "$ROOT"

# Configure TUNA mirror & DNS
echo 'Server = https://mirrors.tuna.tsinghua.edu.cn/archlinuxarm/$arch/$repo' > "$ROOT/etc/pacman.d/mirrorlist"
rm -f "$ROOT/etc/resolv.conf"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"

# Mount chroot filesystems
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"
cleanup() {
    umount -lf "$ROOT/sys" "$ROOT/proc" "$ROOT/dev/pts" "$ROOT/dev" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== [2/5] Initializing pacman keyring & upgrading base ==="
chroot "$ROOT" /bin/bash -c '
set -euo pipefail
sed -i "s/^CheckSpace/#CheckSpace/" /etc/pacman.conf
sed -i "s/^#DisableSandbox/DisableSandbox/" /etc/pacman.conf
sed -i "s/^#ParallelDownloads.*/ParallelDownloads = 8/" /etc/pacman.conf
pacman-key --init
pacman-key --populate archlinuxarm
# Remove linux-aarch64 & huge PC GPU firmware packages (we use our own mainline kernel + muyu firmware)
pacman -Rdd --noconfirm linux-aarch64 linux-firmware linux-firmware-amdgpu linux-firmware-intel linux-firmware-nvidia linux-firmware-radeon linux-firmware-mediatek linux-firmware-broadcom linux-firmware-realtek linux-firmware-atheros linux-firmware-cirrus linux-firmware-other linux-firmware-whence 2>/dev/null || true
pacman -Syu --noconfirm --overwrite "*"
'

echo "=== [3/5] Installing KDE Plasma 6 (Wayland) + Mesa + PipeWire + Hardware Stack ==="
chroot "$ROOT" /bin/bash -c '
set -euo pipefail
pacman -S --noconfirm --needed \
    sudo vim git curl wget htop fastfetch openssh \
    networkmanager bluez bluez-utils bluez-deprecated-tools \
    python python-gobject \
    mesa vulkan-freedreno wayland xorg-xwayland \
    pipewire pipewire-alsa pipewire-pulse wireplumber alsa-utils \
    plasma-desktop kwin sddm sddm-kcm kscreen powerdevil \
    plasma-nm plasma-pa bluedevil breeze breeze-gtk kdialog \
    konsole dolphin kate spectacle ark gwenview \
    noto-fonts noto-fonts-cjk noto-fonts-emoji \
    fcitx5 fcitx5-chinese-addons fcitx5-configtool fcitx5-gtk fcitx5-qt \
    firefox
pacman -S --noconfirm --needed plasma-keyboard 2>/dev/null || true
'

echo "=== [4/5] Configuring Xiaomi Pad 7 Pro (muyu) hardware & services ==="
echo "muyu-arch" > "$ROOT/etc/hostname"
cat > "$ROOT/etc/hosts" <<'HOSTS'
127.0.0.1 localhost
127.0.0.1 muyu-arch
HOSTS

# Locale (en_US + zh_CN) & Timezone
sed -i 's/^#en_US.UTF-8/en_US.UTF-8/' "$ROOT/etc/locale.gen"
sed -i 's/^#zh_CN.UTF-8/zh_CN.UTF-8/' "$ROOT/etc/locale.gen"
chroot "$ROOT" locale-gen
echo 'LANG=zh_CN.UTF-8' > "$ROOT/etc/locale.conf"
ln -sf /usr/share/zoneinfo/Asia/Shanghai "$ROOT/etc/localtime"

# Users: root:muyu and muyu:muyu (wheel NOPASSWD)
chroot "$ROOT" /bin/bash -c '
echo "root:muyu" | chpasswd
id -u muyu >/dev/null 2>&1 || useradd -m -G wheel,video,audio,input,render,storage -s /bin/bash muyu
echo "muyu:muyu" | chpasswd
echo "%wheel ALL=(ALL:ALL) NOPASSWD: ALL" > /etc/sudoers.d/99-wheel-nopasswd
chmod 440 /etc/sudoers.d/99-wheel-nopasswd
'

# SSH root login enabled
mkdir -p "$ROOT/etc/ssh/sshd_config.d"
cat > "$ROOT/etc/ssh/sshd_config.d/99-muyu.conf" <<'SSHD'
PermitRootLogin yes
PasswordAuthentication yes
SSHD

# Copy all Muyu hardware firmware
mkdir -p "$ROOT/usr/lib/firmware"
cp -a "$WORK_DIR/out/initramfs_root/lib/firmware/." "$ROOT/usr/lib/firmware/"

# Install Focus Pen BLE pressure bridge, udev rule, and static switch-slot
mkdir -p "$ROOT/usr/local/bin" "$ROOT/etc/udev/rules.d" "$ROOT/etc/systemd/system" "$ROOT/usr/share/applications"
cp "$PHASE3_DIR/focus_pen_ble.py" "$ROOT/usr/local/bin/focus_pen_ble.py"
chmod +x "$ROOT/usr/local/bin/focus_pen_ble.py"
cp "$PHASE3_DIR/out/switch-slot" "$ROOT/usr/local/bin/switch-slot"
chmod +x "$ROOT/usr/local/bin/switch-slot"
cp "$PHASE3_DIR/switch-slot.py" "$ROOT/usr/local/bin/switch-slot.py"
chmod +x "$ROOT/usr/local/bin/switch-slot.py"
if [ -f "$PHASE3_DIR/out/xiaomi_devauth" ]; then
    cp "$PHASE3_DIR/out/xiaomi_devauth" "$ROOT/usr/local/bin/xiaomi_devauth"
    chmod +x "$ROOT/usr/local/bin/xiaomi_devauth"
fi

cat > "$ROOT/etc/udev/rules.d/99-xiaomi-focus-pen.rules" <<'UDEV'
ATTRS{name}=="Xiaomi Focus Pen*", ENV{LIBINPUT_IGNORE_DEVICE}="1"
UDEV

# Quad AW88261 + AudioReach QUINARY_MI2S_RX init script & service
cat > "$ROOT/usr/local/bin/muyu-audio-init.sh" <<'AUD'
#!/bin/bash
for i in $(seq 1 15); do
    [ -e /dev/snd/controlC0 ] && break
    sleep 0.5
done
amixer -c 0 cset name='QUINARY_MI2S_RX Audio Mixer MultiMedia1' 1,1 >/dev/null 2>&1 || true
amixer -c 0 cset name='stream0.vol_ctrl0 MultiMedia1 Playback Volu' 8192 >/dev/null 2>&1 || true
for pfx in SpkrTopLeft SpkrBotLeft SpkrTopRight SpkrBotRight; do
    amixer -c 0 cset name="$pfx Profile Set" 0 >/dev/null 2>&1 || true
    amixer -c 0 cset name="$pfx PCM Playback Volume" 295 >/dev/null 2>&1 || true
    amixer -c 0 cset name="$pfx Playback Switch" 1 >/dev/null 2>&1 || true
done
AUD
chmod +x "$ROOT/usr/local/bin/muyu-audio-init.sh"

cat > "$ROOT/etc/systemd/system/muyu-audio.service" <<'SVC'
[Unit]
Description=Xiaomi Pad 7 Pro Quad AW88261 AudioReach Init
After=sound.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/muyu-audio-init.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVC

cat > "$ROOT/etc/systemd/system/muyu-focus-pen.service" <<'SVC'
[Unit]
Description=Xiaomi Focus Pen (P81c) 8192-level BLE Pressure Bridge
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStartPre=/bin/sh -c 'rfkill unblock bluetooth 2>/dev/null || true; sleep 1; hciconfig hci0 up 2>/dev/null || true; bluetoothctl power on >/dev/null 2>&1 || true'
ExecStart=/usr/local/bin/focus_pen_ble.py XX:XX:XX:XX:XX:XX
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SVC

# USB-NCM (usb0 = 172.16.42.1/24) static config for NetworkManager
mkdir -p "$ROOT/etc/NetworkManager/system-connections"
cat > "$ROOT/etc/NetworkManager/system-connections/usb0.nmconnection" <<'NM'
[connection]
id=usb0
type=ethernet
interface-name=usb0
autoconnect=true

[ipv4]
method=manual
address1=172.16.42.1/24

[ipv6]
method=ignore
NM
chmod 600 "$ROOT/etc/NetworkManager/system-connections/usb0.nmconnection"

# SDDM autologin into KDE Plasma 6 (Wayland) as user muyu
mkdir -p "$ROOT/etc/sddm.conf.d"
cat > "$ROOT/etc/sddm.conf.d/autologin.conf" <<'SDDM'
[Autologin]
User=muyu
Session=plasma

[General]
DisplayServer=wayland
SDDM

# KDE Plasma 6 Wayland default HiDPI + Fcitx5 env + Maliit OSK
mkdir -p "$ROOT/home/muyu/.config"
cat > "$ROOT/home/muyu/.config/kwinrc" <<'KWIN'
[Wayland]
InputMethod[$e]=/usr/share/applications/org.fcitx.Fcitx5.desktop
VirtualKeyboardEnabled=true
KWIN

cat > "$ROOT/home/muyu/.config/kcminputrc" <<'KCMIN'
[ButtonRebinds][TabletTool][Novatek NT36532 Stylus]
329=Key,Ctrl+Z
331=MouseButton,273
332=MouseButton,274

[ButtonRebinds][TabletTool][<default>]
329=Key,Ctrl+Z
331=MouseButton,273
332=MouseButton,274

[Libinput][0][0][Novatek NT36532 Stylus]
OutputName=DSI-1
TabletToolRelativeMode=false
KCMIN

cat > "$ROOT/etc/environment" <<'ENV'
XMODIFIERS=@im=fcitx
MOZ_ENABLE_WAYLAND=1
LIBVA_DRIVER_NAME=qcom
MOZ_DISABLE_RDD_SANDBOX=1
ENV

# Enable Hardware WebRender (Adreno 735 / FD735) + Qualcomm Iris VPU3.3 VA-API in Firefox & mpv
mkdir -p "$ROOT/usr/lib/firefox/browser/defaults/preferences" "$ROOT/etc/mpv"
cat > "$ROOT/usr/lib/firefox/browser/defaults/preferences/muyu-video.js" <<'JS'
// Enable Hardware WebRender (Adreno 735 / FD735) + Qualcomm Iris VPU3.3 VA-API
pref("gfx.webrender.all", true);
pref("media.hardware-video-decoding.enabled", true);
pref("media.hardware-video-decoding.force-enabled", true);
pref("media.ffmpeg.vaapi.enabled", true);
pref("media.rdd-ffmpeg.enabled", true);
pref("media.hevc.enabled", true);
pref("media.av1.enabled", true);
pref("widget.dmabuf.force-enabled", true);
JS

cat > "$ROOT/etc/mpv/mpv.conf" <<'MPV'
vo=gpu-next
gpu-api=opengl
hwdec=vaapi
MPV

# Native KDE "Switch to HyperOS" app with confirmation dialog
cat > "$ROOT/usr/local/bin/switch-to-hyperos-kde.sh" <<'SWKDE'
#!/bin/bash
if command -v kdialog >/dev/null 2>&1; then
    kdialog --title "切换系统" --yesno "确定切换到 HyperOS (Slot A) 并重启平板？" || exit 0
fi
exec sudo /usr/local/bin/switch-slot a
SWKDE
chmod +x "$ROOT/usr/local/bin/switch-to-hyperos-kde.sh"

cat > "$ROOT/usr/share/applications/switch-to-hyperos.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=切换回 HyperOS
Comment=Switch active A/B slot to HyperOS (Slot A) and reboot
Exec=/usr/local/bin/switch-to-hyperos-kde.sh
Icon=system-reboot
Terminal=false
Categories=System;
DESK

mkdir -p "$ROOT/home/muyu/Desktop"
cp "$ROOT/usr/share/applications/switch-to-hyperos.desktop" "$ROOT/home/muyu/Desktop/switch-to-hyperos.desktop"
chmod +x "$ROOT/home/muyu/Desktop/switch-to-hyperos.desktop"
chroot "$ROOT" chown -R muyu:muyu /home/muyu

# Enable systemd services
chroot "$ROOT" systemctl enable NetworkManager bluetooth sshd sddm muyu-audio muyu-focus-pen serial-getty@ttyGS0

# Clean pacman cache to keep transfer fast
rm -rf "$ROOT/var/cache/pacman/pkg/"*

echo "=== [5/5] Packing arch_muyu_kde6.tar.zst ==="
cleanup
trap - EXIT
tar --zstd -cpf "$OUT/arch_muyu_kde6.tar.zst" -C "$ROOT" .
ls -lh "$OUT/arch_muyu_kde6.tar.zst"
