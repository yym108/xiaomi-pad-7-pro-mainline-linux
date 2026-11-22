/*
 * Static ARM64 Qualcomm SM8635 A/B slot switcher (works on both Android & Linux).
 * - Resolves LUN 0 (userdata) and LUN 4 (boot_a/boot_b) dynamically via /sys/class/block.
 * - Swaps Qualcomm inactive Type GUID (77036CD4-03D5-42BB-8ED1-37E5A88BAA34) across *_a/*_b.
 * - Updates boot_a/boot_b flags, LUN 0 countrycode/vbmeta_system active bits, and UFS bBootLunEn.
 * - Preserves Qualcomm's exact Backup GPT PartitionEntryLBA.
 *
 * Usage:
 *   switch-slot a [--no-reboot]   # Switch to Slot A (HyperOS)
 *   switch-slot b [--no-reboot]   # Switch to Slot B (Ubuntu Linux)
 *   switch-slot show              # Print current slot & UFS Boot LUN
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <unistd.h>

#define SECTOR 4096
#define SG_IO 0x2285

static const uint8_t INACTIVE_GUID[16] = {
	0xd4, 0x6c, 0x03, 0x77, 0xd5, 0x03, 0xbb, 0x42,
	0x8e, 0xd1, 0x37, 0xe5, 0xa8, 0x8b, 0xaa, 0x34
};

struct sg_io_v4 {
	int32_t guard;
	uint32_t protocol;
	uint32_t subprotocol;
	uint32_t request_len;
	uint64_t request;
	uint64_t request_tag;
	uint32_t request_attr;
	uint32_t request_priority;
	uint32_t request_extra;
	uint32_t max_response_len;
	uint64_t response;
	uint32_t dout_iovec_count;
	uint32_t dout_xfer_len;
	uint32_t din_iovec_count;
	uint32_t din_xfer_len;
	uint64_t dout_xferp;
	uint64_t din_xferp;
	uint32_t timeout;
	uint32_t flags;
	uint64_t usr_ptr;
	uint32_t spare_in;
	uint32_t driver_status;
	uint32_t transport_status;
	uint32_t device_status;
	uint32_t retry_delay;
	uint32_t info;
	uint32_t duration;
	uint32_t response_len;
	int32_t din_resid;
	int32_t dout_resid;
	uint64_t generated_tag;
	uint32_t spare_out;
	uint32_t padding;
};

static uint32_t crc32_calc(const uint8_t *buf, size_t len)
{
	uint32_t crc = 0xFFFFFFFFU;
	for (size_t i = 0; i < len; i++) {
		crc ^= buf[i];
		for (int j = 0; j < 8; j++)
			crc = (crc >> 1) ^ (0xEDB88320U & (-(int32_t)(crc & 1)));
	}
	return ~crc;
}

static uint32_t get_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
	       ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint64_t get_le64(const uint8_t *p)
{
	return (uint64_t)get_le32(p) | ((uint64_t)get_le32(p + 4) << 32);
}

static void put_le32(uint8_t *p, uint32_t v)
{
	p[0] = v & 0xff; p[1] = (v >> 8) & 0xff;
	p[2] = (v >> 16) & 0xff; p[3] = (v >> 24) & 0xff;
}

static void put_le64(uint8_t *p, uint64_t v)
{
	put_le32(p, (uint32_t)v);
	put_le32(p + 4, (uint32_t)(v >> 32));
}

static int find_disk_with(const char *partname, char *out, size_t out_sz)
{
	DIR *d = opendir("/sys/class/block");
	if (!d)
		return -1;
	struct dirent *de;
	char needle[128];
	snprintf(needle, sizeof(needle), "PARTNAME=%s\n", partname);

	while ((de = readdir(d)) != NULL) {
		if (strncmp(de->d_name, "sd", 2) != 0)
			continue;
		char uev[256];
		snprintf(uev, sizeof(uev), "/sys/class/block/%s/uevent", de->d_name);
		FILE *f = fopen(uev, "r");
		if (!f)
			continue;
		char buf[1024];
		size_t n = fread(buf, 1, sizeof(buf) - 1, f);
		fclose(f);
		buf[n] = '\0';
		if (strstr(buf, needle)) {
			char disk[64];
			snprintf(disk, sizeof(disk), "%s", de->d_name);
			size_t len = strlen(disk);
			while (len > 0 && disk[len - 1] >= '0' && disk[len - 1] <= '9')
				disk[--len] = '\0';
			/* Android uses /dev/block/sdX, Linux uses /dev/sdX */
			snprintf(out, out_sz, "/dev/block/%s", disk);
			if (access(out, F_OK) != 0)
				snprintf(out, out_sz, "/dev/%s", disk);
			closedir(d);
			return 0;
		}
	}
	closedir(d);
	return -1;
}

