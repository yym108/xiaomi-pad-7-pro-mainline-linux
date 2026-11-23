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

CPU_BASE = Path("/sys/devices/system/cpu/cpufreq")
GPU_BASE = Path("/sys/class/devfreq/3d00000.gpu")
BAT_BASE = Path("/sys/class/power_supply/qcom-battmgr-bat")
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
    }


def classify_input_device(name: str) -> str | None:
    low = name.lower()
    if "touchscreen" in low:
        return "touch"
    if "touchpad" in low or "mouse" in low or "keyboard" in low:
        return "desktop"
    if "gpio-keys" in low:
        return "switch"
    return None


def input_monitor_loop() -> None:
    """Monitor touchscreen vs keyboard/touchpad/hall events to auto-switch KWin Touch Mode."""
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
        else:
            print(
                "Usage: muyu-power [save|balance|perf|cycle|status | touch on|off|toggle | touch-auto on|off|toggle]",
                file=sys.stderr,
            )
            return 2
        with urllib.request.urlopen(url, timeout=2) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        print(
            f"mode={d['mode']} ({d['label']}) "
            f"touch={'on' if d['touch_mode'] else 'off'} (auto={'on' if d['touch_auto'] else 'off'}) "
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


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(cli_main(sys.argv[1:]))

    with lock:
        apply_mode_locked(load_saved_mode())
        set_touch_auto_locked(load_bool(TOUCH_AUTO_FILE, True))
        set_touch_mode_locked(load_bool(TOUCH_MODE_FILE, False), source="boot")

    threading.Thread(target=input_monitor_loop, daemon=True).start()
    threading.Thread(target=bt_watchdog_loop, daemon=True).start()
    print(f"[power-mode] listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
