#!/usr/bin/env python3
"""Minimal AMD (no GFNI / AVX-VNNI) patch for the security-ported lapis image.

Reproducible subset of gbos-x86-port/patches/apply_{system,vendor}_update.py, without the
author's hand-patched binaries:
  * system_a + vendor_a: libz.so rebuilt for baseline x86-64 (no AVX-VNNI Adler-32 loop);
  * system_a: ro.dalvik.vm.native.bridge=0 (libhoudini makes zygote abort silently);
  * vendor_a: vm-pointer-bridge (usb-tablet -> clickable pointer + wheel);
  * vendor_a: fake AC / 100% battery after boot (no ChromeOS EC in a VM, so the setup
    wizard would refuse to continue at 0%).

  * system_a: gscd / timberslide disabled (ChromeOS GSC and EC daemons, crash-loop in a VM);
  * initrd: first-stage fstab mounts system without dm-verity (system_a is rewritten in place).

Run only while the VM is stopped: it rewrites the image in place.
  patch_partitions.py [all|partitions|system|vendor|initrd]     (default: all)
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GBOS_ROOT', '/mnt/dados/gbos'))
LINUX_DIR = Path(os.environ.get('GBOS_LINUX_DIR', ROOT / 'gbos-vm-linux'))
PORT_PATCHES = Path(os.environ.get('GBOS_PORT_PATCHES', HERE.parents[1] / 'patches'))
LIBZ = Path(os.environ.get('GBOS_LIBZ', HERE.parent / 'zlib-build/out/libz.so'))
POINTER_BRIDGE = Path(os.environ.get('GBOS_POINTER_BRIDGE', HERE.parents[1] / 'pointer-bridge/vm-pointer-bridge'))
IMAGE = LINUX_DIR / 'image-security/googlebook-security.bin'
SUPER_BASE = 1458176 * 512
SUPER_END = 26624000 * 512
NATIVE_BRIDGE_KEY = 'ro.dalvik.vm.native.bridge='
# ChromeOS-only daemons: gscd needs the GSC/TPM, timberslide needs /dev/cros_ec. Both crash-loop in a VM.
UNUSED_SYSTEM_SERVICES = ['system/etc/init/gscd.rc', 'system/etc/init/timberslide.rc']

FAKE_BATTERY_RC = (
    b'# VM: fake a charged, plugged-in battery and keep the screen awake\n'
    b'service vm-fake-battery /system/bin/sh -c "for i in $$(seq 1 60); do cmd battery set ac 1 && break; sleep 5; done; '
    b'cmd battery set usb 0; cmd battery set status 2; cmd battery set level 100; svc power stayon true"\n'
    b'    class late_start\n'
    b'    user shell\n'
    b'    group shell\n'
    b'    seclabel u:r:shell:s0\n'
    b'    oneshot\n'
    b'    disabled\n\n'
    b'on property:sys.boot_completed=1 && property:init.svc.zygote=running\n'
    b'    start vm-fake-battery\n'
)


# QEMU usb-tablet -> uinput pointer + wheel (pointer-bridge/). The Wacom tablet alone is treated as a
# stylus, so clicks are unreliable and there is no wheel.
POINTER_BRIDGE_RC = (
    b'# VM: usb-tablet -> uinput (pointer + wheel); the helper waits for the tablet itself\n'
    b'service vm-pointer-bridge /vendor/bin/vm-pointer-bridge\n'
    b'    class late_start\n'
    b'    user root\n'
    b'    group root input uhid\n'
    b'    seclabel u:r:shell:s0\n'
)


def disable_native_bridge(prop: str) -> str:
    lines = [l for l in prop.splitlines() if not l.startswith(NATIVE_BRIDGE_KEY)]
    kept_position = next((i for i, l in enumerate(prop.splitlines()) if l.startswith(NATIVE_BRIDGE_KEY)), len(lines))
    lines.insert(kept_position, NATIVE_BRIDGE_KEY + '0')
    return '\n'.join(lines) + '\n'


DISABLED_LINE = '    disabled\n'


def disable_service(rc: str) -> str:
    lines = rc.splitlines(keepends=True)
    out = []
    for i, line in enumerate(lines):
        out.append(line)
        already = i + 1 < len(lines) and lines[i + 1] == DISABLED_LINE
        if line.startswith('service ') and not already:
            out.append(DISABLED_LINE)
    return ''.join(out)


def drop_avb(fstab: str, mounts: set) -> str:
    def strip(line):
        fields = line.split()
        if not fields or fields[0] not in mounts:
            return line
        return line.replace(',avb=vbmeta', '')
    return ''.join(strip(l) for l in fstab.splitlines(keepends=True))


def load_helpers():
    sys.path[:0] = [str(LINUX_DIR), str(PORT_PATCHES)]
    os.environ.setdefault('GBOS_LINUX_DIR', str(LINUX_DIR))
    import lp_tools, erofs_metadata, make_lapis_security_port as port, apply_system_update
    return lp_tools, erofs_metadata, port, apply_system_update


def replace_file(files, fs, rel, data, meta_of, label_of):
    meta = meta_of(fs, '/' + rel)
    files[rel] = (data, label_of(meta), meta['mode'] & 0o7777, meta['uid'], meta['gid'])


def build_system_files(fs, h):
    _, erofs_metadata, port, _ = h
    files = {}
    replace_file(files, fs, 'system/lib64/libz.so', LIBZ.read_bytes(), erofs_metadata.metadata, port._label)
    prop = port.dump_cat(fs, '/system/build.prop').decode()
    replace_file(files, fs, 'system/build.prop', disable_native_bridge(prop).encode(), erofs_metadata.metadata, port._label)
    for rel in UNUSED_SYSTEM_SERVICES:
        rc = disable_service(port.dump_cat(fs, '/' + rel).decode())
        replace_file(files, fs, rel, rc.encode(), erofs_metadata.metadata, port._label)
    return files


def build_vendor_files(fs, h):
    _, erofs_metadata, port, _ = h
    files = {
        'etc/init/vm-fake-battery.rc': (FAKE_BATTERY_RC, 'vendor_configs_file', 0o644, 0, 0),
        'etc/init/vm-pointer-bridge.rc': (POINTER_BRIDGE_RC, 'vendor_configs_file', 0o644, 0, 0),
        'bin/vm-pointer-bridge': (POINTER_BRIDGE.read_bytes(), 'vendor_file', 0o755, 0, 0),
    }
    if not port._exists(fs, 'lib64/libz.so'):
        return files
    replace_file(files, fs, 'lib64/libz.so', LIBZ.read_bytes(), erofs_metadata.metadata, port._label)
    return files


PARTITIONS = {
    'system_a': {'files': build_system_files, 'fix_root': lambda h: h[3].restore_system_root_xattr},
    'vendor_a': {'files': build_vendor_files, 'fix_root': lambda h: h[2].restore_root_xattr},
}


def read_partition(image, offset, port, out):
    with image.open('rb') as f:
        f.seek(offset + 1024)
        sb = f.read(128)
        fs_size = int.from_bytes(sb[36:40], 'little') << sb[12]
        f.seek(offset)
        with out.open('wb') as g:
            remaining = fs_size
            while remaining:
                chunk = f.read(min(remaining, 1 << 22))
                g.write(chunk)
                remaining -= len(chunk)
    return int.from_bytes(sb[24:32], 'little'), port.compress_option(sb)


def rebuild_erofs(src_fs, files, build_time, zopt, port, fix_root, tmp):
    tar = tmp / 'overlay.tar'
    port.overlay_tar(tar, port.dir_entries(src_fs, files, {}), files)
    new_fs = tmp / 'new.erofs'
    shutil.copyfile(src_fs, new_fs)
    cmd = ['mkfs.erofs', '--incremental=data', '--tar=f', '-x0', f'-b{port.BLOCK}', f'-T{build_time}']
    cmd += [zopt] if zopt else []
    subprocess.run(cmd + [str(new_fs), str(tar)], check=True)
    fix_root(new_fs, src_fs)
    subprocess.run(['fsck.erofs', str(new_fs)], check=True)
    return new_fs


def find_free_extent(sup, name, length):
    used = sorted((s, s + sz) for p in sup.names() if p != name for s, sz in sup.layout(p))
    align = 1 << 20
    cur = SUPER_BASE + align
    for s, e in used:
        aligned = (cur + align - 1) // align * align
        if s > aligned and s - aligned >= length:
            return aligned
        cur = max(cur, e)
    aligned = (cur + align - 1) // align * align
    if aligned + length > SUPER_END:
        raise SystemExit(f'not enough free space in super to relocate {name}')
    return aligned


def write_back(image, sup, name, offset, size, new_fs):
    data = new_fs.read_bytes()
    fits = len(data) <= size
    length = size if fits else (len(data) + 511) // 512 * 512
    start = offset if fits else find_free_extent(sup, name, length)
    with image.open('r+b') as f:
        f.seek(start)
        f.write(data)
        f.write(bytes(length - len(data)))
    if not fits:
        sup.set_single_extent(name, length // 512, (start - SUPER_BASE) // 512)
    print(f'{name}: {len(data)} bytes at {start} ({"in place" if fits else "relocated"})')


def patch_partition(name, strategy, h):
    lp_tools, _, port, _ = h
    sup = lp_tools.Super(IMAGE, SUPER_BASE)
    offset, size = sup.layout(name)[0]
    tmp = Path(tempfile.mkdtemp(prefix=f'gbos-{name}-', dir=IMAGE.parent))
    try:
        src_fs = tmp / 'src.erofs'
        build_time, zopt = read_partition(IMAGE, offset, port, src_fs)
        files = strategy['files'](src_fs, h)
        new_fs = rebuild_erofs(src_fs, files, build_time, zopt, port, strategy['fix_root'](h), tmp)
        write_back(IMAGE, sup, name, offset, size, new_fs)
    finally:
        shutil.rmtree(tmp)


def patch_partitions(names=tuple(PARTITIONS)):
    if not LIBZ.exists():
        raise SystemExit(f'missing {LIBZ}: build it with vm-amd/zlib-build/build.sh')
    if not POINTER_BRIDGE.exists():
        raise SystemExit(f'missing {POINTER_BRIDGE}: build it with pointer-bridge/build.sh')
    h = load_helpers()
    for name in names:
        patch_partition(name, PARTITIONS[name], h)


def patch_initrd():
    """system_a is rewritten in place (its hashtree is gone), so first-stage mount must skip verity for it."""
    _, _, port, _ = load_helpers()
    import cpio_tools
    initrd = IMAGE.parent / 'initrd'
    cpio = subprocess.run(['lz4', '-d', '-c', str(initrd)], check=True, capture_output=True).stdout
    entries = cpio_tools.read(cpio)
    mode, data = entries[port.FSTAB]
    entries[port.FSTAB] = (mode, drop_avb(data.decode(), UNVERIFIED_MOUNTS).encode())
    blob = subprocess.run(['lz4', '-l', '-c'], input=cpio_tools.write(entries), check=True, capture_output=True).stdout
    initrd.write_bytes(blob)
    print(f'initrd: avb removed for {sorted(UNVERIFIED_MOUNTS)}')


UNVERIFIED_MOUNTS = {'system'}
STEPS = {
    'partitions': [patch_partitions],
    'system': [lambda: patch_partitions(('system_a',))],
    'vendor': [lambda: patch_partitions(('vendor_a',))],
    'initrd': [patch_initrd],
    'all': [patch_partitions, patch_initrd],
}


def main():
    step = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if step not in STEPS:
        raise SystemExit(f'usage: patch_partitions.py [{"|".join(STEPS)}]')
    for run in STEPS[step]:
        run()


if __name__ == '__main__':
    main()
