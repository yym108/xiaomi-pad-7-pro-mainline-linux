# 小米平板 7 Pro（`muyu` / SM8635）主线 Linux 7.4 疑难排障与修复记录

> 记录日期：2026-11-22  
> 适用内核：主线 Linux `7.4.0-rc3`（分支 `muyu-7.4-rc3-phase1`，补丁 `0024`–`0028`）  
> 适用系统：Slot B Arch Linux ARM (`aarch64`) + KDE Plasma 6.8.3 (Wayland)

本文档完整记录以下关键子系统的排障过程、根因定位、修复方案与真机实测结果：
1. **高通 Iris VPU 3.3 硬件编解码与 Firefox / mpv / FFmpeg 4K60 VA-API 零拷贝硬解打通全过程**
2. **长时间待机 / DPMS 息屏唤醒后触控与焦点笔失灵、磁吸键盘需重插 Bug 的定位与修复**
3. **磁吸键盘背光 LED 类与多媒体键（Consumer Control）适配**
4. **息屏 / 睡眠 / 关机瞬间闪花屏（双 KTZ8866 背光时序）定位与修复**

---

## 一、Qualcomm Iris VPU 3.3 硬件编解码与 Firefox 4K60 VA-API 零拷贝硬解

### 1.1 问题现象

在早期为 SM8635（`cliffs`）合入主线 Iris 驱动（补丁 `0014`–`0015`）后，固件 `vpu30_2v.mbn` 可以正常启动并注册 `/dev/video0`（解码器）与 `/dev/video1`（编码器），但实际运行时存在两层阻断：

1. **内核 VPU 硬件层失败**：
   - 使用 GStreamer `v4l2h264dec` 或 FFmpeg VA-API 解码时，固件返回 `HFI_INFO_DATA_CORRUPT (0x6000002)`，无法输出正常视频帧；
   - 使用 `v4l2h264enc` 编码时，固件直接返回 `SESSION_ERROR`；
   - 若通过 HFI 向 `PROD_IRIS3_2P` 固件发送 `HFI_PROP_DEBUG_CONFIG (0x0300000a)` 或 `HFI_PROP_DEBUG_LOG_LEVEL (0x0300000b)`，会触发 `SYSTEM_ERROR` 并导致 `core->lock` 死锁。
2. **Firefox 用户态硬解静默回退软解**：
   - 即使内核层与 `ffmpeg` / `mpv` VA-API 4K60 硬解全部跑通，打开 Firefox 播放 4K 视频时，`/dev/video0` 仍显示 `idle`，`video_cc_mvs0_clk` 保持关闭，Firefox `RDD Process` CPU 占用飙升至 **158%**、内存占用 **17.4%**。

---

### 1.2 根因定位

#### A. 内核层（逆向原厂 `vendor_dlkm_a/lib/modules/msm_video.ko`）

通过反汇编原厂 `msm_video.ko` 中的 `cliffs` 平台定义，确认此前将 SM8635 归类为 SM8550/VPU3.0 配置存在硬件代差：

- **VPU 架构代际错误**：SM8635（`cliffs`）实际属于 **Iris 3.3 2-pipe** 架构（需使用 `iris_vpu33_p4_gen2_desc`、`sm8650_clk_reset_table`、`sm8650_controller_reset_table` 与 `platform_inst_cap_qcs8300`），而不能沿用 SM8550 的 VPU3.0 复位序列与缓冲区尺寸算法；
- **缺失 `cliffs_reg_preset_table` 5 个 QoS 预设寄存器**：原厂驱动在 VPU 上电初始化阶段必须写入以下 5 个硬件寄存器，否则 DMA/NoC 访存与解码管线会出现数据损坏与会话异常：
  - `0x0b0088 = 0x00000000`
  - `0x010830 = 0x33332222`
  - `0x010834 = 0x44444444`
  - `0x010838 = 0x00001022`
  - `0x0a013c = 0x00000099`
- **缺少 `qcom-va-driver` 所需的 HFI/V4L2 补丁**：`radxa-pkg/qcom-va-driver` 需要内核侧同步合入其 5 个 Iris HFI/V4L2 增强补丁（色彩空间、控制项、HFI 响应/缓冲区处理等）。

#### B. Firefox 用户态侧（逐层排查定位出 4 个拦路虎）

