#!/bin/bash
# Boot Googlebook OS (lapis, x86_64) on this AMD host (Ryzen 5 5600GT, Zen 3: no GFNI / AVX-VNNI).
#
#   run_vm.sh [name]          SDL window, /data kept in a qcow2 overlay (base image stays clean)
#
# Env:
#   GBOS_FRESH=1       throw the overlay away and start from a clean /data
#   GBOS_MEM=4G        guest RAM            GBOS_HOST_MEM_MAX=7G   cgroup cap for QEMU
#   GBOS_DISPLAY_ARGS  e.g. "-display egl-headless -vnc 127.0.0.1:7" for headless
set -eu
GBOS="${GBOS_ROOT:-/mnt/dados/gbos}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
NAME="${1:-daily}"
WORK="$GBOS/gbos-vm-linux"
BASE="$WORK/image-security/googlebook-security.bin"
OVERLAY="${GBOS_OVERLAY:-$GBOS/vm/persist.qcow2}"
KERNEL="${GBOS_KERNEL:-$GBOS/kernel/out/arch/x86/boot/bzImage}"

for f in "$BASE" "$KERNEL" "$WORK/image-security/initrd"; do
  [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }
done
if pgrep -x qemu-system-x86 >/dev/null; then echo "QEMU is already running" >&2; exit 1; fi

mkdir -p "$(dirname "$OVERLAY")"
[ "${GBOS_FRESH:-0}" = 1 ] && rm -f "$OVERLAY"
[ -f "$OVERLAY" ] || qemu-img create -q -f qcow2 -b "$BASE" -F raw "$OVERLAY"

AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
[ "$AVAIL_MB" -ge 6000 ] || echo "warning: only ${AVAIL_MB} MB RAM available; close heavy apps first" >&2

# cfi=norand: the gfni_emu entry stubs carry fixed kCFI type hashes.
ARGS="cfi=norand loglevel=${GBOS_LOGLEVEL:-4} androidboot.selinux=permissive enforcing=0 \
androidboot.vendor.boot_patchlevel=2026-09-01 androidboot.boot_patchlevel=2026-09-01 \
androidboot.dalvik.vm.enable_uffd_gc=true androidboot.zygote.disable_gl_preload"

# The image has no in-guest pointer bridge, so use the Wacom tablet (4:3 keeps the pointer 1:1).
export GBOS_WACOM=1 GBOS_KERNEL="$KERNEL" GBOS_OVERLAY="$OVERLAY"
exec systemd-run --user --scope -q -p MemoryMax="${GBOS_HOST_MEM_MAX:-7G}" -p MemorySwapMax=0 -p CPUQuota=400% \
  "$HERE/../host/boot_vm_overlay.sh" "$WORK" "$NAME" "${GBOS_SECS:-28800}" "$ARGS"
