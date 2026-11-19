#!/usr/bin/env python3
"""Build the gbos overlay layer: everything vm-amd adds to a *stock* lapis image, as one erofs
(with SELinux labels) that the first-stage fstab mounts over /vendor and /odm.

  build_layer.py OUT_DISK_IMG

The stock system_a / vendor_a stay byte-identical, so an OTA can verify and apply, and the layer
applies to whichever slot boots. /system itself cannot be overlaid on a user build (fs_mgr
allowlist), so the few system tweaks are done from a vendor init script (bind mount, stop).

Inputs (env, defaults under GBOS_ROOT):
  GBOS_STOCK_IMAGE   stock recovery image (labels, original build.prop)
  GBOS_PORT_IMAGE    gbos-vm-linux security-ported image + manifest.json next to it
                     (software KeyMint, Cuttlefish composer, Venus/VirGL, boot control, ...)
  GBOS_LIBZ          baseline libz.so (vm-amd/zlib-build)
  GBOS_FUTILITY_STUB   layer/stubs/out/futility (layer/stubs/build.sh)
  GBOS_GSCUTIL_STUB    layer/stubs/out/gscutil (layer/stubs/build.sh)
  GBOS_VM_MOUSE        pointer/out/vm-mouse (pointer/build.sh)
  GBOS_SWIFTSHADER     graphics/vulkan.pastel.so (layer/fetch_swiftshader.py)
  GBOS_VENDOR_MODULES  combined /vendor/lib/modules (kernel/resolve_modules.py --vendor-out)
  GBOS_FIRMWARE        firmware tree (kernel/collect_firmware.py), added under /vendor/firmware
"""
import io
import json
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bootanimation import build_bootanimation

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GBOS_ROOT', '/mnt/dados/gbos'))
LINUX_DIR = Path(os.environ.get('GBOS_LINUX_DIR', ROOT / 'gbos-vm-linux'))
STOCK_IMAGE = Path(os.environ.get('GBOS_STOCK_IMAGE', LINUX_DIR / 'android-desktop_signed_recovery_image.bin'))
PORT_IMAGE = Path(os.environ.get('GBOS_PORT_IMAGE', LINUX_DIR / 'image-security/googlebook-security.bin'))
LIBZ = Path(os.environ.get('GBOS_LIBZ', HERE.parent / 'zlib-build/out/libz.so'))
FUTILITY_STUB = Path(os.environ.get('GBOS_FUTILITY_STUB', HERE / 'stubs/out/futility'))
GSCUTIL_STUB = Path(os.environ.get('GBOS_GSCUTIL_STUB', HERE / 'stubs/out/gscutil'))
VM_MOUSE = Path(os.environ.get('GBOS_VM_MOUSE', HERE.parent / 'pointer/out/vm-mouse'))
SWIFTSHADER = Path(os.environ.get('GBOS_SWIFTSHADER', ROOT / 'graphics/vulkan.pastel.so'))
VENDOR_MODULES = Path(os.environ.get('GBOS_VENDOR_MODULES', ROOT / 'dlkm/vendor-modules'))
FIRMWARE = Path(os.environ.get('GBOS_FIRMWARE', ROOT / 'dlkm/firmware'))
SUPER_BASE = 1458176 * 512
PARTITION_NAME = 'gbos_layer'

# Files of the security port that edit a stock file instead of adding one. They are turned into
# additive files under /odm, which Android reads after /vendor: build.prop -> /odm/etc/build.prop
# overrides, vendor_file_contexts -> the added lines in /odm/etc/selinux/odm_file_contexts (device
# labels such as /dev/ttyS0 and the virtio-gpu sysfs node; the layer's own files carry xattrs).
PORT_EDITS = {'build.prop', 'etc/selinux/vendor_file_contexts'}

SYSTEM_LIBZ = 'etc/gbos/system/lib64/libz.so'
SYSTEM_BOOTANIMATION = 'etc/gbos/system/media/bootanimation.zip'
SYSTEM_GSCUTIL = 'etc/gbos/system/bin/gscutil'
SYSTEM_GSCUTIL_REAL = 'etc/gbos/system/bin/gscutil.real'
# The stock animation fades to black after ~5 s; this frame (wordmark) is held until boot completes.
BOOTANIMATION_HOLD_FRAME = 'part0/Googlebook+Gemini-Cropped-FullHeight-3x150.png'
BOOTANIMATION_TEXT = 'Starting up. The first boot can take a few minutes.'