1. **RDD 沙盒拦截 `/dev/video0`**：Firefox Linux 的 RDD 进程沙盒默认阻止访问 V4L2 设备节点 `/dev/video0`，导致 `qcom_drv_video.so` 打开解码器失败并静默回退软解。必须设置 `MOZ_DISABLE_RDD_SANDBOX=1`；
2. **Freedreno `FD735` 默认关闭硬件 WebRender**：通过 `MOZ_LOG=FFmpegVideo:5` 抓取日志发现 `Hardware WebRender is off, VAAPI is disabled`，需显式开启 `gfx.webrender.all = true`；
3. **Arch Linux Firefox 全局配置目录差异**：Arch Linux 打包的 Firefox 读取的是 `/usr/lib/firefox/browser/defaults/preferences/*.js`，而非 `/usr/lib/firefox/defaults/pref/*.js`；
4. **Firefox 160 XDG 配置目录覆盖**：Firefox 160 已将用户配置迁至 `/home/muyu/.config/mozilla/firefox/*/`，此前为规避旧驱动卡死而写入的 `user.js`（禁用 VA-API）保留在该目录下，优先级高于系统默认配置，导致重启 Firefox 后依然被覆盖关闭。

---

### 1.3 解决方案

1. **内核层补丁（已提交并导出）**：
   - 补丁 `0024`（`7bc6f6542`）：`media: qcom: iris: apply qcom-va-driver HFI/V4L2 enhancements for VA-API decode/encode`
   - 补丁 `0025`（`289348151`）：`media: qcom: iris: switch SM8635 to Iris 3.3 2-pipe with cliffs QoS register presets`
   - 严禁向 `PROD_IRIS3_2P` 固件发送 `HFI_PROP_DEBUG_CONFIG` / `HFI_PROP_DEBUG_LOG_LEVEL`。
2. **用户态 VA-API 驱动与环境配置（已同步至平板与 `rootfs/build_arch_kde6.sh`）**：
   - 编译安装 `radxa-pkg/qcom-va-driver` 至 `/usr/lib/dri/qcom_drv_video.so`（启用 EGL/GLES 零拷贝与 Vulkan VPP）；
   - `/etc/environment` 写入：
     ```env
     LIBVA_DRIVER_NAME=qcom
     MOZ_DISABLE_RDD_SANDBOX=1
     ```
   - `/usr/lib/firefox/browser/defaults/preferences/muyu-video.js`（及用户 profile 的 `user.js`）写入：
     ```javascript
     pref("gfx.webrender.all", true);
     pref("media.hardware-video-decoding.enabled", true);
     pref("media.hardware-video-decoding.force-enabled", true);
     pref("media.ffmpeg.vaapi.enabled", true);
     pref("media.rdd-ffmpeg.enabled", true);
     pref("media.hevc.enabled", true);
     pref("media.av1.enabled", true);
     pref("widget.dmabuf.force-enabled", true);
     ```
   - `/etc/mpv/mpv.conf` 默认开启零拷贝渲染：
     ```ini
     vo=gpu-next
     gpu-api=opengl
     hwdec=vaapi
     ```

---

### 1.4 真机实测成绩（零 `dmesg` 报错、零 SMMU 缺页）

| 测试项 | 编码格式 / 分辨率 | 实测表现 | 状态 |
| --- | --- | --- | :---: |
| GStreamer `v4l2h264dec` | 720p30 H.264 | 0.047s 完成 (`RC=0`) | ✅ |
| GStreamer `v4l2h264enc` | 480p30 NV12 → H.264 | 0.0045s 完成 (`RC=0`) | ✅ |
| FFmpeg VA-API 零拷贝硬解 | **4K60 H.264** | **`3.30x` (~198 FPS)** | ✅ |
| FFmpeg VA-API 零拷贝硬解 | **4K60 HEVC 8-bit** | **`3.27x` (~196 FPS)** | ✅ |
| FFmpeg VA-API 零拷贝硬解 | **4K60 HEVC 10-bit** | **`3.21x` (~193 FPS)** | ✅ |
| FFmpeg VA-API 零拷贝硬解 | **4K60 VP9** | **`2.26x` (~136 FPS)** | ✅ |
| FFmpeg VA-API 零拷贝硬解 | **1080p60 AV1** | **`7.89x` (~473 FPS)** | ✅ |
| FFmpeg VA-API 硬件编码 | **1080p60 H.264 / HEVC** | **`5.72x` (~343 FPS) / `5.92x` (~355 FPS)** | ✅ |
| `mpv` Wayland 实际渲染 | **4K60 HEVC 10-bit / H.264** | `Using hardware decoding (vaapi)`，`vaapi[nv12]` 零丢帧 | ✅ |
| Firefox `/usr/lib/firefox/gfxtest vaapi` | H.264 / HEVC / VP9 / AV1 | `VAAPI_SUPPORTED: TRUE`, `VAAPI_HWCODECS: 13616` | ✅ |

