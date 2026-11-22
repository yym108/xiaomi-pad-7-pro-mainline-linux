#!/usr/bin/env python3
"""Xiaomi Focus Pen (P81c) native BLE L2CAP ATT pressure & button bridge for NT36532.

Zero external dependencies (no gatttool / bleak required): speaks BLE ATT directly
over Linux AF_BLUETOOTH L2CAP CID=4 sockets.
"""

import ctypes
import errno
import os
import select
import socket
import struct
import subprocess
import sys
import time

PEN_MAC = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PEN_MAC", "XX:XX:XX:XX:XX:XX")
PEN_ALIAS = "Xiaomi Focus Pen"
SYSFS_PATH = "/sys/bus/spi/devices/spi0.0/pen_ble_state"

AF_BLUETOOTH = 31
SOCK_SEQPACKET = 5
BTPROTO_L2CAP = 0
SOL_BLUETOOTH = 274
BT_SECURITY = 4
BT_SECURITY_LOW = 1
BDADDR_LE_PUBLIC = 1
ATT_CID = 4

ATT_OP_EXCHANGE_MTU_REQ = 0x02
ATT_OP_WRITE_REQ = 0x12
ATT_OP_HANDLE_NOTIFY = 0x1B

CCCD_HANDLES = (0x002B, 0x002F, 0x0039, 0x003F, 0x0042)
PRESSURE_HANDLE = 0x002E
BUTTON_HANDLE = 0x002A

libc = ctypes.CDLL("libc.so.6", use_errno=True)


def mac_to_bytes(mac: str) -> bytes:
    return bytes(int(x, 16) for x in reversed(mac.split(":")))


def sockaddr_l2(mac_bytes: bytes, cid: int = ATT_CID, addr_type: int = BDADDR_LE_PUBLIC) -> bytes:
    # struct sockaddr_l2 { sa_family_t l2_family; uint16_t l2_psm;
    #                      bdaddr_t l2_bdaddr; uint16_t l2_cid; uint8_t l2_bdaddr_type; };
    return struct.pack("<HH6sHB", AF_BLUETOOTH, 0, mac_bytes, cid, addr_type)


def write_state(fd: int, pressure: int, buttons: int) -> None:
    try:
        os.pwrite(fd, f"{pressure} {buttons}\n".encode(), 0)
    except OSError:
        pass


def get_so_error(s_fd: int) -> int:
    err = ctypes.c_int(0)
    err_len = ctypes.c_uint(ctypes.sizeof(err))
    libc.getsockopt(
        s_fd,
        socket.SOL_SOCKET,
        socket.SO_ERROR,
        ctypes.byref(err),
        ctypes.byref(err_len),
    )
    return err.value


def run_once(sysfs_fd: int) -> None:
    s_fd = libc.socket(AF_BLUETOOTH, SOCK_SEQPACKET, BTPROTO_L2CAP)
    if s_fd < 0:
        err = ctypes.get_errno()
        print(f"[focus-pen] socket() failed: errno={err}", flush=True)
        return

    pressure = 0
    buttons = 0
    try:
        src = sockaddr_l2(b"\x00" * 6)
        if libc.bind(s_fd, src, len(src)) < 0:
            print(f"[focus-pen] bind() failed: errno={ctypes.get_errno()}", flush=True)
            return

        sec = struct.pack("<BB", BT_SECURITY_LOW, 0)
        libc.setsockopt(s_fd, SOL_BLUETOOTH, BT_SECURITY, sec, len(sec))

        os.set_blocking(s_fd, False)
        dst = sockaddr_l2(mac_to_bytes(PEN_MAC))
        rc = libc.connect(s_fd, dst, len(dst))
        if rc < 0:
            err = ctypes.get_errno()
            if err != errno.EINPROGRESS:
                return
            _, w, _ = select.select([], [s_fd], [], 5.0)
            if not w:
                return
            so_err = get_so_error(s_fd)
            if so_err != 0:
                return

        print(f"[focus-pen] connected to {PEN_MAC} (native L2CAP ATT)", flush=True)
        dev_path = f"/org/bluez/hci0/dev_{PEN_MAC.upper().replace(':', '_')}"
        subprocess.run(
            ["busctl", "set-property", "org.bluez", dev_path,
             "org.bluez.Device1", "Alias", "s", PEN_ALIAS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        buf = ctypes.create_string_buffer(256)

        mtu_req = struct.pack("<BH", ATT_OP_EXCHANGE_MTU_REQ, 247)
        libc.send(s_fd, mtu_req, len(mtu_req), 0)
        if select.select([s_fd], [], [], 1.0)[0]:
            libc.recv(s_fd, buf, 256, 0)

        for h in CCCD_HANDLES:
            req = struct.pack("<BHH", ATT_OP_WRITE_REQ, h, 0x0001)
            libc.send(s_fd, req, len(req), 0)
            r, _, _ = select.select([s_fd], [], [], 1.0)
            if r:
                libc.recv(s_fd, buf, 256, 0)

        write_state(sysfs_fd, 0, 0)
        got_first_pressure = False

        while True:
            r, _, _ = select.select([s_fd], [], [], 2.0)
            if not r:
                continue
            n = libc.recv(s_fd, buf, 256, 0)
            if n <= 0:
                if n < 0 and ctypes.get_errno() in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                    continue
                print(f"[focus-pen] disconnected from {PEN_MAC}", flush=True)
                return
            raw = buf.raw[:n]
            if n >= 3 and raw[0] == ATT_OP_HANDLE_NOTIFY:
                hnd = struct.unpack_from("<H", raw, 1)[0]
                if hnd == PRESSURE_HANDLE and n >= 5:
                    pressure = min(8191, struct.unpack_from("<H", raw, 3)[0])
                    write_state(sysfs_fd, pressure, buttons)
                    if not got_first_pressure:
                        got_first_pressure = True
                        print(f"[focus-pen] streaming pressure (first={pressure})", flush=True)
                elif hnd == BUTTON_HANDLE and n >= 4:
                    code = raw[3]
                    buttons = 1 if code == 0x4E else (2 if code == 0x4B else (4 if code == 0x6E else 0))
                    write_state(sysfs_fd, pressure, buttons)
                    print(f"[focus-pen] button code=0x{code:02x} -> mask={buttons}", flush=True)
    finally:
        write_state(sysfs_fd, 0, 0)
        libc.close(s_fd)


def main() -> None:
    while not os.path.exists(SYSFS_PATH):
        time.sleep(1)
    sysfs_fd = os.open(SYSFS_PATH, os.O_WRONLY)
    print(f"[focus-pen] bridge started for {PEN_MAC} -> {SYSFS_PATH}", flush=True)
    while True:
        run_once(sysfs_fd)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
