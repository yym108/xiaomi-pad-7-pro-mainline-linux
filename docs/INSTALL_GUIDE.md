# 小米平板 7 Pro（`muyu` / SM8635）主线 Linux 构建与双系统部署指南

> 适用设备：Xiaomi Pad 7 Pro（代号 `muyu`，骁龙 8s Gen 3 / SM8635 `cliffs`）  
> 目标架构：**A/B 独立双系统** — Slot A 完整保留原厂 HyperOS，Slot B 独立运行主线 Linux `7.4.0-rc3` + Arch Linux ARM (KDE Plasma 6 Wayland)

---

## ⚠️ 0. 风险提示与前提条件（必读）

> [!CAUTION]
> **操作分区表与底层引导存在变砖风险，开始前请务必确认：**
> 1. 平板已解锁 **Bootloader**，并在原厂 HyperOS 上获取了 **Root 权限**（推荐 KernelSU Next）。
> 2. **绝对不要刷写或修改 Slot A 的任何分区**（`boot_a`、`vbmeta_a`、`init_boot_a`、`vendor_boot_a` 等），始终保留原厂 HyperOS 作为保底恢复环境。
> 3. **务必提前冷备**：
>    - 原厂 `boot_b`、`vbmeta_b`；
>    - 与当前 HyperOS 版本对应的官方线刷包中的 `gpt_both0.bin` 与 `gpt_both4.bin`（救砖恢复 UFS 分区表必备）。
> 4. **固件版权声明**：高通与小米闭源固件（`adsp`、`cdsp`、`ipa`、`wcn7850`、`vpu30_2v.mbn`、`novatek_nt36532_o81_fw_tm.bin`、`nanosic/MCU_Upgrade.bin`、`devauth.mbn` 等）**不包含在本仓库中**，必须从你自己的设备原厂分区提取。

---

## 📦 1. 环境准备与固件提取

### 1.1 宿主机依赖
- 推荐使用 **ARM64 Linux 或 macOS (Apple Silicon)** + **Docker**（x86_64 宿主机也可通过 `qemu-user-static` 运行 arm64 容器）。
- 宿主机安装 `adb`、`fastboot`、`git`、`python3`。

### 1.2 从原厂系统提取闭源固件
在已 Root 的 HyperOS 中（或从官方线刷包解包 `vendor.img`、`odm.img`、`modem.img`）提取以下固件，放入 `kernel/firmware/`（供后续打包进 rootfs 的 `/lib/firmware/`）：

| 目标路径（`/lib/firmware/` 下） | 来源 / 用途 |
| --- | --- |
| `qcom/sm8635/adsp*`, `cdsp*`, `ipa_fws.mbn` | ADSP 音频、CDSP、IPA 网络协处理器 |
| `qcom/sm8635/a735_zap*`, `qcom/gen71100_*` | Adreno 735 GPU 微码与 ZAP 着色器 |
| `qcom/vpu/vpu30_2v.mbn` | Qualcomm Iris VPU 3.3 硬件编解码固件 |
| `ath12k/WCN7850/hw2.0/*`, `qca/hpnv21*`, `qca/hpbtfw21*` | WCN7850 Wi-Fi 7 与蓝牙 5.4 固件 |
| `novatek_nt36532_o81_fw_tm.bin`（或 `_csot.bin`） | 联咏 NT36532 TDDI 触摸屏与焦点笔 SRAM 固件 |
| `nanosic/MCU_Upgrade.bin` | Nanosic WN8030 磁吸键盘控制器 RAM 固件 |
| `qcom/sm8635/devauth.mbn` | 小米磁吸键盘 QTEE 5.2.0 MiAuth 认证 TA |

---

## 🐧 2. 编译主线 Linux `7.4.0-rc3` 内核

### 2.1 构建原生 ARM64 编译容器
```bash
docker build -t muyu-kernel-builder:latest -f kernel/Dockerfile kernel/
```

### 2.2 拉取主线源码并打入全套补丁（`0001`–`0028`）
```bash
git clone --depth=1 --branch v7.4-rc3 https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git kernel/linux
cd kernel/linux
git checkout -b muyu-7.4-rc3
git am ../patches/*.patch
cd ../..
```

