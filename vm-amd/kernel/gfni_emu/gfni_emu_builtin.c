// SPDX-License-Identifier: GPL-2.0
/*
 * Built-in wrapper for gbos-x86-port/module/gfni_emu.c.
 *
 * Android's first-stage init already executes GFNI / AVX-VNNI code, so the #UD
 * emulator must be registered before userspace starts; a loadable module would
 * come too late. The emulator source is included verbatim and its init routine
 * is registered as a core initcall.
 */
#include "gfni_emu.c"

core_initcall(real_gfni_emu_init);
