// SPDX-License-Identifier: MIT
/*
 * gbos-boot: the pendrive's bootloader, run as PID 1 of the stage-1 initramfs (Fedora's kernel,
 * which has kexec; ours does not, and adding it would change the core and break module CRCs).
 *
 * Does what boot/run_vm.sh + boot/slot.py do for the VM: find the gbos disk (GPT with a
 * "gbos_layer" partition), pick the A/B slot from misc's bootloader_control (spending a try on an
 * unverified slot), measure vbmeta_<slot>, and kexec our kernel with the Android ramdisk and a
 * command line carrying slot, vbmeta size/digest and the disk's controller as boot device.
 *
 *   gbos-boot                       as /init of the stage-1 initramfs
 *   gbos-boot --plan DISK [--write] print SLOT/VBMETA_SIZE/VBMETA_DIGEST for a disk image (tests)
 *   gbos-boot --boot-device SYSPATH print the androidboot.boot_devices value for a block device
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#define SECTOR 512
#define CONTROL_OFFSET 2048 /* offsetof(bootloader_message_ab, slot_suffix) */
#define CONTROL_SIZE 32
#define CONTROL_MAGIC 0x42414342u
#define AVB_HEADER 256
#define MAX_VBMETA (64 * 1024)

/* ---- sha256 (FIPS 180-4) ------------------------------------------------------------------ */

static const uint32_t K[64] = {
	0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
	0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
	0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
	0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
	0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
	0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
	0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
	0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(uint32_t h[8], const uint8_t *p)
{
	uint32_t w[64], a, b, c, d, e, f, g, k;
	for (int i = 0; i < 16; i++)
		w[i] = (uint32_t)p[4 * i] << 24 | p[4 * i + 1] << 16 | p[4 * i + 2] << 8 | p[4 * i + 3];
	for (int i = 16; i < 64; i++) {
		uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
		uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
		w[i] = w[i - 16] + s0 + w[i - 7] + s1;
	}
	a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], k = h[7];
	for (int i = 0; i < 64; i++) {
		uint32_t t1 = k + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
		uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
		k = g, g = f, f = e, e = d + t1, d = c, c = b, b = a, a = t1 + t2;
	}
	h[0] += a, h[1] += b, h[2] += c, h[3] += d, h[4] += e, h[5] += f, h[6] += g, h[7] += k;
}

static void sha256_hex(const uint8_t *data, size_t len, char out[65])
{
	uint32_t h[8] = { 0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
			  0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19 };
	uint8_t tail[128] = { 0 };
	size_t full = len / 64 * 64, rest = len - full, tlen = rest < 56 ? 64 : 128;
	for (size_t i = 0; i < full; i += 64)
		sha256_block(h, data + i);
	memcpy(tail, data + full, rest);
	tail[rest] = 0x80;
	uint64_t bits = (uint64_t)len * 8;
	for (int i = 0; i < 8; i++)
		tail[tlen - 1 - i] = (uint8_t)(bits >> (8 * i));
	for (size_t i = 0; i < tlen; i += 64)
		sha256_block(h, tail + i);
	for (int i = 0; i < 8; i++)
		snprintf(out + 8 * i, 9, "%08x", h[i]);
}

/* ---- crc32 (zlib) --------------------------------------------------------------------------- */

static uint32_t crc32(const uint8_t *p, size_t n)
{
	uint32_t c = 0xffffffffu;
	while (n--) {
		c ^= *p++;
		for (int k = 0; k < 8; k++)
			c = c & 1 ? (c >> 1) ^ 0xedb88320u : c >> 1;
	}
	return ~c;
}

/* ---- GPT -------------------------------------------------------------------------------- */

struct part { char name[37]; uint64_t start; };

struct disk {
	int fd;
	struct part parts[128];
	int n;
};

static int pread_full(int fd, void *buf, size_t len, off_t off)
{
	return pread(fd, buf, len, off) == (ssize_t)len ? 0 : -1;
}

static int read_gpt(struct disk *d)
{
	uint8_t h[SECTOR];
	if (pread_full(d->fd, h, SECTOR, SECTOR) || memcmp(h, "EFI PART", 8))
		return -1;
	uint64_t lba;
	uint32_t count, size;
	memcpy(&lba, h + 72, 8);
	memcpy(&count, h + 80, 4);
	memcpy(&size, h + 84, 4);
	if (size != 128 || count > 128)
		return -1;
	uint8_t e[128 * 128];
	if (pread_full(d->fd, e, (size_t)count * size, (off_t)(lba * SECTOR)))
		return -1;
	d->n = 0;
	for (uint32_t i = 0; i < count; i++) {
		const uint8_t *p = e + i * size;
		static const uint8_t zero[16];
		if (!memcmp(p, zero, 16))
			continue;
		struct part *q = &d->parts[d->n++];
		memcpy(&q->start, p + 32, 8);
		for (int c = 0; c < 36; c++)
			q->name[c] = (char)p[56 + 2 * c]; /* partition names are ASCII */
		q->name[36] = 0;
	}
	return 0;
}