#### Firefox 4K 实时播放前后对比

| 监控指标 | 修复前（CPU 软解） | 修复后（Iris VPU 3.3 + VA-API 零拷贝） |
| --- | :---: | :---: |
| **`/dev/video0` 占用** | `idle` | ✅ `RDD Process` 持续占用 |
| **`video_cc_mvs0_clk`** | `0 / 192 MHz`（关闭） | ✅ `1 / 240–338 MHz` 动态变频 |
| **VA-API & DMABUF** | 未加载 | ✅ `qcom_drv_video.so` + `/dmabuf:` 直通 `renderD128` |
| **`RDD Process` CPU 占用** | **`158%`** | **`7.8% – 8.7%`**（下降约 **20 倍**） |
| **`RDD Process` 内存占用** | **`17.4%`** | **`1.4%`**（下降约 **12 倍**） |

---

## 二、长时间待机 / DPMS 息屏唤醒后触控、焦点笔失灵与磁吸键盘需重插 Bug

### 2.1 问题现象

平板在长时间待机（息屏或触发过系统 `PM: suspend` 后唤醒）重新亮屏后出现以下异常：
1. **手指触控完全失灵**：检查 `/proc/interrupts` 发现 `spi0.0`（NT36532 IRQ 171）计数完全静止（`diff=0`）；
2. **焦点笔（`P81c`）可检测磁吸/取下，但无法触控书写**：磁吸检测走的是 PMIC/BLE 通道所以正常，但笔尖坐标依赖 NT36532 的 TDDI 主动笔扫描（`{ 0x7B, 0x02 }`），因此一同失效；
3. **磁吸键盘（Nanosic WN8030）无响应，必须取下重新磁吸才恢复**。

*(注：旧内核上的临时应急恢复命令为热重载 `spi0.0`：`echo -n spi0.0 > /sys/bus/spi/drivers/novatek-nt36532-spi/unbind && sleep 0.3 && echo -n spi0.0 > /sys/bus/spi/drivers/novatek-nt36532-spi/bind`)*

---

### 2.2 根因分析

#### A. NT36532 触控与焦点笔失灵

- 小米平板 7 Pro 采用 **联咏 NT36532 TDDI（屏触一体）** 芯片，显示面板与触控共用供电与硬件复位引脚（`disp0_reset_n`）。
- 长时间待机时，KDE Plasma 会先触发 **DRM/DPMS 息屏**（调用 `panel_unprepare`），而不是每次都进入整机 S3 休眠；再次亮屏时，面板驱动 `panel-novatek-nt36532.c` 会拉复位脚并重新下发初始化序列，**同时清空了 NT36532 内部 SRAM 中运行的触控固件与焦点笔扫描状态**。
- 此前 `novatek-nt36532-spi.c` 只注册了整机 `PM_OPS`（`.suspend` / `.resume`），**没有注册 `drm_panel_follower`**，导致单纯的 DPMS 息屏→亮屏循环结束后，触控驱动根本不知道面板已复位，既没有重新下载 `novatek_nt36532_o81_fw_tm.bin`，也没有重新发送焦点笔使能命令 `{ 0x7B, 0x02 }`。

#### B. Nanosic WN8030 磁吸键盘休眠后需重新磁吸