SYSTEM_TWEAKS_RC = f'''# gbos: changes to /system without touching the partition (it cannot be overlaid on a user build).
# The shipped libz is built with AVX-VNNI; every APK/JAR read traps into gfni_emu without this.
on early-init
    mount none /vendor/{SYSTEM_LIBZ} /system/lib64/libz.so bind
    # Boot animation that stays up until boot completes (layer/bootanimation.py).
    mount none /vendor/{SYSTEM_BOOTANIMATION} /system/media/bootanimation.zip bind
    # No GSC: its firmware update fails the OTA postinstall (layer/stubs/gscutil.c). Keep the real
    # gscutil reachable for the wrapper, then put the wrapper in its place.
    mount none /system/bin/gscutil /vendor/{SYSTEM_GSCUTIL_REAL} bind
    # /system is shared: a peer of it would receive the next mount too, and the wrapper would
    # end up exec'ing itself.
    mount none none /vendor/{SYSTEM_GSCUTIL_REAL} private
    mount none /vendor/{SYSTEM_GSCUTIL} /system/bin/gscutil bind

# ChromeOS GSC / EC daemons: no such hardware in a VM, they only crash-loop. (init.svc.* of system
# services cannot be used as triggers from /vendor, so stop them at fixed points instead.)
on boot
    stop android.system.desktop.security.gscd
    stop timberslide
on property:sys.boot_completed=1
    stop android.system.desktop.security.gscd
    stop timberslide
'''.encode()

# gbos: power in a VM. QEMU has no battery and the ChromeOS EC is missing, so the battery read 0 %
# and setup refused to go on; test_power (kernel/fedora_drivers.py VM_EXTRAS) gives the health HAL
# a full battery on AC through /sys/class/power_supply. The guest never suspends (s2idle left the
# window blank mid-update) and the screen stays on while "plugged in".
TEST_POWER_OPTIONS = 'options test_power ac_online=on battery_status=full battery_capacity=100\n'

VM_POWER_RC = (
    b'on early-boot && property:ro.boot.gbos.vm=1\n'
    b'    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe -d /vendor/lib/modules test_power\n\n'
    b'on boot && property:ro.boot.gbos.vm=1\n'
    b'    write /sys/power/wake_lock gbos_vm\n\n'
    b'service vm-stay-awake /system/bin/sh -c "for i in $$(seq 1 60); do '
    b'cmd settings put global stay_on_while_plugged_in 7 && break; sleep 5; done; '
    b'cmd settings put system screen_off_timeout 2147483647; '
    b'cmd settings put system mouse_pointer_acceleration_enabled 0"\n'
    b'    class late_start\n'
    b'    user shell\n'
    b'    group shell\n'
    b'    seclabel u:r:shell:s0\n'
    b'    oneshot\n'
    b'    disabled\n\n'
    b'on property:sys.boot_completed=1 && property:ro.boot.gbos.vm=1\n'
    b'    start vm-stay-awake\n'
)

# gbos: QEMU usb-tablet -> relative "VM Mouse" (pointer/vm_mouse.c), VM only.
VM_MOUSE_RC = (
    b'service vm-mouse /vendor/bin/vm-mouse\n'
    b'    class late_start\n'
    b'    user root\n'
    b'    group root input uhid\n'
    b'    disabled\n\n'
    b'on boot && property:ro.boot.gbos.vm=1\n'
    b'    start vm-mouse\n'
)

