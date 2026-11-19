#!/usr/bin/env python3
"""Stage the host's linux-firmware tree, uncompressed, for the GKI kernel.

  collect_firmware.py FW_ROOT OUT_DIR [MODULES_DIR]

The GKI config has no CONFIG_FW_LOADER_COMPRESS, so Fedora's *.xz / *.zst files are decompressed.
Symlinks (linux-firmware's WHENCE links) are kept, pointing at the decompressed names. With
MODULES_DIR, every firmware name the built modules declare (modinfo -F firmware) is checked and
the ones the host does not have are listed in OUT_DIR/MISSING.txt.
"""
import fnmatch
import lzma
import os
import shutil
import subprocess
import sys
from pathlib import Path

DECOMPRESSORS = {'.xz': lzma.open}
try:
    from compression import zstd
    DECOMPRESSORS['.zst'] = zstd.open
except ImportError:
    pass


def strip_compression(name: str) -> str:
    for suffix in DECOMPRESSORS:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def find_missing(declared: set, available: set) -> set:
    def present(name):
        if any(c in name for c in '*?['):
            return any(fnmatch.fnmatch(a, name) for a in available)
        return name in available
    return {n for n in declared if not present(n)}


def copy_file(src: Path, dst: Path):
    opener = DECOMPRESSORS.get(src.suffix)
    if not opener:
        shutil.copy2(src, dst)
        return
    with opener(src, 'rb') as f, dst.open('wb') as g:
        shutil.copyfileobj(f, g)


def stage(fw_root: Path, out: Path) -> set:
    staged = set()
    for src in sorted(fw_root.rglob('*')):
        if src.is_dir() and not src.is_symlink():
            continue
        rel = strip_compression(str(src.relative_to(fw_root)))
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if src.is_symlink():
            os.symlink(strip_compression(os.readlink(src)), dst)
        else:
            copy_file(src, dst)
        staged.add(rel)
    return staged


def declared_firmware(modules_dir: Path) -> set:
    names = set()
    for ko in modules_dir.rglob('*.ko'):
        out = subprocess.run(['modinfo', '-F', 'firmware', str(ko)], capture_output=True, text=True).stdout
        names.update(l.strip() for l in out.splitlines() if l.strip())
    return names


def main():
    fw_root, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    staged = stage(fw_root, out)
    print(f'staged {len(staged)} firmware entries -> {out}')
    if len(sys.argv) > 3:
        missing = sorted(find_missing(declared_firmware(Path(sys.argv[3])), staged))
        (out / 'MISSING.txt').write_text('\n'.join(missing) + '\n')
        print(f'{len(missing)} declared firmware names not on the host (see MISSING.txt)')


if __name__ == '__main__':
    main()
