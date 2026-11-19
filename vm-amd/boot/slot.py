#!/usr/bin/env python3
"""A/B slot selection for the VM, done the way a bootloader would.

update_engine and the software boot-control HAL keep `struct bootloader_control`
(hardware/interfaces/boot/1.1/default/boot_control) in the misc partition. QEMU boots the
kernel directly, so the launcher plays bootloader: pick the bootable slot with the highest
priority, spend one try if it has not booted successfully yet, and pass its suffix and vbmeta
to the kernel command line.

  slot.py boot-args DISK.qcow2 RAW_BASE_IMAGE      prints SLOT=.. VBMETA_SIZE=.. VBMETA_DIGEST=..
"""
import hashlib
import re
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

MAGIC = 0x42414342  # "BCAB"
CONTROL_OFFSET = 2048  # offsetof(bootloader_message_ab, slot_suffix)
CONTROL_SIZE = 32
SLOT_INFO = 12
CRC_OFFSET = 28
AVB_HEADER = 256


@dataclass(frozen=True)
class BootControl:
    nb_slot: int
    slots: tuple  # (priority, tries_remaining, successful_boot) per slot

    @staticmethod
    def suffix(index):
        return '_' + 'abcd'[index]


def parse_control(raw: bytes):
    if len(raw) < CONTROL_SIZE or struct.unpack_from('<I', raw, 4)[0] != MAGIC:
        return None
    if zlib.crc32(raw[:CRC_OFFSET]) != struct.unpack_from('<I', raw, CRC_OFFSET)[0]:
        return None
    nb_slot = raw[9] & 0x7
    slots = tuple((b & 0xF, (b >> 4) & 0x7, bool(b >> 7))
                  for b in (raw[SLOT_INFO + 2 * i] for i in range(nb_slot)))
    return BootControl(nb_slot, slots)


def serialize_control(ctrl: BootControl, original: bytes) -> bytes:
    raw = bytearray(original[:CONTROL_SIZE])
    for i, (priority, tries, ok) in enumerate(ctrl.slots):
        raw[SLOT_INFO + 2 * i] = (priority & 0xF) | ((tries & 0x7) << 4) | (int(ok) << 7)
    struct.pack_into('<I', raw, CRC_OFFSET, zlib.crc32(bytes(raw[:CRC_OFFSET])))
    return bytes(raw)


def choose_slot(ctrl):
    """(slot index, updated control or None when nothing needs to be written)."""
    if ctrl is None:
        return 0, None
    bootable = [i for i, (priority, tries, ok) in enumerate(ctrl.slots) if priority and (ok or tries)]
    if not bootable:
        return 0, None
    best = max(bootable, key=lambda i: ctrl.slots[i][0])
    priority, tries, ok = ctrl.slots[best]
    if ok:
        return best, None
    slots = list(ctrl.slots)
    slots[best] = (priority, tries - 1, ok)
    return best, replace(ctrl, slots=tuple(slots))


def vbmeta_size(header: bytes) -> int:
    if header[:4] != b'AVB0':
        raise ValueError('not a vbmeta image')
    auth, aux = struct.unpack_from('>QQ', header, 12)
    return AVB_HEADER + auth + aux


HEX_LINE = re.compile(r'^[0-9a-f]{8,}:\s+((?:[0-9a-f]{2}\s){1,16})', re.M)


def parse_qemu_io_hexdump(text: str) -> bytes:
    return b''.join(bytes.fromhex(m.group(1)) for m in HEX_LINE.finditer(text))


# ---- disk access through qemu-io (the guest's writes live in the qcow2 overlay) ------------

def read_disk(disk, offset, length):
    out = subprocess.run(['qemu-io', '-r', '-f', 'qcow2', '-c', f'read -v {offset} {length}', str(disk)],
                         capture_output=True, text=True, check=True).stdout
    data = parse_qemu_io_hexdump(out)
    if len(data) != length:
        raise RuntimeError(f'short read at {offset}: {len(data)}/{length}')
    return data


def write_disk(disk, offset, data):
    with tempfile.NamedTemporaryFile() as f:
        f.write(data)
        f.flush()
        subprocess.run(['qemu-io', '-f', 'qcow2', '-c', f'write -s {f.name} {offset} {len(data)}', str(disk)],
                       capture_output=True, check=True)


def partition_offsets(raw_image):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from gbos.gpt import partitions
    return {p['name']: p['start'] * 512 for p in partitions(raw_image)}


def boot_args(disk, raw_image):
    parts = partition_offsets(raw_image)
    misc = parts['misc'] + CONTROL_OFFSET
    raw = read_disk(disk, misc, CONTROL_SIZE)
    index, updated = choose_slot(parse_control(raw))
    if updated:
        write_disk(disk, misc, serialize_control(updated, raw))
    suffix = BootControl.suffix(index)
    vbmeta = parts['vbmeta' + suffix]
    size = vbmeta_size(read_disk(disk, vbmeta, AVB_HEADER))
    digest = hashlib.sha256(read_disk(disk, vbmeta, size)).hexdigest()
    return {'SLOT': suffix, 'VBMETA_SIZE': size, 'VBMETA_DIGEST': digest}


def main():
    if sys.argv[1:2] != ['boot-args'] or len(sys.argv) != 4:
        raise SystemExit(__doc__)
    for key, value in boot_args(sys.argv[2], sys.argv[3]).items():
        print(f'{key}={value}')


if __name__ == '__main__':
    main()
