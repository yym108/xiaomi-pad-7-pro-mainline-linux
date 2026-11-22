#!/usr/bin/env python3
import hashlib
import struct
import sys
from pathlib import Path


def pad_to_page(data: bytes, page_size: int = 4096) -> bytes:
    rem = len(data) % page_size
    if rem:
        return data + b"\x00" * (page_size - rem)
    return data


def pack_v2(kernel_path: Path, ramdisk_path: Path, dtb_path: Path | None, cmdline: str, out_path: Path):
    page_size = 4096
    kernel = kernel_path.read_bytes()
    ramdisk = ramdisk_path.read_bytes()
    dtb = dtb_path.read_bytes() if dtb_path else b""

    base = 0x80000000
    kernel_addr = base + 0x00008000
    ramdisk_addr = base + 0x01000000
    second_addr = 0x00000000
    tags_addr = base + 0x00000100
    dtb_addr = base + 0x01F00000 if dtb else 0

    cmdline_bytes = cmdline.encode("ascii")
    if len(cmdline_bytes) > 512:
        cmdline_main = cmdline_bytes[:512]
        cmdline_extra = cmdline_bytes[512:1536]
    else:
        cmdline_main = cmdline_bytes
        cmdline_extra = b""

    h = hashlib.sha1()
    for part in (kernel, ramdisk, b"", dtb):
        h.update(part)
        h.update(struct.pack("<I", len(part)))
    sha = h.digest() + b"\x00" * 12  # 32 bytes

    header_size = 1660
    hdr = struct.pack(
        "<8s10I16s512s32s1024sIQIIQ",
        b"ANDROID!",
        len(kernel),
        kernel_addr,
        len(ramdisk),
        ramdisk_addr,
        0,  # second_size
        second_addr,
        tags_addr,
        page_size,
        2,  # header_version
        0,  # os_version
        b"",  # name
        cmdline_main.ljust(512, b"\x00"),
        sha,
        cmdline_extra.ljust(1024, b"\x00"),
        0,  # recovery_dtbo_size
        0,  # recovery_dtbo_offset
        header_size,
        len(dtb),
        dtb_addr,
    )
    assert len(hdr) == header_size

    out = bytearray()
    out.extend(pad_to_page(hdr, page_size))
    out.extend(pad_to_page(kernel, page_size))
    out.extend(pad_to_page(ramdisk, page_size))
    if dtb:
        out.extend(pad_to_page(dtb, page_size))

    out_path.write_bytes(bytes(out))
    print(f"Packed {out_path} ({len(out)} bytes): kernel={len(kernel)}, ramdisk={len(ramdisk)}, dtb={len(dtb)}")


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    out_dir = base_dir / "out"
    linux_dir = base_dir / "linux"
    image_raw = linux_dir / "arch/arm64/boot/Image"
    image_gz = linux_dir / "arch/arm64/boot/Image.gz"
    dtb = linux_dir / "arch/arm64/boot/dts/qcom/sm8635-xiaomi-muyu.dtb"
    ramdisk = out_dir / "initramfs.cpio.gz"

    image_dtb = out_dir / "Image-dtb"
    image_dtb.write_bytes(image_raw.read_bytes() + dtb.read_bytes())

    image_gz_dtb = out_dir / "Image.gz-dtb"
    image_gz_dtb.write_bytes(image_gz.read_bytes() + dtb.read_bytes())

    cmdline = "console=tty0 loglevel=7 ignore_loglevel rw"
    pack_v2(image_dtb, ramdisk, None, cmdline, out_dir / "boot_mainline_test_v2_raw_appended.img")
    pack_v2(image_raw, ramdisk, dtb, cmdline, out_dir / "boot_mainline_test_v2_raw_sepdtb.img")
    pack_v2(image_gz_dtb, ramdisk, None, cmdline, out_dir / "boot_mainline_test_v2_appended.img")
    pack_v2(image_gz, ramdisk, dtb, cmdline, out_dir / "boot_mainline_test_v2_sepdtb.img")
