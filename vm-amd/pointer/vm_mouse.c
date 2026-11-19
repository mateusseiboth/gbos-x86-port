// SPDX-License-Identifier: MIT
/*
 * vm-mouse: QEMU's usb-tablet (absolute, needs no grab, works under Wayland) re-emitted as a plain
 * relative mouse ("VM Mouse") through uinput, so Android sees an ordinary mouse: cursor, hover,
 * right click, wheel. Android ignores the tablet itself (no BTN_TOUCH, no REL axes); presenting it
 * as a hovering stylus (the old pointer-bridge) made InputDispatcher abort when a window changed
 * under the pen.
 *
 * Absolute -> relative: Android's cursor is moved by the pixel delta to each new position (pointer
 * acceleration is off, see vm-power.rc). The cursor is parked in the corner first, and every move
 * that reaches a display edge overshoots, so Android clamps it and both positions realign. A display
 * size change resets that, as does the first event.
 *
 *   vm-mouse                    daemon (Android)
 *   vm-mouse --track W H        host test: stdin "x y" pixel targets -> stdout "dx dy" moves
 *   vm-mouse --scale MIN MAX W  host test: stdin raw tablet values -> stdout pixels
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SLAM 32000 /* beyond any display: Android clamps the cursor to the edge */
#define EDGE 64

struct tracker {
	int w, h, x, y, synced;
};

static void tracker_reset(struct tracker *t, int w, int h)
{
	*t = (struct tracker){ .w = w, .h = h };
}

static int scale(long v, long min, long max, int size)
{
	if (v < min)
		v = min;
	if (v > max)
		v = max;
	return (int)((v - min) * (size - 1) / (max - min));
}

/* Relative moves (dx, dy pairs in out[]) that bring Android's cursor to (tx, ty); returns the count. */
static int tracker_move(struct tracker *t, int tx, int ty, int out[4])
{
	int n = 0;
	if (!t->synced) {
		out[0] = -SLAM, out[1] = -SLAM;
		n = 1;
		t->x = t->y = 0;
		t->synced = 1;
	}
	int dx = tx - t->x, dy = ty - t->y;
	if (dx && tx == 0)
		dx -= EDGE;
	if (dx && tx == t->w - 1)
		dx += EDGE;
	if (dy && ty == 0)
		dy -= EDGE;
	if (dy && ty == t->h - 1)
		dy += EDGE;
	t->x = tx, t->y = ty;
	if (dx || dy)
		out[2 * n] = dx, out[2 * n + 1] = dy, n++;
	return n;
}

#ifdef VM_MOUSE_HOST_TEST

