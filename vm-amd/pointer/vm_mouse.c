// SPDX-License-Identifier: MIT
/*
 * vm-mouse: QEMU's usb-tablet (absolute, needs no grab, works under Wayland) re-emitted through
 * uinput as two devices Android handles natively:
 *
 *   "VM Touch"  a touchscreen with the tablet's own axis range: Android maps that range onto the
 *               whole display, so a click lands exactly under the host cursor at any window size.
 *               Left button = touch; right button = long press (context menu); no hover.
 *   "VM Wheel"  a relative mouse that only scrolls. Scroll goes where Android's mouse cursor is, so
 *               before each scroll that cursor is parked in the corner (Android clamps it) and moved
 *               to the pointer's pixel: nothing accumulates between scrolls.
 *
 * Android ignores the tablet itself (no BTN_TOUCH, no REL axes). Earlier attempts: a hovering stylus
 * (InputDispatcher aborted when a window changed under the pen) and an absolute -> relative mouse
 * (drifted: moves made before Android opened the device were lost).
 *
 *   vm-mouse                         daemon (Android)
 *   vm-mouse --scale MIN MAX W       host test: stdin raw tablet values -> stdout pixels
 *   vm-mouse --wheel-moves X Y       host test: wheel cursor moves for pixel (X, Y)
 *   vm-mouse --release-at DOWN UP    host test: when a right click's touch is lifted (ms)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SLAM 32000          /* beyond any display: Android clamps the cursor to the edge */
#define LONG_PRESS_MS 650   /* Android's long-press timeout is 400-500 ms */

static int scale(long v, long min, long max, int size)
{
	if (v < min)
		v = min;
	if (v > max)
		v = max;
	return (int)((v - min) * (size - 1) / (max - min));
}

/* Relative moves (dx, dy pairs) that put the wheel cursor on pixel (x, y). */
static int wheel_moves(int x, int y, int out[4])
{
	out[0] = -SLAM, out[1] = -SLAM, out[2] = x, out[3] = y;
	return 2;
}

/* A right click is a touch held at least LONG_PRESS_MS: when to lift it. */
static long release_at(long down_ms, long up_ms)
{
	return up_ms > down_ms + LONG_PRESS_MS ? up_ms : down_ms + LONG_PRESS_MS;
}

#ifdef VM_MOUSE_HOST_TEST

int main(int argc, char **argv)
{
	if (argc == 5 && !strcmp(argv[1], "--scale")) {
		long v;
		while (scanf("%ld", &v) == 1)
			printf("%d\n", scale(v, atol(argv[2]), atol(argv[3]), atoi(argv[4])));
		return 0;
	}
	if (argc == 4 && !strcmp(argv[1], "--wheel-moves")) {
		int out[4];
		for (int i = 0, n = wheel_moves(atoi(argv[2]), atoi(argv[3]), out); i < n; i++)
			printf("%d %d\n", out[2 * i], out[2 * i + 1]);
		return 0;
	}
	if (argc == 4 && !strcmp(argv[1], "--release-at")) {
		printf("%ld\n", release_at(atol(argv[2]), atol(argv[3])));
		return 0;
	}
	return 2;
}

#else /* Android daemon */

#include <android/log.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <poll.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define LOG(...) __android_log_print(ANDROID_LOG_WARN, "vm-mouse", __VA_ARGS__)

static volatile sig_atomic_t g_stop;
static void on_signal(int s)
{
	(void)s;
	g_stop = 1;
}

static long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int find_tablet(void)
{
	char path[64], name[128];
	for (int i = 0; i < 64; i++) {
		snprintf(path, sizeof path, "/dev/input/event%d", i);
		int fd = open(path, O_RDONLY | O_CLOEXEC);
		if (fd < 0)
			continue;
		memset(name, 0, sizeof name);
		if (ioctl(fd, EVIOCGNAME(sizeof name - 1), name) >= 0 && strstr(name, "USB Tablet")) {
			LOG("tablet \"%s\" at %s", name, path);
			return fd;
		}
		close(fd);
	}
	return -1;
}