### 2.3 生成独立双系统 `switch_root` initramfs
Android 15 GKI 的 `init_boot_b` / `vendor_boot_b` 会覆盖普通外置 ramdisk，因此必须将最小 `switch_root` initramfs 内嵌进内核并开启 `CONFIG_INITRAMFS_FORCE=y`：
```bash
docker run --rm -v "$PWD":/work -w /work muyu-kernel-builder:latest \
  bash dualboot/build_phase3_initramfs.sh
```

### 2.4 编译内核与设备树
> **重要编译规范**：
> - 必须在容器内使用原生 ARM64 **`gcc`** 编译，**不要使用 `LLVM=1`**；
> - 每次修改并重新编译 DTB 后，必须先执行 `rm -f arch/arm64/kernel/setup.o` 再编 `Image`；
> - 内核镜像 `Image + DTB` 打包后必须 **`< 64 MiB`**（`boot_b` 分区上限为 64 MiB）。

```bash
docker run --rm -v "$PWD":/work -w /work muyu-kernel-builder:latest bash -c '
  set -euo pipefail
  cd kernel/linux
  make defconfig
  scripts/kconfig/merge_config.sh -m .config ../muyu_slim.config
  make olddefconfig
  make -j$(nproc) qcom/sm8635-xiaomi-muyu.dtb
  rm -f arch/arm64/kernel/setup.o
  make -j$(nproc) Image
'
```

### 2.5 打包 Android Boot Image v4
必须将 `Image` 与 `sm8635-xiaomi-muyu.dtb` **拼接**后打包（不能直接打包裸 `Image`）：
```bash
cat kernel/linux/arch/arm64/boot/Image \
    kernel/linux/arch/arm64/boot/dts/qcom/sm8635-xiaomi-muyu.dtb \
    > dualboot/out/Image-dtb

python3 kernel/pack_bootimg.py \
  --kernel dualboot/out/Image-dtb \
  --output dualboot/out/boot_mainline_dualboot.img
```
确认输出文件 `dualboot/out/boot_mainline_dualboot.img` 大小在 ~55 MiB（`< 64 MiB`）。

---

## 🖥️ 3. 构建 Arch Linux ARM + KDE Plasma 6 根文件系统

1. 下载最新 `ArchLinuxARM-aarch64-latest.tar.gz` 到 `rootfs/` 目录；
2. （可选）如果你使用小米焦点触控笔（`P81c`），先获取你自己的触控笔蓝牙 MAC 地址，并替换 `rootfs/build_arch_kde6.sh` 中的 `XX:XX:XX:XX:XX:XX`；
3. 在 ARM64 Docker 容器中运行构建脚本：
   ```bash
   docker run --rm --privileged -v "$PWD":/work -w /work muyu-kernel-builder:latest \
     bash rootfs/build_arch_kde6.sh
   ```
   脚本会自动完成以下配置：
   - 安装 KDE Plasma 6 (Wayland)、SDDM 自动登录、Fcitx5 中文输入法（原生 Wayland 模式）、Mesa Freedreno + Vulkan、PipeWire + WirePlumber；
   - 预装四扬声器 UCM2 配置（`rootfs/ucm2/`）、焦点笔 BLE 压感桥接（`muyu-focus-pen.service`）、焦点笔磁吸无线充守护进程与 KDE 面板小组件（`rootfs/pen_charger/`）、键盘 MiAuth 认证服务（`muyu-kb-auth.service`）；
   - 配置 Firefox 硬件 WebRender + `qcom-va-driver` 4K60 VA-API 零拷贝硬解（`/etc/environment` + `/usr/lib/firefox/browser/defaults/preferences/muyu-video.js`）与 `mpv` 默认硬解（`/etc/mpv/mpv.conf`）；
   - 生成桌面「切换回 HyperOS」快捷图标。
