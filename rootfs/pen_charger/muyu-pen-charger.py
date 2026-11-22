#!/usr/bin/env python3
"""Xiaomi Pad 7 Pro (muyu) Focus Pen Magnetic Qi Wireless Charging Daemon.

- Auto-enables Qi TX when Focus Pen snaps onto the magnetic dock (pen_hall3 == 0)
- Auto-disables Qi TX when detached (pen_hall3 == 1)
- Sends native KDE Plasma notifications via notify-send
- Exposes a local localhost-only HTTP JSON API on 127.0.0.1:18731 for the KDE Plasmoid
"""

import json
import os
import pwd
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

SYSFS = "/sys/bus/auxiliary/devices/pmic_glink.power-supply.0"
STATE_FILE = "/run/muyu-pen-charger.json"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18731
DESKTOP_USER = "muyu"

state_lock = threading.Lock()
state = {
    "attached": False,
    "charging": False,
    "pen_soc": -1,
    "tx_vout": 0,
    "tx_iout": 0,
    "power_mw": 0,
    "place_err": 0,
    "reverse_chg_state": 0,
    "reverse_chg_mode": 0,
    "mode": "auto",  # "auto" | "on" | "off"
}


def read_int(name: str, default: int = 0) -> int:
    try:
        with open(f"{SYSFS}/{name}", "r") as f:
            return int(f.read().strip())
    except Exception:
        return default


def write_int(name: str, val: int) -> None:
    try:
        with open(f"{SYSFS}/{name}", "w") as f:
            f.write(f"{val}\n")
    except Exception as e:
        print(f"[pen-charger] write {name}={val} failed: {e}", flush=True)


def notify_kde(title: str, body: str, icon: str = "battery-charging") -> None:
    try:
        pw = pwd.getpwnam(DESKTOP_USER)
        uid = pw.pw_uid
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        subprocess.Popen(
            [
                "su",
                "-s",
                "/bin/sh",
                DESKTOP_USER,
                "-c",
                f'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/{uid}/bus" '
                f'notify-send -a "焦点触控笔" -i "{icon}" -t 3500 "{title}" "{body}"',
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def apply_mode_locked() -> None:
    mode = state["mode"]
    attached = state["attached"]
    place_err = state["place_err"]
    want_tx = 1 if (mode == "on" or (mode == "auto" and attached and place_err == 0)) else 0
    if state["reverse_chg_mode"] != want_tx:
        write_int("reverse_chg_mode", want_tx)
        state["reverse_chg_mode"] = want_tx
        print(f"[pen-charger] set reverse_chg_mode={want_tx} (mode={mode}, attached={attached})", flush=True)


def poll_loop() -> None:
    prev_attached = None
    prev_place_err = 0
    notified_soc = False

    while True:
        hall3 = read_int("pen_hall3", 1)
        attached = (hall3 == 0)
        place_err = read_int("pen_place_err", 0)
        raw_soc = read_int("pen_soc", 255)
        tx_vout = read_int("tx_vout", 0)
        tx_iout = read_int("tx_iout", 0)
        chg_state = read_int("reverse_chg_state", 0)
        chg_mode = read_int("reverse_chg_mode", 0)

        with state_lock:
            state["attached"] = attached
            state["place_err"] = place_err
            if 0 <= raw_soc <= 100:
                state["pen_soc"] = raw_soc
            state["tx_vout"] = tx_vout
            state["tx_iout"] = tx_iout
            state["power_mw"] = int(tx_vout * tx_iout / 1000)
            state["reverse_chg_state"] = chg_state
            state["reverse_chg_mode"] = chg_mode
            apply_mode_locked()
            state["charging"] = bool(state["reverse_chg_mode"] == 1 and attached and (tx_vout > 1000 or chg_state in (1, 3, 4)))
            snap = dict(state)

        # Write JSON snapshot
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, STATE_FILE)
        except Exception:
            pass

        # Notifications on attach/detach transitions
        if prev_attached is not None and attached != prev_attached:
            if attached:
                notified_soc = False
                soc_txt = f"当前电量 {snap['pen_soc']}%" if snap["pen_soc"] >= 0 else "正在握手读取电量..."
                notify_kde("焦点触控笔已磁吸", f"已自动开启无线充电 · {soc_txt}", "battery-charging")
            else:
                soc_txt = f"电量 {snap['pen_soc']}%" if snap["pen_soc"] >= 0 else "已停止无线充电"
                notify_kde("焦点触控笔已取下", soc_txt, "draw-freehand")
        elif attached and not notified_soc and 0 <= raw_soc <= 100 and prev_attached is True:
            notified_soc = True

        if place_err != 0 and prev_place_err == 0:
            notify_kde("焦点触控笔摆放偏移", "请重新对齐磁吸充电位", "dialog-warning")

        prev_attached = attached
        prev_place_err = place_err
        time.sleep(1.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/status":
            with state_lock:
                self._send_json(dict(state))
            return
        if p.path == "/mode":
            qs = parse_qs(p.query)
            v = qs.get("v", [""])[0]
            if v in ("auto", "on", "off"):
                with state_lock:
                    state["mode"] = v
                    apply_mode_locked()
                    snap = dict(state)
                self._send_json(snap)
                return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        self.do_GET()


def main():
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    print(f"[pen-charger] listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
