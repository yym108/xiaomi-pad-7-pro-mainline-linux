#!/bin/bash
set -euo pipefail

# Run inside Docker muyu-kernel-builder:latest (native arm64 Ubuntu 24.04)
WORK="/phase3"
ROOT="/tmp/ubuntu-noble-arm64"
OUT="$WORK/out"
mkdir -p "$OUT"

apt-get update -qq
apt-get install -y -qq mmdebstrap squashfs-tools >/dev/null

rm -rf "$ROOT"
echo "=== Building Ubuntu 24.04 (noble) ARM64 rootfs with Mesa 25.2 + Weston 13 + PulseAudio + APT ==="

MIRROR="http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports"
mmdebstrap --arch=arm64 --variant=minbase \
    --components="main restricted universe multiverse" \
    --include="apt,ubuntu-keyring,udev,dbus,dbus-x11,seatd,weston,foot,fonts-dejavu-core,fonts-wqy-zenhei,libgl1-mesa-dri,mesa-vulkan-drivers,libegl-mesa0,libgles2,libgbm1,mesa-utils,vulkan-tools,glmark2-wayland,alsa-utils,pulseaudio,pulseaudio-utils,ffmpeg,libavcodec-extra,libgtk-3-0t64,libdbus-glib-1-2,libxt6t64,libinput-tools,evtest,iproute2,iputils-ping,openssh-server,wpasupplicant,iw,rfkill,udhcpc,bluez,curl,xz-utils,nano,procps,pciutils,usbutils,i2c-tools,ca-certificates,kmod" \
    noble "$ROOT" \
    "deb $MIRROR noble main restricted universe multiverse" \
    "deb $MIRROR noble-updates main restricted universe multiverse" \
    "deb $MIRROR noble-security main restricted universe multiverse"

echo "=== Configuring Xiaomi Pad 7 Pro (muyu) rootfs ==="

echo "muyu-linux" > "$ROOT/etc/hostname"
cat > "$ROOT/etc/hosts" <<'HOSTS'
127.0.0.1 localhost
127.0.0.1 muyu-linux
HOSTS

# Root password: muyu
chroot "$ROOT" bash -c 'echo "root:muyu" | chpasswd'

# SSH config for USB-NCM (172.16.42.1)
mkdir -p "$ROOT/run/sshd" "$ROOT/etc/ssh/sshd_config.d"
cat > "$ROOT/etc/ssh/sshd_config.d/99-muyu.conf" <<'SSHD'
PermitRootLogin yes
PasswordAuthentication yes
SSHD
chroot "$ROOT" ssh-keygen -A

# Weston configuration for 3200x2136 Dual-DSI HiDPI tablet display + touch
mkdir -p "$ROOT/etc/xdg/weston"
cat > "$ROOT/etc/xdg/weston/weston.ini" <<'WESTON'
[core]
backend=drm-backend.so
idle-time=0

[output]
name=DSI-1
scale=2

[shell]
background-color=0xff1b263b
panel-color=0xcc0d1b2a
locking=false
clock-format=seconds

[launcher]
icon=/usr/share/weston/icon_terminal.png
path=/usr/bin/foot

[launcher]
icon=/usr/share/weston/icon_flower.png
path=/usr/bin/weston-simple-egl
WESTON

# Audio init & PulseAudio daemon (Quad AW88261 + AudioReach VA LPAIF SD1)
mkdir -p "$ROOT/usr/local/bin" "$ROOT/etc/pulse"
cat > "$ROOT/etc/pulse/muyu.pa" <<'PAEOF'
load-module module-native-protocol-unix auth-anonymous=1
load-module module-alsa-sink device=hw:0,0 rate=48000 channels=2 format=s16le tsched=0 mmap=0 fragments=4 fragment_size=9600 sink_name=muyu_spkr
set-default-sink muyu_spkr
PAEOF

cat > "$ROOT/usr/local/bin/start-audio.sh" <<'SAUD'
#!/bin/bash
set -e
export XDG_RUNTIME_DIR=/run/user/0
mkdir -p "$XDG_RUNTIME_DIR/pulse"
chmod 0700 "$XDG_RUNTIME_DIR"

