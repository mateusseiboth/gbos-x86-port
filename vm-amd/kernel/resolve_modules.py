#!/usr/bin/env python3
"""Pick one module per name when our Fedora-driver build and Google's shipped modules overlap.

  resolve_modules.py OUR_MODULES_DIR GOOGLE_KO_DIR [GOOGLE_KO_DIR ...] --symvers MODULE_SYMVERS --out LIST

Only one module of a given name can be loaded, and each side's consumers were linked against its
own providers (Google builds DRM helpers, xe, ptp, ... from its own trees). Start from Google's
module for every shared name, switch a name to ours when that satisfies a consumer of ours without
breaking any Google module, and drop the modules of ours that no choice can satisfy (dependents
follow). Google's modules are never dropped. Writes "<side> <name>" lines plus "dropped <name>".
"""
import argparse
import os
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Module:
    name: str
    side: str  # 'google' or 'ours'
    exports: dict = field(default_factory=dict)  # symbol -> crc
    needs: dict = field(default_factory=dict)  # symbol -> crc


def _other(side):
    return 'ours' if side == 'google' else 'google'


def _broken(cands, chosen, dropped, kernel):
    """{name: unsatisfied needs} for the chosen modules that cannot load.

    A module loads when every need is exported, with the same CRC, by the kernel or by a module
    that loads itself, so a module that cannot load exports nothing (fixpoint).
    """
    active = {n: cands[n][side] for n, side in chosen.items() if n not in dropped}
    exports, loaded, changed = dict(kernel), set(), True
    while changed:
        changed = False
        for name, m in active.items():
            if name not in loaded and all(exports.get(s) == c for s, c in m.needs.items()):
                loaded.add(name)
                exports.update(m.exports)
                changed = True
    return {n: {s: c for s, c in m.needs.items() if exports.get(s) != c}
            for n, m in active.items() if n not in loaded}


def _google_broken(broken, chosen):
    return {n for n in broken if chosen[n] == 'google'}


def _improves(name, cands, chosen, dropped, kernel, broken):
    trial = dict(chosen, **{name: _other(chosen[name])})
    after = _broken(cands, trial, dropped, kernel)
    return _google_broken(after, trial) <= _google_broken(broken, chosen) and len(after) < len(broken)


def _switch_candidates(cands, chosen, dropped, broken):
    """Names worth flipping: providers of what a broken module of ours needs, and Google modules
    that cannot load but have a counterpart of ours."""
    wanted = {}
    for name, unsat in broken.items():
        if chosen[name] == 'ours':
            wanted.update(unsat)
    providers = {n for n, c in cands.items() if n not in dropped and _other(chosen[n]) in c
                 and any(c[_other(chosen[n])].exports.get(s) == crc for s, crc in wanted.items())}
    replaceable = {n for n in broken if chosen[n] == 'google' and 'ours' in cands[n]}
    return sorted(providers | replaceable)


def resolve(mods, kernel):
    """(chosen side per name, names of ours that had to be dropped)."""
    cands = {}
    for m in mods:
        cands.setdefault(m.name, {})[m.side] = m
    chosen = {n: 'google' if 'google' in c else 'ours' for n, c in cands.items()}
    dropped = set()
    while True:
        broken = _broken(cands, chosen, dropped, kernel)
        switch = next((n for n in _switch_candidates(cands, chosen, dropped, broken)
                       if _improves(n, cands, chosen, dropped, kernel, broken)), None)
        if switch:
            chosen[switch] = _other(chosen[switch])
            continue
        ours_broken = {n for n in broken if chosen[n] == 'ours'}
        if not ours_broken:
            return chosen, dropped
        dropped.update(ours_broken)


# ---- reading .ko files ---------------------------------------------------------------------

def _sections(data):
    shoff, = struct.unpack_from('<Q', data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', data, 0x3a)
    secs = [struct.unpack_from('<IIQQQQ', data, shoff + i * shentsize) for i in range(shnum)]
    stroff = secs[shstrndx][4]
    return {data[stroff + s[0]:data.index(b'\0', stroff + s[0])].decode(): s for s in secs}


def read_module(path, side):
    data = Path(path).read_bytes()
    secs = _sections(data)
    needs = {}
    if '__versions' in secs:
        _, _, _, _, off, size = secs['__versions']
        for k in range(0, size, 64):
            crc = struct.unpack_from('<Q', data, off + k)[0] & 0xffffffff
            name = data[off + k + 8: off + k + 64].split(b'\0')[0].decode()
            if name != 'module_layout':
                needs[name] = crc
    exports = {}
    if '__kcrctab' in secs:
        base = secs['__kcrctab'][4]
        out = subprocess.run(['llvm-nm', str(path)], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2].startswith('__crc_'):
                exports[parts[2][6:]] = struct.unpack_from('<I', data, base + int(parts[0], 16))[0]
    return Module(os.path.basename(path), side, exports, needs)


def read_kernel_exports(symvers):
    """vmlinux exports from Module.symvers (module exports come from the .ko files)."""
    exports = {}
    for line in Path(symvers).read_text().splitlines():
        parts = line.split('\t')
        if len(parts) > 2 and parts[2] == 'vmlinux':
            exports[parts[1]] = int(parts[0], 16)
    return exports


def find_modules(root):
    """*.ko below root without following symlinks (lib/modules/<ver>/build points at the build tree)."""
    return [os.path.join(d, f) for d, _, files in os.walk(root) for f in files if f.endswith('.ko')]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ours')
    ap.add_argument('google', nargs='+')
    ap.add_argument('--symvers', required=True)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    ours = find_modules(a.ours)
    google = [f for d in a.google for f in find_modules(d)]
    mods = [read_module(f, 'ours') for f in ours] + [read_module(f, 'google') for f in google]
    chosen, dropped = resolve(mods, read_kernel_exports(a.symvers))
    lines = [f'{side} {name}' for name, side in sorted(chosen.items()) if name not in dropped]
    lines += [f'dropped {name}' for name in sorted(dropped)]
    Path(a.out).write_text('\n'.join(lines) + '\n')
    shared = {m.name for m in mods if m.side == 'google'} & {m.name for m in mods if m.side == 'ours'}
    switched = sorted(n for n in shared if chosen[n] == 'ours')
    print(f'{len(chosen)} names, {len(shared)} shared ({len(switched)} use ours: {switched}), '
          f'{len(dropped)} dropped: {sorted(dropped)}')


if __name__ == '__main__':
    sys.exit(main())
