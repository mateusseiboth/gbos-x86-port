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
#   GBOS_FRESH=1       start with a clean /data            GBOS_SMP=8  GBOS_MEM=8G  GBOS_HOST_MEM_MAX=12G
#   GBOS_DISPLAY_ARGS  e.g. "-display egl-headless -vnc 127.0.0.1:7"
#   GBOS_NET_ARGS      e.g. "-nic none" for offline         
#   GBOS_LOGLEVEL      kernel console loglevel (7 shows gfni_emu counters)
#   GBOS_ONCE=1        do not relaunch after a guest reboot
#   GBOS_VULKAN=1      Vulkan rendering through Venus (patched virglrenderer, vm-amd/virgl)
set -eu
GBOS="${GBOS_ROOT:-/mnt/dados/gbos}"
HERE="$(cd "$(dirname "$0")" && pwd -P)"
NAME="${1:-daily}"
BASE="${GBOS_BASE:-$GBOS/vm/stock/googlebook.bin}"
LAYER="${GBOS_LAYER:-$GBOS/vm/layer.img}"
# Graphics stack (the layer's /odm/etc/gbos/gpu_<stack>.prop): GL over virgl, or with GBOS_VULKAN=1
# Vulkan through Venus on the patched virglrenderer from vm-amd/virgl (build.sh); QEMU alone loads
# it, the system library is untouched.
GPU_STACK=virgl
if [ "${GBOS_VULKAN:-0}" = 1 ]; then
  GPU_STACK=venus
  [ -f "$HERE/virgl/out/lib/libvirglrenderer.so.1" ] || { echo "run vm-amd/virgl/build.sh first" >&2; exit 1; }
  export LD_LIBRARY_PATH="$HERE/virgl/out/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export VIRGL_GBM_LAYOUT_FORCE_ENABLE=1
fi
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
# 8 of the host's 12 threads and 8 GiB by default; the host keeps the rest.
: "${GBOS_SMP:=8}" "${GBOS_MEM:=8G}" "${GBOS_HOST_MEM_MAX:=12G}"
NEED_MB=$(( ${GBOS_MEM%G} * 1024 + 2048 ))
AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
[ "$AVAIL_MB" -ge "$NEED_MB" ] || echo "warning: only ${AVAIL_MB} MB RAM available for a ${GBOS_MEM} guest; close heavy apps or set GBOS_MEM lower" >&2

: "${GBOS_XRES:=1440}" "${GBOS_YRES:=1080}"
: "${GBOS_GPU_ARGS:=-device virtio-gpu-gl-pci,hostmem=${GBOS_HOSTMEM:-2G},blob=true,venus=true,addr=0x1,xres=$GBOS_XRES,yres=$GBOS_YRES}"
# GTK, show-cursor=on: the cursor is the guest's (virtio-gpu cursor plane). SDL either hides it on grab
# (relative mode) or, with show-cursor=on, feeds its own warps back as motion and the cursor drifts;
# GTK shows it and remembers where it warped to.
: "${GBOS_DISPLAY_ARGS:=-display gtk,gl=on,show-cursor=on}"
# usb-tablet: absolute, no grab (a captured relative mouse cannot work under Wayland, which forbids
# warping the pointer); the layer's vm-mouse turns it into a plain mouse for Android.
: "${GBOS_INPUT_ARGS:=-device qemu-xhci,id=xhci -device usb-kbd,bus=xhci.0 -device usb-tablet,bus=xhci.0}"
: "${GBOS_NET_ARGS:=-netdev user,id=net0,ipv6=off -device virtio-net-pci,netdev=net0}"

boot_once() {
  eval "$(python3 "$HERE/boot/slot.py" boot-args "$OVERLAY" "$BASE")"
  mkdir -p "$LOG"
  [ -f "$LOG/serial.log" ] && mv -f "$LOG/serial.log" "$LOG/serial.log.prev"
  echo "booting slot $SLOT (vbmeta $VBMETA_SIZE bytes); the window stays black for ~20 s, then the boot animation" >&2
  # i8042=off: with Fedora's psmouse loaded, the guest enables the PS/2 mouse and QEMU then sends the
  # pointer there instead of to the USB mouse.
  # Shared arguments live in boot/cmdline.txt (also used by the pendrive); the VM adds the serial
  # console, its loglevel, slot, vbmeta and the NVMe controller as boot device.
  local shared
  shared="$(sed 's/#.*//' "$HERE/boot/cmdline.txt" | tr -s ' \n' ' ')"
  local cmd="console=ttyS0,115200 earlycon=uart8250,io,0x3f8,115200 console_msg_format=syslog \
$shared loglevel=${GBOS_LOGLEVEL:-4} androidboot.gbos.vm=1 androidboot.gbos.gpu=$GPU_STACK androidboot.slot_suffix=$SLOT androidboot.boot_devices=pci0000:00/0000:00:04.0 \
androidboot.vbmeta.size=$VBMETA_SIZE androidboot.vbmeta.hash_alg=sha256 androidboot.vbmeta.digest=$VBMETA_DIGEST"
  # shellcheck disable=SC2086
  systemd-run --user --scope -q -p MemoryMax="$GBOS_HOST_MEM_MAX" -p MemorySwapMax=0 -p CPUQuota=$(( GBOS_SMP * 100 ))% \
    timeout "${GBOS_SECS:-28800}" qemu-system-x86_64 -enable-kvm -cpu host -smp "$GBOS_SMP" -m "$GBOS_MEM" -machine q35,i8042=off \
    -nodefaults -no-reboot -kernel "$KERNEL" -initrd "$INITRD" -append "$cmd" \
    -drive if=none,id=disk,format=qcow2,file="$OVERLAY" \
    -drive if=none,id=layer,format=raw,readonly=on,file="$LAYER" \
    -device nvme,id=nvme0,serial=gb0,addr=0x4 \
    -device nvme-ns,drive=disk,bus=nvme0,nsid=1 -device nvme-ns,drive=layer,bus=nvme0,nsid=2 \
    $GBOS_GPU_ARGS $GBOS_INPUT_ARGS $GBOS_NET_ARGS $GBOS_DISPLAY_ARGS \
    -serial file:"$LOG/serial.log" -monitor none &
  local qemu=$!
  python3 "$HERE/boot/progress.py" "$LOG/serial.log" "$qemu" &
  wait "$qemu" || true
  wait
}

# -no-reboot turns a guest reboot into an exit; relaunch on "Restarting system" (an OTA, a crash
# reboot), stop when the window was closed or the guest powered off.
while :; do
  boot_once
  [ "${GBOS_ONCE:-0}" = 1 ] && break
  tail -c 4096 "$LOG/serial.log" | grep -aq 'reboot: Restarting system' || break
  echo "guest rebooted; starting again" >&2
done
