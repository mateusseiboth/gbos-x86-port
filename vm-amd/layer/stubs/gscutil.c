// SPDX-License-Identifier: MIT
/*
 * gbos: wrapper bound over /system/bin/gscutil in a VM (or a PC without a Google Security Chip).
 *
 * The OTA postinstall step (google_desktop_firmware_updater -> IGscutilExecutor) runs
 * "gscutil perform-post-install ..." to update the GSC firmware. There is no GSC here, so the real
 * tool fails and update_engine rejects the whole update (kPostinstallRunnerError). Report that one
 * step as done; every other command runs the real gscutil (bound at GSCUTIL_REAL), unchanged.
 */
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef GSCUTIL_REAL
#define GSCUTIL_REAL "/vendor/etc/gbos/system/bin/gscutil.real"
#endif

/* The kept copy can turn out to be this wrapper (a bind mount that received the wrapper's mount by
 * propagation); exec'ing it would loop forever while init waits for gscutil. */
static int is_self(const char *path)
{
	struct stat self, real;

	return stat("/proc/self/exe", &self) == 0 && stat(path, &real) == 0 &&
	       self.st_dev == real.st_dev && self.st_ino == real.st_ino;
}

int main(int argc, char **argv)
{
	if (argc > 1 && strcmp(argv[1], "perform-post-install") == 0) {
		puts("gbos: no GSC firmware to update, skipping");
		return 0;
	}
	if (is_self(GSCUTIL_REAL)) {
		fprintf(stderr, "gbos: " GSCUTIL_REAL " is this wrapper, not the real gscutil\n");
		return 127;
	}
	execv(GSCUTIL_REAL, argv);
	perror("gbos: exec " GSCUTIL_REAL);
	return 127;
}