static const struct part *find_part(const struct disk *d, const char *name)
{
	for (int i = 0; i < d->n; i++)
		if (!strcmp(d->parts[i].name, name))
			return &d->parts[i];
	return NULL;
}

/* ---- A/B (hardware/interfaces/boot/1.1/default/boot_control, as boot/slot.py) ----------- */

struct slot_info { int priority, tries, ok; };

static int parse_control(const uint8_t *raw, struct slot_info s[4], int *nb)
{
	uint32_t magic, crc;
	memcpy(&magic, raw + 4, 4);
	memcpy(&crc, raw + 28, 4);
	if (magic != CONTROL_MAGIC || crc32(raw, 28) != crc)
		return -1;
	*nb = raw[9] & 7;
	for (int i = 0; i < *nb && i < 4; i++) {
		uint8_t b = raw[12 + 2 * i];
		s[i] = (struct slot_info){ b & 0xf, (b >> 4) & 7, b >> 7 };
	}
	return 0;
}

/* Slot to boot; *spend is set when a try must be written back. */
static int choose_slot(const struct slot_info *s, int nb, int *spend)
{
	int best = -1;
	*spend = 0;
	for (int i = 0; i < nb; i++)
		if (s[i].priority && (s[i].ok || s[i].tries) && (best < 0 || s[i].priority > s[best].priority))
			best = i;
	if (best < 0)
		return 0;
	*spend = !s[best].ok;
	return best;
}

struct plan { char slot[3]; uint32_t vbmeta_size; char digest[65]; };

static int make_plan(struct disk *d, int write_back, struct plan *out)
{
	const struct part *misc = find_part(d, "misc");
	if (!misc)
		return -1;
	off_t ctl_off = (off_t)(misc->start * SECTOR + CONTROL_OFFSET);
	uint8_t raw[CONTROL_SIZE];
	if (pread_full(d->fd, raw, CONTROL_SIZE, ctl_off))
		return -1;
	struct slot_info s[4];
	int nb = 0, spend = 0, idx = 0;
	if (!parse_control(raw, s, &nb))
		idx = choose_slot(s, nb, &spend);
	if (spend && write_back) {
		raw[12 + 2 * idx] = (uint8_t)((s[idx].priority & 0xf) | (((s[idx].tries - 1) & 7) << 4));
		uint32_t crc = crc32(raw, 28);
		memcpy(raw + 28, &crc, 4);
		if (pwrite(d->fd, raw, CONTROL_SIZE, ctl_off) != CONTROL_SIZE || fsync(d->fd))
			return -1;
	}
	snprintf(out->slot, sizeof out->slot, "_%c", 'a' + idx);
	char name[16];
	snprintf(name, sizeof name, "vbmeta%s", out->slot);
	const struct part *vb = find_part(d, name);
	if (!vb)
		return -1;
	static uint8_t buf[MAX_VBMETA];
	if (pread_full(d->fd, buf, AVB_HEADER, (off_t)(vb->start * SECTOR)) || memcmp(buf, "AVB0", 4))
		return -1;
	uint64_t auth = 0, aux = 0;
	for (int i = 0; i < 8; i++)
		auth = auth << 8 | buf[12 + i], aux = aux << 8 | buf[20 + i];
	uint64_t size = AVB_HEADER + auth + aux;
	if (size > MAX_VBMETA || pread_full(d->fd, buf, size, (off_t)(vb->start * SECTOR)))
		return -1;
	out->vbmeta_size = (uint32_t)size;
	sha256_hex(buf, size, out->digest);
	return 0;
}

/* "/sys/devices/pci0000:00/0000:00:14.0/usb2/..." -> "pci0000:00/0000:00:14.0": the deepest PCI
 * device on the path, i.e. the disk's controller. */
static int boot_device(const char *syspath, char *out, size_t outlen)
{
	const char *p = strstr(syspath, "/devices/");
	if (!p)
		return -1;
	p += strlen("/devices/");
	const char *end = NULL, *seg = p;
	while (*seg) {
		const char *next = strchr(seg, '/');
		size_t len = next ? (size_t)(next - seg) : strlen(seg);
		/* PCI functions look like 0000:00:14.0 (dddd:bb:dd.f) */
		if (len == 12 && seg[4] == ':' && seg[7] == ':' && seg[10] == '.')
			end = seg + len;
		else if (!strncmp(seg, "pci", 3) && !end)
			end = seg + len;
		if (!next)
			break;
		seg = next + 1;
	}
	if (!end || (size_t)(end - p) >= outlen)
		return -1;
	memcpy(out, p, (size_t)(end - p));
	out[end - p] = 0;
	return 0;
}

/* ---- stage-1 init ----------------------------------------------------------------------- */

static void say(const char *fmt, const char *arg)
{
	fprintf(stderr, "gbos-boot: ");
	fprintf(stderr, fmt, arg);
	fputc('\n', stderr);
}

