# vm-amd: KVM VM on an AMD host without GFNI (Zen 3)

Runs the lapis (x86_64) image under QEMU/KVM on a Ryzen 5 5600GT (Zen 3: VAES/VPCLMULQDQ present,
**no GFNI, no AVX-VNNI**). Unlike `host/`, which uses Google's shipped kernel, this path builds AOSP
`kernel/common` with `module/gfni_emu.c` **built in** (registered as a `core_initcall`, before `/init`)
and Fedora's hardware drivers as modules (`kernel/out-full`), which the layer ships together with
Fedora's firmware.

The image stays **stock**: `system_a` / `vendor_a` are byte-identical and dm-verity is on. Everything
vm-amd changes lives in one erofs **layer** that the first-stage fstab mounts over `/vendor` and `/odm`.
An OTA can therefore verify and apply, and the layer applies to whichever slot boots; `run_vm.sh` reads
the A/B state from `misc` like a bootloader and relaunches the VM after the guest reboots.

Verified 2026-11-19: stock image + layer boots to the setup wizard (first boot ~165 s), clicks work, ~1.5–3k emulated instructions/s with the libz bind, no unsupported opcodes.
**OTA end to end**: the wizard's 164 MB update downloaded and applied in ~8 min, the guest rebooted,
`run_vm.sh` came back up on slot `_b` with the layer on top, setup continued with `/data` intact, and
the boot-control HAL marked `_b` successful. With the Fedora-driver kernel and the module layer the
guest boots in ~80-95 s (fresh `/data`); ueventd loads Google's modules and ours from the combined index
(`lpc_ich`, `iTCO_wdt`, `ahci`, `qemu_fw_cfg`, ... next to Google's `joydev`, `mac80211`), no CRC or
SELinux failures.

## Layout (`GBOS_ROOT`, default `/mnt/dados/gbos`)

| Path | What |
|---|---|
| `$GBOS_ROOT/gbos-x86-port` | this repo (`vm-amd/` is symlinked as `$GBOS_ROOT/tuned`) |
| `$GBOS_ROOT/gbos-vm-linux` | [chaitan3/gbos-vm-linux](https://github.com/chaitan3/gbos-vm-linux) checkout: recovery image, donor, guest Mesa, security-ported image (source of the layer's port files) |
| `$GBOS_ROOT/kernel/common` | AOSP `kernel/common` @ `android17-6.18-2026-06_r52` (`3938c8d97142`) |
| `$GBOS_ROOT/kernel/out` | GKI-config kernel build (the CRC reference for `out-full`) |
| `$GBOS_ROOT/kernel/out-full` | kernel with Fedora's drivers: what `run_vm.sh` boots |
| `$GBOS_ROOT/dlkm/` | `staging` (modules_install), `vendor-modules` (the layer's `/vendor/lib/modules`), `firmware` |
| `$GBOS_ROOT/vm/stock/googlebook.bin` | stock recovery image with userdata grown to 64 GiB (16 GiB filled up with the OTA and app updates) |
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
| `kernel/resolve_modules.py` | picks ours or Google's for every module name both sides ship; writes the combined `/vendor/lib/modules` |
| `zlib-build/build.sh` | `patches/build_libz.sh` pointed at a local AOSP `external/zlib` checkout |
| `layer/build_layer.py` | builds `vm/layer.img` (`--vulkan`: `vm/layer-vulkan.img`) |
| `virgl/` | virglrenderer 1.3.0 + `external-egl-gbm.patch` for Vulkan (Venus) under QEMU; `build.sh` -> `virgl/out` |
| `boot/slot.py` | A/B selection from `misc` + vbmeta size/digest of the chosen slot |
| `boot/initrd.py` | adds the layer to the ramdisk's first-stage fstab |
| `boot/progress.py` | prints the boot phases from the serial log while QEMU runs |
| `layer/bootanimation.py` | the stock boot animation plus a held frame that loops until boot completes |
| `run_vm.sh` | launcher |

Tests: `python3 -m unittest` in `kernel/`, `layer/` and `boot/`.

## Run

```sh
vm-amd/run_vm.sh                                   # SDL window, /data persists in vm/data.qcow2
GBOS_FRESH=1 vm-amd/run_vm.sh                      # start with a clean /data
GBOS_NET_ARGS="-nic none" vm-amd/run_vm.sh         # offline
GBOS_DISPLAY_ARGS="-display egl-headless -vnc 127.0.0.1:7" vm-amd/run_vm.sh
GBOS_VULKAN=1 vm-amd/run_vm.sh                     # Vulkan rendering (layer-vulkan.img + virgl/out)
python3 host/vnc.py shot out.png --port 5907       # headless screenshot
```

The terminal shows the boot phases as the serial log reaches them (`boot/progress.py`): kernel
started, Android userspace running, boot animation on screen, boot completed. The window is black for
the first ~20 s (Google's virtio-gpu module sets up no framebuffer console), then shows the boot
animation until Android is ready.

`GBOS_SMP` (vCPUs, 8), `GBOS_MEM` (guest RAM, 8G), `GBOS_HOST_MEM_MAX` (cgroup cap, 12G), `GBOS_LOGLEVEL` (7 shows gfni_emu
counters), `GBOS_ONCE=1` (no relaunch after a guest reboot). Serial log:
`$GBOS_ROOT/vm/logs/<name>/serial.log`.

The mouse is QEMU's `usb-mouse`, a real relative mouse for Android (cursor, hover, right click, wheel,
acceleration): click the window to capture it, Ctrl+Alt+G releases it. `show-cursor=on` keeps the
cursor visible while captured (it is Android's, drawn on the virtio-gpu cursor plane). The tablet + `pointer-bridge`
path showed up as a hovering stylus, and system_server's InputDispatcher aborted (`Expected
ACTION_HOVER_MOVE instead of MotionEvent`) when a window changed under it.

## Build from scratch

```sh
# kernel
cd $GBOS_ROOT/kernel/common && git apply $GBOS_ROOT/tuned/kernel/patches/gbos-amd-vm.patch
mkdir -p ../out && cp $GBOS_ROOT/gbos-x86-port/kernel/shipped_kernel_config.txt ../out/.config
scripts/kconfig/merge_config.sh -m -O ../out ../out/.config $GBOS_ROOT/tuned/kernel/gbos-vm.config
$GBOS_ROOT/tuned/kernel/kmake.sh olddefconfig && $GBOS_ROOT/tuned/kernel/kmake.sh -j11 bzImage modules
python3 $GBOS_ROOT/gbos-x86-port/kernel/check_crc.py ../out/Module.symvers <dir with Google's .ko>   # expect crc-mismatch=0

# kernel with Fedora's drivers (separate build dir)
python3 $GBOS_ROOT/tuned/kernel/fedora_drivers.py /boot/config-$(uname -r) ../out/.config $GBOS_ROOT/tuned/kernel/fedora-drivers.config
mkdir -p ../out-full && scripts/kconfig/merge_config.sh -m -O ../out-full ../out/.config $GBOS_ROOT/tuned/kernel/fedora-drivers.config
KOUT=$GBOS_ROOT/kernel/out-full $GBOS_ROOT/tuned/kernel/kmake.sh olddefconfig
python3 $GBOS_ROOT/tuned/kernel/fedora_drivers.py check ../out/.config ../out-full/.config         # expect "core unchanged"
KOUT=$GBOS_ROOT/kernel/out-full $GBOS_ROOT/tuned/kernel/kmake.sh -j11 bzImage modules
KOUT=$GBOS_ROOT/kernel/out-full $GBOS_ROOT/tuned/kernel/kmake.sh INSTALL_MOD_PATH=$GBOS_ROOT/dlkm/staging INSTALL_MOD_STRIP=1 modules_install
# Google's modules: vendor_dlkm_a / system_dlkm_a extracted under work/shipped-dlkm, the ramdisks' under work/shipped-ko
python3 $GBOS_ROOT/tuned/kernel/resolve_modules.py $GBOS_ROOT/dlkm/staging $GBOS_ROOT/work/shipped-dlkm $GBOS_ROOT/work/shipped-ko \
  --symvers ../out-full/Module.symvers --out $GBOS_ROOT/dlkm/modules.selection --vendor-out $GBOS_ROOT/dlkm/vendor-modules
python3 $GBOS_ROOT/tuned/kernel/collect_firmware.py /lib/firmware $GBOS_ROOT/dlkm/firmware $GBOS_ROOT/dlkm/staging/lib/modules/<release>

# image inputs: gbos-vm-linux README steps 1-6 (the security-ported image is only read by build_layer.py)
cp --sparse=always $GBOS_ROOT/gbos-vm-linux/android-desktop_signed_recovery_image.bin $GBOS_ROOT/vm/stock/googlebook.bin
python3 $GBOS_ROOT/gbos-vm-linux/expand_lapis_userdata.py $GBOS_ROOT/vm/stock/googlebook.bin 64
git clone --depth 1 https://android.googlesource.com/platform/external/zlib $GBOS_ROOT/tuned/zlib-build/src
ANDROID_NDK=$GBOS_ROOT/gbos-vm-linux/android-ndk-r30 bash $GBOS_ROOT/tuned/zlib-build/build.sh
ANDROID_NDK=$GBOS_ROOT/gbos-vm-linux/android-ndk-r30 bash $GBOS_ROOT/tuned/layer/stubs/build.sh
python3 $GBOS_ROOT/tuned/layer/build_layer.py $GBOS_ROOT/vm/layer.img
python3 $GBOS_ROOT/tuned/layer/build_layer.py $GBOS_ROOT/vm/layer-vulkan.img --vulkan   # optional
$GBOS_ROOT/tuned/virgl/build.sh                                                       # for GBOS_VULKAN=1
python3 $GBOS_ROOT/tuned/boot/initrd.py $GBOS_ROOT/gbos-vm-linux/initrd $GBOS_ROOT/vm/initrd
```

## What the layer contains

* From the security port (read from its patched `vendor_a` by its `manifest.json`): software KeyMint,
  nonsecure Gatekeeper, Cuttlefish DRM composer and boot control, Venus/VirGL drivers, neutered Trusty
  services. Its two edits become additive files under `/odm`: `build.prop` overrides ->
  `/odm/etc/build.prop` (loaded after `/vendor/build.prop`), `vendor_file_contexts` additions (device
  labels: `/dev/ttyS0`, the virtio-gpu sysfs node) -> `/odm/etc/selinux/odm_file_contexts`.
* `ro.dalvik.vm.native.bridge=0` (libhoudini makes zygote abort silently).
* Rendering: `vm/layer.img` keeps GL over virgl (the security port's choice); `vm/layer-vulkan.img`
  (`build_layer.py --vulkan`, run with `GBOS_VULKAN=1`) renders apps and SurfaceFlinger with Vulkan
  through Venus. That needs `vm-amd/virgl`: QEMU hands virglrenderer its own EGL display, and in that
  mode virglrenderer never created a GBM device, so no guest buffer could be exported to Venus;
  SurfaceFlinger aborted importing a gralloc buffer (`vkGetMemoryResourcePropertiesMESA`, invalid
  res_id) and the guest boot-looped. `virgl/external-egl-gbm.patch` creates the GBM device in that mode
  too, and `VIRGL_GBM_LAYOUT_FORCE_ENABLE=1` allocates shared/scanout buffers through it.
  **Experimental, does not boot yet**: Venus no longer goes fatal, but SurfaceFlinger still aborts
  (`Failed to create a valid texture`): app/SystemUI buffers are plain texture + render-target
  resources, not GBM-allocated, and even GBM-allocated ones would need the guest (minigbm, stride
  `width * 4`) and the host (amdgpu, 256-byte pitch) to agree on the layout, which the virgl path has
  no channel for (ChromeOS uses `RESOURCE_INFO_CROS` + crosvm). The real fix is gfxstream through
  rutabaga (Fedora's rutabaga is built without it) with Cuttlefish's guest graphics stack.
* Baseline `libz.so` for `/vendor/lib64`, and for `/system/lib64` through a bind mount at `early-init`:
  `/system` cannot be overlaid on a user build (fs_mgr allowlist).
* Boot animation: the stock one fades to black after ~5 s (enough for a real Googlebook); the layer's
  copy keeps playing it, then holds the wordmark with "Starting up. The first boot can take a few
  minutes." until boot completes. Bound over `/system/media/bootanimation.zip` at `early-init`.
* `gscd` / `timberslide` stopped at `boot` (ChromeOS GSC / EC daemons that crash-loop in a VM).
* **Power** (`vm-power.rc`): the kernel's `test_power` module (built through `fedora_drivers.py`
  `VM_EXTRAS`, options `ac_online=on battery_status=full battery_capacity=100`) gives the health HAL a
  full battery on AC through `/sys/class/power_supply`; the old `cmd battery set` override lived only in
  system_server and got lost. A kernel wake lock (`gbos_vm`) keeps the guest from suspending (s2idle
  left the window on "Display output is not active" in the middle of an update), and
  `stay_on_while_plugged_in` / `screen_off_timeout` keep the screen on (`svc power stayon` needs
  `WRITE_SETTINGS`, which the shell lacks on a user build).
* `/vendor/bin/futility` replaced by a stub (`layer/stubs/futility.c`, `layer/stubs/build.sh`): the OTA
  postinstall step flashes the AP firmware with `futility update`; there is no SPI flash, and the real
  tool's failure made update_engine reject the whole update (`kPostinstallRunnerError`).
* `/system/bin/gscutil` wrapped (`layer/stubs/gscutil.c`, bound at `early-init`): the postinstall
  step also updates the GSC firmware (`gscutil perform-post-install`), which fails without a GSC. The
  wrapper reports that one command as done and runs the real gscutil (kept at
  `/vendor/etc/gbos/system/bin/gscutil.real`) for everything else. The kept copy is made a private
  mount before the wrapper covers the original: `/system` is shared, so otherwise the wrapper's mount
  propagated onto the copy, the wrapper exec'd itself and init hung on `gscutil perform-os-startup`.
* **Drivers**: a `/vendor/lib/modules` directory in place of the stock symlink to
  `/vendor_dlkm/lib/modules`. ueventd (modalias handling), `init.insmod.sh` and the vendor rc files all
  read that path, so its index decides per name: our 3316 modules are files there, Google's 564 are named
  by their real path (`/vendor_dlkm/lib/modules/x.ko`, `/system/lib/modules/x.ko`; libmodprobe opens
  modules with `O_NOFOLLOW`, so links do not work). `modules.load` / `.blocklist` / `.options` are
  Google's. `.ko` files are labeled `vendor_kernel_modules`, which the stock policy already lets
  ueventd and the modprobe domains load.
* **Firmware**: Fedora's linux-firmware under `/vendor/firmware` (`vendor_firmware_file`, the
  `firmware_class.path`), except files the stock vendor already ships. The layer is lz4hc-compressed
  (~770 MB).
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
* **Fedora modules vs Google's** (`kernel/resolve_modules.py`): ~540 module names exist on both sides
  (DRM helpers, cfg80211, bluetooth, SOF, virtio, ...) and only one of each can load. Starting from
  Google's, a name switches to ours when that lets more modules load without breaking a Google module;
  a module counts as loadable only if every need is met by modules that load (fixpoint). Result on the
  lapis build: 3958 names, 20 use ours (`cfg80211`, `iwlwifi`/`iwlmvm`/`iwlmld`, `drm_ttm_helper`, `ptp`,
  `rust_binder`, ...), 36 of ours dropped (niche Bluetooth, Qualcomm codecs, `cs8409`, AMD SOF/ACP: the
  SOF core stays Google's because its Intel SOF drivers depend on it). Output: `dlkm/modules.selection`
  and, with `--vendor-out`, the layer's `/vendor/lib/modules`. That index is built from the vendor_dlkm
  the layer was made for: after an OTA that changes Google's module set, rerun the resolver and
  `build_layer.py` (an unchanged set keeps working, since the index names files, not contents).
* **No hardware watchdog drivers** (`drivers/watchdog/`, `ipmi_watchdog`): nothing in this Android
  feeds one. With `iTCO_wdt` loaded, QEMU's ICH9 TCO timer fired ~3 min into boot and reset the guest,
  which `-no-reboot` turned into QEMU closing without a message. A real PC (`iTCO_wdt`, `sp5100_tco`)
  would reboot the same way.
* **`-machine q35,i8042=off`**: Fedora's `psmouse` enables the Q35 PS/2 mouse, and QEMU then sends
  the pointer to it instead of to the USB mouse/tablet, and clicks did nothing.
  Real PCs keep `psmouse` (PS/2 touchpads).
* Modules load only after switch_root (ueventd). Storage/USB controllers that must exist before that
  (`ahci`, NVMe is built in) work in the VM because the disk is NVMe; a real-PC image needs them in the
  first-stage ramdisk.
* Firmware: `collect_firmware.py` stages Fedora's linux-firmware (7404 entries, 1.1 GB uncompressed);
  476 names declared by modules are not packaged by Fedora (mostly NVIDIA, DVB, legacy b43/Broadcom).
* `TRIM_UNUSED_KSYMS` off (Google's KMI list is not public). `WERROR` / `OBJTOOL_WERROR` off (Fedora's
  clang 23 has warnings clang 22 lacks; codegen is unchanged). `protected_module_names_list` is empty.
* `patches/apply_*_update.py` cannot run on a fresh checkout: they read binaries the author patched by hand.
* Known harmless noise: `secretkeeper-hal-trusty` without Trusty, `adbd_tradeinmode` denials,
  `vrend ... RegionSampling`.
