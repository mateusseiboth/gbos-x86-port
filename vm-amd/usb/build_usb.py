#!/usr/bin/env python3
"""Build a bootable USB (or any disk) image of Googlebook OS for a real PC.

  build_usb.py OUT.img --size BYTES

Layout: a new EFI system partition first (BOOTX64.EFI = Fedora's kernel + a stage-1 initramfs with
gbos-boot, our kernel and the Android ramdisk), then every stock partition except userdata, packed in
their original order (names, type and unique GUIDs, attributes kept: Android finds them by name), the
overlay layer as "gbos_layer", and userdata filling the rest (created empty; Android formats it).

Boot: firmware -> BOOTX64.EFI -> gbos-boot (A/B slot from misc, vbmeta) -> kexec our kernel.
Secure Boot must be off (nothing here is signed).

Inputs (env, defaults under GBOS_ROOT): GBOS_STOCK_IMAGE (vm/stock/googlebook.bin), GBOS_LAYER
(vm/layer.img), GBOS_KERNEL (kernel/out-full bzImage), GBOS_INITRD (vm/initrd), GBOS_STAGE1_KERNEL
(/boot/vmlinuz-$(uname -r)) and its modules (/lib/modules/<release>).
"""
import argparse
import os
import shutil
import struct
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GBOS_ROOT', '/mnt/dados/gbos'))
SECTOR = 512
MIB = 2048  # sectors
ESP_TYPE = 'C12A7328-F81F-11D2-BA4B-00A0C93EC93B'
LINUX_DATA_TYPE = '0FC63DAF-8483-4772-8E79-3D69D8477DE4'
ESP_SECTORS = 256 * MIB
MIN_USERDATA_SECTORS = 4 * 1024 * MIB  # 4 GiB
BACKUP_GPT_SECTORS = 33


@dataclass(frozen=True)
class Part:
    name: str
    type: str
    guid: str
    attrs: int
    start: int
    sectors: int
    source: object = None  # stock start sector, 'esp', 'layer', or None (left empty)


# ---- planning ----------------------------------------------------------------------------

def _align(sector):
    return (sector + MIB - 1) // MIB * MIB


def layout(stock, esp_sectors, layer_sectors, disk_sectors):
    """New partition table: ESP, stock partitions (no userdata) in disk order, layer, userdata."""
    userdata = next(p for p in stock if p.name == 'userdata')
    others = sorted((p for p in stock if p.name != 'userdata'), key=lambda p: p.start)
    out, cursor = [], MIB
    esp = Part('GBOS-EFI', ESP_TYPE, str(uuid.uuid4()).upper(), 0, cursor, esp_sectors, 'esp')
    for p in [esp] + [replace(p, source=p.start) for p in others] + [
            Part('gbos_layer', LINUX_DATA_TYPE, str(uuid.uuid4()).upper(), 0, 0, layer_sectors, 'layer')]:
        cursor = _align(cursor)
        out.append(replace(p, start=cursor))
        cursor += p.sectors
    start = _align(cursor)
    end = (disk_sectors - BACKUP_GPT_SECTORS) // MIB * MIB
    if end - start < MIN_USERDATA_SECTORS:
        raise ValueError(f'disk too small: userdata would get {(end - start) * SECTOR >> 20} MiB')
    return out + [replace(userdata, start=start, sectors=end - start, source=None)]


def android_cmdline(shared):
    """boot/cmdline.txt (comments and line breaks dropped) with the screen as console."""
    words = [w for line in shared.splitlines() for w in line.split('#', 1)[0].split()]
    return ' '.join(['console=ttyS0,115200', 'console=tty0'] + words)


# ---- reading the stock image -------------------------------------------------------------

def read_gpt(image):
    with open(image, 'rb') as f:
        f.seek(SECTOR)
        h = f.read(92)
        if h[:8] != b'EFI PART':
            raise ValueError(f'{image}: not a GPT disk')
        lba, count, size = struct.unpack_from('<QII', h, 72)
        f.seek(lba * SECTOR)
        table = f.read(count * size)
    parts = []
    for i in range(count):
        e = table[i * size:(i + 1) * size]
        if e[:16] == b'\0' * 16:
            continue
        first, last, attrs = struct.unpack_from('<QQQ', e, 32)
        parts.append(Part(name=e[56:128].decode('utf-16le').rstrip('\0'),
                          type=str(uuid.UUID(bytes_le=e[:16])).upper(),
                          guid=str(uuid.UUID(bytes_le=e[16:32])).upper(),
                          attrs=attrs, start=first, sectors=last - first + 1))
    return parts


# ---- stage 1 (BOOTX64.EFI) ---------------------------------------------------------------

def build_gbos_boot(out):
    subprocess.run(['cc', '-O2', '-static', '-Wall', '-Wextra', '-Werror', '-o', str(out),
                    str(HERE / 'gbos_boot.c')], check=True)


def build_stage1_initramfs(out, kernel, initrd, cmdline, modules_dir, tmp):
    root = tmp / 'stage1'
    for d in ('proc', 'sys', 'dev', 'gbos', 'modules'):
        (root / d).mkdir(parents=True, exist_ok=True)
    build_gbos_boot(root / 'init')
    shutil.copy(kernel, root / 'gbos/kernel')
    shutil.copy(initrd, root / 'gbos/initrd')
    (root / 'gbos/cmdline').write_text(cmdline + '\n')
    for name in ('usb-storage', 'uas'):
        src = Path(modules_dir) / f'kernel/drivers/usb/storage/{name}.ko.xz'
        with open(root / f'modules/{name}.ko', 'wb') as f:
            subprocess.run(['xz', '-dc', str(src)], stdout=f, check=True)
    files = subprocess.run(['find', '.'], cwd=root, capture_output=True, text=True, check=True).stdout
    with open(out, 'wb') as f:
        subprocess.run(['cpio', '-o', '-H', 'newc', '--quiet'], cwd=root, input=files.encode(), stdout=f,
                       check=True)


