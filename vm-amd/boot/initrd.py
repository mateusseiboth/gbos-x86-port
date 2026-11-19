#!/usr/bin/env python3
"""Add the gbos layer to the VM ramdisk's first-stage fstab.

  initrd.py SRC_INITRD OUT_INITRD      (SRC: gbos-vm-linux/initrd, built by prepare.sh)

The stock entries keep avb=vbmeta: system and vendor stay byte-identical, the layer goes on top.
The layer partition is mounted under /mnt/vendor (a first-stage tmpfs that switch_root moves
into the new root); a mount point that does not exist in /system would make switch_root abort.
"""
import os
import subprocess
import sys

FSTAB = 'first_stage_ramdisk/fstab.android-desktop'
LAYER_ENTRIES = (
    '/dev/block/by-name/gbos_layer /mnt/vendor/gbos erofs ro wait,first_stage_mount\n'
    'overlay /vendor overlay ro,lowerdir=/mnt/vendor/gbos/vendor:/vendor first_stage_mount\n'
    'overlay /odm overlay ro,lowerdir=/mnt/vendor/gbos/odm:/odm first_stage_mount\n'
)


def add_layer_entries(fstab: str) -> str:
    if LAYER_ENTRIES in fstab:
        return fstab
    return fstab + ('' if fstab.endswith('\n') else '\n') + LAYER_ENTRIES


def main():
    src, out = sys.argv[1:3]
    sys.path.insert(0, os.environ.get('GBOS_LINUX_DIR', '/mnt/dados/gbos/gbos-vm-linux'))
    import cpio_tools
    cpio = subprocess.run(['lz4', '-d', '-c', src], check=True, capture_output=True).stdout
    entries = cpio_tools.read(cpio)
    mode, data = entries[FSTAB]
    entries[FSTAB] = (mode, add_layer_entries(data.decode()).encode())
    blob = subprocess.run(['lz4', '-l', '-c'], input=cpio_tools.write(entries), check=True, capture_output=True).stdout
    with open(out, 'wb') as f:
        f.write(blob)
    print(f'{out}: layer entries added to {FSTAB}')


if __name__ == '__main__':
    main()