int main(int argc, char **argv)
{
	if (argc == 4 && !strcmp(argv[1], "--track")) {
		struct tracker t;
		tracker_reset(&t, atoi(argv[2]), atoi(argv[3]));
		int x, y, out[4];
		while (scanf("%d %d", &x, &y) == 2)
			for (int i = 0, n = tracker_move(&t, x, y, out); i < n; i++)
				printf("%d %d\n", out[2 * i], out[2 * i + 1]);
		return 0;
	}
	if (argc == 5 && !strcmp(argv[1], "--scale")) {
		long v;
		while (scanf("%ld", &v) == 1)
			printf("%d\n", scale(v, atol(argv[2]), atol(argv[3]), atoi(argv[4])));
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

static int make_mouse(void)
{
	int fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0)
		return -1;
	ioctl(fd, UI_SET_EVBIT, EV_KEY);
	ioctl(fd, UI_SET_EVBIT, EV_REL);
	ioctl(fd, UI_SET_EVBIT, EV_SYN);
	static const int keys[] = { BTN_LEFT, BTN_RIGHT, BTN_MIDDLE };
	for (unsigned i = 0; i < sizeof keys / sizeof keys[0]; i++)
		ioctl(fd, UI_SET_KEYBIT, keys[i]);
	static const int rels[] = { REL_X, REL_Y, REL_WHEEL, REL_HWHEEL };
	for (unsigned i = 0; i < sizeof rels / sizeof rels[0]; i++)
		ioctl(fd, UI_SET_RELBIT, rels[i]);
	struct uinput_setup s;
	memset(&s, 0, sizeof s);
	snprintf(s.name, sizeof s.name, "VM Mouse");
	s.id.bustype = BUS_USB;
	s.id.vendor = 0x1d6b;
	s.id.product = 0x0105;
	s.id.version = 1;
	if (ioctl(fd, UI_DEV_SETUP, &s) < 0 || ioctl(fd, UI_DEV_CREATE) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

static void emit(int fd, int type, int code, int value)
{
	struct input_event ev;
	memset(&ev, 0, sizeof ev);
	ev.type = type, ev.code = code, ev.value = value;
	if (write(fd, &ev, sizeof ev) < 0 && errno != EAGAIN)
		LOG("write failed: %s", strerror(errno));
}

static long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
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
	int mouse = make_mouse();
	if (mouse < 0) {
		LOG("cannot create the uinput mouse: %s", strerror(errno));
		close(tab);
		return 1;
	}
	int w = 1440, h = 1080;
	display_size(&w, &h);
	struct tracker t;
	tracker_reset(&t, w, h);
	LOG("running, display %dx%d", w, h);

	long ax_v = 0, ay_v = 0, checked = now_ms();
	int moved = 0, wheel = 0, hwheel = 0, rc = 0;
	int buttons[3] = { 0 }, sent[3] = { 0 };
	struct pollfd pfd = { .fd = tab, .events = POLLIN };
	while (!g_stop) {
		if (poll(&pfd, 1, 1000) < 0 && errno != EINTR) {
			rc = 1;
			break;
		}
		if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) {
			rc = 1;
			break;
		}
		if (now_ms() - checked > 2000) { /* the window (and so the display mode) can be resized */
			int nw = w, nh = h;
			if (!display_size(&nw, &nh) && (nw != w || nh != h)) {
				w = nw, h = nh;
				tracker_reset(&t, w, h);
				LOG("display %dx%d", w, h);
			}
			checked = now_ms();
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
				ax_v = e->value, moved = 1;
			else if (e->type == EV_ABS && e->code == ABS_Y)
				ay_v = e->value, moved = 1;
			else if (e->type == EV_REL && e->code == REL_WHEEL)
				wheel += e->value;
			else if (e->type == EV_REL && e->code == REL_HWHEEL)
				hwheel += e->value;
			else if (e->type == EV_KEY && e->code >= BTN_LEFT && e->code <= BTN_MIDDLE)
				buttons[e->code - BTN_LEFT] = e->value ? 1 : 0;
			if (e->type != EV_SYN || e->code != SYN_REPORT)
				continue;
			/* Motion first, in its own reports, so a click lands where the cursor now is. */
			if (moved) {
				int out[4];
				int tx = scale(ax_v, ax.minimum, ax.maximum, w), ty = scale(ay_v, ay.minimum, ay.maximum, h);
				for (int k = 0, m = tracker_move(&t, tx, ty, out); k < m; k++) {
					emit(mouse, EV_REL, REL_X, out[2 * k]);
					emit(mouse, EV_REL, REL_Y, out[2 * k + 1]);
					emit(mouse, EV_SYN, SYN_REPORT, 0);
				}
				moved = 0;
			}
			for (int b = 0; b < 3; b++)
				if (buttons[b] != sent[b])
					emit(mouse, EV_KEY, BTN_LEFT + b, buttons[b]), sent[b] = buttons[b];
			if (wheel)
				emit(mouse, EV_REL, REL_WHEEL, wheel);
			if (hwheel)
				emit(mouse, EV_REL, REL_HWHEEL, hwheel);
			wheel = hwheel = 0;
			emit(mouse, EV_SYN, SYN_REPORT, 0);
		}
	}
	ioctl(mouse, UI_DEV_DESTROY);
	close(mouse);
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
