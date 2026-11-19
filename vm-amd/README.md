# vm-amd: KVM VM on an AMD host without GFNI (Zen 3)

Runs the lapis (x86_64) image under QEMU/KVM on a Ryzen 5 5600GT (Zen 3: VAES/VPCLMULQDQ present,
**no GFNI, no AVX-VNNI**). Unlike `host/`, which uses Google's shipped kernel, this path builds AOSP
`kernel/common` with `module/gfni_emu.c` **built in** (registered as a `core_initcall`, before `/init`).

The image stays **stock**: `system_a` / `vendor_a` are byte-identical and dm-verity is on. Everything
vm-amd changes lives in one erofs **layer** that the first-stage fstab mounts over `/vendor` and `/odm`.
An OTA can therefore verify and apply, and the layer applies to whichever slot boots; `run_vm.sh` reads
the A/B state from `misc` like a bootloader and relaunches the VM after the guest reboots.

Verified 2026-11-19: stock image + layer boots to the setup wizard (first boot ~165 s), clicks work
through the pointer bridge, ~1.5–3k emulated instructions/s with the libz bind, no unsupported opcodes.
**OTA end to end**: the wizard's 164 MB update downloaded and applied in ~8 min, the guest rebooted,
`run_vm.sh` came back up on slot `_b` with the layer on top, setup continued with `/data` intact, and
the boot-control HAL marked `_b` successful.

## Layout (`GBOS_ROOT`, default `/mnt/dados/gbos`)

