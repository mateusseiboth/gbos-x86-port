# vm-amd: KVM VM on an AMD host without GFNI (Zen 3)

Runs the lapis (x86_64) image under QEMU/KVM on a Ryzen 5 5600GT (Zen 3: VAES/VPCLMULQDQ present,
**no GFNI, no AVX-VNNI**). Unlike `host/`, which uses Google's shipped kernel, this path builds AOSP
`kernel/common` with `module/gfni_emu.c` **built in** (registered as a `core_initcall`, before `/init`),
plus a small, reproducible image patch that does not depend on hand-patched binaries.

Verified 2026-11-19: full boot to the setup wizard in ~90 s, ~1–3k emulated instructions/s after the
libz swap, no unsupported opcodes.

## Layout (`GBOS_ROOT`, default `/mnt/dados/gbos`)

| Path | What |
|---|---|
| `$GBOS_ROOT/gbos-x86-port` | this repo (`vm-amd/` is symlinked as `$GBOS_ROOT/tuned`) |
| `$GBOS_ROOT/gbos-vm-linux` | [chaitan3/gbos-vm-linux](https://github.com/chaitan3/gbos-vm-linux) checkout, builds `image-security/` |
| `$GBOS_ROOT/kernel/common` | AOSP `kernel/common` @ `android17-6.18-2026-06_r52` (`3938c8d97142`) |
| `$GBOS_ROOT/kernel/out` | VM kernel build (`KOUT` selects another, e.g. `out-full`) |
| `$GBOS_ROOT/vm/persist.qcow2` | overlay holding the VM's `/data` |

| File | What |
|---|---|
| `kernel/kmake.sh` | make wrapper (rustc 1.91.1, `KBUILD_GENDWARFKSYMS_STABLE=1`, SHA-1 signing allowed) |
| `kernel/gbos-vm.config` | config delta on top of `kernel/shipped_kernel_config.txt` |
| `kernel/gfni_emu/` | Makefile + `gfni_emu_builtin.c` (includes `module/gfni_emu.c`, adds the initcall) |
| `kernel/patches/gbos-amd-vm.patch` | everything above plus `kernel/patches/0001-*`, as one patch for the kernel tree |
| `zlib-build/build.sh` | `patches/build_libz.sh` pointed at a local AOSP `external/zlib` checkout |
| `image/patch_partitions.py` | image patch (tests: `python3 -m unittest test_patch_partitions`) |
| `run_vm.sh` | launcher: qcow2 overlay, cgroup limits, our kernel, `cfi=norand` |

## Run

```sh
vm-amd/run_vm.sh                                   # SDL window, /data persists in the overlay
GBOS_FRESH=1 vm-amd/run_vm.sh                      # start with a clean /data
GBOS_NET_ARGS="-nic none" vm-amd/run_vm.sh         # offline (e.g. to get through setup)
GBOS_DISPLAY_ARGS="-display egl-headless -vnc 127.0.0.1:7" vm-amd/run_vm.sh
python3 host/vnc.py shot out.png --port 5907       # headless screenshot
```

`GBOS_MEM` (guest RAM, 4G), `GBOS_HOST_MEM_MAX` (cgroup cap, 7G), `GBOS_LOGLEVEL` (7 shows gfni_emu
counters), `GBOS_SECS`. Serial log: `$GBOS_ROOT/gbos-vm-linux/logs/<name>/serial.log`.

## Build from scratch

```sh
# kernel
cd $GBOS_ROOT/kernel/common && git apply $GBOS_ROOT/gbos-x86-port/vm-amd/kernel/patches/gbos-amd-vm.patch
mkdir -p ../out && cp $GBOS_ROOT/gbos-x86-port/kernel/shipped_kernel_config.txt ../out/.config
scripts/kconfig/merge_config.sh -m -O ../out ../out/.config $GBOS_ROOT/tuned/kernel/gbos-vm.config
$GBOS_ROOT/tuned/kernel/kmake.sh olddefconfig && $GBOS_ROOT/tuned/kernel/kmake.sh -j11 bzImage modules
python3 $GBOS_ROOT/gbos-x86-port/kernel/check_crc.py ../out/Module.symvers <dir with Google's .ko>   # expect crc-mismatch=0

# image: gbos-vm-linux README steps 1-6, then
git clone --depth 1 https://android.googlesource.com/platform/external/zlib $GBOS_ROOT/tuned/zlib-build/src
ANDROID_NDK=$GBOS_ROOT/gbos-vm-linux/android-ndk-r30 bash $GBOS_ROOT/tuned/zlib-build/build.sh
python3 $GBOS_ROOT/tuned/image/patch_partitions.py     # all | partitions | system | vendor | initrd
```

`build_security_image.sh` recreates the image *and* the initrd, so run `patch_partitions.py` again
after it, with the VM stopped.

## What the image patch does

* `system_a`, `vendor_a`: `libz.so` rebuilt for baseline x86-64 (the shipped Adler-32 loop uses AVX-VNNI).
* `system_a`: `ro.dalvik.vm.native.bridge=0`; `gscd` and `timberslide` disabled (ChromeOS GSC / EC
  daemons that crash-loop in a VM).
* `vendor_a`: fake AC / 100 % battery, re-applied every time zygote comes up (the values live in
  `system_server`, which forgets them when it restarts; `sys.boot_completed` does not fire again).
* initrd: first-stage fstab mounts `system` without `avb` (the partition is rewritten in place, so its
  hashtree is gone).

## Notes

* **gfni_emu built in**: first-stage `init` already executes GFNI code, a loadable module would come too
  late. The hand-written kCFI hash of the die-notifier stub (`0xd5127a3b`) matches
  `hw_breakpoint_exceptions_notify` in our `vmlinux`; needs `cfi=norand`.
* **Non-resident pages no longer mean SIGILL**: the emulator reads code and memory operands with page
  faults disabled. A page evicted under host memory pressure used to turn into SIGILL (seen in
  `system_server`, `vgf2p8affineqb ... (%rip)` in libc's `portable_simd_strchr_avx2`). It now queues a
  `task_work` that faults the range in on the way back to user mode and lets the instruction trap again;
  a range that really is unmapped gets SIGSEGV.
* **CRCs**: `KBUILD_GENDWARFKSYMS_STABLE=1` and `CONFIG_LOCALVERSION` equal to the shipped release give
  0 mismatches over 79 modules / 6590 symbols (ramdisks + CI virtio modules). Known exception:
  `cfg80211` (vendor_dlkm) differs in `net_ns_type_operations`; irrelevant in the VM (no Wi-Fi).
* `TRIM_UNUSED_KSYMS` off (Google's KMI list is not public). `WERROR` / `OBJTOOL_WERROR` off (Fedora's
  clang 23 has warnings clang 22 lacks; codegen is unchanged). `protected_module_names_list` is empty
  (Kleaf generates it; the check is disabled by `kernel/patches/0001-*`).
* `patches/apply_*_update.py` cannot run on a fresh checkout: they read binaries the author patched by
  hand (`surfaceflinger.patched.elf`, `init.patched.elf`, ...). `image/patch_partitions.py` keeps only
  what is needed to boot.
* Setup wizard: it may offer a ~164 MB OTA. It lands in slot `_b` and `run_vm.sh` always boots `_a`;
  if the wizard gets stuck on it, finish setup offline (`GBOS_NET_ARGS="-nic none"`).
* Known harmless noise: `secretkeeper-hal-trusty` without Trusty, `vrend ... RegionSampling`.
