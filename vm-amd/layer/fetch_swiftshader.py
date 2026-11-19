#!/usr/bin/env python3
"""Extract SwiftShader's Vulkan driver (vulkan.pastel.so) from a Cuttlefish image zip.

  fetch_swiftshader.py CUTTLEFISH_IMG_ZIP OUT.so

It ships in Cuttlefish's com.google.cf.vulkan APEX (vendor_a, inside super.img). The layer installs it
as /vendor/lib64/hw/vulkan.pastel.so for GPUs without drivers in the image (gpu_amd/swiftshader.prop).
Default zip: gbos-vm-linux's aosp_cf_x86_64_only_phone-img-*.zip (fetch_cuttlefish_donor.sh).
"""
import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

LINUX_DIR = Path('/mnt/dados/gbos/gbos-vm-linux')
APEX = '/apex/com.google.cf.vulkan.apex'
LIB = '/lib64/hw/vulkan.pastel.so'


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    sys.path.insert(0, str(LINUX_DIR))
    import unpack_cuttlefish_donor as donor
    with tempfile.TemporaryDirectory(dir=out.parent) as td:
        td = Path(td)
        zipfile.ZipFile(src).extract('super.img', td)
        donor.unsparse(td / 'super.img', td / 'super.raw')
        (td / 'super.img').unlink()
        offset = donor.super_partitions(td / 'super.raw')['vendor_a']
        apex = subprocess.check_output(['dump.erofs', f'--offset={offset}', f'--path={APEX}', '--cat',
                                        str(td / 'super.raw')])
        (td / 'payload.img').write_bytes(zipfile.ZipFile(io.BytesIO(apex)).read('apex_payload.img'))
        subprocess.run(['debugfs', '-R', f'dump {LIB} {out}', str(td / 'payload.img')], check=True,
                       capture_output=True)
    if out.read_bytes()[:4] != b'\x7fELF':
        raise SystemExit(f'{out}: not an ELF')
    print(f'{out}: {out.stat().st_size >> 20} MiB')


if __name__ == '__main__':
    main()