# Versioned names (_202604) are how vendor-side policy refers to platform types.
ODM_SEPOLICY = b''';; gbos layer policy, compiled by init together with the stock policy.
;; VM_MOUSE_RC: reads the tablet, writes uinput, reads the display mode under /sys/class/drm.
(type gbos_vm_mouse)
(roletype object_r gbos_vm_mouse)
(type gbos_vm_mouse_exec)
(roletype object_r gbos_vm_mouse_exec)
(typeattributeset domain (gbos_vm_mouse))
(typeattributeset file_type (gbos_vm_mouse_exec))
(typeattributeset exec_type (gbos_vm_mouse_exec))
(typeattributeset vendor_file_type (gbos_vm_mouse_exec))
(allow init_202604 gbos_vm_mouse_exec (file (read getattr map execute open)))
(allow init_202604 gbos_vm_mouse (process (transition siginh rlimitinh)))
(dontaudit init_202604 gbos_vm_mouse (process (noatsecure)))
(typetransition init_202604 gbos_vm_mouse_exec process gbos_vm_mouse)
(allow gbos_vm_mouse gbos_vm_mouse_exec (file (read getattr map execute open entrypoint)))
(allow gbos_vm_mouse input_device_202604 (dir (ioctl read getattr lock open watch watch_reads search)))
(allow gbos_vm_mouse input_device_202604 (chr_file (ioctl read write getattr lock append map open)))
(allow gbos_vm_mouse uhid_device_202604 (chr_file (ioctl read write getattr lock append map open)))
(allow gbos_vm_mouse sysfs_202604 (dir (read open getattr search)))
(allow gbos_vm_mouse sysfs_202604 (file (read open getattr)))
(allow gbos_vm_mouse sysfs_202604 (lnk_file (read getattr)))
(allow gbos_vm_mouse sysfs_gpu_202604 (dir (read open getattr search)))
(allow gbos_vm_mouse sysfs_gpu_202604 (file (read open getattr)))
(allow gbos_vm_mouse sysfs_gpu_202604 (lnk_file (read getattr)))
;; SYSTEM_TWEAKS_RC: the gscutil wrapper (system_ext types) runs the real gscutil kept under /vendor.
(allow vendor_init_202604 gscutil_exec (file (mounton getattr)))
(allow init_202604 gscutil_exec (file (mounton getattr)))
(allow gscutil gscutil_exec (file (read getattr map execute open execute_no_trans)))
(allow gscutil vendor_file_202604 (dir (search getattr)))
(allow gscutil vendor_configs_file_202604 (dir (search getattr)))
;; VM_POWER_RC: hold a kernel wake lock so the guest never suspends.
(allow vendor_init_202604 sysfs_wake_lock_202604 (file (open write getattr)))
(allow vendor_init_202604 self (capability2 (block_suspend)))
;; SYSTEM_TWEAKS_RC: bind the baseline libz and the boot animation over their /system files.
(allow vendor_init_202604 system_lib_file_202604 (file (mounton getattr)))
(allow init_202604 system_lib_file_202604 (file (mounton getattr)))
(allow vendor_init_202604 system_file_202604 (file (mounton getattr)))
(allow init_202604 system_file_202604 (file (mounton getattr)))
'''

# Any file here makes init skip the vendor precompiled policy (its .sha256 sidecars are missing)
# and compile the CIL files, odm_sepolicy.cil included.
PRECOMPILED_PLACEHOLDER = b'gbos: forces on-device policy compilation, see odm_sepolicy.cil\n'


def prop_overrides(original: str, patched: str) -> dict:
    def assignments(text):
        values = {}
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
        return values
    before, after = assignments(original), assignments(patched)
    return {k: v for k, v in after.items() if before.get(k) != v}


def added_lines(original: str, patched: str) -> list:
    before = set(original.splitlines())
    return [l for l in patched.splitlines() if l.strip() and not l.startswith('#') and l not in before]


def odm_build_prop(overrides: dict) -> str:
    header = '# gbos: loaded after /vendor/build.prop, so these override it.\n'
    return header + ''.join(f'{k}={overrides[k]}\n' for k in sorted(overrides))


@dataclass(frozen=True)
class Symlink:
    target: str


# Label of a directory the stock vendor does not have, by its top-level component.
NEW_DIR_LABELS = {'firmware': 'vendor_firmware_file'}


def new_dir_meta(rel: str):
    return (0o755, 0, 2000, NEW_DIR_LABELS.get(rel.split('/')[0], 'vendor_file'))


# ueventd, vendor_modprobe and init.insmod.sh may only load .ko files labeled vendor_kernel_modules.
MODULE_LABELS = {'.ko': 'vendor_kernel_modules'}


def module_label(rel: str) -> str:
    return MODULE_LABELS.get(posixpath.splitext(rel)[1], 'vendor_file')


def firmware_label(rel: str) -> str:
    return 'vendor_firmware_file'