排查 `drivers/hid/hid-nanosic-wn8030.c` 发现 4 处休眠恢复缺陷：
1. **12 秒保活定时器 `wake_worker` 休眠后丢失**：`nanosic_wn8030_suspend()` 中调用了 `cancel_delayed_work_sync(&nanosic->wake_worker)`，但 `nanosic_wn8030_resume()` 中**从未重新调度 `wake_worker`**，导致键盘唤醒后十几秒即自动断电休眠；
2. **触控板未重新使能**：MCU 唤醒或硬复位后丢失了触控板使能状态，但 `resume()` 未重新调用 `nanosic_wn8030_set_touchpad(nanosic, true)`；
3. **内部 Flash MCU 唤醒/复位等待时间过短**：`muyu` 的 WN8030 运行内部 Flash 固件，`resume()` 中释放 `sleep_gpio` 仅等 `25ms`、触发硬复位后仅等 `20ms` 即发 I2C 命令，容易在 MCU 尚未就绪时丢失 `set_kb_power(true)`；
4. **重复附着通知被忽略**：MCU 唤醒后重新上报键盘已连接包（`0x38 0x80 0xa2`，`buf[12] & 0x3 == 0x3`）时，因为驱动侧 `nanosic->keyboard_attached` 仍为 `true`，直接跳过整个分支，既不重开触控板也不重启 `wake_worker`。

---

### 2.3 修复方案（内核补丁 `0026`，提交 `3746aa9ee`）

补丁文件：`kernel/patches/0026-Input-HID-muyu-fix-DPMS-touch-stylus-loss-via-drm_pa.patch`

1. **设备树建立面板-触控依赖（`sm8635-xiaomi-muyu.dts`）**：
   - 为 DSI 面板节点添加标签 `panel0: panel@0`；
   - 在 `touchscreen@0` 节点添加 `panel = <&panel0>;`，供 DRM Panel Follower 框架自动绑定。
2. **NT36532 接入 `drm_panel_follower`（`drivers/input/touchscreen/novatek-nt36532-spi.c`）**：
   - 在 `probe()` 中检测 `drm_is_panel_follower(&spi->dev)` 并通过 `devm_drm_panel_add_follower()` 注册 `.panel_prepared` / `.panel_unpreparing` 回调；
   - 每次 **DPMS 息屏或整机休眠** 触发 `panel_unpreparing` 时：取消延迟任务、安全关闭 IRQ、释放残留手指与笔触事件；
   - 每次 **DPMS 亮屏或整机唤醒** 触发 `panel_prepared` 时：200ms 后自动重新下载 TDDI 固件、重新发送焦点笔使能命令 `{ 0x7B, 0x02 }` 并重新开启 IRQ；
   - 新增 `irq_enabled` 状态位保证 `disable_irq()` / `enable_irq()` 严格成对，固件下载前自动暂停中断避免竞态；启动期若 rootfs 尚未挂载，自动按 1s 间隔重试加载固件（最多 10 次）。
3. **Nanosic WN8030 休眠唤醒恢复（`drivers/hid/hid-nanosic-wn8030.c`）**：
   - `suspend()` / `resume()` 增加 `irq_requested` 防护；
   - `resume()` 将 `sleep_gpio` 唤醒等待延长至 `50ms`，硬复位后等待延长至 `80ms`；
   - `resume()` 唤醒键盘供电后，若 `keyboard_attached == true`，延迟 `20ms` 自动重新调用 `set_touchpad(true)` 并通过 `mod_delayed_work()` 重启 12 秒 `wake_worker` 保活任务；
   - `handle_vendor()` 收到 `0xa2` 已连接事件时，即使 `keyboard_attached` 已为 `true`，也会刷新触控板使能与 `wake_worker` 保活定时器。

---

### 2.4 构建与验证方法

- 新内核已完成编译与 V4 启动镜像打包：`dualboot/out/boot_mainline_dualboot.img`（`55.07 MiB < 64 MiB`，已通过 `unpack_bootimg` 校验）。
- 刷入 `boot_b` 并重启至新内核后，可按以下步骤回归验证：
  1. **DPMS 息屏/亮屏测试**：在 KDE 中触发息屏（或等待自动息屏）后再亮屏，观察 `dmesg | grep NT36532` 是否自动打印 `NT36532 ready`，验证手指触控与焦点笔书写均在 ~0.3s 内自动恢复；
  2. **整机休眠/唤醒测试**：触发一次待机唤醒后，无需拔插磁吸键盘，直接按键与滑动触控板并静置超过 30 秒，确认键盘与触控板持续可用不掉线。

---

## 三、磁吸键盘背光与多媒体快捷键适配（补丁 `0027`，提交 `61c60252c`）