| Path | What |
|---|---|
| `$GBOS_ROOT/gbos-x86-port` | this repo (`vm-amd/` is symlinked as `$GBOS_ROOT/tuned`) |
| `$GBOS_ROOT/gbos-vm-linux` | [chaitan3/gbos-vm-linux](https://github.com/chaitan3/gbos-vm-linux) checkout: recovery image, donor, guest Mesa, security-ported image (source of the layer's port files) |
| `$GBOS_ROOT/kernel/common` | AOSP `kernel/common` @ `android17-6.18-2026-06_r52` (`3938c8d97142`) |
| `$GBOS_ROOT/kernel/out` | VM kernel build (`KOUT` selects another, e.g. `out-full`) |
| `$GBOS_ROOT/vm/stock/googlebook.bin` | stock recovery image with userdata grown to 16 GiB |
| `$GBOS_ROOT/vm/layer.img` | the layer (GPT, partition `gbos_layer`), second NVMe namespace |
| `$GBOS_ROOT/vm/initrd` | VM ramdisk with the layer entries in the first-stage fstab |
| `$GBOS_ROOT/vm/data.qcow2` | overlay holding everything the guest writes (`/data`, misc, OTA slots) |

| File | What |
|---|---|
| `kernel/kmake.sh` | make wrapper (rustc 1.91.1, `KBUILD_GENDWARFKSYMS_STABLE=1`, SHA-1 signing allowed) |
| `kernel/gbos-vm.config` | config delta on top of `kernel/shipped_kernel_config.txt` |
| `kernel/gfni_emu/` | Makefile + `gfni_emu_builtin.c` (includes `module/gfni_emu.c`, adds the initcall) |
| `kernel/patches/gbos-amd-vm.patch` | the whole kernel tree delta (gfni_emu, overlayfs caller creds, 0001) |
| `kernel/fedora_drivers.py` | config fragment with Fedora's hardware drivers as modules, CRC-safe (see below) |
| `kernel/collect_firmware.py` | stages linux-firmware uncompressed for the GKI kernel |
| `zlib-build/build.sh` | `patches/build_libz.sh` pointed at a local AOSP `external/zlib` checkout |
| `layer/build_layer.py` | builds `vm/layer.img` |
| `boot/slot.py` | A/B selection from `misc` + vbmeta size/digest of the chosen slot |
| `boot/initrd.py` | adds the layer to the ramdisk's first-stage fstab |
| `run_vm.sh` | launcher |

Tests: `python3 -m unittest` in `kernel/`, `layer/` and `boot/`.

## Run

```sh
vm-amd/run_vm.sh                                   # SDL window, /data persists in vm/data.qcow2
GBOS_FRESH=1 vm-amd/run_vm.sh                      # start with a clean /data
GBOS_NET_ARGS="-nic none" vm-amd/run_vm.sh         # offline
GBOS_DISPLAY_ARGS="-display egl-headless -vnc 127.0.0.1:7" vm-amd/run_vm.sh
python3 host/vnc.py shot out.png --port 5907       # headless screenshot
```

`GBOS_MEM` (guest RAM, 4G), `GBOS_HOST_MEM_MAX` (cgroup cap, 7G), `GBOS_LOGLEVEL` (7 shows gfni_emu
counters), `GBOS_ONCE=1` (no relaunch after a guest reboot), `GBOS_WACOM=1` (old Wacom tablet instead of
usb-tablet + bridge). Serial log: `$GBOS_ROOT/vm/logs/<name>/serial.log`.

## Build from scratch

```sh
# kernel
cd $GBOS_ROOT/kernel/common && git apply $GBOS_ROOT/tuned/kernel/patches/gbos-amd-vm.patch
mkdir -p ../out && cp $GBOS_ROOT/gbos-x86-port/kernel/shipped_kernel_config.txt ../out/.config
scripts/kconfig/merge_config.sh -m -O ../out ../out/.config $GBOS_ROOT/tuned/kernel/gbos-vm.config
$GBOS_ROOT/tuned/kernel/kmake.sh olddefconfig && $GBOS_ROOT/tuned/kernel/kmake.sh -j11 bzImage modules
python3 $GBOS_ROOT/gbos-x86-port/kernel/check_crc.py ../out/Module.symvers <dir with Google's .ko>   # expect crc-mismatch=0

# image inputs: gbos-vm-linux README steps 1-6 (the security-ported image is only read by build_layer.py)
cp --sparse=always $GBOS_ROOT/gbos-vm-linux/android-desktop_signed_recovery_image.bin $GBOS_ROOT/vm/stock/googlebook.bin
python3 $GBOS_ROOT/gbos-vm-linux/expand_lapis_userdata.py $GBOS_ROOT/vm/stock/googlebook.bin 16
git clone --depth 1 https://android.googlesource.com/platform/external/zlib $GBOS_ROOT/tuned/zlib-build/src
ANDROID_NDK=$GBOS_ROOT/gbos-vm-linux/android-ndk-r30 bash $GBOS_ROOT/tuned/zlib-build/build.sh
ANDROID_NDK=$GBOS_ROOT/gbos-vm-linux/android-ndk-r30 bash pointer-bridge/build.sh
ANDROID_NDK=$GBOS_ROOT/gbos-vm-linux/android-ndk-r30 bash $GBOS_ROOT/tuned/layer/stubs/build.sh
python3 $GBOS_ROOT/tuned/layer/build_layer.py $GBOS_ROOT/vm/layer.img
python3 $GBOS_ROOT/tuned/boot/initrd.py $GBOS_ROOT/gbos-vm-linux/initrd $GBOS_ROOT/vm/initrd
```

## What the layer contains

* From the security port (read from its patched `vendor_a` by its `manifest.json`): software KeyMint,
  nonsecure Gatekeeper, Cuttlefish DRM composer and boot control, Venus/VirGL drivers, neutered Trusty
  services. Its two edits become additive files under `/odm`: `build.prop` overrides ->
  `/odm/etc/build.prop` (loaded after `/vendor/build.prop`), `vendor_file_contexts` additions (device
  labels: `/dev/ttyS0`, the virtio-gpu sysfs node) -> `/odm/etc/selinux/odm_file_contexts`.
* `ro.dalvik.vm.native.bridge=0` (libhoudini makes zygote abort silently).
* Baseline `libz.so` for `/vendor/lib64`, and for `/system/lib64` through a bind mount at `early-init`:
  `/system` cannot be overlaid on a user build (fs_mgr allowlist).
* `gscd` / `timberslide` stopped at `boot` (ChromeOS GSC / EC daemons that crash-loop in a VM).
* Fake AC / 100 % battery, applied as soon as zygote is up (the setup wizard checks it before
  `boot_completed`) and again whenever zygote restarts.
* `/vendor/bin/futility` replaced by a stub (`layer/stubs/futility.c`, `layer/stubs/build.sh`): the OTA
  postinstall step flashes the AP firmware with `futility update`; there is no SPI flash, and the real
  tool's failure made update_engine reject the whole update (`kPostinstallRunnerError`).
* `vm-pointer-bridge` (usb-tablet -> clickable pointer + wheel) in its own SELinux domain.
* `odm_sepolicy.cil` (that domain, the libz bind) plus a placeholder `/odm/etc/selinux/precompiled_sepolicy`:
  without its `.sha256` sidecars init skips the precompiled policy and compiles the CIL files, ours
  included (`secilc -N`, so no neverallow checks).

## Notes

* **SELinux is enforcing.** `androidboot.selinux=permissive` is ignored on a user build.
* **Overlay credentials**: first-stage init mounts the overlays before any policy is loaded, so the
  mounter is `u:r:kernel:s0`, which the policy does not let read vendor files for apexd & co.
  `overlay.gbos_caller_creds=1` (kernel patch) checks layer access with the caller's credentials, like
  Android's dropped `override_creds=off`.
* **The layer mount point** is under `/mnt/vendor` (a first-stage tmpfs moved by switch_root); a mount
  point missing from `/system` makes switch_root abort.
* **gfni_emu built in**: first-stage `init` already executes GFNI code. The hand-written kCFI hash of the
  die-notifier stub (`0xd5127a3b`) matches `hw_breakpoint_exceptions_notify`; needs `cfi=norand`.
* **Non-resident pages no longer mean SIGILL**: reads with page faults disabled used to fail on an evicted
  page (seen in `system_server`, `vgf2p8affineqb ... (%rip)` in libc's `strchr_avx2`); the emulator now
  faults the range in from a `task_work` and lets the instruction trap again.
* **CRCs**: `KBUILD_GENDWARFKSYMS_STABLE=1` and `CONFIG_LOCALVERSION` equal to the shipped release give
  0 mismatches over 79 shipped modules / 6590 symbols. Known exception: `cfg80211` (vendor_dlkm) differs
  in `net_ns_type_operations`. `rust_binder.ko` from the image does not load (Rust symbol hashes come from
  Google's toolchain); the C binder is used.
* **Fedora drivers** (`kernel/fedora_drivers.py`): options GKI leaves off are taken from a Fedora config
  as modules. Any option that adds a member to a type reachable from an exported symbol changes thousands
  of CRCs (gendwarfksyms walks the whole graph), so those are kept at their GKI value (`CORE_PROTECTED`,
  found by diffing `pahole --sizes` and `gendwarfksyms --symtypes` between the two builds) together with
  every driver that selects them; `fedora_drivers.py check` verifies the merged config.
* `TRIM_UNUSED_KSYMS` off (Google's KMI list is not public). `WERROR` / `OBJTOOL_WERROR` off (Fedora's
  clang 23 has warnings clang 22 lacks; codegen is unchanged). `protected_module_names_list` is empty.
* `patches/apply_*_update.py` cannot run on a fresh checkout: they read binaries the author patched by hand.
* Known harmless noise: `secretkeeper-hal-trusty` without Trusty, `adbd_tradeinmode` denials,
  `vrend ... RegionSampling`.
