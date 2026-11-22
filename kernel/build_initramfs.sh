#!/bin/bash
set -euo pipefail

OUT_DIR="/work/out"
ROOTFS="/work/out/initramfs_root"
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"/{bin,sbin,etc,proc,sys,dev,mnt,tmp,var/log,var/lib/misc,lib/firmware/ath12k/WCN7850/hw2.0,lib/firmware/qca}
mkdir -p "$OUT_DIR"

cp /work/firmware/ath12k/WCN7850/hw2.0/* "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/"
cp /work/firmware/qca/* "$ROOTFS/lib/firmware/qca/"
cp /work/firmware/qcom/sm8635/xiaomi/muyu/* "$ROOTFS/lib/firmware/"
ln -sf . "$ROOTFS/lib/firmware/qcom"
ln -sf . "$ROOTFS/lib/firmware/sm8650"
for req in \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/amss.bin" \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/m3.bin" \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/board-2.bin" \
    "$ROOTFS/lib/firmware/ath12k/WCN7850/hw2.0/board.bin" \
    "$ROOTFS/lib/firmware/qca/hmtbtfw20.tlv" \
    "$ROOTFS/lib/firmware/adsp.mdt" \
    "$ROOTFS/lib/firmware/adsp_dtb.mdt" \
    "$ROOTFS/lib/firmware/cdsp.mdt" \
    "$ROOTFS/lib/firmware/cdsp_dtb.mdt" \
    "$ROOTFS/lib/firmware/battmgr.jsn" \
    "$ROOTFS/lib/firmware/gen71100_sqe.fw" \
    "$ROOTFS/lib/firmware/gen71100_gmu.bin" \
    "$ROOTFS/lib/firmware/gen71100_zap.mbn"; do
    test -s "$req" || { echo "MISSING/EMPTY FIRMWARE: $req" >&2; exit 1; }
done

cp /bin/busybox "$ROOTFS/bin/busybox"
chmod +x "$ROOTFS/bin/busybox"

for cmd in sh ash mount umount mkdir mknod ls cat echo grep awk sed dmesg sleep ip ifconfig route udhcpd telnetd httpd ps kill reboot poweroff sync chmod chown ln rm cp mv touch head tail wc uname dd tr basename readlink date cut od hexdump; do
    ln -sf /bin/busybox "$ROOTFS/bin/$cmd"
done
ln -sf /bin/busybox "$ROOTFS/sbin/init"

gcc -static -O2 -Wall -o "$ROOTFS/bin/i2c_probe" -x c - <<'CSRC'
#include <errno.h>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

static const uint8_t probe_addrs[] = { 0x0e, 0x11, 0x34, 0x35, 0x36, 0x37, 0x4c, 0x57 };
static const uint8_t ktz_regs[] = { 0x01, 0x02, 0x04, 0x05 };

int main(int argc, char **argv)
{
	int fd, acks = 0, enxious = 0, others = 0;

	setvbuf(stdout, NULL, _IONBF, 0);
	alarm(5);

	if (argc != 2) {
		fprintf(stderr, "usage: %s /dev/i2c-N\n", argv[0]);
		return 2;
	}

	fd = open(argv[1], O_RDWR);
	if (fd < 0) {
		printf("%s: open failed (%d: %s)\n", argv[1], errno, strerror(errno));
		return 1;
	}

	printf("%s:", argv[1]);
	for (size_t i = 0; i < sizeof(probe_addrs); i++) {
		uint8_t addr = probe_addrs[i];
		uint8_t val = 0;

		if (ioctl(fd, I2C_SLAVE, addr) < 0) {
			others++;
			printf(" 0x%02x(ioctl_err=%d)", addr, errno);
			continue;
		}
		if (read(fd, &val, 1) == 1) {
			acks++;
			printf(" 0x%02x=ACK(0x%02x)", addr, val);
			if (addr == 0x11) {
				printf("[KTZ:");
				for (size_t j = 0; j < sizeof(ktz_regs); j++) {
					uint8_t r = ktz_regs[j], out = 0;
					struct i2c_msg msgs[2] = {
						{ .addr = 0x11, .flags = 0, .len = 1, .buf = &r },
						{ .addr = 0x11, .flags = I2C_M_RD, .len = 1, .buf = &out },
					};
					struct i2c_rdwr_ioctl_data data = { .msgs = msgs, .nmsgs = 2 };
					if (ioctl(fd, I2C_RDWR, &data) >= 0)
						printf(" r%02x=%02x", r, out);
					else
						printf(" r%02x=err%d", r, errno);
				}
				printf("]");
			}
		} else {
			int e = errno;
			if (e == ENXIO) {
				enxious++;
			} else {
				others++;
				printf(" 0x%02x=err%d(%s)", addr, e, strerror(e));
				break;
			}
		}
	}
	printf(" [ack=%d enxio=%d other=%d]\n", acks, enxious, others);
	close(fd);
	return 0;
}
CSRC

cat > /tmp/bt_up.c <<'BTSRC'
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#ifndef AF_BLUETOOTH
#define AF_BLUETOOTH 31
#endif
#define BTPROTO_HCI 1
#define HCIDEVUP _IOW('H', 201, int)
#define HCIGETDEVINFO _IOR('H', 211, int)

struct hci_dev_stats {
	uint32_t err_rx, err_tx, cmd_tx, evt_rx, acl_tx, acl_rx, sco_tx, sco_rx, byte_rx, byte_tx;
};

struct hci_dev_info {
	uint16_t dev_id;
	char name[8];
	uint8_t bdaddr[6];
	uint32_t flags;
	uint8_t type;
	uint8_t features[8];
	uint32_t pkt_type, link_policy, link_mode;
	uint16_t acl_mtu, acl_pkts, sco_mtu, sco_pkts;
	struct hci_dev_stats stat;
};

int main(void)
{
	int fd = socket(AF_BLUETOOTH, SOCK_RAW, BTPROTO_HCI);
	int up_ret, saved_errno = 0;
	struct hci_dev_info di;

	if (fd < 0) {
		perror("socket(AF_BLUETOOTH)");
		return 1;
	}

	up_ret = ioctl(fd, HCIDEVUP, 0);
	if (up_ret < 0) {
		saved_errno = errno;
		if (saved_errno != EALREADY)
			fprintf(stderr, "HCIDEVUP(0) failed: %d (%s)\n", saved_errno, strerror(saved_errno));
	}

	memset(&di, 0, sizeof(di));
	di.dev_id = 0;
	if (ioctl(fd, HCIGETDEVINFO, &di) < 0) {
		perror("HCIGETDEVINFO(0)");
		close(fd);
		return 1;
	}

	printf("hci0 name=%s flags=0x%x up=%d type=%u bdaddr=%02x:%02x:%02x:%02x:%02x:%02x acl_mtu=%u cmd_tx=%u evt_rx=%u\n",
	       di.name, di.flags, !!(di.flags & 1), di.type,
	       di.bdaddr[5], di.bdaddr[4], di.bdaddr[3], di.bdaddr[2], di.bdaddr[1], di.bdaddr[0],
	       di.acl_mtu, di.stat.cmd_tx, di.stat.evt_rx);

	close(fd);
	return (up_ret == 0 || saved_errno == EALREADY) ? 0 : 1;
}
BTSRC
$(command -v aarch64-linux-gnu-gcc || echo gcc) -static -O2 -Wall -o "$ROOTFS/bin/bt_up" /tmp/bt_up.c
rm -f /tmp/bt_up.c

cat > /tmp/gpu_probe.c <<'GPUSRC'
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

struct drm_version {
	int version_major;
	int version_minor;
	int version_patchlevel;
	size_t name_len;
	char *name;
	size_t date_len;
	char *date;
	size_t desc_len;
	char *desc;
};

struct drm_msm_param {
	uint32_t pipe;
	uint32_t param;
	uint64_t value;
	uint32_t len;
	uint32_t pad;
};

#define DRM_IOCTL_VERSION _IOWR('d', 0x00, struct drm_version)
#define DRM_IOCTL_MSM_GET_PARAM _IOWR('d', 0x40, struct drm_msm_param)

static void query_param(int fd, uint32_t id, const char *label)
{
	struct drm_msm_param p = { .pipe = 0x10, .param = id, .value = 0, .len = 0, .pad = 0 };
	if (ioctl(fd, DRM_IOCTL_MSM_GET_PARAM, &p) == 0)
		printf("  %-14s (0x%02x) = 0x%llx (%llu)\n", label, id,
		       (unsigned long long)p.value, (unsigned long long)p.value);
	else
		printf("  %-14s (0x%02x) = err %d (%s)\n", label, id, errno, strerror(errno));
}

int main(void)
{
	const char *node = "/dev/dri/renderD128";
	int fd = open(node, O_RDWR);
	char name[64] = {0}, date[64] = {0}, desc[128] = {0};
	struct drm_version v = {
		.name_len = sizeof(name) - 1, .name = name,
		.date_len = sizeof(date) - 1, .date = date,
		.desc_len = sizeof(desc) - 1, .desc = desc,
	};

	if (fd < 0) {
		node = "/dev/dri/card0";
		fd = open(node, O_RDWR);
	}
	if (fd < 0) {
		printf("gpu_probe: open /dev/dri/{renderD128,card0} failed: %d (%s)\n", errno, strerror(errno));
		return 1;
	}

	if (ioctl(fd, DRM_IOCTL_VERSION, &v) == 0)
		printf("DRM %s: driver=%s v%d.%d.%d desc=\"%s\"\n",
		       node, name, v.version_major, v.version_minor, v.version_patchlevel, desc);

	query_param(fd, 0x01, "GPU_ID");
	query_param(fd, 0x02, "GMEM_SIZE");
	query_param(fd, 0x03, "CHIP_ID");
	query_param(fd, 0x04, "MAX_FREQ");
	query_param(fd, 0x05, "TIMESTAMP_1");
	usleep(10000);
	query_param(fd, 0x05, "TIMESTAMP_2");
	query_param(fd, 0x06, "GMEM_BASE");
	query_param(fd, 0x07, "NR_RINGS");
	query_param(fd, 0x0d, "RAYTRACING");

	close(fd);
	return 0;
}
GPUSRC
$(command -v aarch64-linux-gnu-gcc || echo gcc) -static -O2 -Wall -o "$ROOTFS/bin/gpu_probe" /tmp/gpu_probe.c
rm -f /tmp/gpu_probe.c

cat > /tmp/fb_test.c <<'FBSRC'
#include <fcntl.h>
#include <linux/fb.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

int main(void) {
	struct fb_var_screeninfo vinfo;
	struct fb_fix_screeninfo finfo;
	int fd = open("/dev/fb0", O_RDWR);
	if (fd < 0) { perror("open /dev/fb0"); return 1; }
	if (ioctl(fd, FBIOGET_FSCREENINFO, &finfo) < 0 ||
	    ioctl(fd, FBIOGET_VSCREENINFO, &vinfo) < 0) {
		perror("fb ioctl"); close(fd); return 1;
	}
	printf("fb0: %ux%u bpp=%u line_len=%u smem_len=%u\n",
	       vinfo.xres, vinfo.yres, vinfo.bits_per_pixel, finfo.line_length, finfo.smem_len);
	uint8_t *map = mmap(NULL, finfo.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (map == MAP_FAILED) { perror("mmap"); close(fd); return 1; }
	uint32_t colors[4] = { 0xFFFFFFFF, 0xFFFF0000, 0xFF00FF00, 0xFF0000FF };
	for (uint32_t y = 0; y < vinfo.yres; y++) {
		uint32_t c = colors[(y * 4) / vinfo.yres];
		uint32_t *row = (uint32_t *)(map + y * finfo.line_length);
		for (uint32_t x = 0; x < vinfo.xres; x++)
			row[x] = c;
	}
	msync(map, finfo.smem_len, MS_SYNC);
	ioctl(fd, FBIOPAN_DISPLAY, &vinfo);
	munmap(map, finfo.smem_len);
	close(fd);
	printf("fb_test: drew 4 color bands\n");
	return 0;
}
FBSRC
$(command -v aarch64-linux-gnu-gcc || echo gcc) -static -O2 -Wall -o "$ROOTFS/bin/fb_test" /tmp/fb_test.c
rm -f /tmp/fb_test.c

cat > /tmp/touch_probe.c <<'TPSRC'
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	char path[64], name[128];
	int fd = -1, i, timeout_s = 20;
	struct pollfd pfd;
	struct input_event ev;
	time_t start;

	if (argc > 1)
		sscanf(argv[1], "%d", &timeout_s);

	for (i = 0; i < 16; i++) {
		snprintf(path, sizeof(path), "/dev/input/event%d", i);
		fd = open(path, O_RDONLY | O_NONBLOCK);
		if (fd < 0)
			continue;
		memset(name, 0, sizeof(name));
		ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name);
		printf("touch_probe: %s -> %s\n", path, name);
		if (strstr(name, "Novatek") || strstr(name, "NT36532"))
			break;
		close(fd);
		fd = -1;
	}
	if (fd < 0) {
		printf("touch_probe: Novatek event device not found\n");
		return 1;
	}

	printf("touch_probe: listening on %s for %ds...\n", path, timeout_s);
	fflush(stdout);
	start = time(NULL);
	pfd.fd = fd;
	pfd.events = POLLIN;
	while (time(NULL) - start < timeout_s) {
		int r = poll(&pfd, 1, 500);
		if (r <= 0)
			continue;
		while (read(fd, &ev, sizeof(ev)) == sizeof(ev)) {
			if (ev.type == EV_ABS) {
				const char *cn = "ABS";
				if (ev.code == ABS_MT_SLOT) cn = "SLOT";
				else if (ev.code == ABS_MT_TRACKING_ID) cn = "TRK_ID";
				else if (ev.code == ABS_MT_POSITION_X) cn = "MT_X";
				else if (ev.code == ABS_MT_POSITION_Y) cn = "MT_Y";
				printf("EVENT %s(%u) = %d\n", cn, ev.code, ev.value);
			} else if (ev.type == EV_SYN && ev.code == SYN_REPORT) {
				printf("--- SYN ---\n");
				fflush(stdout);
			}
		}
	}
	printf("touch_probe: done\n");
	close(fd);
	return 0;
}
TPSRC
$(command -v aarch64-linux-gnu-gcc || echo gcc) -static -O2 -Wall -o "$ROOTFS/bin/touch_probe" /tmp/touch_probe.c
rm -f /tmp/touch_probe.c

cat > "$ROOTFS/bin/run_diag.sh" <<'DIAG'
#!/bin/sh
export PATH=/bin:/sbin

run_bounded() {
    label="$1"
    secs="$2"
    shift 2
    echo ">>> PROBE [$label] limit=${secs}s"
    rm -f /tmp/p.out
    "$@" > /tmp/p.out 2>&1 &
    pid=$!
    t=0
    finished=0
    st=""
    while [ "$t" -lt "$secs" ]; do
        sleep 1
        t=$((t + 1))
        st=$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | awk '{print $1}')
        case "$st" in
            ""|Z|X|x)
                finished=1
                break
                ;;
        esac
    done
    if [ "$finished" -eq 1 ]; then
        wait "$pid" 2>/dev/null
        rc=$?
        echo "STATUS=DONE rc=$rc elapsed<=${t}s"
        cat /tmp/p.out 2>/dev/null
    else
        echo "STATUS=TIMEOUT_${secs}s pid=$pid state=$st"
        cat /tmp/p.out 2>/dev/null
        echo "--- wchan ---"
        cat "/proc/$pid/wchan" 2>/dev/null
        echo ""
        echo "--- stat ---"
        cat "/proc/$pid/stat" 2>/dev/null
        echo "--- stack ---"
        cat "/proc/$pid/stack" 2>/dev/null
        kill -9 "$pid" 2>/dev/null || true
    fi
}

echo "=== Section 1: D-state threads & devices_deferred ==="
for p in /proc/[0-9]*; do
    [ -r "$p/stat" ] || continue
    st=$(sed 's/.*) //' "$p/stat" 2>/dev/null | awk '{print $1}')
    if [ "$st" = "D" ]; then
        pid=$(basename "$p")
        comm=$(cat "$p/comm" 2>/dev/null)
        wch=$(cat "$p/wchan" 2>/dev/null)
        echo "D-STATE pid=$pid comm=$comm wchan=$wch"
        cat "$p/stack" 2>/dev/null
    fi
done
run_bounded "devices_deferred" 2 cat /sys/kernel/debug/devices_deferred

echo "=== Section 2: Core checks ==="
uname -a
echo "online=$(cat /sys/devices/system/cpu/online 2>/dev/null)"
run_bounded "cpufreq" 2 sh -c 'for c in 0 3 7; do echo "cpu$c: cur=$(cat /sys/devices/system/cpu/cpu$c/cpufreq/scaling_cur_freq 2>/dev/null)"; done'
run_bounded "rtc" 2 sh -c 'echo "rtc0=$(cat /sys/class/rtc/rtc0/date 2>/dev/null) $(cat /sys/class/rtc/rtc0/time 2>/dev/null)"'
run_bounded "thermal" 2 sh -c 'for z in /sys/class/thermal/thermal_zone*; do [ -d "$z" ] && echo "$(basename "$z"): type=$(cat "$z/type" 2>/dev/null) temp=$(cat "$z/temp" 2>/dev/null)"; done'
run_bounded "pinctrl" 2 cat /sys/kernel/debug/pinctrl/pinctrl-devices
run_bounded "ufs" 2 sh -c 'ls -la /sys/bus/platform/devices/1d80000.phy/driver /sys/bus/platform/devices/1d84000.ufshc/driver 2>&1; cat /proc/partitions 2>&1 | head -n 15'
run_bounded "socinfo" 2 sh -c 'for f in family machine soc_id revision serial_number; do echo "$f=$(cat /sys/devices/soc0/$f 2>/dev/null)"; done; for f in build_id chip_id foundry_id hardware_platform pmic_model pmic_model_array; do echo "dbg_$f=$(cat /sys/kernel/debug/qcom_socinfo/$f 2>/dev/null | tr "\n" " ")"; done'

echo "=== Section 3: Driver binds & PM GenPD summary ==="
run_bounded "binds" 2 sh -c 'for dev in f100000.pinctrl 406000.mailbox c300000.power-management a00000.dma-controller 800000.dma-controller ac0000.geniqup 8c0000.geniqup a80000.i2c a8c000.i2c a90000.spi 88c000.i2c 890000.i2c 10c3000.rng 221c8000.efuse af00000.clock-controller aaf0000.clock-controller 3d90000.clock-controller 25000000.system-cache-controller; do echo "$dev: driver=$(readlink /sys/bus/platform/devices/$dev/driver 2>/dev/null)"; done; ls -la /sys/bus/platform/devices/*power-controller*/driver /sys/bus/platform/devices/*smp2p*/driver /sys/bus/spmi/devices/*/driver /sys/bus/iio/devices/ /sys/class/spi_master/ /sys/bus/nvmem/devices/ /dev/i2c-* 2>&1; echo "rng_current=$(cat /sys/class/misc/hw_random/rng_current 2>/dev/null)"; for a in /sys/bus/i2c/devices/i2c-*; do [ -d "$a" ] && echo "$(basename "$a"): name=$(cat "$a/name" 2>/dev/null)"; done'
run_bounded "pm_genpd" 2 cat /sys/kernel/debug/pm_genpd/pm_genpd_summary
run_bounded "clk_subsys" 2 sh -c 'grep -E "disp_cc_pll|disp_cc_mdss_mdp_clk|video_cc_pll|video_cc_mvs0_clk|gpu_cc_pll|gpu_cc_cx_gmu|gcc_gpu_gpll0|gcc_disp_hf_axi|gcc_video_axi" /sys/kernel/debug/clk/clk_summary 2>&1 | head -n 40'
run_bounded "icc_llcc" 2 sh -c 'ls -la /sys/bus/platform/devices/*bcm-voter*/driver /sys/bus/platform/devices/*interconnect*/driver /sys/bus/platform/devices/25000000.system-cache-controller/driver 2>&1; echo "icc_summary_lines=$(wc -l < /sys/kernel/debug/interconnect/interconnect_summary 2>/dev/null)"; head -n 30 /sys/kernel/debug/interconnect/interconnect_summary 2>&1'
run_bounded "pcie" 2 sh -c 'for dev in 1c00000.pcie 1c06000.phy wcn7850-pmu; do echo "$dev: driver=$(readlink /sys/bus/platform/devices/$dev/driver 2>/dev/null)"; done; ls -la /sys/bus/platform/devices/*wcn7850*/driver /sys/bus/platform/devices/*wifi*/driver 2>&1; for p in /sys/bus/pci/devices/*; do [ -d "$p" ] && echo "PCI $(basename "$p"): vendor=$(cat "$p/vendor" 2>/dev/null) device=$(cat "$p/device" 2>/dev/null) class=$(cat "$p/class" 2>/dev/null) speed=$(cat "$p/current_link_speed" 2>/dev/null) width=$(cat "$p/current_link_width" 2>/dev/null) driver=$(readlink "$p/driver" 2>/dev/null)"; done; dmesg | grep -iE "its|pcie|qmp|wcn|pwrctrl|pwrseq" | head -n 40'
run_bounded "wifi" 8 sh -c 'ls -l /lib/firmware/ath12k/WCN7850/hw2.0; i=0; while [ $i -lt 6 ] && [ ! -d /sys/class/net/wlan0 ]; do sleep 1; i=$((i + 1)); done; echo "pci_drv=$(readlink /sys/bus/pci/devices/0000:01:00.0/driver 2>/dev/null) subsys=$(cat /sys/bus/pci/devices/0000:01:00.0/subsystem_vendor 2>/dev/null):$(cat /sys/bus/pci/devices/0000:01:00.0/subsystem_device 2>/dev/null)"; for r in /sys/class/rfkill/rfkill*; do [ -d "$r" ] && echo "rfkill=$(basename "$r") name=$(cat "$r/name" 2>/dev/null) type=$(cat "$r/type" 2>/dev/null) soft=$(cat "$r/soft" 2>/dev/null) hard=$(cat "$r/hard" 2>/dev/null) state=$(cat "$r/state" 2>/dev/null)"; done; ls -la /sys/class/net /sys/class/ieee80211 2>&1; if [ -d /sys/class/net/wlan0 ]; then ip link set wlan0 up 2>&1; ip link show wlan0; echo "mac=$(cat /sys/class/net/wlan0/address 2>/dev/null) state=$(cat /sys/class/net/wlan0/operstate 2>/dev/null)"; else echo "NO_WLAN0"; fi; grep -iE "ath12k|mhi|pcie" /proc/interrupts || true; dmesg | grep -iE "ath12k|mhi|qrtr|qmi|wlan0|firmware|regdb|board|rfkill" | tail -n 60'
run_bounded "bluetooth" 18 sh -c 'ls -l /lib/firmware/qca/hmtbtfw20.tlv /lib/firmware/qca/hmtnv20.bin; echo "uart10_drv=$(readlink /sys/bus/platform/devices/888000.serial/driver 2>/dev/null)"; echo "pmk8550_gpio_drv=$(readlink /sys/bus/platform/devices/*b800*/driver 2>/dev/null)"; ls -la /sys/bus/serial/devices 2>&1; w=0; while [ $w -lt 8 ]; do if dmesg | grep -qE "QCA setup on UART is completed|Retry BT power ON:2"; then break; fi; sleep 1; w=$((w + 1)); done; grep -iE "wrap1_s2|qupv3_wrap_1|clk1|xo-board|bi_tcxo|rf_clk1" /sys/kernel/debug/clk/clk_summary 2>&1; grep -E "gpio(3|34|35|40|41|42|43|80|156)[^0-9]" /sys/kernel/debug/gpio 2>&1; grep -E "vreg_s1b|vreg_s2b|vreg_s3b|vreg_s3e|vreg_l5b|vreg_l7b|vreg_l4e" /sys/kernel/debug/regulator/regulator_summary 2>/dev/null; grep -iE "qup1_core|qhm_qup1|qhs_qup1" /sys/kernel/debug/interconnect/interconnect_summary 2>&1; grep -iE "geni_serial|888000" /proc/interrupts 2>&1; dmesg | grep -iE "888000|geni|serdev|hci|qca|bluetooth|wcn7850|pwrseq" | tail -n 50; if [ -d /sys/class/bluetooth/hci0 ]; then echo "hci0_sysfs_present"; /bin/bt_up 2>&1 || echo "bt_up_rc=$?"; cat /sys/class/bluetooth/hci0/address /sys/class/bluetooth/hci0/name 2>&1; dmesg | grep -iE "hci0|qca|bluetooth|888000" | tail -n 30; else echo "NO_HCI0"; fi'
run_bounded "rproc_pmic_glink" 14 sh -c 'w=0; while [ $w -lt 8 ]; do if [ -e /sys/class/power_supply/qcom-battmgr-bat/capacity ] && [ "$(cat /sys/class/power_supply/qcom-battmgr-bat/capacity 2>/dev/null)" != "" ]; then break; fi; sleep 1; w=$((w + 1)); done; for r in /sys/class/remoteproc/remoteproc*; do [ -d "$r" ] && echo "$(basename "$r"): name=$(cat "$r/name" 2>/dev/null) state=$(cat "$r/state" 2>/dev/null) fw=$(cat "$r/firmware" 2>/dev/null)"; done; echo "--- rpmsg devices ---"; for d in /sys/bus/rpmsg/devices/*; do [ -e "$d" ] && echo "$(basename "$d") -> $(basename "$(readlink "$d/driver" 2>/dev/null)" 2>/dev/null)"; done; echo "--- fastrpc nodes ---"; ls -la /dev/fastrpc* 2>&1; echo "--- power_supply ---"; for ps in /sys/class/power_supply/*; do [ -d "$ps" ] || continue; echo "=== $(basename "$ps") ==="; cat "$ps/uevent" 2>&1; for attr in status health present charge_type capacity voltage_now voltage_max voltage_ocv current_now power_now temp technology cycle_count charge_counter charge_full_design charge_full model_name online usb_type; do [ -e "$ps/$attr" ] && echo "  $attr=$(cat "$ps/$attr" 2>&1)"; done; done; echo "--- typec ---"; ls -la /sys/class/typec/ 2>&1; for tc in /sys/class/typec/port*; do [ -d "$tc" ] && echo "$(basename "$tc"): data_role=$(cat "$tc/data_role" 2>/dev/null) power_role=$(cat "$tc/power_role" 2>/dev/null) pwr_opmode=$(cat "$tc/power_operation_mode" 2>/dev/null) orientation=$(cat "$tc/orientation" 2>/dev/null)"; done; dmesg | grep -iE "remoteproc|rproc|adsp|cdsp|glink|pmic_glink|battmgr|ucsi|fastrpc|pd_mapper|pdr|qrtr" | tail -n 60'
run_bounded "gpu_adreno" 12 sh -c 'for dev in 3da0000.iommu 3d6a000.gmu 3d00000.gpu; do echo "$dev: driver=$(readlink /sys/bus/platform/devices/$dev/driver 2>/dev/null)"; done; ls -la /dev/dri /sys/class/drm /sys/class/devfreq 2>&1; /bin/gpu_probe 2>&1 || echo "gpu_probe_rc=$?"; for df in /sys/class/devfreq/*; do [ -d "$df" ] && echo "devfreq $(basename "$df"): cur=$(cat "$df/cur_freq" 2>/dev/null) min=$(cat "$df/min_freq" 2>/dev/null) max=$(cat "$df/max_freq" 2>/dev/null) avail=$(cat "$df/available_frequencies" 2>/dev/null)"; done; grep -iE "gpu_cc|gmu|gfx" /sys/kernel/debug/clk/clk_summary 2>&1; grep -iE "gpu|gmu|gfx" /sys/kernel/debug/pm_genpd/pm_genpd_summary 2>&1; grep -iE "gpu|gmu|kgsl|3da0000" /proc/interrupts 2>&1; dmesg | grep -iE "drm|msm|adreno|gmu|3d00000|3d6a000|3da0000|zap" | tail -n 60'
run_bounded "display_drm" 5 sh -c 'for dev in ae00000.display-subsystem ae01000.display-controller ae94000.dsi ae95000.phy ae96000.dsi ae97000.phy; do echo "$dev: driver=$(readlink /sys/bus/platform/devices/$dev/driver 2>/dev/null)"; done; echo "--- backlight ---"; for b in /sys/class/backlight/*; do [ -d "$b" ] && echo "$(basename "$b"): br=$(cat "$b/brightness" 2>/dev/null) actual=$(cat "$b/actual_brightness" 2>/dev/null) max=$(cat "$b/max_brightness" 2>/dev/null) bl_power=$(cat "$b/bl_power" 2>/dev/null)"; done; echo "--- connector ---"; for f in status enabled dpms modes; do [ -e "/sys/class/drm/card0-DSI-1/$f" ] && echo "$f=$(cat "/sys/class/drm/card0-DSI-1/$f" 2>/dev/null | tr "\n" " ")"; done; echo "--- disp clks ---"; grep -iE "disp_cc|dsi|mdp" /sys/kernel/debug/clk/clk_summary 2>&1 | grep -v "dptx" | head -n 40; echo "--- gpio ---"; grep -E "gpio(2|16|17|18|19|26|54|74|75|133)[^0-9]" /sys/kernel/debug/gpio 2>&1; echo "--- spi4 pins/clk ---"; grep -E "gpio(16|17|18|19|26|54)[^0-9]" /sys/kernel/debug/pinctrl/f100000.pinctrl/pinconf-pins 2>&1; grep -i "wrap1_s4" /sys/kernel/debug/clk/clk_summary 2>&1; echo "--- irq ---"; grep -iE "mdss|dpu|dsi|ae0|ae9" /proc/interrupts 2>&1; echo "--- dri/0/state ---"; head -n 55 /sys/kernel/debug/dri/0/state 2>&1; echo "--- fb_test ---"; /bin/fb_test 2>&1'