def build_uki(out, stage1_kernel, initramfs):
    # rdinit=/init: gbos-boot; quiet keeps the screen for its messages.
    subprocess.run(['ukify', 'build', f'--linux={stage1_kernel}', f'--initrd={initramfs}',
                    '--cmdline=rdinit=/init quiet console=ttyS0,115200 console=tty0', f'--output={out}'],
                   check=True, capture_output=True)


def build_esp(out, uki):
    out.unlink(missing_ok=True)
    subprocess.run(['mkfs.fat', '-F', '32', '-n', 'GBOS-EFI', '-C', str(out), str(ESP_SECTORS // 2)],
                   check=True, capture_output=True)
    subprocess.run(['mmd', '-i', str(out), '::/EFI', '::/EFI/BOOT'], check=True)
    subprocess.run(['mcopy', '-i', str(out), str(uki), '::/EFI/BOOT/BOOTX64.EFI'], check=True)


# ---- writing the disk --------------------------------------------------------------------

def _copy(dst, dst_off, src_path, src_off, length):
    """Copy a byte range, leaving all-zero chunks as holes (the output file is sparse)."""
    chunk = 8 << 20
    with open(src_path, 'rb') as s:
        s.seek(src_off)
        done = 0
        while done < length:
            data = s.read(min(chunk, length - done))
            if not data:
                break
            if data.count(0) != len(data):
                dst.seek(dst_off + done)
                dst.write(data)
            done += len(data)


def layer_partition(layer_disk):
    p = next(p for p in read_gpt(layer_disk) if p.name == 'gbos_layer')
    return p.start * SECTOR, p.sectors


def write_image(out, parts, disk_sectors, stock_image, esp_img, layer_disk):
    out.unlink(missing_ok=True)
    with open(out, 'wb') as f:
        f.truncate(disk_sectors * SECTOR)
    args = ['sgdisk', '-o', '-a', '1']
    for i, p in enumerate(parts, 1):
        args += ['-n', f'{i}:{p.start}:{p.start + p.sectors - 1}', '-t', f'{i}:{p.type}',
                 '-u', f'{i}:{p.guid}', '-c', f'{i}:{p.name}']
        args += [f'--attributes={i}:=:{p.attrs:016x}'] if p.attrs else []
    subprocess.run(args + [str(out)], check=True, capture_output=True)
    layer_off, _ = layer_partition(layer_disk)
    with open(out, 'r+b') as f:
        for p in parts:
            src = {'esp': (esp_img, 0), 'layer': (layer_disk, layer_off)}.get(p.source,
                                                                              (stock_image, (p.source or 0) * SECTOR))
            if p.source is not None:
                _copy(f, p.start * SECTOR, src[0], src[1], p.sectors * SECTOR)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out', type=Path)
    ap.add_argument('--size', type=int, required=True, help='disk size in bytes (e.g. the stick size)')
    a = ap.parse_args()
    release = os.uname().release
    stock = Path(os.environ.get('GBOS_STOCK_IMAGE', ROOT / 'vm/stock/googlebook.bin'))
    layer = Path(os.environ.get('GBOS_LAYER', ROOT / 'vm/layer.img'))
    kernel = Path(os.environ.get('GBOS_KERNEL', ROOT / 'kernel/out-full/arch/x86/boot/bzImage'))
    initrd = Path(os.environ.get('GBOS_INITRD', ROOT / 'vm/initrd'))
    stage1_kernel = Path(os.environ.get('GBOS_STAGE1_KERNEL', f'/boot/vmlinuz-{release}'))
    modules = Path(os.environ.get('GBOS_STAGE1_MODULES', f'/lib/modules/{release}'))
    for need in (stock, layer, kernel, initrd, stage1_kernel, modules):
        if not need.exists():
            raise SystemExit(f'missing {need}')

    disk_sectors = a.size // SECTOR
    _, layer_sectors = layer_partition(layer)
    parts = layout(read_gpt(stock), ESP_SECTORS, layer_sectors, disk_sectors)
    # GBOS_CMDLINE_EXTRA: debugging aid, appended (e.g. loglevel=7 drm.debug=0x2).
    cmdline = ' '.join(filter(None, [android_cmdline((HERE.parent / 'boot/cmdline.txt').read_text()),
                                     os.environ.get('GBOS_CMDLINE_EXTRA', '')]))
    tmp = Path(tempfile.mkdtemp(prefix='gbos-usb-', dir=a.out.parent))
    try:
        build_stage1_initramfs(tmp / 'stage1.cpio', kernel, initrd, cmdline, modules, tmp)
        build_uki(tmp / 'BOOTX64.EFI', stage1_kernel, tmp / 'stage1.cpio')
        build_esp(tmp / 'esp.img', tmp / 'BOOTX64.EFI')
        write_image(a.out, parts, disk_sectors, stock, tmp / 'esp.img', layer)
    finally:
        shutil.rmtree(tmp)
    data = parts[-1]
    print(f'{a.out}: {len(parts)} partitions, userdata {data.sectors * SECTOR >> 30} GiB')


if __name__ == '__main__':
    main()