# Configure AudioReach QUINARY_MI2S_RX + Quad AW88261 SmartPAs
amixer -c 0 cset name='QUINARY_MI2S_RX Audio Mixer MultiMedia1' 1,1 >/dev/null 2>&1 || true
amixer -c 0 cset name='stream0.vol_ctrl0 MultiMedia1 Playback Volu' 8192 >/dev/null 2>&1 || true
for pfx in SpkrTopLeft SpkrBotLeft SpkrTopRight SpkrBotRight; do
    amixer -c 0 cset name="$pfx Profile Set" 0 >/dev/null 2>&1 || true
    amixer -c 0 cset name="$pfx PCM Playback Volume" 295 >/dev/null 2>&1 || true
    amixer -c 0 cset name="$pfx Playback Switch" 1 >/dev/null 2>&1 || true
done

if ! pgrep -x pulseaudio >/dev/null 2>&1; then
    pulseaudio --system=false --disallow-exit --exit-idle-time=-1 \
        -n -F /etc/pulse/muyu.pa -D --log-target=file:/tmp/pulse.log 2>&1 || true
fi
echo "[muyu] Audio ready (Quad AW88261 + PulseAudio sink: muyu_spkr)"
SAUD
chmod +x "$ROOT/usr/local/bin/start-audio.sh"

# Audio test script (Quad AW88261 + AudioReach QUINARY_MI2S_RX)
cat > "$ROOT/usr/local/bin/test-audio.sh" <<'AUD'
#!/bin/bash
set -e
echo "=== ALSA Cards ==="
cat /proc/asound/cards
echo "=== ALSA PCMs ==="
cat /proc/asound/pcm
start-audio.sh
echo "=== PulseAudio Sinks ==="
XDG_RUNTIME_DIR=/run/user/0 pactl list short sinks || true
echo "=== Playing 440Hz test tone via PulseAudio ==="
XDG_RUNTIME_DIR=/run/user/0 paplay --raw --rate=48000 --channels=2 --format=s16le /dev/null 2>/dev/null || true
pkill -x pulseaudio 2>/dev/null || true
speaker-test -D hw:0,0 -c 2 -r 48000 -F S16_LE -t sine -f 440 -l 1 -p 48000 || true
start-audio.sh >/dev/null 2>&1 || true
AUD
chmod +x "$ROOT/usr/local/bin/test-audio.sh"

# Wi-Fi helper (WCN7850 5GHz/6GHz WPA2/WPA3-SAE + DHCP + HTTP time sync)
cat > "$ROOT/usr/local/bin/wifi-connect.sh" <<'WIFI'
#!/bin/bash
set -e
SSID="${1:-}"
PSK="${2:-}"
if [ -z "$SSID" ] || [ -z "$PSK" ]; then
    echo "Usage: wifi-connect.sh <SSID> <PASSWORD>"
    exit 1
fi
rfkill unblock wifi 2>/dev/null || true
IFACE=$(ls /sys/class/net | grep -E '^(wlp|wlan)' | head -n 1)
if [ -z "$IFACE" ]; then
    echo "[muyu] No Wi-Fi interface found"
    exit 1
fi
ip link set "$IFACE" up
pkill -9 wpa_supplicant 2>/dev/null || true
rm -rf /run/wpa_supplicant
mkdir -p /run/wpa_supplicant
cat > /tmp/wpa.conf <<EOF
ctrl_interface=/run/wpa_supplicant
country=CN
sae_pwe=2

network={
    ssid="$SSID"
    psk="$PSK"
    key_mgmt=WPA-PSK SAE
    ieee80211w=1
}
EOF
wpa_supplicant -B -i "$IFACE" -c /tmp/wpa.conf -f /tmp/wpa.log
for i in $(seq 1 30); do
    if iw dev "$IFACE" link | grep -q "Connected to"; then
        break
    fi
    sleep 0.5