def tree_entries(root: Path, label_of, skip=()):
    """Layer file entries for a host directory tree; contents stay on disk until the tar is written."""
    entries = {}
    for d, _, names in os.walk(root):
        for n in names:
            path = Path(d) / n
            rel = path.relative_to(root).as_posix()
            if rel in skip:
                continue
            entries[rel] = ((Symlink(os.readlink(path)), 'vendor_file', 0o777, 0, 0) if path.is_symlink()
                            else (path, label_of(rel), 0o644, 0, 0))
    return entries


def parse_erofs_ls(text: str) -> dict:
    """{name: is_dir} from `dump.erofs --ls` output."""
    rows = text.split('FILENAME', 1)[1].splitlines() if 'FILENAME' in text else []
    out = {}
    for row in rows:
        f = row.split(None, 2)
        if len(f) == 3 and f[2] not in ('.', '..'):
            out[f[2]] = f[1] == '2'
    return out


def ancestor_dirs(files) -> set:
    dirs = set()
    for path in files:
        parent = posixpath.dirname(path)
        while parent:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    return dirs


def layer_path(partition: str, rel: str) -> str:
    norm = posixpath.normpath(rel)
    if rel.startswith('/') or norm.startswith('..'):
        raise ValueError(f'path escapes {partition}: {rel}')
    return f'{partition}/{norm}'


# ---- image access -------------------------------------------------------------------------

def _helpers():
    sys.path.insert(0, str(LINUX_DIR))
    import lp_tools, erofs_metadata, make_lapis_security_port as port
    return lp_tools, erofs_metadata, port


def extract_partition(image: Path, name: str, out: Path) -> Path:
    lp_tools, _, _ = _helpers()
    offset, _ = lp_tools.Super(image, SUPER_BASE).layout(name)[0]
    with image.open('rb') as f:
        f.seek(offset + 1024)
        sb = f.read(128)
        size = int.from_bytes(sb[36:40], 'little') << sb[12]
        f.seek(offset)
        with out.open('wb') as g:
            while size:
                chunk = f.read(min(size, 1 << 22))
                g.write(chunk)
                size -= len(chunk)
    return out


def _entry(fs, rel, data):
    _, erofs_metadata, port = _helpers()
    m = erofs_metadata.metadata(fs, '/' + rel)
    return (data, port._label(m), m['mode'] & 0o7777, m['uid'], m['gid'])


def port_files(port_fs: Path, stock_fs: Path):
    """(files, build.prop overrides) taken from the security port's patched vendor."""
    _, _, port = _helpers()
    manifest = json.loads((PORT_IMAGE.parent / 'manifest.json').read_text())
    files = {rel: _entry(port_fs, rel, port.dump_cat(port_fs, '/' + rel))
             for rel in manifest['files'] if rel not in PORT_EDITS}
    def both(rel):
        return port.dump_cat(stock_fs, '/' + rel).decode(), port.dump_cat(port_fs, '/' + rel).decode()
    overrides = prop_overrides(*both('build.prop'))
    contexts = added_lines(*both('etc/selinux/vendor_file_contexts'))
    return files, overrides, contexts


def stock_tree(fs: Path, rel: str) -> set:
    """Every path below rel in a stock erofs image."""
    out = subprocess.run(['dump.erofs', '--ls', f'--path=/{rel}', str(fs)], check=True,
                         capture_output=True, text=True).stdout
    paths = set()
    for name, is_dir in parse_erofs_ls(out).items():
        paths.add(f'{rel}/{name}')
        paths |= stock_tree(fs, f'{rel}/{name}') if is_dir else set()
    return paths


def driver_files(stock_vendor: Path):
    """Fedora-driver modules (in place of the /vendor/lib/modules symlink) and the firmware the
    stock vendor does not already ship."""
    modules = {f'lib/modules/{k}': v for k, v in tree_entries(VENDOR_MODULES, module_label).items()}
    options = (VENDOR_MODULES / 'modules.options').read_text() + TEST_POWER_OPTIONS
    modules['lib/modules/modules.options'] = (options.encode(), 'vendor_file', 0o644, 0, 0)
    stock = stock_tree(stock_vendor, 'firmware')
    firmware = {f'firmware/{k}': v for k, v in tree_entries(FIRMWARE, firmware_label, skip={'MISSING.txt'}).items()
                if f'firmware/{k}' not in stock}
    return {**modules, **firmware}


