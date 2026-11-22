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
    "charge_done": False,
    "pen_soc": -1,
    "tx_vout": 0,
    "tx_iout": 0,
    "power_mw": 0,
    "place_err": 0,
    "reverse_chg_state": 0,
    "reverse_chg_mode": 0,
    "mode": "auto",  # "auto" | "on" | "off"
}
session_triggered = False
full_since = 0.0


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


def apply_mode_locked(force_retrigger: bool = False) -> None:
    global session_triggered, full_since
    mode = state["mode"]
    attached = state["attached"]
    place_err = state["place_err"]
    chg_mode = state["reverse_chg_mode"]
    pen_soc = state["pen_soc"]

    if force_retrigger:
        session_triggered = False
        state["charge_done"] = False
        full_since = 0.0

    # Off, detached, or misaligned: always turn TX off and reset session
    if mode == "off" or (mode == "auto" and (not attached or place_err != 0)):
        session_triggered = False
        state["charge_done"] = False
        full_since = 0.0
        if chg_mode != 0:
            write_int("reverse_chg_mode", 0)
            state["reverse_chg_mode"] = 0
            print(f"[pen-charger] set reverse_chg_mode=0 (mode={mode}, attached={attached}, place_err={place_err})", flush=True)
        return

    # Edge-triggered start: only enable reverse_chg_mode ONCE per attach/manual session.
    # If ADSP/Qi TX subsequently turns reverse_chg_mode back to 0 (e.g. pen reached 100%
    # and sent Qi EPT Charge Complete, or thermal protection), do NOT re-enable it in a loop!
    if not session_triggered:
        session_triggered = True
        full_since = 0.0
        if chg_mode != 1:
            write_int("reverse_chg_mode", 1)
            state["reverse_chg_mode"] = 1
            print(f"[pen-charger] set reverse_chg_mode=1 (mode={mode}, attached={attached})", flush=True)
        return

    # Session already started: respect hardware auto-stop (ADSP dropped reverse_chg_mode -> 0)
    if chg_mode == 0:
        if not state["charge_done"]:
            state["charge_done"] = True
            print(f"[pen-charger] hardware stopped TX (pen_soc={pen_soc}%, state={state['reverse_chg_state']}); staying idle", flush=True)
        return

    # Safety cutoff in auto mode: if pen stays at 100% for >15s while TX is still on, stop TX
    if mode == "auto" and pen_soc >= 100:
        now = time.monotonic()
        if full_since == 0.0:
            full_since = now
        elif now - full_since >= 15.0:
            write_int("reverse_chg_mode", 0)
            state["reverse_chg_mode"] = 0
            state["charge_done"] = True
            print("[pen-charger] pen_soc=100% cutoff -> set reverse_chg_mode=0", flush=True)
    else:
        full_since = 0.0


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

        # Attach/detach state transitions (status reflected via Plasmoid icon; no desktop popup)
        if prev_attached is not None and attached != prev_attached:
            if attached:
                notified_soc = False
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
                    apply_mode_locked(force_retrigger=True)
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