done
iw dev "$IFACE" link
cat > /tmp/udhcpc.script <<'DHCP'
#!/bin/sh
case "$1" in
  deconfig) ip addr flush dev "$interface" ;;
  bound|renew)
    ip addr flush dev "$interface"
    ip addr add "$ip/$mask" dev "$interface"
    [ -n "$router" ] && ip route replace default via "$router" dev "$interface"
    : > /etc/resolv.conf
    for d in $dns; do echo "nameserver $d" >> /etc/resolv.conf; done
    ;;
esac
DHCP
chmod +x /tmp/udhcpc.script
udhcpc -i "$IFACE" -s /tmp/udhcpc.script -n -q
# Sync clock from HTTP Date header so apt/TLS work immediately
HTTP_DATE=$(curl -sI --max-time 5 http://mirrors.tuna.tsinghua.edu.cn | grep -i '^date:' | sed 's/^[Dd]ate: //;s/\r//')
[ -n "$HTTP_DATE" ] && date -u -s "$HTTP_DATE" >/dev/null 2>&1 || true
ip -4 addr show "$IFACE"
WIFI
chmod +x "$ROOT/usr/local/bin/wifi-connect.sh"

# Bluetooth helper (WCN7850 BT 5.4)
cat > "$ROOT/usr/local/bin/bt-setup.sh" <<'BT'
#!/bin/bash
set -e
rfkill unblock bluetooth 2>/dev/null || true
mkdir -p /run/dbus
if ! pgrep -x dbus-daemon >/dev/null 2>&1; then
    rm -f /run/dbus/pid /run/dbus/system_bus_socket
    dbus-daemon --system --fork
fi
if ! pgrep -x bluetoothd >/dev/null 2>&1; then
    nohup /usr/libexec/bluetooth/bluetoothd --experimental >/tmp/bluetoothd.log 2>&1 </dev/null &
    sleep 1.2
fi
bluetoothctl power on >/dev/null 2>&1 || true
if [ -n "${1:-}" ]; then
    echo "[muyu] Scanning 8s for '$1'..."
    bluetoothctl --timeout 8 scan on >/dev/null 2>&1 || true
    MAC=$(bluetoothctl devices | grep -i "$1" | head -n 1 | awk '{print $2}')
    if [ -n "$MAC" ]; then
        echo "[muyu] Pairing & connecting $MAC..."
        (echo "pair $MAC"; sleep 4; echo "trust $MAC"; echo "connect $MAC"; sleep 3; echo "quit") | bluetoothctl
        SYSTEMD_IGNORE_CHROOT=1 udevadm trigger --action=add --subsystem-match=input 2>/dev/null || true
    else
        echo "[muyu] Device '$1' not found. Available devices:"
        bluetoothctl devices
    fi
else
    echo "[muyu] Bluetooth ready (hci0). Run: bt-setup.sh <name_or_mac> or bluetoothctl"
fi
BT
chmod +x "$ROOT/usr/local/bin/bt-setup.sh"

# Firefox ARM64 (zh-CN) installer & launcher
cat > "$ROOT/usr/local/bin/install-firefox.sh" <<'FF'
#!/bin/bash
set -e
if [ "$(date +%Y)" -lt 2025 ]; then
    HTTP_DATE=$(curl -sI --max-time 5 http://mirrors.tuna.tsinghua.edu.cn | grep -i '^date:' | sed 's/^[Dd]ate: //;s/\r//')
    [ -n "$HTTP_DATE" ] && date -u -s "$HTTP_DATE" >/dev/null 2>&1 || true
fi
if [ ! -x /opt/firefox/firefox ]; then
    echo "[muyu] Downloading Firefox ARM64 (zh-CN)..."
    curl -fL "https://download.mozilla.org/?product=firefox-latest-ssl&os=linux64-aarch64&lang=zh-CN" -o /tmp/firefox.tar.xz
    mkdir -p /opt
    tar -xJf /tmp/firefox.tar.xz -C /opt
    rm -f /tmp/firefox.tar.xz
    ln -sf /opt/firefox/firefox /usr/local/bin/firefox
fi
mkdir -p /opt/firefox/defaults/pref
cat > /opt/firefox/defaults/pref/autoconfig.js <<'CFG'
pref("general.config.filename", "muyu-hwdec.cfg");
pref("general.config.obscure_value", 0);
CFG
cat > /opt/firefox/muyu-hwdec.cfg <<'MCFG'
// Muyu Iris VPU3.0 & Adreno 735 HW acceleration defaults
defaultPref("gfx.webrender.all", true);
defaultPref("widget.dmabuf.force-enabled", true);
defaultPref("media.hardware-video-decoding.enabled", true);
defaultPref("media.hardware-video-decoding.force-enabled", true);
defaultPref("media.ffmpeg.vaapi.enabled", true);
defaultPref("media.hevc.enabled", true);
defaultPref("media.av1.enabled", false);
MCFG
start-audio.sh >/dev/null 2>&1 || true
export XDG_RUNTIME_DIR=/run/user/0
export WAYLAND_DISPLAY=$(ls /run/user/0/wayland-* 2>/dev/null | head -n1 | xargs basename 2>/dev/null || echo wayland-0)
export MOZ_ENABLE_WAYLAND=1
export MOZ_WEBRENDER=1
export MOZ_DISABLE_CONTENT_SANDBOX=1
export MOZ_DISABLE_GMP_SANDBOX=1
export MOZ_DISABLE_RDD_SANDBOX=1
export MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1
export HOME=/root
URL="${1:-https://www.bilibili.com}"
setsid dbus-run-session -- /opt/firefox/firefox "$URL" >/tmp/firefox.log 2>&1 </dev/null &
echo "[muyu] Firefox launched ($URL)"
FF
chmod +x "$ROOT/usr/local/bin/install-firefox.sh"

# GPU test script (Freedreno EGL/GLES + Turnip Vulkan)
cat > "$ROOT/usr/local/bin/test-gpu.sh" <<'GPU'
#!/bin/bash
echo "=== DRM Devices ==="
ls -la /dev/dri/
echo "=== EGL Info (GBM / Freedreno) ==="
eglinfo -B 2>&1 | head -n 60
echo "=== Vulkan Summary (Turnip) ==="
vulkaninfo --summary 2>&1 | head -n 60
GPU
chmod +x "$ROOT/usr/local/bin/test-gpu.sh"

# Weston start script
cat > "$ROOT/usr/local/bin/start-weston.sh" <<'WSTART'
#!/bin/bash
set -e
export XDG_RUNTIME_DIR=/run/user/0
mkdir -p "$XDG_RUNTIME_DIR"
chmod 0700 "$XDG_RUNTIME_DIR"
export LIBSEAT_BACKEND=seatd
export WLR_RENDERER=gles2

# Stop any previous weston
pkill -x weston 2>/dev/null || true
sleep 0.3

echo "[muyu] Starting Weston 13 on DSI-1 (3200x2136 @ scale=2)..."
weston --config=/etc/xdg/weston/weston.ini --log=/tmp/weston.log >/tmp/weston.out 2>&1 &
WPID=$!
for i in $(seq 1 30); do
    WSOCK=$(ls "$XDG_RUNTIME_DIR"/wayland-[0-9]* 2>/dev/null | grep -v '\.lock$' | head -n1 || true)
    if [ -n "$WSOCK" ] && [ -S "$WSOCK" ]; then
        echo "[muyu] Weston ready (PID=$WPID, WAYLAND_DISPLAY=$(basename "$WSOCK"))"
        exit 0
    fi
    sleep 0.2
done
echo "[muyu] Weston did not create wayland socket in time; log:"
cat /tmp/weston.log /tmp/weston.out 2>/dev/null || true
exit 1
WSTART
chmod +x "$ROOT/usr/local/bin/start-weston.sh"

# Xiaomi Focus Pen (P81c) BLE 8192-level pressure bridge & udev rule
mkdir -p "$ROOT/etc/udev/rules.d"
cat > "$ROOT/etc/udev/rules.d/99-xiaomi-focus-pen.rules" <<'PENUDEV'
# Ignore Xiaomi Focus Pen bogus BLE HID keyboard status keycodes; real stylus
# coordinates, 8192-level pressure, tilt and side buttons come via NT36532 Stylus.
ATTRS{name}=="Xiaomi Focus Pen*", ENV{LIBINPUT_IGNORE_DEVICE}="1"
PENUDEV
if [ -f "$WORK/focus_pen_ble.py" ]; then
    cp "$WORK/focus_pen_ble.py" "$ROOT/usr/local/bin/focus_pen_ble.py"
    chmod +x "$ROOT/usr/local/bin/focus_pen_ble.py"
fi

# Lightweight Phase-3 init inside Ubuntu rootfs
cat > "$ROOT/sbin/muyu-init" <<'INIT'
#!/bin/bash
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root
export TERM=xterm-256color

hostname -F /etc/hostname 2>/dev/null || true
mkdir -p /run/dbus /run/sshd /run/udev /run/user/0 /tmp
chmod 1777 /tmp
chmod 0700 /run/user/0

# Start udevd so libinput & DRM & ALSA devices get proper udev metadata
/lib/systemd/systemd-udevd --daemon 2>/dev/null || /usr/lib/systemd/systemd-udevd --daemon 2>/dev/null || true
sleep 0.3
SYSTEMD_IGNORE_CHROOT=1 udevadm trigger --action=add 2>/dev/null || true
SYSTEMD_IGNORE_CHROOT=1 udevadm settle --timeout=5 2>/dev/null || true

# Start dbus-daemon
dbus-daemon --system --fork 2>/dev/null || true

# Start seatd for Wayland DRM/input access
seatd -u root -g root >/tmp/seatd.log 2>&1 &
sleep 0.3

# Start sshd on USB-NCM (172.16.42.1)
/usr/sbin/sshd 2>/dev/null || true

# Ensure V4L2 video codec nodes are accessible
chmod 666 /dev/video* 2>/dev/null || true

# Initialize Quad AW88261 speakers + PulseAudio sink
/usr/local/bin/start-audio.sh >/tmp/audio-init.log 2>&1 || true

# Auto-start Bluetooth & Xiaomi Focus Pen (P81c) 8192-level BLE pressure bridge
rfkill unblock bluetooth 2>/dev/null || true
if ! pgrep -x bluetoothd >/dev/null 2>&1; then
    nohup /usr/libexec/bluetooth/bluetoothd --experimental >/tmp/bluetoothd.log 2>&1 </dev/null &
    sleep 1
fi
hciconfig hci0 up 2>/dev/null || true
bluetoothctl power on >/dev/null 2>&1 || true
if [ -x /usr/local/bin/focus_pen_ble.py ] && ! pgrep -f focus_pen_ble.py >/dev/null 2>&1; then
    nohup /usr/local/bin/focus_pen_ble.py >/tmp/focus_pen.log 2>&1 </dev/null &
fi

echo "=== UBUNTU 24.04 ARM64 ON XIAOMI PAD 7 PRO (MUYU) READY ==="
echo "  SSH: root@172.16.42.1 (password: muyu)"
echo "  Commands: start-weston.sh | wifi-connect.sh | bt-setup.sh | install-firefox.sh | test-gpu.sh | test-audio.sh"
exec /bin/bash -i
INIT
chmod +x "$ROOT/sbin/muyu-init"

# Clean apt caches to keep squashfs compact
rm -rf "$ROOT/var/lib/apt/lists/"* "$ROOT/var/cache/apt/archives/"*.deb "$ROOT/usr/share/doc/"* "$ROOT/usr/share/man/"*

echo "=== Packing SquashFS (zstd) ==="
rm -f "$OUT/rootfs.squashfs"
mksquashfs "$ROOT" "$OUT/rootfs.squashfs" -comp zstd -Xcompression-level 15 -noappend -processors "$(nproc)"
ls -lh "$OUT/rootfs.squashfs"
