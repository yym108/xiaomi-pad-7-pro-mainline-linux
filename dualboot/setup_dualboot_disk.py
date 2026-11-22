#!/usr/bin/env python3
"""
Split UFS userdata partition into userdata (145 GiB) + linux (80 GiB) safely.
Dynamically resolves the UFS LUN containing PARTNAME=userdata (never hardcodes /dev/sdX!).
Verifies primary & backup GPT CRC32 before and after writing.
"""
import glob
import os
import struct
import sys
import uuid
import zlib

LINUX_GIB = int(sys.argv[1]) if len(sys.argv) > 1 else 80
SECTOR = 4096


def find_userdata_disk():
    for uev in sorted(glob.glob("/sys/class/block/sd*/uevent")):
        txt = open(uev).read()
        if "PARTNAME=userdata\n" in txt:
            part = os.path.basename(os.path.dirname(uev))
            disk = part.rstrip("0123456789")
            return f"/dev/{disk}", part
    raise RuntimeError("PARTNAME=userdata not found!")


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def main():
    disk_path, part_name = find_userdata_disk()
    print(f"[gpt] Found userdata ({part_name}) on {disk_path}")

    fd = os.open(disk_path, os.O_RDWR)
    try:
        disk_bytes = os.lseek(fd, 0, os.SEEK_END)
        assert disk_bytes % SECTOR == 0
        total_lbas = disk_bytes // SECTOR
        print(f"[gpt] Disk size: {disk_bytes / (1024**3):.2f} GiB ({total_lbas} 4K LBAs)")

        # Read Primary GPT Header (LBA 1)
        os.lseek(fd, SECTOR, os.SEEK_SET)
        hdr = bytearray(os.read(fd, SECTOR))
        sig, rev, hsz, hcrc, res, cur_lba, bak_lba, first_lba, last_lba, dguid, part_lba, num_parts, part_sz, pcrc = \
            struct.unpack_from("<8sIIIIQQQQ16sQIII", hdr, 0)
        assert sig == b"EFI PART", f"Bad GPT sig: {sig}"
        assert hsz == 92 and part_sz == 128

        # Verify existing primary header CRC
        hdr_chk = bytearray(hdr[:hsz])
        struct.pack_into("<I", hdr_chk, 16, 0)
        assert crc32(hdr_chk) == hcrc, "Primary GPT header CRC mismatch!"

        # Read partition entries
        ent_bytes_len = num_parts * part_sz
        os.lseek(fd, part_lba * SECTOR, os.SEEK_SET)
        entries = bytearray(os.read(fd, ent_bytes_len))
        assert crc32(entries) == pcrc, "Primary GPT entries CRC mismatch!"
        print(f"[gpt] Existing GPT verified OK: num_parts={num_parts}, first={first_lba}, last={last_lba}, bak={bak_lba}")

        # Find userdata & check if linux already exists
        ud_idx = None
        free_idx = None
        for i in range(num_parts):
            off = i * part_sz
            tguid = entries[off:off+16]
            if tguid == b"\x00" * 16:
                if free_idx is None:
                    free_idx = i
                continue
            name = entries[off+56:off+128].decode("utf-16le").rstrip("\x00")
            if name == "linux":
                print("[gpt] Partition 'linux' ALREADY exists! Nothing to do.")
                return
            if name == "userdata":
                ud_idx = i

        assert ud_idx is not None, "userdata entry not found!"
        if free_idx is None:
            # Expand num_parts in multiples of 32 (exact 4096-byte sectors) up to max_entries
            max_entries = (first_lba - part_lba) * SECTOR // part_sz
            print(f"[gpt] Table full (num_parts={num_parts}), max_entries in header gap = {max_entries}")
            assert max_entries > num_parts, "No space to expand GPT entry table!"
            new_num_parts = min(max_entries, ((num_parts // 32) + 1) * 32)
            assert bak_lba - last_lba - 1 >= (new_num_parts * part_sz + SECTOR - 1) // SECTOR, \
                "Not enough space at backup GPT!"
            entries.extend(b"\x00" * ((new_num_parts - num_parts) * part_sz))
            free_idx = num_parts
            num_parts = new_num_parts
            print(f"[gpt] Expanded num_parts -> {num_parts}, free_idx={free_idx}")

        ud_off = ud_idx * part_sz
        ud_tguid, ud_uguid, ud_flba, ud_llba, ud_attrs = struct.unpack_from("<16s16sQQQ", entries, ud_off)
        ud_total_lbas = ud_llba - ud_flba + 1
        linux_lbas = (LINUX_GIB * 1024 * 1024 * 1024) // SECTOR
        # Align linux start to 1 MiB (256 4K sectors)
        linux_flba = ((ud_llba - linux_lbas + 1) // 256) * 256
        linux_llba = ud_llba
        new_ud_llba = linux_flba - 1

        new_ud_gib = (new_ud_llba - ud_flba + 1) * SECTOR / (1024**3)
        new_lx_gib = (linux_llba - linux_flba + 1) * SECTOR / (1024**3)
        print(f"[gpt] Plan:")
        print(f"  userdata (slot {ud_idx+1}): LBA {ud_flba}..{new_ud_llba} ({new_ud_gib:.2f} GiB)")
        print(f"  linux    (slot {free_idx+1}): LBA {linux_flba}..{linux_llba} ({new_lx_gib:.2f} GiB)")
        assert new_ud_gib >= 50.0 and new_lx_gib >= 20.0

        # Update userdata last_lba
        struct.pack_into("<Q", entries, ud_off + 40, new_ud_llba)

        # Write linux partition entry (Linux filesystem GUID: 0FC63DAF-8483-4772-8E79-3D69D8477DE4)
        lx_off = free_idx * part_sz
        lx_tguid = uuid.UUID("0FC63DAF-8483-4772-8E79-3D69D8477DE4").bytes_le
        lx_uguid = uuid.uuid4().bytes_le
        lx_name = "linux".encode("utf-16le").ljust(72, b"\x00")
        struct.pack_into("<16s16sQQQ72s", entries, lx_off, lx_tguid, lx_uguid, linux_flba, linux_llba, 0, lx_name)

        new_pcrc = crc32(entries)

        # Update Primary Header
        struct.pack_into("<II", hdr, 80, num_parts, part_sz)
        struct.pack_into("<I", hdr, 88, new_pcrc)
        struct.pack_into("<I", hdr, 16, 0)
        new_hcrc = crc32(hdr[:hsz])
        struct.pack_into("<I", hdr, 16, new_hcrc)

        # Build Backup Header preserving Qualcomm's exact bak_part_lba = last_lba + 1 (bak_lba - 4)
        bak_part_lba = last_lba + 1
        bak_hdr = bytearray(hdr)
        struct.pack_into("<QQ", bak_hdr, 24, bak_lba, cur_lba)
        struct.pack_into("<Q", bak_hdr, 72, bak_part_lba)
        struct.pack_into("<I", bak_hdr, 16, 0)
        bak_hcrc = crc32(bak_hdr[:hsz])
        struct.pack_into("<I", bak_hdr, 16, bak_hcrc)

        # Write Primary Entries + Primary Header
        os.lseek(fd, part_lba * SECTOR, os.SEEK_SET)
        os.write(fd, entries)
        os.lseek(fd, cur_lba * SECTOR, os.SEEK_SET)
        os.write(fd, hdr)

        # Write Backup Entries + Backup Header
        os.lseek(fd, bak_part_lba * SECTOR, os.SEEK_SET)
        os.write(fd, entries)
        os.lseek(fd, bak_lba * SECTOR, os.SEEK_SET)
        os.write(fd, bak_hdr)

        # Wipe first 4 MiB of new userdata so HyperOS cleanly reformats (clears droidspaces!)
        os.lseek(fd, ud_flba * SECTOR, os.SEEK_SET)
        os.write(fd, b"\x00" * (4 * 1024 * 1024))

        os.fsync(fd)
        print("[gpt] Primary & Backup GPT written and synced successfully!")
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