echo "=== Section 4: Live bounded VADC, I2C, TRNG, QFPROM & /proc/interrupts ==="
found_iio=0
for d in /sys/bus/iio/devices/iio:device*; do
    [ -d "$d" ] || continue
    found_iio=1
    echo "--- $(basename "$d") name=$(cat "$d/name" 2>/dev/null) ---"
    ls -1 "$d" 2>&1
    for ch in "$d"/in_*_input; do
        [ -e "$ch" ] || continue
        run_bounded "vadc:$(basename "$d")/$(basename "$ch")" 2 cat "$ch"
    done
done
[ "$found_iio" -eq 0 ] && echo "NO_IIO_DEVICES"

found_i2c=0
for dev in /dev/i2c-*; do
    [ -e "$dev" ] || continue
    found_i2c=1
    run_bounded "i2c:$(basename "$dev")" 4 /bin/i2c_probe "$dev"
done
[ "$found_i2c" -eq 0 ] && echo "NO_I2C_DEVNODES"

run_bounded "trng" 2 sh -c 'cat /sys/class/misc/hw_random/rng_current 2>&1; dd if=/dev/hwrng bs=16 count=1 2>/dev/null | hexdump -C'
run_bounded "qfprom" 2 sh -c 'ls -la /sys/bus/nvmem/devices/ 2>&1; if [ -e /sys/bus/nvmem/devices/qfprom0/nvmem ]; then dd if=/sys/bus/nvmem/devices/qfprom0/nvmem bs=32 count=1 2>/dev/null | hexdump -C; else f=$(ls /sys/bus/nvmem/devices/*/nvmem 2>/dev/null | head -n 1); [ -n "$f" ] && dd if="$f" bs=32 count=1 2>/dev/null | hexdump -C; fi'
run_bounded "interrupts" 2 head -n 35 /proc/interrupts

