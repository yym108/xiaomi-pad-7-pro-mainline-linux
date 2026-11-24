#!/usr/bin/env python3
"""Xiaomi Pad 7 Pro (muyu / SM8635) Hardware Power & Touch Mode Daemon + CLI.

Power Modes:
  - save    : 1.48 / 1.71 / 1.65 GHz CPU, 500 MHz GPU (cool & long battery)
  - balance : 2.02 / 2.57 / 2.67 GHz CPU, 950 MHz GPU (daily default)
  - perf    : 2.02 / 2.80 / 2.92 GHz CPU, 1000 MHz GPU (full performance)

Touch Mode (KWin TabletMode):
  - Manual toggle: on / off
  - Auto-switch toggle: when enabled, touching the touchscreen switches KWin to
    Touch Mode (larger buttons/spacing), and using the keyboard/touchpad switches
    back to compact desktop mode.

Usage:
  muyu-power-mode.py                          # Run daemon (127.0.0.1:18732)
  muyu-power-mode.py save|balance|perf|cycle|status
  muyu-power-mode.py touch on|off|toggle
  muyu-power-mode.py touch-auto on|off|toggle
"""

import glob
import json
import math
import os
import pwd
import select
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18732
DESKTOP_USER = "muyu"
STATE_DIR = Path("/var/lib/muyu-power-mode")
STATE_FILE = STATE_DIR / "mode"
TOUCH_MODE_FILE = STATE_DIR / "touch_mode"
TOUCH_AUTO_FILE = STATE_DIR / "touch_auto"
ROTATION_LOCK_FILE = STATE_DIR / "rotation_lock"
AUTO_BRIGHTNESS_FILE = STATE_DIR / "auto_brightness"
AUTO_KBD_BACKLIGHT_FILE = STATE_DIR / "auto_kbd_backlight"

KBD_BACKLIGHT_PATH = Path("/sys/class/leds/nanosic::kbd_backlight/brightness")
KBD_BACKLIGHT_MAX_PATH = Path("/sys/class/leds/nanosic::kbd_backlight/max_brightness")

CPU_BASE = Path("/sys/devices/system/cpu/cpufreq")
GPU_BASE = Path("/sys/class/devfreq/3d00000.gpu")
BAT_BASE = Path("/sys/class/power_supply/qcom-battmgr-bat")
USB_BASE = Path("/sys/class/power_supply/qcom-battmgr-usb")
PMIC_OEM_BASE = Path("/sys/bus/auxiliary/devices/pmic_glink.power-supply.0")
CPU_THERMAL_ZONES = [
    Path("/sys/class/thermal/thermal_zone0/temp"),
    Path("/sys/class/thermal/thermal_zone1/temp"),
    Path("/sys/class/thermal/thermal_zone2/temp"),
]

PROFILES = {
    "save": {
        "label": "省电模式",
        "cpu": {
            "policy0": ("schedutil", 364800, 1478400),
            "policy3": ("schedutil", 480000, 1708800),
            "policy7": ("schedutil", 480000, 1651200),
        },
        "gpu": ("simple_ondemand", 255000000, 500000000),
    },
    "balance": {
        "label": "均衡模式",
        "cpu": {
            "policy0": ("schedutil", 364800, 2016000),
            "policy3": ("schedutil", 480000, 2572800),
            "policy7": ("schedutil", 480000, 2668800),
        },
        "gpu": ("simple_ondemand", 255000000, 950000000),
    },
    "perf": {
        "label": "性能模式",
        "cpu": {
            "policy0": ("schedutil", 1017600, 2016000),
            "policy3": ("schedutil", 1190400, 2803200),
            "policy7": ("schedutil", 1209600, 2918400),
        },
        "gpu": ("simple_ondemand", 500000000, 1000000000),
    },
}

MODE_ORDER = ["save", "balance", "perf"]
lock = threading.Lock()
current_mode = "balance"
touch_mode = False
touch_auto = True
rotation_lock = False
auto_brightness = False
auto_kbd_backlight = False
last_als_lux: float | None = None
_last_touch_switch = 0.0
_prev_cpu_total = 0
_prev_cpu_idle = 0


def read_int(path: Path, default: int = 0) -> int:
    try:
        return int(path.read_text().strip())
    except Exception:
        return default


def write_str(path: Path, val: str) -> None:
    try:
        if path.exists():
            path.write_text(f"{val}\n")
    except Exception as e:
        print(f"[power-mode] write {path}={val} failed: {e}", flush=True)


def load_saved_mode() -> str:
    try:
        m = STATE_FILE.read_text().strip()
        if m in PROFILES:
            return m
    except Exception:
        pass
    return "balance"


def load_bool(path: Path, default: bool) -> bool:
    try:
        v = path.read_text().strip().lower()
        if v in ("1", "true", "on", "yes"):
            return True
        if v in ("0", "false", "off", "no"):
            return False
    except Exception:
        pass
    return default


