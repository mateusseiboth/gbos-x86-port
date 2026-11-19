// SPDX-License-Identifier: MIT
/*
 * gbos: stand-in for /vendor/bin/futility in a VM (or a PC without ChromeOS firmware).
 *
 * The OTA postinstall step (google_desktop_firmware_updater -> IFutilityExecutor) runs
 * "futility update ..." to flash the AP firmware over SPI. There is no such flash here, so the
 * real tool fails and update_engine rejects the whole update (kPostinstallRunnerError).
 * Report the update as done and refuse everything else, so callers that parse other
 * subcommands get a clean error instead of made-up data.
 */
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
	if (argc > 1 && strcmp(argv[1], "update") == 0) {
		puts("gbos: no AP firmware to update, skipping");
		return 0;
	}
	fprintf(stderr, "gbos: futility %s is not available\n", argc > 1 ? argv[1] : "");
	return 1;
}