def gbos_vendor_files(stock_vendor: Path, stock_system: Path):
    _, _, port = _helpers()
    files = {
        'lib64/libz.so': _entry(stock_vendor, 'lib64/libz.so', LIBZ.read_bytes()),
        SYSTEM_LIBZ: _entry(stock_system, 'system/lib64/libz.so', LIBZ.read_bytes()),
        SYSTEM_BOOTANIMATION: _entry(stock_system, 'system/media/bootanimation.zip', build_bootanimation(
            port.dump_cat(stock_system, '/system/media/bootanimation.zip'), BOOTANIMATION_HOLD_FRAME,
            BOOTANIMATION_TEXT)),
        'etc/init/gbos-system-tweaks.rc': (SYSTEM_TWEAKS_RC, 'vendor_configs_file', 0o644, 0, 0),
        'etc/init/vm-power.rc': (VM_POWER_RC, 'vendor_configs_file', 0o644, 0, 0),
        'etc/init/vm-mouse.rc': (VM_MOUSE_RC, 'vendor_configs_file', 0o644, 0, 0),
        'bin/vm-mouse': (VM_MOUSE.read_bytes(), 'gbos_vm_mouse_exec', 0o755, 0, 2000),
        # OTA postinstall flashes the AP firmware through futility; there is none (stubs/futility.c).
        'bin/futility': _entry(stock_vendor, 'bin/futility', FUTILITY_STUB.read_bytes()),
        SYSTEM_GSCUTIL: _entry(stock_system, 'system/bin/gscutil', GSCUTIL_STUB.read_bytes()),
        # Graphics for GPUs without drivers in the image (gpu_amd/swiftshader.prop).
        'lib64/hw/vulkan.pastel.so': _entry(stock_vendor, 'lib64/hw/vulkan.intel.so', SWIFTSHADER.read_bytes()),
        'lib64/libminigbm_gralloc.so': _entry(stock_vendor, 'lib64/libminigbm_gralloc.so', patch_minigbm(
            port.dump_cat(stock_vendor, '/lib64/libminigbm_gralloc.so'))),
        SYSTEM_GSCUTIL_REAL: _entry(stock_system, 'system/bin/gscutil', b''),  # mount point
    }
    return files


# Graphics stack, chosen at boot: androidboot.gbos.gpu (run_vm.sh; usb/gbos_boot.c from the PCI
# display controller) selects /odm/etc/gbos/gpu_<stack>.prop, imported by /odm/etc/build.prop.
GPU_KEYS = {'debug.hwui.renderer', 'debug.renderengine.backend', 'debug.renderengine.vulkan',
            'debug.renderengine.graphite_desktop_optin', 'ro.gfx.angle.supported', 'ro.hardware.egl',
            'ro.hardware.vulkan', 'ro.hwui.use_vulkan'}
GPU_IMPORT = 'import /odm/etc/gbos/gpu_${ro.boot.gbos.gpu:-virgl}.prop'

# venus: apps (HWUI) and SurfaceFlinger render with Vulkan through Venus (vulkan.virtio.so -> the host's
# Vulkan driver). Needs QEMU on the patched virglrenderer (vm-amd/virgl, GBOS_VULKAN=1).
VULKAN_PROPS = {
    'debug.hwui.renderer': 'skiavk',
    'ro.hwui.use_vulkan': 'true',
    'debug.renderengine.backend': 'skiavkthreaded',
    'debug.renderengine.vulkan': 'true',
    'ro.hardware.vulkan': 'virtio',
}

# amd / swiftshader: ANGLE (in /system) on SwiftShader Vulkan (vulkan.pastel.so, from Cuttlefish), as
# Cuttlefish's guest_swiftshader mode. The image has no Mesa for AMD GPUs yet.
SOFTWARE_PROPS = {
    'ro.hardware.egl': 'angle',
    'ro.hardware.vulkan': 'pastel',
    'ro.gfx.angle.supported': 'true',
    'debug.hwui.renderer': 'skiagl',
    'ro.hwui.use_vulkan': 'false',
    'debug.renderengine.backend': 'skiaglthreaded',
    'debug.renderengine.vulkan': 'false',
}