static void utf16le_to_ascii(const uint8_t *src, char *dst, size_t max_chars)
{
	size_t i;
	for (i = 0; i < max_chars - 1 && i < 36; i++) {
		uint16_t ch = src[i * 2] | (src[i * 2 + 1] << 8);
		if (ch == 0)
			break;
		dst[i] = (ch < 128) ? (char)ch : '?';
	}
	dst[i] = '\0';
}

static int set_ufs_boot_lun(uint32_t lun_val)
{
	const char *paths[] = { "/dev/bsg/ufs-bsg0", "/dev/ufs-bsg0", NULL };
	int fd = -1;
	for (int i = 0; paths[i]; i++) {
		fd = open(paths[i], O_RDWR);
		if (fd >= 0)
			break;
	}
	if (fd < 0) {
		fprintf(stderr, "[slot] Warning: ufs-bsg0 not found (%s)\n", strerror(errno));
		return -1;
	}

	uint8_t req[36];
	uint8_t rsp[64];
	memset(req, 0, sizeof(req));
	memset(rsp, 0, sizeof(rsp));
	/* msgcode = 0x16 (UPIU_TRANSACTION_QUERY_REQ) */
	put_le32(req + 0, 0x16);
	req[4] = 0x16; /* transaction_code */
	req[9] = 0x81; /* UPIU_QUERY_FUNC_STANDARD_WRITE_REQUEST */
	req[16] = 0x04; /* UPIU_QUERY_OPCODE_WRITE_ATTR */
	req[17] = 0x00; /* QUERY_ATTR_IDN_BOOT_LU_EN */
	/* value (big-endian at offset 24) */
	req[24] = (lun_val >> 24) & 0xff;
	req[25] = (lun_val >> 16) & 0xff;
	req[26] = (lun_val >> 8) & 0xff;
	req[27] = lun_val & 0xff;

	struct sg_io_v4 io;
	memset(&io, 0, sizeof(io));
	io.guard = 'Q';
	io.protocol = 0;    /* BSG_PROTOCOL_SCSI */
	io.subprotocol = 2; /* BSG_SUB_PROTOCOL_SCSI_TRANSPORT */
	io.request_len = sizeof(req);
	io.request = (uintptr_t)req;
	io.max_response_len = sizeof(rsp);
	io.response = (uintptr_t)rsp;
	io.timeout = 5000;

	int ret = ioctl(fd, SG_IO, &io);
	close(fd);
	if (ret < 0) {
		fprintf(stderr, "[slot] SG_IO failed: %s\n", strerror(errno));
		return -1;
	}
	int32_t res = (int32_t)get_le32(rsp);
	if (res != 0) {
		fprintf(stderr, "[slot] UFS bBootLunEn=%u failed: res=%d\n", lun_val, res);
		return -1;
	}
	return 0;
}