4. （可选）如需 4K60 VA-API 硬件视频编解码，在平板进入 Arch Linux 后拉取并编译安装 [`radxa-pkg/qcom-va-driver`](https://github.com/radxa-pkg/qcom-va-driver) 至 `/usr/lib/dri/qcom_drv_video.so`。

> **关于其他发行版**：内核与双系统引导机制本身不绑定 Arch Linux，你也可以替换为 Ubuntu（参考 `dualboot/build_ubuntu_rootfs.sh`）、Debian 或 PostmarketOS，但 `rootfs/` 下针对 Arch + KDE Plasma 6 的服务脚本与配置需按目标发行版自行适配。

---

## 🔀 4. 独立 A/B 双系统分区与刷写

### 4.1 划出独立的 `80 GiB` Linux 分区（LUN 0）
> **SM8635 UFS GPT 硬件约束**：`num_parts` 必须为 32 的倍数（如 `64`），且备份分区表偏移必须为 `bak_part_lba = last_lba + 1`。请直接使用已校验的脚本，切勿用普通 `gdisk` 随意改写。

使用 `dualboot/setup_dualboot_disk.py` 从 LUN 0 的 `userdata` 末尾无损划出 `80 GiB` 的 `ext4` 分区（`PARTNAME=linux`，卷标 `MUYU_LINUX`），并将第 3 步生成的根文件系统完整解压至 `/dev/disk/by-partlabel/linux`。

### 4.2 同步 Slot B 辅助分区并刷入 `boot_b`
在 Root Shell 或 Fastboot 中执行（**严禁触碰任何 `*_a` 分区**）：
1. 从 Slot A 复制 `vendor_boot_a` → `vendor_boot_b`、`dtbo_a` → `dtbo_b`、`vbmeta_system_a` → `vbmeta_system_b`；
2. 对 `vbmeta_b` 关闭 AVB 校验（`fastboot --disable-verity --disable-verification flash vbmeta_b vbmeta.img`）；
3. 将第 2 步打包好的启动镜像刷入 **`boot_b`**：
   ```bash
   fastboot flash boot_b dualboot/out/boot_mainline_dualboot.img
   ```

---

## 🔁 5. 部署一键双向切换工具

### 5.1 为什么不能只用 `fastboot set_active`？
SM8635 在运行态切换 A/B 槽位必须**同时原子更新三处**：
1. LUN 4 的 `boot_a`/`boot_b` 标志位，并互换全部 22 组 `*_a`/`*_b` 分区的 Type GUID；
2. LUN 0 的 `countrycode`/`vbmeta_system` 活动标志位（bit 50）；
3. 通过 `/dev/bsg/ufs-bsg0` 写入 UFS 控制器的 `bBootLunEn`（`1` = Slot A，`2` = Slot B）。

以上全部逻辑已封装在 [`dualboot/switch-slot.c`](../dualboot/switch-slot.c) 中。

### 5.2 编译与安装
1. **编译静态 ARM64 二进制**：
   ```bash
   docker run --rm -v "$PWD":/work -w /work muyu-kernel-builder:latest \
     gcc -O2 -static -o dualboot/out/switch-slot dualboot/switch-slot.c
   ```
2. **部署到 HyperOS（Slot A）**：
   - 将 `switch-slot` 推送到 `/data/adb/switch-slot` 并赋予 `chmod 755`；
   - 编译并安装 [`dualboot/muyu-linux-tile/`](../dualboot/muyu-linux-tile/) 生成的 APK；
   - 打开 **KernelSU Next** → 超级用户列表 → 授予 **「切换 Arch Linux」** Root 权限；
   - 下拉控制中心 → 编辑磁贴 → 添加 **「切换 Arch Linux」**。
3. **部署到 Arch Linux（Slot B）**：
   - 将同一份 `switch-slot` 放到 `/usr/local/bin/switch-slot`，即可通过桌面「切换回 HyperOS」图标一键切回 Slot A。

---

## 🚑 6. 首次启动自检与救砖兜底

### 首次启动自检
- **USB 调试连接**：Linux 启动后默认在 USB-C 启用 USB-NCM 网卡，宿主机连接后可直接 `ssh root@172.16.42.1`；
- **硬件硬解验证**：运行 `LIBVA_DRIVER_NAME=qcom vainfo --display drm --device /dev/dri/renderD128` 确认 H.264 / HEVC / VP9 / AV1 Profile 全部列出；
- **系统服务检查**：运行 `systemctl --failed` 确认 0 失败项。

### 应急恢复（回到 HyperOS）
- 如果 Slot B 引导异常或连续重启触发 Mi Recovery：
  - 长按 **电源键 + 音量减** 进入 Fastboot，执行：
    ```bash
    fastboot set_active a
    fastboot reboot
    ```
  - 因为 Slot A 全程未做任何修改，执行上述两条命令即可 100% 瞬间回到原厂 HyperOS。
- 若遇排障问题，请参阅 [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md)。