def gpu_props(overrides: dict) -> dict:
    """{stack: props}. virgl is the security port's GL-over-virgl stack; intel keeps the stock drivers."""
    virgl = {k: v for k, v in overrides.items() if k in GPU_KEYS}
    return {'virgl': virgl, 'venus': dict(virgl, **VULKAN_PROPS), 'intel': {},
            'amd': SOFTWARE_PROPS, 'swiftshader': SOFTWARE_PROPS}


def patch_minigbm(lib: bytes) -> bytes:
    """Google's minigbm has no amdgpu backend; its "radeon" entry is the generic dumb-buffer backend,
    which amdgpu supports, and the names have the same length."""
    old, new = b'\x00radeon\x00', b'\x00amdgpu\x00'
    if lib.count(old) != 1:
        raise ValueError(f'expected one "radeon" driver name in minigbm, found {lib.count(old)}')
    return lib.replace(old, new)


# PCI identity files and drm/ nodes of any PCI device, at any bridge depth, as sysfs_gpu: libdrm and
# Mesa read them to find the GPU. The security port labels only 0000:00:01.0 (the VM's GPU slot).
PCI_DEVICE = r'/sys/devices/pci[0-9a-f:]+(/[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])+'
GPU_SYSFS_CONTEXTS = [
    PCI_DEVICE + r'/drm(/.*)? u:object_r:sysfs_gpu:s0',
    PCI_DEVICE + r'/(uevent|vendor|device|subsystem_vendor|subsystem_device|revision|class|boot_vga|config)'
    r' u:object_r:sysfs_gpu:s0',
]


def odm_files(overrides: dict, contexts: list):
    base = {k: v for k, v in overrides.items() if k not in GPU_KEYS}
    base['ro.dalvik.vm.native.bridge'] = '0'
    stacks = {f'etc/gbos/gpu_{stack}.prop': (odm_build_prop(props).encode(), 'vendor_configs_file', 0o644, 0, 0)
              for stack, props in gpu_props(overrides).items()}
    return {
        'etc/build.prop': ((odm_build_prop(base) + GPU_IMPORT + '\n').encode(), 'vendor_configs_file', 0o644, 0, 0),
        **stacks,
        'etc/selinux/odm_file_contexts': (''.join(l + '\n' for l in contexts + GPU_SYSFS_CONTEXTS).encode(),
                                          'vendor_configs_file', 0o644, 0, 0),
        'etc/selinux/odm_sepolicy.cil': (ODM_SEPOLICY, 'vendor_configs_file', 0o644, 0, 0),
        'etc/selinux/precompiled_sepolicy': (PRECOMPILED_PLACEHOLDER, 'vendor_configs_file', 0o644, 0, 0),
    }


def layer_dirs(vendor_files, stock_vendor: Path, stock_system: Path):
    _, erofs_metadata, port = _helpers()
    root = erofs_metadata.metadata(stock_vendor, '/')
    odm = erofs_metadata.metadata(stock_system, '/odm')
    dirs = {'.': (0o755, 0, 0, port._label(root)),
            'vendor': (root['mode'] & 0o7777, root['uid'], root['gid'], port._label(root)),
            'odm': (odm['mode'] & 0o7777, odm['uid'], odm['gid'], port._label(odm)),
            'odm/etc': (0o755, 0, 2000, 'vendor_configs_file'),
            'odm/etc/selinux': (0o755, 0, 0, 'vendor_configs_file'),
            'odm/etc/gbos': (0o755, 0, 0, 'vendor_configs_file')}
    # lib/modules is a symlink in the stock vendor: the layer's directory takes its place.
    new_dirs = {d: new_dir_meta(d) for d in sorted(ancestor_dirs(vendor_files)) if not _is_stock_dir(stock_vendor, d)}
    for rel, meta in port.dir_entries(stock_vendor, vendor_files, new_dirs).items():
        dirs[layer_path('vendor', rel)] = meta
    return dirs


def _is_stock_dir(fs: Path, rel: str) -> bool:
    _, erofs_metadata, _ = _helpers()
    try:
        return stat.S_ISDIR(erofs_metadata.metadata(fs, '/' + rel)['mode'])
    except Exception:
        return False


