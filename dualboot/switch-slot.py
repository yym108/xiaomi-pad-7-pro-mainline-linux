#!/usr/bin/env python3
"""
Qualcomm SM8635 A/B active boot slot switcher (100% compatible with ABL/XBL).
- Updates UFS bBootLunEn (1=Slot A, 2=Slot B) via /dev/bsg/ufs-bsg0.
- Updates LUN 4 (boot_a/boot_b flags + swaps all *_a/*_b Qualcomm Type GUIDs).
- Updates LUN 0 (countrycode_a/b & vbmeta_system_a/b active bit 50).
- Preserves Qualcomm's exact Backup GPT PartitionEntryLBA on all LUNs.

Usage:
  switch-slot.py a [--no-reboot]   # Switch to Slot A (HyperOS)
  switch-slot.py b [--no-reboot]   # Switch to Slot B (Ubuntu Linux)
  switch-slot.py show              # Show current slot, GUIDs, and UFS Boot LUN
"""
import ctypes
import fcntl
import glob
import os
import struct
import sys
import time
import uuid
import zlib

SECTOR = 4096
INACTIVE_GUID = uuid.UUID("77036CD4-03D5-42BB-8ED1-37E5A88BAA34").bytes_le
SG_IO = 0x2285
BSG_PROTOCOL_SCSI = 0
BSG_SUB_PROTOCOL_SCSI_TRANSPORT = 2
UPIU_TRANSACTION_QUERY_REQ = 0x16
UPIU_QUERY_FUNC_STANDARD_WRITE_REQUEST = 0x81
UPIU_QUERY_OPCODE_WRITE_ATTR = 0x04
QUERY_ATTR_IDN_BOOT_LU_EN = 0x00