- **协议确认**：Nanosic WN8030 控制帧 `0x23` 控制键盘背光亮度（范围 `0–100`）；HID Report ID `0x06` 上报顶排 Consumer Control 多媒体 / 音量 / 亮度快捷键。
- **实现方案**（`drivers/hid/hid-nanosic-wn8030.c`）：
  1. 实现 `nanosic_wn8030_set_backlight()` 并注册标准 Linux LED 类设备 `/sys/class/leds/nanosic::kbd_backlight`（`max_brightness=100`，默认 `50`），由 `UPower` 与 KDE Plasma 6「亮度与色彩」托盘原生接管；
  2. 休眠时自动熄灭背光（`0`），唤醒与重新磁吸时自动恢复上次亮度；
  3. 补齐 HID Consumer Control（Report ID `0x06`）描述符与中断上报分支。
- **真机实测**：`UPower KbdBacklight` 识别 `50/100`，KDE 托盘滑块实时调节与物理背光点亮全部生效。

---

## 四、息屏 / 睡眠 / 关机瞬间闪花屏修复（补丁 `0028`，提交 `efc1256a6`）

### 4.1 根因分析

1. **双 KTZ8866 背光未在内核侧联动熄灭**：
   - 小米平板 7 Pro 由主背光 `ktz8866-backlight` 与副背光 `ktz8866-backlight-sec` 共同照明，但设备树 `panel@0` 仅绑定了 `backlight = <&backlight>;`；
   - 息屏 / 休眠时，DRM `drm_panel_disable()` 仅将主背光 `bl_power` 置为关闭（不修改 `brightness`），用户态同步守护进程 `muyu-backlight-sync.service` 既未响应 `bl_power` 变化，又会在睡眠/关机时被冻结或停止；导致**副背光仍处于点亮状态时，DPU 已停止 DSI 视频流且 `nt36532_unprepare()` 拉高复位脚**，液晶失信号瞬间的乱码被背光照亮，表现为“闪花屏”。
2. **缺少背光消隐等待与 `.shutdown` 回调**：
   - `panel-novatek-nt36532.c` 此前未实现 `.disable` / `.enable` 回调，主背光下发关断指令后 0ms 即停 DSI 视频流；
   - `ktz8866.c` 缺少 `.shutdown` 回调，关机/重启时背光不会提前熄灭。

### 4.2 修复方案

补丁文件：`kernel/patches/0028-drm-panel-nt36532-extinguish-dual-KTZ8866-backlights.patch`

1. **设备树（`sm8635-xiaomi-muyu.dts`）**：在 `panel0: panel@0` 节点新增 `backlight-sec = <&backlight_sec>;`；
2. **面板驱动（`drivers/gpu/drm/panel/panel-novatek-nt36532.c`）**：
   - `nt36532_probe()` 通过 `of_find_backlight_by_node()` 绑定可选副背光（未就绪时 `-EPROBE_DEFER`）；
   - `nt36532_disable()` 在 DPU 停视频流之前，**同步关闭主/副两颗背光并等待 `25ms` 确保 LED 彻底熄灭**；
   - `nt36532_enable()` 在首帧 DSI 视频输出后等待 `20ms` 稳定，再同时点亮主/副背光；
3. **背光驱动（`drivers/video/backlight/ktz8866.c`）**：注册 `.shutdown = ktz8866_remove`，确保关机/重启阶段两颗 KTZ8866 强制熄灭。

### 4.3 真机验证

刷入内核 **`#271`**（`7.4.0-rc3-muyu-slim-gefc1256a61a4`）后，息屏、合盖睡眠与关机/重启均彻底黑屏过渡，无闪花屏现象；`systemctl --failed` 为 0 项。

---

## 五、桌面体验同步修复汇总（Slot B Arch Linux ARM + KDE Plasma 6）

- **Fcitx5 Wayland 诊断弹窗消除**：从 `/etc/environment` 与 `rootfs/build_arch_kde6.sh` 移除多余的 `QT_IM_MODULE=fcitx`（保留 `XMODIFIERS=@im=fcitx`，由 `kwin_wayland` 原生 Virtual Keyboard 拉起 `fcitx5`）；
- **蓝牙焦点笔名称持久化**：在 `focus_pen_ble.py` 中连笔后通过 `busctl` 自动将焦点笔蓝牙别名设为 `Xiaomi Focus Pen`；
- **`muyu-kb-auth.service` 开机 `FAILED` 修复**：将 `ExecStartPre=/sbin/modprobe -q qcomtee` 改为 `ExecStartPre=-/sbin/modprobe -q qcomtee`（因 `qcomtee` 已内置进内核 `/dev/tee0`），服务开机稳定 `active (running)`。