def save_text(path: Path, val: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{val}\n")
    except Exception as e:
        print(f"[power-mode] save {path} failed: {e}", flush=True)


def apply_kwin_tablet_mode(enabled: bool) -> None:
    val = "on" if enabled else "off"
    btn_sz = "ButtonVeryLarge" if enabled else "ButtonDefault"
    try:
        pw = pwd.getpwnam(DESKTOP_USER)
        uid = pw.pw_uid
        bus = f"/run/user/{uid}/bus"
        if not os.path.exists(bus):
            return
        env_prefix = (
            f'XDG_RUNTIME_DIR="/run/user/{uid}" '
            f'DBUS_SESSION_BUS_ADDRESS="unix:path={bus}"'
        )
        cmd = (
            f'{env_prefix} kwriteconfig6 --notify --file kwinrc --group Input --key TabletMode {val}; '
            f'{env_prefix} kwriteconfig6 --file breezerc --group Windeco --key ButtonSize {btn_sz}; '
            f'{env_prefix} qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1'
        )
        subprocess.run(
            ["su", "-s", "/bin/sh", DESKTOP_USER, "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except Exception as e:
        print(f"[power-mode] apply_kwin_tablet_mode({val}) failed: {e}", flush=True)


def set_touch_mode_locked(enabled: bool, source: str = "manual") -> None:
    global touch_mode, _last_touch_switch
    if touch_mode == enabled and source == "auto":
        return
    touch_mode = enabled
    _last_touch_switch = time.monotonic()
    save_text(TOUCH_MODE_FILE, "1" if enabled else "0")
    threading.Thread(target=apply_kwin_tablet_mode, args=(enabled,), daemon=True).start()
    print(f"[power-mode] touch_mode={enabled} ({source})", flush=True)


def set_touch_auto_locked(enabled: bool) -> None:
    global touch_auto
    touch_auto = enabled
    save_text(TOUCH_AUTO_FILE, "1" if enabled else "0")
    print(f"[power-mode] touch_auto={enabled}", flush=True)


def _find_wayland_display(uid: int) -> str | None:
    runtime_dir = Path(f"/run/user/{uid}")
    if not runtime_dir.is_dir():
        return None
    for sock in sorted(runtime_dir.glob("wayland-*")):
        if not sock.name.endswith(".lock"):
            return sock.name
    return None


def _run_kscreen_doctor(*args: str) -> bool:
    try:
        pw = pwd.getpwnam(DESKTOP_USER)
        uid = pw.pw_uid
        wdisp = _find_wayland_display(uid)
        if not wdisp:
            return False
        arg_str = " ".join(args)
        cmd = (
            f'XDG_RUNTIME_DIR="/run/user/{uid}" '
            f'WAYLAND_DISPLAY="{wdisp}" '
            f'kscreen-doctor {arg_str} >/dev/null 2>&1'
        )
        r = subprocess.run(
            ["su", "-s", "/bin/sh", DESKTOP_USER, "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"[power-mode] kscreen-doctor {args} failed: {e}", flush=True)
        return False


def apply_rotation_lock(locked: bool) -> None:
    policy = "never" if locked else "always"
    _run_kscreen_doctor(f"output.DSI-1.autoRotatePolicy.{policy}")


def set_rotation_lock_locked(locked: bool) -> None:
    global rotation_lock
    rotation_lock = locked
    save_text(ROTATION_LOCK_FILE, "1" if locked else "0")
    threading.Thread(target=apply_rotation_lock, args=(locked,), daemon=True).start()
    print(f"[power-mode] rotation_lock={locked}", flush=True)


def apply_auto_brightness(enabled: bool) -> None:
    # Always keep KWin's built-in autoBrightness disabled so it does not fight our ALS loop
    _run_kscreen_doctor("output.DSI-1.autoBrightness.disable")


def set_auto_brightness_locked(enabled: bool) -> None:
    global auto_brightness
    auto_brightness = enabled
    save_text(AUTO_BRIGHTNESS_FILE, "1" if enabled else "0")
    threading.Thread(target=apply_auto_brightness, args=(enabled,), daemon=True).start()
    print(f"[power-mode] auto_brightness={enabled}", flush=True)


def set_auto_kbd_backlight_locked(enabled: bool) -> None:
    global auto_kbd_backlight
    auto_kbd_backlight = enabled
    save_text(AUTO_KBD_BACKLIGHT_FILE, "1" if enabled else "0")
    print(f"[power-mode] auto_kbd_backlight={enabled}", flush=True)


def apply_mode_locked(mode: str) -> None:
    global current_mode
    if mode not in PROFILES:
        mode = "balance"
    prof = PROFILES[mode]

    for policy, (gov, fmin, fmax) in prof["cpu"].items():
        pdir = CPU_BASE / policy
        if not pdir.is_dir():
            continue
        write_str(pdir / "scaling_governor", gov)
        cur_max = read_int(pdir / "scaling_max_freq", fmax)
        if fmin > cur_max:
            write_str(pdir / "scaling_max_freq", str(fmax))
            write_str(pdir / "scaling_min_freq", str(fmin))
        else:
            write_str(pdir / "scaling_min_freq", str(fmin))
            write_str(pdir / "scaling_max_freq", str(fmax))

    if GPU_BASE.is_dir():
        ggov, gmin, gmax = prof["gpu"]
        write_str(GPU_BASE / "governor", ggov)
        cur_gmax = read_int(GPU_BASE / "max_freq", gmax)
        if gmin > cur_gmax:
            write_str(GPU_BASE / "max_freq", str(gmax))
            write_str(GPU_BASE / "min_freq", str(gmin))
        else:
            write_str(GPU_BASE / "min_freq", str(gmin))
            write_str(GPU_BASE / "max_freq", str(gmax))

    current_mode = mode
    save_text(STATE_FILE, mode)
    print(f"[power-mode] applied mode={mode}", flush=True)


def read_cpu_usage_pct() -> int:
    global _prev_cpu_total, _prev_cpu_idle
    try:
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in parts[:8]]
        idle = vals[3] + vals[4]
        total = sum(vals)
        if _prev_cpu_total == 0:
            _prev_cpu_total, _prev_cpu_idle = total, idle
            return 0
        dt = total - _prev_cpu_total
        di = idle - _prev_cpu_idle
        _prev_cpu_total, _prev_cpu_idle = total, idle
        if dt <= 0:
            return 0
        return max(0, min(100, int(round((dt - di) * 100.0 / dt))))
    except Exception:
        return 0


def read_mem_gb() -> tuple[float, float]:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0])
        total_kb = info.get("MemTotal", 0)
        avail_kb = info.get("MemAvailable", 0)
        used_kb = max(0, total_kb - avail_kb)
        return round(used_kb / 1048576.0, 1), round(total_kb / 1048576.0, 1)
    except Exception:
        return 0.0, 0.0


def read_battery_power() -> tuple[str, float, float]:
    status = "Unknown"
    try:
        status = (BAT_BASE / "status").read_text().strip()
    except Exception:
        pass
    uv = read_int(BAT_BASE / "voltage_now", 0)
    ua = read_int(BAT_BASE / "current_now", 0)
    watts = round(abs(uv * ua) / 1e12, 1)
    if status == "Discharging":
        watts = -watts
    bat_temp = round(read_int(BAT_BASE / "temp", 0) / 10.0, 1) if (BAT_BASE / "temp").exists() else 0.0
    return status, watts, bat_temp


def get_status_locked() -> dict:
    c0 = read_int(CPU_BASE / "policy0/scaling_max_freq") // 1000
    c3 = read_int(CPU_BASE / "policy3/scaling_max_freq") // 1000
    c7 = read_int(CPU_BASE / "policy7/scaling_max_freq") // 1000
    gmax = read_int(GPU_BASE / "max_freq") // 1000000
    gcur = read_int(GPU_BASE / "cur_freq") // 1000000
    temps = [read_int(z) for z in CPU_THERMAL_ZONES if z.exists()]
    cpu_temp = round(max(temps) / 1000.0, 1) if temps else 0.0
    bat_status, bat_watts, bat_temp = read_battery_power()
    cpu_pct = read_cpu_usage_pct()
    mem_used_gb, mem_total_gb = read_mem_gb()
    return {
        "mode": current_mode,
        "label": PROFILES[current_mode]["label"],
        "cpu0_max_mhz": c0,
        "cpu3_max_mhz": c3,
        "cpu7_max_mhz": c7,
        "gpu_max_mhz": gmax,
        "gpu_cur_mhz": gcur,
        "cpu_temp_c": cpu_temp,
        "bat_temp_c": bat_temp,
        "bat_status": bat_status,
        "bat_watts": bat_watts,
        "cpu_pct": cpu_pct,
        "mem_used_gb": mem_used_gb,
        "mem_total_gb": mem_total_gb,
        "touch_mode": touch_mode,
        "touch_auto": touch_auto,
        "rotation_lock": rotation_lock,
        "auto_brightness": auto_brightness,
        "auto_kbd_backlight": auto_kbd_backlight,
        "als_lux": round(last_als_lux, 1) if last_als_lux is not None else None,
    }


def classify_input_device(name: str) -> str | None:
    low = name.lower()
    if "touchscreen" in low:
        return "touch"
    if "touchpad" in low or "mouse" in low:
        return "desktop"
    if "gpio-keys" in low:
        return "switch"
    return None


def input_monitor_loop() -> None:
    """Monitor touchscreen vs mouse/touchpad/hall events to auto-switch KWin Touch Mode."""
    EV_KEY = 0x01
    EV_REL = 0x02
    EV_ABS = 0x03
    EV_SW = 0x05
    SW_TABLET_MODE = 0x01
    EV_STRUCT = struct.Struct("llHHI")

    fds: dict[int, tuple[str, str]] = {}

    def refresh_devices() -> None:
        existing_paths = {p for (_, p) in fds.values()}
        for ev_path in sorted(glob.glob("/dev/input/event*")):
            if ev_path in existing_paths:
                continue
            ev_name = os.path.basename(ev_path)
            sys_name_path = Path(f"/sys/class/input/{ev_name}/device/name")
            try:
                dev_name = sys_name_path.read_text().strip()
            except Exception:
                continue
            role = classify_input_device(dev_name)
            if not role:
                continue
            try:
                fd = os.open(ev_path, os.O_RDONLY | os.O_NONBLOCK)
                fds[fd] = (role, ev_path)
            except Exception:
                pass

    last_scan = 0.0
    while True:
        now = time.monotonic()
        if now - last_scan >= 5.0:
            refresh_devices()
            last_scan = now

        if not fds:
            time.sleep(1.0)
            continue

        try:
            rlist, _, _ = select.select(list(fds.keys()), [], [], 1.0)
        except Exception:
            time.sleep(0.5)
            continue

        for fd in rlist:
            role, ev_path = fds.get(fd, (None, ""))
            if not role:
                continue
            try:
                data = os.read(fd, EV_STRUCT.size * 32)
            except BlockingIOError:
                continue
            except OSError:
                try:
                    os.close(fd)
                except Exception:
                    pass
                fds.pop(fd, None)
                continue

            if not data:
                continue

            desired: bool | None = None
            for offset in range(0, len(data) - EV_STRUCT.size + 1, EV_STRUCT.size):
                _, _, etype, code, value = EV_STRUCT.unpack_from(data, offset)
                if role == "touch" and etype in (EV_ABS, EV_KEY):
                    desired = True
                elif role == "desktop" and etype in (EV_KEY, EV_REL, EV_ABS):
                    desired = False
                elif role == "switch" and etype == EV_SW and code == SW_TABLET_MODE:
                    desired = bool(value)

            if desired is not None:
                with lock:
                    if touch_auto and (time.monotonic() - _last_touch_switch >= 0.35):
                        set_touch_mode_locked(desired, source="auto")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        qs = parse_qs(p.query)
        v = qs.get("v", [""])[0].lower()

        if p.path == "/status":
            with lock:
                self._send_json(get_status_locked())
            return

        if p.path == "/mode":
            with lock:
                if v == "cycle":
                    idx = (MODE_ORDER.index(current_mode) + 1) % len(MODE_ORDER)
                    apply_mode_locked(MODE_ORDER[idx])
                    self._send_json(get_status_locked())
                    return
                if v in PROFILES:
                    apply_mode_locked(v)
                    self._send_json(get_status_locked())
                    return

        if p.path == "/touch":
            with lock:
                if v in ("on", "1", "true"):
                    set_touch_mode_locked(True, source="manual")
                elif v in ("off", "0", "false"):
                    set_touch_mode_locked(False, source="manual")
                elif v == "toggle":
                    set_touch_mode_locked(not touch_mode, source="manual")
                else:
                    self._send_json({"error": "invalid value"}, 400)
                    return
                self._send_json(get_status_locked())
            return

        if p.path == "/touch_auto":
            with lock:
                if v in ("on", "1", "true"):
                    set_touch_auto_locked(True)
                elif v in ("off", "0", "false"):
                    set_touch_auto_locked(False)
                elif v == "toggle":
                    set_touch_auto_locked(not touch_auto)
                else:
                    self._send_json({"error": "invalid value"}, 400)
                    return
                self._send_json(get_status_locked())
            return

        if p.path == "/rotation_lock":
            with lock:
                if v in ("on", "1", "true"):
                    set_rotation_lock_locked(True)
                elif v in ("off", "0", "false"):
                    set_rotation_lock_locked(False)
                elif v == "toggle":
                    set_rotation_lock_locked(not rotation_lock)
                else:
                    self._send_json({"error": "invalid value"}, 400)
                    return
                self._send_json(get_status_locked())
            return

        if p.path == "/auto_brightness":
            with lock:
                if v in ("on", "1", "true"):
                    set_auto_brightness_locked(True)
                elif v in ("off", "0", "false"):
                    set_auto_brightness_locked(False)
                elif v == "toggle":
                    set_auto_brightness_locked(not auto_brightness)
                else:
                    self._send_json({"error": "invalid value"}, 400)
                    return
                self._send_json(get_status_locked())
            return

        if p.path == "/auto_kbd_backlight":
            with lock:
                if v in ("on", "1", "true"):
                    set_auto_kbd_backlight_locked(True)
                elif v in ("off", "0", "false"):
                    set_auto_kbd_backlight_locked(False)
                elif v == "toggle":
                    set_auto_kbd_backlight_locked(not auto_kbd_backlight)
                else:
                    self._send_json({"error": "invalid value"}, 400)
                    return
                self._send_json(get_status_locked())
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.do_GET()


def cli_main(argv: list[str]) -> int:
    arg = argv[0]
    try:
        if arg == "status":
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/status"
        elif arg in ("save", "balance", "perf", "cycle"):
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/mode?v={arg}"
        elif arg == "touch" and len(argv) >= 2:
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/touch?v={argv[1]}"
        elif arg == "touch-auto" and len(argv) >= 2:
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/touch_auto?v={argv[1]}"
        elif arg == "rotation-lock" and len(argv) >= 2:
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/rotation_lock?v={argv[1]}"
        elif arg == "auto-brightness" and len(argv) >= 2:
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/auto_brightness?v={argv[1]}"
        elif arg == "kbd-auto" and len(argv) >= 2:
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/auto_kbd_backlight?v={argv[1]}"
        else:
            print(
                "Usage: muyu-power [save|balance|perf|cycle|status | touch|touch-auto|rotation-lock|auto-brightness|kbd-auto on|off|toggle]",
                file=sys.stderr,
            )
            return 2
        with urllib.request.urlopen(url, timeout=2) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        print(
            f"mode={d['mode']} ({d['label']}) "
            f"touch={'on' if d['touch_mode'] else 'off'} (auto={'on' if d['touch_auto'] else 'off'}) "
            f"rot_lock={'on' if d['rotation_lock'] else 'off'} "
            f"auto_bri={'on' if d['auto_brightness'] else 'off'} "
            f"kbd_auto={'on' if d['auto_kbd_backlight'] else 'off'} "
            f"pwr={d['bat_watts']}W cpu={d['cpu_pct']}% soc={d['cpu_temp_c']}C"
        )
        return 0
    except Exception as e:
        print(f"Error contacting daemon: {e}", file=sys.stderr)
        return 1


def bt_watchdog_loop() -> None:
    """Self-healing watchdog for WCN7850 Bluetooth + Focus Pen after S3/s2idle suspend."""
    bad_streak = 0
    while True:
        time.sleep(8)
        try:
            r = subprocess.run(
                ["btmgmt", "info"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if "hci0:" in r.stdout:
                bad_streak = 0
                continue
        except Exception:
            pass

        bad_streak += 1
        # Require 2 consecutive checks (~16s) so we don't fight normal boot/reload
        if bad_streak < 2:
            continue

        print("[bt-watchdog] hci0 missing; rebinding 888000.serial + restarting bluetooth & focus-pen...", flush=True)
        try:
            Path("/sys/bus/platform/drivers/qcom_geni_serial/unbind").write_text("888000.serial\n")
        except Exception:
            pass
        time.sleep(1.0)
        try:
            Path("/sys/bus/platform/drivers/qcom_geni_serial/bind").write_text("888000.serial\n")
        except Exception:
            pass

        for _ in range(6):
            time.sleep(1.0)
            try:
                r = subprocess.run(["btmgmt", "info"], capture_output=True, text=True, timeout=2)
                if "hci0:" in r.stdout:
                    break
            except Exception:
                pass

        subprocess.run(["systemctl", "restart", "bluetooth.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        time.sleep(2.0)
        subprocess.run(["bluetoothctl", "power", "on"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        subprocess.run(["systemctl", "restart", "muyu-focus-pen.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        print("[bt-watchdog] WCN7850 Bluetooth & Focus Pen recovered.", flush=True)
        bad_streak = 0


def _write_sysfs(path: Path, text: str) -> bool:
    try:
        path.write_text(f"{text}\n")
        return True
    except Exception:
        return False


def _set_input_suspend(val: int) -> bool:
    node = PMIC_OEM_BASE / "input_suspend"
    if node.exists():
        return _write_sysfs(node, str(val))
    xm = PMIC_OEM_BASE / "xm_prop"
    if xm.exists():
        return _write_sysfs(xm, f"0x35 {val}")
    return False


def _set_vdm_verified(val: int) -> bool:
    node = PMIC_OEM_BASE / "vdm_verified"
    if node.exists():
        return _write_sysfs(node, str(val))
    xm = PMIC_OEM_BASE / "xm_prop"
    if xm.exists():
        return _write_sysfs(xm, f"0x19 {val}")
    return False


def charging_manager_loop() -> None:
    """Unlock Xiaomi 67W PD_PPS dual charge-pump fast charging (and 24W standard PD fallback).

    - Authenticates stock dual-cell battery (XM_PROP_AUTHENTIC + SLAVE_AUTHENTIC)
      so ADSP lifts the unauthenticated 1.5A/5V fallback cap.
    - On PD plug-in, sends XM_PROP_VDM_CMD_VERIFIED (0x19=1) + pd_verified=1
      once per attach to unlock Xiaomi 67W PD_PPS (11V/6.1A APDO + dual SMB1393
      2:1 charge pumps).
    - For non-Xiaomi standard USB-PD adapters capped at 1.5A on hot-plug while
      advertising >= 2.0A, pulses XM_PROP_INPUT_SUSPEND (0x35) 1 -> 0 once to
      trigger a clean 3.0A ICL re-settlement (~24W).
    - Applies software battery thermal hysteresis via thermal_level while keeping
      ADSP hardware JEITA protections untouched.
    """
    auth_node = PMIC_OEM_BASE / "battery_auth"
    pd_ver_node = PMIC_OEM_BASE / "pd_verified"
    apdo_node = PMIC_OEM_BASE / "apdo_max"
    therm_node = PMIC_OEM_BASE / "thermal_level"
    plug_seen_ts: float | None = None
    unlocked_this_plug = False
    last_therm_lvl: int | None = None

    while True:
        try:
            # 1. Keep stock battery authentication active
            if auth_node.exists():
                try:
                    if auth_node.read_text().strip() != "1":
                        if _write_sysfs(auth_node, "1"):
                            print("[chg-mgr] Enabled battery_auth=1", flush=True)
                except Exception:
                    pass

            # 2. One-shot PD_PPS (67W) / standard PD (24W) unlock on plug-in
            online = read_int(USB_BASE / "online", 0)
            if online == 0:
                plug_seen_ts = None
                unlocked_this_plug = False
            else:
                now = time.monotonic()
                if plug_seen_ts is None:
                    plug_seen_ts = now
                if not unlocked_this_plug and (now - plug_seen_ts) >= 3.0:
                    try:
                        usb_type = (USB_BASE / "usb_type").read_text().strip()
                    except Exception:
                        usb_type = ""

                    if "[PD_PPS]" in usb_type:
                        unlocked_this_plug = True
                    elif "[PD]" in usb_type:
                        # Tier 1: Try Xiaomi UVDM verified -> PD_PPS 67W dual charge pumps
                        if _set_vdm_verified(1):
                            time.sleep(1.0)
                            if pd_ver_node.exists():
                                _write_sysfs(pd_ver_node, "1")
                            time.sleep(3.0)
                            try:
                                usb_type = (USB_BASE / "usb_type").read_text().strip()
                            except Exception:
                                pass

                        if "[PD_PPS]" in usb_type:
                            apdo = read_int(apdo_node, 0)
                            icl = read_int(USB_BASE / "input_current_limit", 0)
                            print(
                                f"[chg-mgr] Unlocked Xiaomi PD_PPS ({apdo}W APDO, ICL={icl/1e6:.2f}A, dual charge pumps)",
                                flush=True,
                            )
                        else:
                            # Tier 2: Standard USB-PD fallback — pulse input_suspend if stuck at 1.5A
                            imax = read_int(USB_BASE / "current_max", 0)
                            icl = read_int(USB_BASE / "input_current_limit", 0)
                            if imax >= 2000000 and 0 < icl <= 1500000:
                                print(
                                    f"[chg-mgr] Standard PD (Imax={imax/1e6:.2f}A) capped at ICL={icl/1e6:.2f}A; "
                                    "pulsing input_suspend 1->0 to re-run AICL...",
                                    flush=True,
                                )
                                if _set_input_suspend(1):
                                    time.sleep(1.5)
                                    _set_input_suspend(0)
                                    time.sleep(2.5)
                                    new_icl = read_int(USB_BASE / "input_current_limit", 0)
                                    print(f"[chg-mgr] Post-pulse ICL={new_icl/1e6:.2f}A", flush=True)
                        unlocked_this_plug = True

            # 3. Battery thermal closed-loop guard (hysteresis)
            if therm_node.exists() and online == 1:
                temp_c = read_int(BAT_BASE / "temp", 0) / 10.0
                target_lvl = last_therm_lvl if last_therm_lvl is not None else 0
                if temp_c >= 43.0:
                    target_lvl = 6
                elif temp_c >= 41.0:
                    target_lvl = max(target_lvl, 3)
                elif temp_c <= 38.0:
                    target_lvl = 0
                if target_lvl != last_therm_lvl:
                    if _write_sysfs(therm_node, str(target_lvl)):
                        print(
                            f"[chg-mgr] Battery temp={temp_c:.1f}C -> thermal_level={target_lvl}",
                            flush=True,
                        )
                        last_therm_lvl = target_lvl
        except Exception:
            pass

        time.sleep(3.0)


def _lux_to_kbd_brightness(lux: float, current_level: int | None) -> int:
    """Map ambient light (lux) to nanosic::kbd_backlight (0..100) with hysteresis."""
    # 4 tiers: bright (>120 lux) -> 0, indoor (55..120) -> 25, dim (18..55) -> 55, dark (<18) -> 85
    if current_level is None:
        if lux >= 120.0:
            return 0
        if lux >= 55.0:
            return 25
        if lux >= 18.0:
            return 55
        return 85

    if current_level == 0:
        if lux < 95.0:
            return _lux_to_kbd_brightness(lux, None)
        return 0
    if current_level == 25:
        if lux >= 135.0:
            return 0
        if lux < 40.0:
            return _lux_to_kbd_brightness(lux, None)
        return 25
    if current_level == 55:
        if lux >= 70.0:
            return _lux_to_kbd_brightness(lux, None)
        if lux < 12.0:
            return 85
        return 55
    # current_level == 85
    if lux >= 26.0:
        return _lux_to_kbd_brightness(lux, None)
    return 85


def _set_kbd_backlight_synced(val: int) -> bool:
    """Set keyboard backlight via PowerDevil (silent, updates KDE slider) with UPower/sysfs fallback."""
    try:
        pw = pwd.getpwnam(DESKTOP_USER)
        uid = pw.pw_uid
        bus = f"/run/user/{uid}/bus"
        if os.path.exists(bus):
            cmd = (
                f'XDG_RUNTIME_DIR="/run/user/{uid}" '
                f'DBUS_SESSION_BUS_ADDRESS="unix:path={bus}" '
                f'qdbus6 org.kde.Solid.PowerManagement '
                f'/org/kde/Solid/PowerManagement/Actions/KeyboardBrightnessControl '
                f'org.kde.Solid.PowerManagement.Actions.KeyboardBrightnessControl.setKeyboardBrightnessSilent {val} '
                f'>/dev/null 2>&1'
            )
            r = subprocess.run(
                ["su", "-s", "/bin/sh", DESKTOP_USER, "-c", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if r.returncode == 0:
                return True
    except Exception:
        pass

    try:
        r = subprocess.run(
            [
                "gdbus", "call", "--system",
                "--dest", "org.freedesktop.UPower",
                "--object-path", "/org/freedesktop/UPower/KbdBacklight",
                "--method", "org.freedesktop.UPower.KbdBacklight.SetBrightness",
                str(val),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass

    return _write_sysfs(KBD_BACKLIGHT_PATH, str(val))


def _set_screen_brightness_synced(val_10k: int) -> bool:
    """Set screen brightness (0..10000) via PowerDevil setBrightnessSilent so KDE slider stays in sync."""
    val_10k = max(500, min(10000, int(val_10k)))
    try:
        pw = pwd.getpwnam(DESKTOP_USER)
        uid = pw.pw_uid
        bus = f"/run/user/{uid}/bus"
        if os.path.exists(bus):
            cmd = (
                f'XDG_RUNTIME_DIR="/run/user/{uid}" '
                f'DBUS_SESSION_BUS_ADDRESS="unix:path={bus}" '
                f'qdbus6 org.kde.Solid.PowerManagement '
                f'/org/kde/Solid/PowerManagement/Actions/BrightnessControl '
                f'org.kde.Solid.PowerManagement.Actions.BrightnessControl.setBrightnessSilent {val_10k} '
                f'>/dev/null 2>&1'
            )
            r = subprocess.run(
                ["su", "-s", "/bin/sh", DESKTOP_USER, "-c", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if r.returncode == 0:
                return True
    except Exception:
        pass
    return False


def _get_screen_brightness_10k() -> int | None:
    try:
        cur = read_int(Path("/sys/class/backlight/ktz8866-backlight/brightness"), -1)
        mx = read_int(Path("/sys/class/backlight/ktz8866-backlight/max_brightness"), 2047)
        if cur >= 0 and mx > 0:
            return int(round(cur * 10000.0 / mx))
    except Exception:
        pass
    return None


def _lux_to_screen_brightness_10k(lux: float) -> int:
    """Map ambient light (lux) to PowerDevil screen brightness (0..10000) on a natural log curve."""
    pct = 10.0 + 27.0 * math.log10(max(0.0, lux) + 1.0)
    pct = max(10.0, min(100.0, pct))
    return int(round(pct * 100.0))


def als_kbd_backlight_loop() -> None:
    """Auto-adjust screen brightness & keyboard backlight from iio-sensor-proxy ALS."""
    global last_als_lux
    proc: subprocess.Popen | None = None
    last_kbd_level: int | None = None
    last_scr_target: int | None = None
    last_scr_applied: int | None = None
    manual_pause_until = 0.0
    screen_was_off = False
    last_wall = time.time()
    last_mono = time.monotonic()

    def reset_screen_state() -> None:
        nonlocal last_scr_target, last_scr_applied, manual_pause_until
        last_scr_target = None
        last_scr_applied = None
        manual_pause_until = 0.0

    def apply_screen_for_lux(lux_val: float, force: bool = False) -> None:
        nonlocal last_scr_target, last_scr_applied, manual_pause_until, screen_was_off
        cur_bri = _get_screen_brightness_10k()
        if cur_bri is not None and cur_bri <= 0:
            screen_was_off = True
            reset_screen_state()
            return

        now = time.monotonic()
        if screen_was_off:
            screen_was_off = False
            reset_screen_state()
            force = True

        # Detect manual slider drag only when screen was already on and steady
        if (
            not force
            and last_scr_applied is not None
            and cur_bri is not None
            and abs(cur_bri - last_scr_applied) >= 700
        ):
            manual_pause_until = now + 12.0
            last_scr_applied = cur_bri
            last_scr_target = cur_bri
            return

        if not force and now < manual_pause_until:
            return

        target = _lux_to_screen_brightness_10k(lux_val)
        if force or last_scr_target is None or abs(target - last_scr_target) >= 350:
            start_bri = cur_bri if cur_bri is not None else target
            steps = 3 if abs(target - start_bri) >= 1200 else 1
            ok = False
            for s in range(1, steps + 1):
                step_val = int(round(start_bri + (target - start_bri) * (s / steps)))
                if _set_screen_brightness_synced(step_val):
                    ok = True
                if s < steps:
                    time.sleep(0.08)
            if ok:
                actual = _get_screen_brightness_10k()
                last_scr_applied = actual if (actual is not None and actual > 0) else target
                last_scr_target = target
                print(
                    f"[als-scr] lux={lux_val:.1f} -> screen_brightness={target/100:.0f}%",
                    flush=True,
                )

    while True:
        now_wall = time.time()
        now_mono = time.monotonic()
        # Detect system suspend/resume (wall clock jumped ahead of monotonic clock)
        if (now_wall - last_wall) - (now_mono - last_mono) > 5.0:
            reset_screen_state()
            last_kbd_level = None
            screen_was_off = True
        last_wall = now_wall
        last_mono = now_mono

        with lock:
            en_scr = auto_brightness
            en_kbd = auto_kbd_backlight

        if not en_scr and not en_kbd:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                proc = None
            last_kbd_level = None
            reset_screen_state()
            time.sleep(0.5)
            continue

        if not en_kbd:
            last_kbd_level = None
        if not en_scr:
            reset_screen_state()

        if proc is None or proc.poll() is not None:
            try:
                proc = subprocess.Popen(
                    ["monitor-sensor", "--light"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except Exception as e:
                print(f"[als] failed to start monitor-sensor: {e}", flush=True)
                time.sleep(3.0)
                continue

        try:
            rlist, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not rlist:
                # Even without new ALS events, catch screen-off / screen-on transitions
                if en_scr:
                    cur_bri = _get_screen_brightness_10k()
                    if cur_bri is not None and cur_bri <= 0:
                        if not screen_was_off:
                            screen_was_off = True
                            reset_screen_state()
                    elif screen_was_off and last_als_lux is not None:
                        apply_screen_for_lux(last_als_lux, force=True)
                continue

            line = proc.stdout.readline()
            if not line:
                proc = None
                time.sleep(1.0)
                continue
            lux_val: float | None = None
            if "Light changed:" in line:
                lux_val = float(line.split("Light changed:", 1)[1].strip().split()[0])
            elif "Has ambient light sensor (value:" in line:
                raw = line.split("value:", 1)[1].split(",", 1)[0].strip()
                v = float(raw)
                if v > 0.0:
                    lux_val = v
            if lux_val is None:
                continue

            with lock:
                last_als_lux = lux_val
                en_scr = auto_brightness
                en_kbd = auto_kbd_backlight

            # 1. Screen auto-brightness (via PowerDevil setBrightnessSilent)
            if en_scr:
                apply_screen_for_lux(lux_val, force=False)

            # 2. Keyboard auto-backlight (via PowerDevil setKeyboardBrightnessSilent)
            if en_kbd and KBD_BACKLIGHT_PATH.exists():
                target = _lux_to_kbd_brightness(lux_val, last_kbd_level)
                if target != last_kbd_level:
                    max_b = read_int(KBD_BACKLIGHT_MAX_PATH, 100)
                    scaled = max(0, min(max_b, int(round(target * max_b / 100.0))))
                    if _set_kbd_backlight_synced(scaled):
                        print(
                            f"[als-kbd] lux={lux_val:.1f} -> kbd_backlight={scaled}/{max_b}",
                            flush=True,
                        )
                        last_kbd_level = target
        except Exception as e:
            print(f"[als] loop error: {e}", flush=True)
            time.sleep(1.0)


def desktop_session_sync_loop() -> None:
    """Wait for Wayland session after boot and apply persisted rotation_lock & auto_brightness."""
    for _ in range(60):
        try:
            pw = pwd.getpwnam(DESKTOP_USER)
            if _find_wayland_display(pw.pw_uid):
                time.sleep(2.0)
                with lock:
                    rl = rotation_lock
                    ab = auto_brightness
                apply_rotation_lock(rl)
                apply_auto_brightness(ab)
                print(f"[power-mode] synced display state on session ready: rot_lock={rl}, auto_bri={ab}", flush=True)
                return
        except Exception:
            pass
        time.sleep(1.0)


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(cli_main(sys.argv[1:]))

    with lock:
        apply_mode_locked(load_saved_mode())
        set_touch_auto_locked(load_bool(TOUCH_AUTO_FILE, True))
        set_touch_mode_locked(load_bool(TOUCH_MODE_FILE, False), source="boot")
        set_rotation_lock_locked(load_bool(ROTATION_LOCK_FILE, False))
        set_auto_brightness_locked(load_bool(AUTO_BRIGHTNESS_FILE, False))
        set_auto_kbd_backlight_locked(load_bool(AUTO_KBD_BACKLIGHT_FILE, False))

    threading.Thread(target=input_monitor_loop, daemon=True).start()
    # Note: WCN7850 BT sleep/resume is exclusively owned by /usr/lib/systemd/system-sleep/muyu-bt-resume.
    # Running a concurrent bt_watchdog_loop races with pre-suspend unbind and can deadlock hci_unregister_dev.
    threading.Thread(target=charging_manager_loop, daemon=True).start()
    threading.Thread(target=als_kbd_backlight_loop, daemon=True).start()
    threading.Thread(target=desktop_session_sync_loop, daemon=True).start()
    print(f"[power-mode] listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