/* Preferred mode of the first connected DRM connector ("WxH" on the first line of its "modes" file). */
static int display_size(int *w, int *h)
{
	DIR *d = opendir("/sys/class/drm");
	if (!d)
		return -1;
	struct dirent *e;
	int ok = -1;
	while (ok < 0 && (e = readdir(d))) {
		if (strncmp(e->d_name, "card", 4) || !strchr(e->d_name, '-'))
			continue;
		char p[300], st[32] = "";
		snprintf(p, sizeof p, "/sys/class/drm/%s/status", e->d_name);
		FILE *f = fopen(p, "r");
		if (f) {
			if (!fgets(st, sizeof st, f))
				st[0] = 0;
			fclose(f);
		}
		if (strncmp(st, "connected", 9))
			continue;
		snprintf(p, sizeof p, "/sys/class/drm/%s/modes", e->d_name);
		f = fopen(p, "r");
		if (!f)
			continue;
		if (fscanf(f, "%dx%d", w, h) == 2 && *w > 0 && *h > 0)
			ok = 0;
		fclose(f);
	}
	closedir(d);
	return ok;
}

static int ui_create(int fd, const char *name, int product)
{
	struct uinput_setup s;
	memset(&s, 0, sizeof s);
	snprintf(s.name, sizeof s.name, "%s", name);
	s.id.bustype = BUS_USB;
	s.id.vendor = 0x1d6b;
	s.id.product = product;
	s.id.version = 1;
	if (ioctl(fd, UI_DEV_SETUP, &s) < 0 || ioctl(fd, UI_DEV_CREATE) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

static int make_touch(const struct input_absinfo *ax, const struct input_absinfo *ay)
{
	int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0)
		return -1;
	ioctl(fd, UI_SET_EVBIT, EV_KEY);
	ioctl(fd, UI_SET_EVBIT, EV_ABS);
	ioctl(fd, UI_SET_EVBIT, EV_SYN);
	ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH);
	ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT);
	const struct input_absinfo *info[2] = { ax, ay };
	for (int axis = ABS_X; axis <= ABS_Y; axis++) {
		struct uinput_abs_setup a;
		memset(&a, 0, sizeof a);
		a.code = axis;
		a.absinfo.minimum = info[axis]->minimum;
		a.absinfo.maximum = info[axis]->maximum;
		ioctl(fd, UI_SET_ABSBIT, axis);
		ioctl(fd, UI_ABS_SETUP, &a);
	}
	return ui_create(fd, "VM Touch", 0x0106);
}

static int make_wheel(void)
{
	int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0)
		return -1;
	ioctl(fd, UI_SET_EVBIT, EV_KEY);
	ioctl(fd, UI_SET_EVBIT, EV_REL);
	ioctl(fd, UI_SET_EVBIT, EV_SYN);
	ioctl(fd, UI_SET_KEYBIT, BTN_LEFT); /* BTN_MOUSE + REL_X/REL_Y => Android "cursor" class */
	static const int rels[] = { REL_X, REL_Y, REL_WHEEL, REL_HWHEEL };
	for (unsigned i = 0; i < sizeof rels / sizeof rels[0]; i++)
		ioctl(fd, UI_SET_RELBIT, rels[i]);
	return ui_create(fd, "VM Wheel", 0x0104);
}

static void emit(int fd, int type, int code, int value)
{
	struct input_event ev;
	memset(&ev, 0, sizeof ev);
	ev.type = type, ev.code = code, ev.value = value;
	if (write(fd, &ev, sizeof ev) < 0 && errno != EAGAIN)
		LOG("write failed: %s", strerror(errno));
}

static void touch(int fd, int down, long x, long y)
{
	emit(fd, EV_ABS, ABS_X, (int)x);
	emit(fd, EV_ABS, ABS_Y, (int)y);
	emit(fd, EV_KEY, BTN_TOUCH, down);
	emit(fd, EV_SYN, SYN_REPORT, 0);
}