static void halt_with(const char *msg)
{
	say("%s", msg);
	say("%s", "cannot boot Googlebook OS; power off to retry");
	for (;;)
		pause();
}

static void load_modules(void)
{
	/* Fedora builds usb-storage/uas as modules; order matters (uas needs usb-storage). */
	static const char *const order[] = { "/modules/usb-storage.ko", "/modules/uas.ko" };
	for (unsigned i = 0; i < sizeof order / sizeof order[0]; i++) {
		int fd = open(order[i], O_RDONLY | O_CLOEXEC);
		if (fd < 0)
			continue;
		if (syscall(SYS_finit_module, fd, "", 0) && errno != EEXIST)
			say("cannot load %s", order[i]);
		close(fd);
	}
}

/* The disk holding a "gbos_layer" partition, opened read-write. */
static int find_disk(struct disk *d, char *syspath, size_t len)
{
	DIR *dir = opendir("/sys/block");
	if (!dir)
		return -1;
	struct dirent *e;
	int found = -1;
	while (found < 0 && (e = readdir(dir))) {
		if (e->d_name[0] == '.' || !strncmp(e->d_name, "loop", 4) || !strncmp(e->d_name, "ram", 3) ||
		    !strncmp(e->d_name, "zram", 4))
			continue;
		char dev[PATH_MAX];
		snprintf(dev, sizeof dev, "/dev/%s", e->d_name);
		d->fd = open(dev, O_RDWR | O_CLOEXEC);
		if (d->fd < 0)
			continue;
		if (!read_gpt(d) && find_part(d, "gbos_layer")) {
			char link[PATH_MAX];
			snprintf(link, sizeof link, "/sys/block/%s", e->d_name);
			found = realpath(link, syspath) ? 0 : -1;
			(void)len;
		}
		if (found < 0)
			close(d->fd);
	}
	closedir(dir);
	return found;
}

static char *read_file(const char *path)
{
	FILE *f = fopen(path, "r");
	if (!f)
		return NULL;
	static char buf[4096];
	size_t n = fread(buf, 1, sizeof buf - 1, f);
	fclose(f);
	while (n && (buf[n - 1] == '\n' || buf[n - 1] == ' '))
		n--;
	buf[n] = 0;
	return buf;
}

static int stage1(void)
{
	mount("proc", "/proc", "proc", 0, NULL);
	mount("sysfs", "/sys", "sysfs", 0, NULL);
	mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
	load_modules();

	struct disk d;
	char syspath[PATH_MAX];
	int tries = 0;
	say("%s", "looking for the Googlebook OS disk");
	while (find_disk(&d, syspath, sizeof syspath)) {
		if (++tries > 60)
			halt_with("no disk with a gbos_layer partition (USB stick not detected?)");
		sleep(1);
	}

	struct plan p;
	if (make_plan(&d, 1, &p))
		halt_with("cannot read misc / vbmeta from the disk");
	char bootdev[256];
	if (boot_device(syspath, bootdev, sizeof bootdev))
		halt_with("cannot find the disk controller");

	const char *base = read_file("/gbos/cmdline");
	if (!base)
		halt_with("missing /gbos/cmdline");
	static char cmdline[8192];
	snprintf(cmdline, sizeof cmdline,
		 "%s androidboot.slot_suffix=%s androidboot.boot_devices=%s androidboot.vbmeta.size=%u "
		 "androidboot.vbmeta.hash_alg=sha256 androidboot.vbmeta.digest=%s",
		 base, p.slot, bootdev, p.vbmeta_size, p.digest);
	say("booting slot %s", p.slot);

	int kernel = open("/gbos/kernel", O_RDONLY | O_CLOEXEC);
	int initrd = open("/gbos/initrd", O_RDONLY | O_CLOEXEC);
	if (kernel < 0 || initrd < 0)
		halt_with("missing /gbos/kernel or /gbos/initrd");
	if (syscall(SYS_kexec_file_load, kernel, initrd, strlen(cmdline) + 1, cmdline, 0UL))
		halt_with(strerror(errno));
	sync();
	reboot(RB_KEXEC);
	halt_with("kexec did not start");
	return 1;
}

int main(int argc, char **argv)
{
	if (argc >= 3 && !strcmp(argv[1], "--plan")) {
		int write_back = argc >= 4 && !strcmp(argv[3], "--write");
		struct disk d = { .fd = open(argv[2], (write_back ? O_RDWR : O_RDONLY) | O_CLOEXEC) };
		struct plan p;
		if (d.fd < 0 || read_gpt(&d) || make_plan(&d, write_back, &p))
			return 1;
		printf("SLOT=%s\nVBMETA_SIZE=%u\nVBMETA_DIGEST=%s\n", p.slot, p.vbmeta_size, p.digest);
		return 0;
	}
	if (argc == 3 && !strcmp(argv[1], "--boot-device")) {
		char out[256];
		if (boot_device(argv[2], out, sizeof out))
			return 1;
		puts(out);
		return 0;
	}
	return stage1();
}
