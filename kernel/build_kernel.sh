#!/bin/bash
set -euo pipefail

cd /work/linux
mkdir -p /work/out

echo "=== [1/4] Building initramfs ==="
bash /work/build_initramfs.sh

echo "=== [2/4] Configuring kernel ==="
make defconfig
./scripts/kconfig/merge_config.sh -m .config /work/muyu_bringup.config
make olddefconfig

echo "=== [3/4] Compiling Image.gz and sm8635-xiaomi-muyu.dtb ==="
NPROC=$(nproc)
make -j"$NPROC" Image.gz qcom/sm8635-xiaomi-muyu.dtb

echo "=== [4/4] Packaging boot_mainline_test.img ==="
cat arch/arm64/boot/Image.gz arch/arm64/boot/dts/qcom/sm8635-xiaomi-muyu.dtb > /work/out/Image.gz-dtb

mkbootimg \
    --kernel /work/out/Image.gz-dtb \
    --ramdisk /work/out/initramfs.cpio.gz \
    --header_version 2 \
    --pagesize 4096 \
    --cmdline "console=tty0 loglevel=7 ignore_loglevel rw" \
    -o /work/out/boot_mainline_test_v2.img

mkbootimg \
    --kernel /work/out/Image.gz-dtb \
    --ramdisk /work/out/initramfs.cpio.gz \
    --header_version 4 \
    --pagesize 4096 \
    --cmdline "console=tty0 loglevel=7 ignore_loglevel rw" \
    -o /work/out/boot_mainline_test_v4.img

ls -lh /work/out/