static int update_lun(const char *disk, int target_slot, int is_lun4)
{
	int fd = open(disk, O_RDWR);
	if (fd < 0) {
		fprintf(stderr, "[slot] Cannot open %s: %s\n", disk, strerror(errno));
		return -1;
	}

	uint8_t hdr[SECTOR], orig_bak[SECTOR];
	if (pread(fd, hdr, SECTOR, SECTOR) != SECTOR) { close(fd); return -1; }
	if (memcmp(hdr, "EFI PART", 8) != 0) { close(fd); return -1; }

	uint32_t hsz = get_le32(hdr + 12);
	uint32_t hcrc = get_le32(hdr + 16);
	uint64_t cur_lba = get_le64(hdr + 24);
	uint64_t bak_lba = get_le64(hdr + 32);
	uint64_t last_lba = get_le64(hdr + 48);
	uint64_t part_lba = get_le64(hdr + 72);
	uint32_t num_parts = get_le32(hdr + 80);
	uint32_t part_sz = get_le32(hdr + 84);
	uint32_t pcrc = get_le32(hdr + 88);

	uint8_t chk[92];
	memcpy(chk, hdr, 92);
	put_le32(chk + 16, 0);
	if (hsz != 92 || part_sz != 128 || crc32_calc(chk, 92) != hcrc) {
		fprintf(stderr, "[slot] %s primary GPT CRC mismatch!\n", disk);
		close(fd);
		return -1;
	}

	size_t ent_len = (size_t)num_parts * part_sz;
	uint8_t *entries = malloc(ent_len);
	if (!entries || pread(fd, entries, ent_len, (off_t)(part_lba * SECTOR)) != (ssize_t)ent_len ||
	    crc32_calc(entries, ent_len) != pcrc) {
		fprintf(stderr, "[slot] %s entries CRC mismatch!\n", disk);
		free(entries);
		close(fd);
		return -1;
	}

	uint64_t bak_part_lba = last_lba + 1;
	if (pread(fd, orig_bak, SECTOR, (off_t)(bak_lba * SECTOR)) == SECTOR &&
	    memcmp(orig_bak, "EFI PART", 8) == 0) {
		uint64_t ob = get_le64(orig_bak + 72);
		if (ob >= last_lba + 1 && ob < bak_lba)
			bak_part_lba = ob;
	}

	if (is_lun4) {
		int idx_a = -1, idx_b = -1, swapped = 0;
		for (uint32_t i = 0; i < num_parts; i++) {
			char na[40];
			utf16le_to_ascii(entries + i * part_sz + 56, na, sizeof(na));
			if (strcmp(na, "boot_a") == 0) idx_a = i;
			if (strcmp(na, "boot_b") == 0) idx_b = i;
			size_t l = strlen(na);
			if (l > 2 && strcmp(na + l - 2, "_a") == 0) {
				char nb[40];
				snprintf(nb, sizeof(nb), "%.*s_b", (int)(l - 2), na);
				for (uint32_t j = 0; j < num_parts; j++) {
					char n2[40];
					utf16le_to_ascii(entries + j * part_sz + 56, n2, sizeof(n2));
					if (strcmp(n2, nb) == 0) {
						uint8_t *ga = entries + i * part_sz;
						uint8_t *gb = entries + j * part_sz;
						int a_inact = (memcmp(ga, INACTIVE_GUID, 16) == 0);
						int b_inact = (memcmp(gb, INACTIVE_GUID, 16) == 0);
						if ((target_slot == 0 && a_inact && !b_inact) ||
						    (target_slot == 1 && b_inact && !a_inact)) {
							uint8_t tmp[16];
							memcpy(tmp, ga, 16);
							memcpy(ga, gb, 16);
							memcpy(gb, tmp, 16);
							swapped++;
						}
						break;
					}
				}
			}
		}
		if (idx_a >= 0 && idx_b >= 0) {
			uint64_t aa = get_le64(entries + idx_a * part_sz + 48);
			uint64_t ab = get_le64(entries + idx_b * part_sz + 48);
			uint64_t m48 = (1ULL << 48) - 1;
			uint64_t ACT = (3ULL << 48) | (1ULL << 50) | (7ULL << 51) | (1ULL << 54);
			uint64_t INA = (2ULL << 48) | (0ULL << 50) | (7ULL << 51) | (1ULL << 54);
			put_le64(entries + idx_a * part_sz + 48, (aa & m48) | (target_slot == 0 ? ACT : INA));
			put_le64(entries + idx_b * part_sz + 48, (ab & m48) | (target_slot == 1 ? ACT : INA));
		}
		printf("[slot] %s: swapped %d GUID pairs, bak_part_lba=%llu\n",
		       disk, swapped, (unsigned long long)bak_part_lba);
	} else {
		uint64_t bit50 = 1ULL << 50;
		for (uint32_t i = 0; i < num_parts; i++) {
			char nm[40];
			utf16le_to_ascii(entries + i * part_sz + 56, nm, sizeof(nm));
			int is_a = (strcmp(nm, "countrycode_a") == 0 || strcmp(nm, "vbmeta_system_a") == 0);
			int is_b = (strcmp(nm, "countrycode_b") == 0 || strcmp(nm, "vbmeta_system_b") == 0);
			if (is_a || is_b) {
				uint64_t fl = get_le64(entries + i * part_sz + 48);
				if ((is_a && target_slot == 0) || (is_b && target_slot == 1))
					fl |= bit50;
				else
					fl &= ~bit50;
				put_le64(entries + i * part_sz + 48, fl);
			}
		}
	}

	put_le32(hdr + 88, crc32_calc(entries, ent_len));
	put_le32(hdr + 16, 0);
	put_le32(hdr + 16, crc32_calc(hdr, hsz));

	uint8_t bak_hdr[SECTOR];
	memcpy(bak_hdr, hdr, SECTOR);
	put_le64(bak_hdr + 24, bak_lba);
	put_le64(bak_hdr + 32, cur_lba);
	put_le64(bak_hdr + 72, bak_part_lba);
	put_le32(bak_hdr + 16, 0);
	put_le32(bak_hdr + 16, crc32_calc(bak_hdr, hsz));

	if (pwrite(fd, entries, ent_len, (off_t)(part_lba * SECTOR)) != (ssize_t)ent_len ||
	    pwrite(fd, hdr, SECTOR, (off_t)(cur_lba * SECTOR)) != SECTOR ||
	    pwrite(fd, entries, ent_len, (off_t)(bak_part_lba * SECTOR)) != (ssize_t)ent_len ||
	    pwrite(fd, bak_hdr, SECTOR, (off_t)(bak_lba * SECTOR)) != SECTOR) {
		fprintf(stderr, "[slot] Write failed on %s: %s\n", disk, strerror(errno));
		free(entries);
		close(fd);
		return -1;
	}
	fsync(fd);
	free(entries);
	close(fd);
	return 0;
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "Usage: %s [a|b] [--no-reboot]\n", argv[0]);
		return 1;
	}
	int target = (argv[1][0] == 'b' || argv[1][0] == 'B') ? 1 : 0;
	int no_reboot = (argc > 2 && strcmp(argv[2], "--no-reboot") == 0);

	char lun4[64], lun0[64];
	if (find_disk_with("boot_a", lun4, sizeof(lun4)) != 0 ||
	    find_disk_with("userdata", lun0, sizeof(lun0)) != 0) {
		fprintf(stderr, "[slot] Failed to find UFS LUNs!\n");
		return 1;
	}

	if (update_lun(lun4, target, 1) != 0)
		return 1;
	if (update_lun(lun0, target, 0) != 0)
		return 1;
	set_ufs_boot_lun(target == 0 ? 1 : 2);
	printf("[slot] Switched active slot to %c successfully!\n", target == 0 ? 'A' : 'B');

	if (!no_reboot) {
		sync();
		sync();
		usleep(200000);
		/* Try clean Android/Linux reboot, fallback to sysrq */
		if (system("/system/bin/svc power reboot 2>/dev/null") != 0) {
			FILE *f = fopen("/proc/sys/kernel/sysrq", "w");
			if (f) { fputs("1\n", f); fclose(f); }
			f = fopen("/proc/sysrq-trigger", "w");
			if (f) { fputs("b\n", f); fclose(f); }
			reboot(RB_AUTOBOOT);
		}
	}
	return 0;
}