echo "=== Section 5: Post-probe dmesg tail ==="
dmesg | tail -n 80
DIAG
chmod +x "$ROOTFS/bin/run_diag.sh"

cat > "$ROOTFS/init" <<'EOF'
#!/bin/sh
export PATH=/bin:/sbin

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /sys/kernel/config /sys/kernel/debug /sys/fs/pstore
mount -t devpts devpts /dev/pts
mount -t debugfs none /sys/kernel/debug 2>/dev/null || true

# Background safety watchdog (65s guarantees HyperOS recovery)
( sleep 65; echo b > /proc/sysrq-trigger 2>/dev/null; reboot -f ) &

# Wait up to 4s ONLY for /dev/ttyGS0 or usb0
for i in 1 2 3 4 5 6 7 8; do
    [ -e /dev/ttyGS0 ] && break
    sleep 0.5 2>/dev/null || sleep 1
done

start_usb0() {
    if [ -d /sys/class/net/usb0 ]; then
        (
            ip link set lo up 2>/dev/null || true
            ip link set usb0 up 2>/dev/null || true
            ip addr add 172.16.42.1/24 dev usb0 2>/dev/null || true
            mkdir -p /var/lib/misc /var/log
            echo "MUYU_USB_ETH_OK" > /var/log/ping.txt
            touch /var/lib/misc/udhcpd.leases
            cat > /etc/udhcpd.conf <<'DHCP'
start 172.16.42.2
end 172.16.42.10
interface usb0
lease_file /var/lib/misc/udhcpd.leases
opt subnet 255.255.255.0
opt lease 86400
DHCP
            udhcpd /etc/udhcpd.conf 2>/dev/null || true
            telnetd -l /bin/sh -p 23 2>/dev/null || true
            httpd -p 8080 -h /var/log 2>/dev/null || true
        ) &
    fi
}

if [ -e /dev/ttyGS0 ]; then
    (
        exec </dev/ttyGS0 >/dev/ttyGS0 2>&1
        echo "=== MUYU READY ==="
        exec sh
    ) &
    start_usb0
    sleep 55
elif [ -d /sys/class/net/usb0 ]; then
    start_usb0
    sleep 35
else
    sleep 10
fi
echo b > /proc/sysrq-trigger 2>/dev/null || reboot -f
EOF
chmod +x "$ROOTFS/init"

(cd "$ROOTFS" && find . -print0 | cpio --null -ov --format=newc | gzip -9 > "$OUT_DIR/initramfs.cpio.gz")
ls -lh "$OUT_DIR/initramfs.cpio.gz"