def _add_bytes(t, ti, data):
    ti.size = len(data)
    t.addfile(ti, io.BytesIO(data))


def _add_path(t, ti, path):
    ti.size = path.stat().st_size
    with path.open('rb') as f:
        t.addfile(ti, f)


def _add_link(t, ti, link):
    ti.type, ti.linkname = tarfile.SYMTYPE, link.target
    t.addfile(ti)


ADD_ENTRY = {bytes: _add_bytes, Path: _add_path, Symlink: _add_link}


def write_tar(path: Path, dirs, files):
    """The layer as a tarball for mkfs.erofs --tar, SELinux labels as xattrs; file data may be
    bytes, a host Path (read while writing) or a Symlink."""
    def info(name, mode, uid, gid, label):
        ti = tarfile.TarInfo(name)
        ti.mode, ti.uid, ti.gid, ti.mtime = mode & 0o7777, uid, gid, 1230768000
        ti.pax_headers = {'SCHILY.xattr.security.selinux': f'u:object_r:{label}:s0'}
        return ti
    with tarfile.open(path, 'w', format=tarfile.PAX_FORMAT) as t:
        for name, (mode, uid, gid, label) in dirs.items():
            ti = info(name, mode, uid, gid, label)
            ti.type = tarfile.DIRTYPE
            t.addfile(ti)
        for name, (data, label, mode, uid, gid) in files.items():
            add = next(fn for kind, fn in ADD_ENTRY.items() if isinstance(data, kind))
            add(t, info(name, mode, uid, gid, label), data)


def build_erofs(dirs, files, out: Path, tmp: Path):
    tar = tmp / 'layer.tar'
    write_tar(tar, dirs, files)
    tar_mb = tar.stat().st_size >> 20
    subprocess.run(['mkfs.erofs', '--tar=f', '-x1', '-zlz4hc', str(out), str(tar)], check=True, capture_output=True)
    print(f'layer tar {tar_mb} MB -> erofs {out.stat().st_size >> 20} MB')
    subprocess.run(['fsck.erofs', str(out)], check=True, capture_output=True)


def build_disk(erofs: Path, disk: Path):
    sectors = (erofs.stat().st_size + 511) // 512
    part_sectors = max(sectors * 2, 65536)
    disk.unlink(missing_ok=True)
    with disk.open('wb') as f:
        f.truncate((2048 + part_sectors + 2048) * 512)
    subprocess.run(['sgdisk', '-o', '-n', f'1:2048:{2048 + part_sectors - 1}', '-c', f'1:{PARTITION_NAME}', str(disk)],
                   check=True, capture_output=True)
    with disk.open('r+b') as f, erofs.open('rb') as src:
        f.seek(2048 * 512)
        shutil.copyfileobj(src, f, 16 << 20)


def main():
    out = Path(sys.argv[1])
    for need in (STOCK_IMAGE, PORT_IMAGE, LIBZ, FUTILITY_STUB, GSCUTIL_STUB, VM_MOUSE, SWIFTSHADER, VENDOR_MODULES,
                 FIRMWARE):
        if not need.exists():
            raise SystemExit(f'missing {need}')
    tmp = Path(tempfile.mkdtemp(prefix='gbos-layer-', dir=out.parent))
    try:
        stock_vendor = extract_partition(STOCK_IMAGE, 'vendor_a', tmp / 'stock_vendor.erofs')
        stock_system = extract_partition(STOCK_IMAGE, 'system_a', tmp / 'stock_system.erofs')
        port_vendor = extract_partition(PORT_IMAGE, 'vendor_a', tmp / 'port_vendor.erofs')
        pfiles, overrides, contexts = port_files(port_vendor, stock_vendor)
        vendor = {**pfiles, **gbos_vendor_files(stock_vendor, stock_system), **driver_files(stock_vendor)}
        files = {layer_path('vendor', k): v for k, v in vendor.items()}
        files.update({layer_path('odm', k): v for k, v in odm_files(overrides, contexts).items()})
        build_erofs(layer_dirs(vendor, stock_vendor, stock_system), files, tmp / 'layer.erofs', tmp)
        build_disk(tmp / 'layer.erofs', out)
        print(f'layer: {len(files)} files, {len(overrides)} prop overrides, {len(contexts)} file contexts -> {out}')
    finally:
        shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