static int run_once(void)
{
	int tab = find_tablet();
	if (tab < 0)
		return 1;
	struct input_absinfo ax, ay;
	if (ioctl(tab, EVIOCGABS(ABS_X), &ax) < 0 || ioctl(tab, EVIOCGABS(ABS_Y), &ay) < 0 ||
	    ax.maximum <= ax.minimum || ay.maximum <= ay.minimum) {
		close(tab);
		return 1;
	}
	ioctl(tab, EVIOCGRAB, 1);
	int tch = make_touch(&ax, &ay), whl = make_wheel();
	if (tch < 0 || whl < 0) {
		LOG("cannot create uinput devices: %s", strerror(errno));
		close(tab);
		return 1;
	}
	LOG("running");

	long x = 0, y = 0, right_down = 0, lift_at = 0;
	int left = 0, touching = 0, wheel = 0, hwheel = 0, rc = 0;
	struct pollfd pfd = { .fd = tab, .events = POLLIN };
	while (!g_stop) {
		int timeout = lift_at ? (int)(lift_at - now_ms()) : 1000;
		if (poll(&pfd, 1, timeout > 0 ? timeout : 0) < 0 && errno != EINTR) {
			rc = 1;
			break;
		}
		if (lift_at && now_ms() >= lift_at) { /* end of a right click's long press */
			touch(tch, 0, x, y);
			touching = 0, lift_at = 0;
		}
		if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
			rc = 1;
			break;
		}
		if (!(pfd.revents & POLLIN))
			continue;
		struct input_event ev[64];
		ssize_t n = read(tab, ev, sizeof ev);
		if (n < 0) {
			if (errno == EAGAIN || errno == EINTR)
				continue;
			rc = 1;
			break;
		}
		for (size_t i = 0; i < (size_t)n / sizeof ev[0]; i++) {
			struct input_event *e = &ev[i];
			if (e->type == EV_ABS && e->code == ABS_X)
				x = e->value;
			else if (e->type == EV_ABS && e->code == ABS_Y)
				y = e->value;
			else if (e->type == EV_REL && e->code == REL_WHEEL)
				wheel += e->value;
			else if (e->type == EV_REL && e->code == REL_HWHEEL)
				hwheel += e->value;
			else if (e->type == EV_KEY && e->code == BTN_LEFT)
				left = e->value != 0;
			else if (e->type == EV_KEY && e->code == BTN_RIGHT && e->value && !touching)
				right_down = now_ms(), touching = 2, touch(tch, 1, x, y);
			else if (e->type == EV_KEY && e->code == BTN_RIGHT && !e->value && touching == 2)
				lift_at = release_at(right_down, now_ms());
			if (e->type != EV_SYN || e->code != SYN_REPORT)
				continue;
			/* Left button: the touch follows it; moving while down is a drag. */
			if (touching != 2 && (left || touching)) {
				touch(tch, left, x, y);
				touching = left;
			}
			if (wheel || hwheel) {
				int px = scale(x, ax.minimum, ax.maximum, 1920), py = scale(y, ay.minimum, ay.maximum, 1080);
				int w = 1920, h = 1080, out[4];
				if (!display_size(&w, &h)) {
					px = scale(x, ax.minimum, ax.maximum, w);
					py = scale(y, ay.minimum, ay.maximum, h);
				}
				for (int k = 0, m = wheel_moves(px, py, out); k < m; k++) {
					emit(whl, EV_REL, REL_X, out[2 * k]);
					emit(whl, EV_REL, REL_Y, out[2 * k + 1]);
					emit(whl, EV_SYN, SYN_REPORT, 0);
				}
				if (wheel)
					emit(whl, EV_REL, REL_WHEEL, wheel);
				if (hwheel)
					emit(whl, EV_REL, REL_HWHEEL, hwheel);
				emit(whl, EV_SYN, SYN_REPORT, 0);
				wheel = hwheel = 0;
			}
		}
	}
	ioctl(tch, UI_DEV_DESTROY);
	ioctl(whl, UI_DEV_DESTROY);
	close(tch);
	close(whl);
	close(tab);
	return rc;
}

int main(void)
{
	signal(SIGTERM, on_signal);
	signal(SIGINT, on_signal);
	while (!g_stop) {
		if (run_once() == 0)
			break;
		sleep(1); /* tablet not there yet, or it went away: retry */
	}
	return 0;
}

#endif
