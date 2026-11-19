#!/usr/bin/env python3
"""Build the gbos overlay layer: everything vm-amd adds to a *stock* lapis image, as one erofs
(with SELinux labels) that the first-stage fstab mounts over /vendor and /odm.

  build_layer.py OUT_DISK_IMG [--policy-check]

The stock system_a / vendor_a stay byte-identical, so an OTA can verify and apply, and the layer
applies to whichever slot boots. /system itself cannot be overlaid on a user build (fs_mgr
allowlist), so the few system tweaks are done from a vendor init script (bind mount, stop).

Inputs (env, defaults under GBOS_ROOT):
  GBOS_STOCK_IMAGE   stock recovery image (labels, original build.prop)
  GBOS_PORT_IMAGE    gbos-vm-linux security-ported image + manifest.json next to it
                     (software KeyMint, Cuttlefish composer, Venus/VirGL, boot control, ...)
  GBOS_LIBZ          baseline libz.so (vm-amd/zlib-build)
  GBOS_POINTER_BRIDGE  pointer-bridge/vm-pointer-bridge
  GBOS_FUTILITY_STUB   layer/stubs/out/futility (layer/stubs/build.sh)
"""
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GBOS_ROOT', '/mnt/dados/gbos'))
LINUX_DIR = Path(os.environ.get('GBOS_LINUX_DIR', ROOT / 'gbos-vm-linux'))
STOCK_IMAGE = Path(os.environ.get('GBOS_STOCK_IMAGE', LINUX_DIR / 'android-desktop_signed_recovery_image.bin'))
PORT_IMAGE = Path(os.environ.get('GBOS_PORT_IMAGE', LINUX_DIR / 'image-security/googlebook-security.bin'))
LIBZ = Path(os.environ.get('GBOS_LIBZ', HERE.parent / 'zlib-build/out/libz.so'))
POINTER_BRIDGE = Path(os.environ.get('GBOS_POINTER_BRIDGE', HERE.parents[1] / 'pointer-bridge/vm-pointer-bridge'))
FUTILITY_STUB = Path(os.environ.get('GBOS_FUTILITY_STUB', HERE / 'stubs/out/futility'))
SUPER_BASE = 1458176 * 512
PARTITION_NAME = 'gbos_layer'

# Files of the security port that edit a stock file instead of adding one. They are turned into
# additive files under /odm, which Android reads after /vendor: build.prop -> /odm/etc/build.prop
# overrides, vendor_file_contexts -> the added lines in /odm/etc/selinux/odm_file_contexts (device
# labels such as /dev/ttyS0 and the virtio-gpu sysfs node; the layer's own files carry xattrs).
PORT_EDITS = {'build.prop', 'etc/selinux/vendor_file_contexts'}

SYSTEM_LIBZ = 'etc/gbos/system/lib64/libz.so'

SYSTEM_TWEAKS_RC = f'''# gbos: changes to /system without touching the partition (it cannot be overlaid on a user build).
# The shipped libz is built with AVX-VNNI; every APK/JAR read traps into gfni_emu without this.
on early-init
    mount none /vendor/{SYSTEM_LIBZ} /system/lib64/libz.so bind

# ChromeOS GSC / EC daemons: no such hardware in a VM, they only crash-loop. (init.svc.* of system
# services cannot be used as triggers from /vendor, so stop them at fixed points instead.)
on boot
    stop android.system.desktop.security.gscd
    stop timberslide
on property:sys.boot_completed=1
    stop android.system.desktop.security.gscd
    stop timberslide
'''.encode()

