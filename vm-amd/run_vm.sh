#!/bin/bash
# Boot Googlebook OS (lapis, x86_64) on an AMD host without GFNI / AVX-VNNI.
#
#   run_vm.sh [name]
#
# Stock image (byte-identical system/vendor, dm-verity on) + the gbos layer (vm/layer.img, second NVMe
# namespace) mounted over /vendor and /odm by the first-stage fstab. The launcher plays bootloader:
# it reads the A/B state from misc, so after an OTA the VM comes back up on the new slot by itself.
#
# Env:
#   GBOS_FRESH=1       start with a clean /data            GBOS_MEM=4G   GBOS_HOST_MEM_MAX=7G
#   GBOS_DISPLAY_ARGS  e.g. "-display egl-headless -vnc 127.0.0.1:7"
#   GBOS_NET_ARGS      e.g. "-nic none" for offline         GBOS_WACOM=1  Wacom tablet instead of usb-tablet
#   GBOS_LOGLEVEL      kernel console loglevel (7 shows gfni_emu counters)
#   GBOS_ONCE=1        do not relaunch after a guest reboot
set -eu
GBOS="${GBOS_ROOT:-/mnt/dados/gbos}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
NAME="${1:-daily}"
BASE="${GBOS_BASE:-$GBOS/vm/stock/googlebook.bin}"
LAYER="${GBOS_LAYER:-$GBOS/vm/layer.img}"
INITRD="${GBOS_INITRD:-$GBOS/vm/initrd}"
KERNEL="${GBOS_KERNEL:-$GBOS/kernel/out-full/arch/x86/boot/bzImage}"
OVERLAY="${GBOS_OVERLAY:-$GBOS/vm/data.qcow2}"
LOG="$GBOS/vm/logs/$NAME"

for f in "$BASE" "$LAYER" "$INITRD" "$KERNEL"; do
  [ -f "$f" ] || { echo "missing $f (see vm-amd/README.md)" >&2; exit 1; }
done
if pgrep -x qemu-system-x86 >/dev/null; then echo "QEMU is already running" >&2; exit 1; fi
[ "${GBOS_FRESH:-0}" = 1 ] && rm -f "$OVERLAY"
[ -f "$OVERLAY" ] || qemu-img create -q -f qcow2 -b "$BASE" -F raw "$OVERLAY"
AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
[ "$AVAIL_MB" -ge 6000 ] || echo "warning: only ${AVAIL_MB} MB RAM available; close heavy apps first" >&2

: "${GBOS_XRES:=1440}" "${GBOS_YRES:=1080}"
: "${GBOS_GPU_ARGS:=-device virtio-gpu-gl-pci,hostmem=${GBOS_HOSTMEM:-2G},blob=true,venus=true,addr=0x1,xres=$GBOS_XRES,yres=$GBOS_YRES}"
: "${GBOS_DISPLAY_ARGS:=-display sdl,gl=on,show-cursor=on}"
TABLET=usb-tablet; [ "${GBOS_WACOM:-0}" = 1 ] && TABLET=usb-wacom-tablet
: "${GBOS_INPUT_ARGS:=-device qemu-xhci,id=xhci -device usb-kbd,bus=xhci.0 -device $TABLET,bus=xhci.0}"
: "${GBOS_NET_ARGS:=-netdev user,id=net0,ipv6=off -device virtio-net-pci,netdev=net0}"

boot_once() {
  eval "$(python3 "$HERE/boot/slot.py" boot-args "$OVERLAY" "$BASE")"
  mkdir -p "$LOG"
  echo "booting slot $SLOT (vbmeta $VBMETA_SIZE bytes)" >&2
  # cfi=norand: gfni_emu's entry stubs carry fixed kCFI hashes. overlay.gbos_caller_creds: the layer is
  # mounted before any SELinux policy exists (see the kernel patch).
  local cmd="console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200 panic=0 root=/dev/ram0 \
loglevel=${GBOS_LOGLEVEL:-4} 8250.nr_uarts=4 console_msg_format=syslog log_buf_len=1M cgroup_disable=pressure \
firmware_class.path=/vendor/firmware binder.impl=rust ia32_emulation=false vsyscall=none printk.devkmsg=on \
cfi=norand overlay.gbos_caller_creds=1 \
androidboot.hardware=android-desktop androidboot.hardware.platform=android-desktop androidboot.slot_suffix=$SLOT \
androidboot.boot_devices=pci0000:00/0000:00:04.0 androidboot.vbmeta.size=$VBMETA_SIZE androidboot.vbmeta.hash_alg=sha256 \
androidboot.vbmeta.digest=$VBMETA_DIGEST androidboot.vbmeta.device_state=unlocked androidboot.verifiedbootstate=orange \
androidboot.veritymode=enforcing androidboot.vendor.apex.com.android.hardware.keymint.strongbox.desktop=none \
androidboot.vendor.boot_patchlevel=2026-09-01 androidboot.boot_patchlevel=2026-09-01 \
androidboot.dalvik.vm.enable_uffd_gc=true androidboot.zygote.disable_gl_preload"
  # shellcheck disable=SC2086
  systemd-run --user --scope -q -p MemoryMax="${GBOS_HOST_MEM_MAX:-7G}" -p MemorySwapMax=0 -p CPUQuota=400% \
    timeout "${GBOS_SECS:-28800}" qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m "${GBOS_MEM:-4G}" -machine q35 \
    -nodefaults -no-reboot -kernel "$KERNEL" -initrd "$INITRD" -append "$cmd" \
    -drive if=none,id=disk,format=qcow2,file="$OVERLAY" \
    -drive if=none,id=layer,format=raw,readonly=on,file="$LAYER" \
    -device nvme,id=nvme0,serial=gb0,addr=0x4 \
    -device nvme-ns,drive=disk,bus=nvme0,nsid=1 -device nvme-ns,drive=layer,bus=nvme0,nsid=2 \
    $GBOS_GPU_ARGS $GBOS_INPUT_ARGS $GBOS_NET_ARGS $GBOS_DISPLAY_ARGS \
    -serial file:"$LOG/serial.log" -monitor none || true
}

# -no-reboot turns a guest reboot into an exit; relaunch on "Restarting system" (an OTA, a crash
# reboot), stop when the window was closed or the guest powered off.
while :; do
  boot_once
  [ "${GBOS_ONCE:-0}" = 1 ] && break
  tail -c 4096 "$LOG/serial.log" | grep -aq 'reboot: Restarting system' || break
  echo "guest rebooted; starting again" >&2
done
