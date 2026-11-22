#!/usr/bin/env python3
"""Xiaomi Pad 7 Pro (muyu / SM8635) Hardware Power & Performance Mode Daemon + CLI.

Modes:
  - save    : 1.48 / 1.71 / 1.65 GHz CPU, 500 MHz GPU (cool & long battery)
  - balance : 2.02 / 2.57 / 2.67 GHz CPU, 950 MHz GPU (daily default)
  - perf    : 2.02 / 2.80 / 2.92 GHz CPU, 1000 MHz GPU (full performance)

Usage:
  muyu-power-mode.py              # Run daemon (HTTP JSON API on 127.0.0.1:18732)
  muyu-power-mode.py <mode>       # CLI: save | balance | perf | cycle | status
"""

import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18732
STATE_DIR = Path("/var/lib/muyu-power-mode")
STATE_FILE = STATE_DIR / "mode"

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
_prev_cpu_total = 0
_prev_cpu_idle = 0


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


def save_mode(mode: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(f"{mode}\n")
    except Exception as e:
        print(f"[power-mode] save_mode failed: {e}", flush=True)


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
        # Raise max before min, or lower min before max, to avoid EINVAL
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
    save_mode(mode)
    print(f"[power-mode] applied mode={mode}", flush=True)


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
    }


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
        if p.path == "/status":
            with lock:
                self._send_json(get_status_locked())
            return
        if p.path == "/mode":
            qs = parse_qs(p.query)
            v = qs.get("v", [""])[0]
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
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.do_GET()


def cli_main(arg: str) -> int:
    # Try daemon HTTP API first (works without sudo!)
    try:
        if arg == "status":
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/status"
        elif arg in ("save", "balance", "perf", "cycle"):
            url = f"http://{LISTEN_HOST}:{LISTEN_PORT}/mode?v={arg}"
        else:
            print("Usage: muyu-power [save|balance|perf|cycle|status]", file=sys.stderr)
            return 2
        with urllib.request.urlopen(url, timeout=2) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        print(
            f"mode={d['mode']} ({d['label']}) "
            f"cpu={d['cpu0_max_mhz']}/{d['cpu3_max_mhz']}/{d['cpu7_max_mhz']}MHz "
            f"gpu={d['gpu_cur_mhz']}/{d['gpu_max_mhz']}MHz "
            f"soc={d['cpu_temp_c']}C bat={d['bat_temp_c']}C"
        )
        return 0
    except Exception:
        # Fallback to direct sysfs when running as root without daemon
        if os.geteuid() != 0:
            print("Daemon not running; run with sudo or start muyu-power-mode.service", file=sys.stderr)
            return 1
        with lock:
            if arg in PROFILES:
                apply_mode_locked(arg)
            elif arg == "cycle":
                m = load_saved_mode()
                apply_mode_locked(MODE_ORDER[(MODE_ORDER.index(m) + 1) % len(MODE_ORDER)])
            else:
                apply_mode_locked(load_saved_mode())
            d = get_status_locked()
        print(json.dumps(d, ensure_ascii=False))
        return 0


def main() -> None:
    if len(sys.argv) > 1:
        sys.exit(cli_main(sys.argv[1]))

    with lock:
        apply_mode_locked(load_saved_mode())
    print(f"[power-mode] listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