FAKE_BATTERY_RC = (
    b'# gbos: no ChromeOS EC in a VM, so the battery reads 0% and setup refuses to continue.\n'
    b'# The fake values live in system_server; re-apply them whenever zygote comes back (and before\n'
    b'# boot_completed: the setup wizard checks the battery early). The loop waits for BatteryService.\n'
    b'service vm-fake-battery /system/bin/sh -c "for i in $$(seq 1 60); do cmd battery set ac 1 && break; sleep 5; done; '
    b'cmd battery set usb 0; cmd battery set status 2; cmd battery set level 100; svc power stayon true"\n'
    b'    class late_start\n'
    b'    user shell\n'
    b'    group shell\n'
    b'    seclabel u:r:shell:s0\n'
    b'    oneshot\n'
    b'    disabled\n\n'
    b'on property:init.svc.zygote=running\n'
    b'    start vm-fake-battery\n'
)

POINTER_BRIDGE_RC = (
    b'# gbos: QEMU usb-tablet -> uinput pointer + wheel (domain gbos_pointer_bridge, odm_sepolicy.cil).\n'
    b'service vm-pointer-bridge /vendor/bin/vm-pointer-bridge\n'
    b'    class late_start\n'
    b'    user root\n'
    b'    group root input uhid\n'
)