class SgIoV4(ctypes.Structure):
    _fields_ = [
        ("guard", ctypes.c_int32),
        ("protocol", ctypes.c_uint32),
        ("subprotocol", ctypes.c_uint32),
        ("request_len", ctypes.c_uint32),
        ("request", ctypes.c_uint64),
        ("request_tag", ctypes.c_uint64),
        ("request_attr", ctypes.c_uint32),
        ("request_priority", ctypes.c_uint32),
        ("request_extra", ctypes.c_uint32),
        ("max_response_len", ctypes.c_uint32),
        ("response", ctypes.c_uint64),
        ("dout_iovec_count", ctypes.c_uint32),
        ("dout_xfer_len", ctypes.c_uint32),
        ("din_iovec_count", ctypes.c_uint32),
        ("din_xfer_len", ctypes.c_uint32),
        ("dout_xferp", ctypes.c_uint64),
        ("din_xferp", ctypes.c_uint64),
        ("timeout", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("usr_ptr", ctypes.c_uint64),
        ("spare_in", ctypes.c_uint32),
        ("driver_status", ctypes.c_uint32),
        ("transport_status", ctypes.c_uint32),
        ("device_status", ctypes.c_uint32),
        ("retry_delay", ctypes.c_uint32),
        ("info", ctypes.c_uint32),
        ("duration", ctypes.c_uint32),
        ("response_len", ctypes.c_uint32),
        ("din_resid", ctypes.c_int32),
        ("dout_resid", ctypes.c_int32),
        ("generated_tag", ctypes.c_uint64),
        ("spare_out", ctypes.c_uint32),
        ("padding", ctypes.c_uint32),
    ]


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def find_disk_with(partname: str) -> str:
    for uev in sorted(glob.glob("/sys/class/block/sd*/uevent")):
        if f"PARTNAME={partname}\n" in open(uev).read():
            part = os.path.basename(os.path.dirname(uev))
            return f"/dev/{part.rstrip('0123456789')}"
    raise RuntimeError(f"PARTNAME={partname} not found!")


def get_boot_lun() -> str:
    paths = glob.glob("/sys/devices/platform/soc@0/*ufshc*/attributes/boot_lun_enabled")
    return open(paths[0]).read().strip() if paths else "unknown"


def set_boot_lun(lun_val: int):
    req_buf = ctypes.create_string_buffer(
        struct.pack(
            "<I10BH4B2HI2I",
            UPIU_TRANSACTION_QUERY_REQ,
            UPIU_TRANSACTION_QUERY_REQ, 0, 0, 0, 0,
            UPIU_QUERY_FUNC_STANDARD_WRITE_REQUEST, 0, 0, 0, 0, 0,
            UPIU_QUERY_OPCODE_WRITE_ATTR, QUERY_ATTR_IDN_BOOT_LU_EN, 0, 0,
            0, 0,
            struct.unpack("<I", struct.pack(">I", lun_val))[0],
            0, 0,
        )
    )
    rsp_buf = ctypes.create_string_buffer(64)
    hdr = SgIoV4()
    hdr.guard = ord("Q")
    hdr.protocol = BSG_PROTOCOL_SCSI
    hdr.subprotocol = BSG_SUB_PROTOCOL_SCSI_TRANSPORT
    hdr.request_len = len(req_buf) - 1
    hdr.request = ctypes.addressof(req_buf)
    hdr.max_response_len = len(rsp_buf)
    hdr.response = ctypes.addressof(rsp_buf)
    hdr.timeout = 5000
    fd = os.open("/dev/bsg/ufs-bsg0", os.O_RDWR)
    try:
        fcntl.ioctl(fd, SG_IO, hdr)
    finally:
        os.close(fd)
    res = struct.unpack_from("<i", rsp_buf.raw, 0)[0]
    if res != 0:
        raise RuntimeError(f"UFS BSG write bBootLunEn={lun_val} failed: result={res}")


def update_gpt(disk_path: str, mutate_fn):
    fd = os.open(disk_path, os.O_RDWR)
    try:
        os.lseek(fd, SECTOR, os.SEEK_SET)
        hdr = bytearray(os.read(fd, SECTOR))
        sig, rev, hsz, hcrc, res, cur_lba, bak_lba, first_lba, last_lba, dguid, part_lba, num_parts, part_sz, pcrc = \
            struct.unpack_from("<8sIIIIQQQQ16sQIII", hdr, 0)
        assert sig == b"EFI PART" and hsz == 92 and part_sz == 128

        hdr_chk = bytearray(hdr[:hsz])
        struct.pack_into("<I", hdr_chk, 16, 0)
        assert crc32(hdr_chk) == hcrc, f"{disk_path}: Primary GPT header CRC mismatch!"

        os.lseek(fd, part_lba * SECTOR, os.SEEK_SET)
        entries = bytearray(os.read(fd, num_parts * part_sz))
        assert crc32(entries) == pcrc, f"{disk_path}: Primary GPT entries CRC mismatch!"

        os.lseek(fd, bak_lba * SECTOR, os.SEEK_SET)
        orig_bak_hdr = os.read(fd, SECTOR)
        bak_part_lba = struct.unpack_from("<Q", orig_bak_hdr, 72)[0]
        if bak_part_lba < last_lba + 1 or bak_part_lba >= bak_lba:
            bak_part_lba = last_lba + 1

        by_name = {}
        for i in range(num_parts):
            off = i * part_sz
            if entries[off:off+16] == b"\x00" * 16:
                continue
            by_name[entries[off+56:off+128].decode("utf-16le").rstrip("\x00")] = i

        msg = mutate_fn(entries, by_name, part_sz)

        struct.pack_into("<I", hdr, 88, crc32(entries))
        struct.pack_into("<I", hdr, 16, 0)
        struct.pack_into("<I", hdr, 16, crc32(hdr[:hsz]))

        bak_hdr = bytearray(hdr)
        struct.pack_into("<QQ", bak_hdr, 24, bak_lba, cur_lba)
        struct.pack_into("<Q", bak_hdr, 72, bak_part_lba)
        struct.pack_into("<I", bak_hdr, 16, 0)
        struct.pack_into("<I", bak_hdr, 16, crc32(bak_hdr[:hsz]))

        os.lseek(fd, part_lba * SECTOR, os.SEEK_SET)
        os.write(fd, entries)
        os.lseek(fd, cur_lba * SECTOR, os.SEEK_SET)
        os.write(fd, hdr)
        os.lseek(fd, bak_part_lba * SECTOR, os.SEEK_SET)
        os.write(fd, entries)
        os.lseek(fd, bak_lba * SECTOR, os.SEEK_SET)
        os.write(fd, bak_hdr)
        os.fsync(fd)
        return msg
    finally:
        os.close(fd)


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "a"
    assert mode in ("a", "b", "show"), "Usage: switch-slot.py [a|b|show] [--no-reboot]"

    lun4 = find_disk_with("boot_a")
    lun0 = find_disk_with("userdata")

    if mode == "show":
        fd = os.open(lun4, os.O_RDONLY)
        os.lseek(fd, SECTOR, os.SEEK_SET)
        hdr = os.read(fd, SECTOR)
        part_lba, num_parts, part_sz = struct.unpack_from("<QII", hdr, 72)
        os.lseek(fd, part_lba * SECTOR, os.SEEK_SET)
        entries = os.read(fd, num_parts * part_sz)
        os.close(fd)
        for i in range(num_parts):
            off = i * part_sz
            name = entries[off+56:off+128].decode("utf-16le").rstrip("\x00")
            if name in ("boot_a", "boot_b"):
                attrs = struct.unpack_from("<Q", entries, off + 48)[0]
                g = uuid.UUID(bytes_le=entries[off:off+16])
                print(f"[slot] {lun4} {name}: attrs=0x{attrs:016x} guid={g}")
        print(f"[slot] UFS boot_lun_enabled = {get_boot_lun()}")
        return

    # 1. Update LUN 4 (boot_a/boot_b flags + swap *_a/*_b Type GUIDs)
    def mutate_lun4(entries, by_name, part_sz):
        swapped = 0
        for name, ia in by_name.items():
            if not name.endswith("_a"):
                continue
            bname = name[:-2] + "_b"
            if bname not in by_name:
                continue
            ib = by_name[bname]
            ga = bytes(entries[ia*part_sz : ia*part_sz+16])
            gb = bytes(entries[ib*part_sz : ib*part_sz+16])
            if (mode == "a" and ga == INACTIVE_GUID and gb != INACTIVE_GUID) or \
               (mode == "b" and gb == INACTIVE_GUID and ga != INACTIVE_GUID):
                entries[ia*part_sz : ia*part_sz+16] = gb
                entries[ib*part_sz : ib*part_sz+16] = ga
                swapped += 1
        ia, ib = by_name["boot_a"], by_name["boot_b"]
        aa = struct.unpack_from("<Q", entries, ia * part_sz + 48)[0]
        ab = struct.unpack_from("<Q", entries, ib * part_sz + 48)[0]
        ACTIVE = (3 << 48) | (1 << 50) | (7 << 51) | (1 << 54)
        INACTIVE = (2 << 48) | (0 << 50) | (7 << 51) | (1 << 54)
        m48 = (1 << 48) - 1
        na = (aa & m48) | (ACTIVE if mode == "a" else INACTIVE)
        nb = (ab & m48) | (ACTIVE if mode == "b" else INACTIVE)
        struct.pack_into("<Q", entries, ia * part_sz + 48, na)
        struct.pack_into("<Q", entries, ib * part_sz + 48, nb)
        return f"swapped {swapped} GUID pairs"

    m4 = update_gpt(lun4, mutate_lun4)

    # 2. Update LUN 0 (countrycode_a/b & vbmeta_system_a/b active bit 50)
    def mutate_lun0(entries, by_name, part_sz):
        bit50 = 1 << 50
        updated = 0
        for base in ("countrycode", "vbmeta_system"):
            if f"{base}_a" in by_name and f"{base}_b" in by_name:
                ia, ib = by_name[f"{base}_a"], by_name[f"{base}_b"]
                aa = struct.unpack_from("<Q", entries, ia * part_sz + 48)[0]
                ab = struct.unpack_from("<Q", entries, ib * part_sz + 48)[0]
                aa = (aa | bit50) if mode == "a" else (aa & ~bit50)
                ab = (ab | bit50) if mode == "b" else (ab & ~bit50)
                struct.pack_into("<Q", entries, ia * part_sz + 48, aa)
                struct.pack_into("<Q", entries, ib * part_sz + 48, ab)
                updated += 1
        return f"updated {updated} LUN0 A/B pairs"

    m0 = update_gpt(lun0, mutate_lun0)

    # 3. Switch UFS Boot LUN (1=Slot A, 2=Slot B)
    target_lun = 1 if mode == "a" else 2
    set_boot_lun(target_lun)
    print(f"[slot] Switched to Slot {mode.upper()}: {m4}, {m0}, bBootLunEn={get_boot_lun()}")

    if "--no-reboot" not in sys.argv:
        print("[slot] Syncing filesystems and rebooting...")
        os.system("sync; sync")
        os.system("mount -o remount,ro / 2>/dev/null || true")
        time.sleep(0.3)
        try:
            open("/proc/sys/kernel/sysrq", "w").write("1\n")
            open("/proc/sysrq-trigger", "w").write("b\n")
        except OSError:
            os.system("reboot -f")


if __name__ == "__main__":
    main()