# Versioned names (_202604) are how vendor-side policy refers to platform types.
ODM_SEPOLICY = b''';; gbos layer policy, compiled by init together with the stock policy.
(type gbos_pointer_bridge)
(roletype object_r gbos_pointer_bridge)
(type gbos_pointer_bridge_exec)
(roletype object_r gbos_pointer_bridge_exec)
(typeattributeset domain (gbos_pointer_bridge))
(typeattributeset file_type (gbos_pointer_bridge_exec))
(typeattributeset exec_type (gbos_pointer_bridge_exec))
(typeattributeset vendor_file_type (gbos_pointer_bridge_exec))
(allow init_202604 gbos_pointer_bridge_exec (file (read getattr map execute open)))
(allow init_202604 gbos_pointer_bridge (process (transition siginh rlimitinh)))
(dontaudit init_202604 gbos_pointer_bridge (process (noatsecure)))
(typetransition init_202604 gbos_pointer_bridge_exec process gbos_pointer_bridge)
(allow gbos_pointer_bridge gbos_pointer_bridge_exec (file (read getattr map execute open entrypoint)))
(allow gbos_pointer_bridge input_device_202604 (dir (ioctl read getattr lock open watch watch_reads search)))
(allow gbos_pointer_bridge input_device_202604 (chr_file (ioctl read write getattr lock append map open)))
(allow gbos_pointer_bridge uhid_device_202604 (chr_file (ioctl read write getattr lock append map open)))
(allow gbos_pointer_bridge sysfs_202604 (dir (read open getattr search)))
(allow gbos_pointer_bridge sysfs_202604 (file (read open getattr)))
(allow gbos_pointer_bridge sysfs_202604 (lnk_file (read getattr)))
;; /sys/class/drm/*/modes for the display size; the virtio-gpu node is sysfs_gpu (odm_file_contexts).
(allow gbos_pointer_bridge sysfs_gpu_202604 (dir (read open getattr search)))
(allow gbos_pointer_bridge sysfs_gpu_202604 (file (read open getattr)))
(allow gbos_pointer_bridge sysfs_gpu_202604 (lnk_file (read getattr)))
;; SYSTEM_TWEAKS_RC: bind the baseline libz over /system/lib64/libz.so.
(allow vendor_init_202604 system_lib_file_202604 (file (mounton getattr)))
(allow init_202604 system_lib_file_202604 (file (mounton getattr)))
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


NEW_DIR = (0o755, 0, 2000, 'vendor_file')  # directories the stock vendor does not have


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


def gbos_vendor_files(stock_vendor: Path, stock_system: Path):
    files = {
        'lib64/libz.so': _entry(stock_vendor, 'lib64/libz.so', LIBZ.read_bytes()),
        SYSTEM_LIBZ: _entry(stock_system, 'system/lib64/libz.so', LIBZ.read_bytes()),
        'etc/init/gbos-system-tweaks.rc': (SYSTEM_TWEAKS_RC, 'vendor_configs_file', 0o644, 0, 0),
        'etc/init/vm-fake-battery.rc': (FAKE_BATTERY_RC, 'vendor_configs_file', 0o644, 0, 0),
        'etc/init/vm-pointer-bridge.rc': (POINTER_BRIDGE_RC, 'vendor_configs_file', 0o644, 0, 0),
        'bin/vm-pointer-bridge': (POINTER_BRIDGE.read_bytes(), 'gbos_pointer_bridge_exec', 0o755, 0, 2000),
        # OTA postinstall flashes the AP firmware through futility; there is none (stubs/futility.c).
        'bin/futility': _entry(stock_vendor, 'bin/futility', FUTILITY_STUB.read_bytes()),
    }
    return files


def odm_files(overrides: dict, contexts: list):
    prop = dict(overrides, **{'ro.dalvik.vm.native.bridge': '0'})
    return {
        'etc/build.prop': (odm_build_prop(prop).encode(), 'vendor_configs_file', 0o644, 0, 0),
        'etc/selinux/odm_file_contexts': (''.join(l + '\n' for l in contexts).encode(), 'vendor_configs_file', 0o644, 0, 0),
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
            'odm/etc/selinux': (0o755, 0, 0, 'vendor_configs_file')}
    new_dirs ={d: NEW_DIR for d in sorted(ancestor_dirs(vendor_files)) if not port._exists(stock_vendor, d)}
    for rel, meta in port.dir_entries(stock_vendor, vendor_files, new_dirs).items():
        dirs[layer_path('vendor', rel)] = meta
    return dirs


def build_erofs(dirs, files, out: Path, tmp: Path):
    _, _, port = _helpers()
    tar = tmp / 'layer.tar'
    port.overlay_tar(tar, dirs, files)
    subprocess.run(['mkfs.erofs', '--tar=f', '-x1', str(out), str(tar)], check=True, capture_output=True)
    subprocess.run(['fsck.erofs', str(out)], check=True, capture_output=True)


def build_disk(erofs: Path, disk: Path):
    sectors = (erofs.stat().st_size + 511) // 512
    part_sectors = max(sectors * 2, 65536)
    disk.unlink(missing_ok=True)
    with disk.open('wb') as f:
        f.truncate((2048 + part_sectors + 2048) * 512)
    subprocess.run(['sgdisk', '-o', '-n', f'1:2048:{2048 + part_sectors - 1}', '-c', f'1:{PARTITION_NAME}', str(disk)],
                   check=True, capture_output=True)
    with disk.open('r+b') as f:
        f.seek(2048 * 512)
        f.write(erofs.read_bytes())


def main():
    out = Path(sys.argv[1])
    for need in (STOCK_IMAGE, PORT_IMAGE, LIBZ, POINTER_BRIDGE, FUTILITY_STUB):
        if not need.exists():
            raise SystemExit(f'missing {need}')
    tmp = Path(tempfile.mkdtemp(prefix='gbos-layer-', dir=out.parent))
    try:
        stock_vendor = extract_partition(STOCK_IMAGE, 'vendor_a', tmp / 'stock_vendor.erofs')
        stock_system = extract_partition(STOCK_IMAGE, 'system_a', tmp / 'stock_system.erofs')
        port_vendor = extract_partition(PORT_IMAGE, 'vendor_a', tmp / 'port_vendor.erofs')
        pfiles, overrides, contexts = port_files(port_vendor, stock_vendor)
        vendor = {**pfiles, **gbos_vendor_files(stock_vendor, stock_system)}
        files = {layer_path('vendor', k): v for k, v in vendor.items()}
        files.update({layer_path('odm', k): v for k, v in odm_files(overrides, contexts).items()})
        build_erofs(layer_dirs(vendor, stock_vendor, stock_system), files, tmp / 'layer.erofs', tmp)
        build_disk(tmp / 'layer.erofs', out)
        print(f'layer: {len(files)} files, {len(overrides)} prop overrides, {len(contexts)} file contexts -> {out}')
    finally:
        shutil.rmtree(tmp)


if __name__ == '__main__':
    main()
