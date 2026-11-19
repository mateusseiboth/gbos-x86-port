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
import shutil
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


def _candidates(mods):
    cands = {}
    for m in mods:
        cands.setdefault(m.name, {})[m.side] = m
    return cands


def resolve(mods, kernel):
    """(chosen side per name, names of ours that had to be dropped)."""
    cands = _candidates(mods)
    chosen = {n: 'google' if 'google' in c else 'ours' for n, c in cands.items()}
    dropped = set()
    while True:
        broken = _broken(cands, chosen, dropped, kernel)
        flip = next((n for n in _switch_candidates(cands, chosen, dropped, broken)
                     if _improves(n, cands, chosen, dropped, kernel, broken)), None)
        if flip:
            chosen[flip] = _other(chosen[flip])
            continue
        ours_broken = {n for n in broken if chosen[n] == 'ours'}
        if not ours_broken:
            return chosen, dropped
        dropped.update(ours_broken)


# ---- /vendor/lib/modules ----------------------------------------------------------------

def _load_order(name, direct, seen):
    """Dependencies of name, each after its own dependencies (post-order)."""
    order = []
    for dep in direct.get(name, ()):
        if dep not in seen:
            seen.add(dep)
            order += _load_order(dep, direct, seen) + [dep]
    return order


def build_modules_dep(mods, chosen, dropped, kernel, device_paths):
    """modules.dep for the combined /vendor/lib/modules.

    Our modules are files of that directory (relative names); Google's are named by their real
    path, since libmodprobe opens modules with O_NOFOLLOW. A Google module without a device path
    sits in the first-stage ramdisk and is already loaded. libmodprobe inserts a line's
    dependencies from last to first, so they are listed in reverse load order.
    """
    cands = _candidates(mods)
    active = {n: cands[n][side] for n, side in chosen.items() if n not in dropped}
    provider = {}
    for name, m in active.items():
        for sym in m.exports:
            provider.setdefault(sym, name)
    direct = {n: sorted({provider[s] for s in m.needs if s not in kernel and s in provider} - {n})
              for n, m in active.items()}
    path = {n: n if chosen[n] == 'ours' else device_paths.get(n) for n in active}
    lines = [' '.join([f'{path[name]}:'] + [path[d] for d in reversed(_load_order(name, direct, {name})) if path[d]])
             for name in sorted(n for n in active if path[n])]
    return ''.join(line + '\n' for line in lines)


def _canonical(name):
    return name.removesuffix('.ko').replace('-', '_')


# Which field of a modules.alias / modules.softdep line names the module.
MODULE_FIELD = {'alias': lambda f: f[-1], 'softdep': lambda f: f[1]}


def filter_module_lines(text, names):
    """depmod's alias/softdep lines that belong to one of names."""
    wanted = {_canonical(n) for n in names}
    keep = [line for line in text.splitlines()
            if (f := line.split()) and f[0] in MODULE_FIELD and _canonical(MODULE_FIELD[f[0]](f)) in wanted]
    return ''.join(line + '\n' for line in keep)


# Extracted partition -> the directory Google's own modules.dep uses for it.
DEVICE_MODULE_DIRS = {'vendor_dlkm_a': '/vendor_dlkm/lib/modules', 'system_dlkm_a': '/system/lib/modules'}


def device_path(path):
    """Where a shipped module lives on the device; None for the first-stage ramdisk ones."""
    parts = Path(path).parts
    part = next((p for p in parts if p in DEVICE_MODULE_DIRS), None)
    return part and f'{DEVICE_MODULE_DIRS[part]}/{parts[-1]}'


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


# Modules of ours that are never shipped. Hardware watchdogs: nothing in this Android
# feeds one, and a driver taking over a timer the firmware left running resets the machine.
EXCLUDED_PATHS = ('/drivers/watchdog/', '/drivers/char/ipmi/ipmi_watchdog.ko')


def is_excluded(path):
    return any(d in path for d in EXCLUDED_PATHS)


def find_modules(root):
    """*.ko below root without following symlinks (lib/modules/<ver>/build points at the build tree)."""
    return [os.path.join(d, f) for d, _, files in os.walk(root) for f in files if f.endswith('.ko')]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ours')
    ap.add_argument('google', nargs='+')
    ap.add_argument('--symvers', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--vendor-out', help='also write the combined /vendor/lib/modules tree')
    a = ap.parse_args()
    ours = [f for f in find_modules(a.ours) if not is_excluded(f)]
    google = [f for d in a.google for f in find_modules(d)]
    mods = [read_module(f, 'ours') for f in ours] + [read_module(f, 'google') for f in google]
    kernel = read_kernel_exports(a.symvers)
    chosen, dropped = resolve(mods, kernel)
    lines = [f'{side} {name}' for name, side in sorted(chosen.items()) if name not in dropped]
    lines += [f'dropped {name}' for name in sorted(dropped)]
    Path(a.out).write_text('\n'.join(lines) + '\n')
    shared = {m.name for m in mods if m.side == 'google'} & {m.name for m in mods if m.side == 'ours'}
    switched = sorted(n for n in shared if chosen[n] == 'ours')
    print(f'{len(chosen)} names, {len(shared)} shared ({len(switched)} use ours: {switched}), '
          f'{len(dropped)} dropped: {sorted(dropped)}')
    if a.vendor_out:
        write_vendor_modules(Path(a.vendor_out), a.ours, ours, google, mods, chosen, dropped, kernel)


# Google's index files that are copied as they are (names only, no paths).
GOOGLE_INDEX_COPIES = ('modules.load', 'modules.blocklist', 'modules.options')


def write_vendor_modules(out, ours_root, ours, google, mods, chosen, dropped, kernel):
    """The directory the layer puts in place of the stock /vendor/lib/modules symlink.

    Every reader (ueventd's modalias handling, init.insmod.sh, vendor rc files) goes through
    /vendor/lib/modules, so one index decides between Google's module and ours for each name.
    """
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    ours_sel = {n for n, side in chosen.items() if side == 'ours' and n not in dropped}
    for f in ours:
        if os.path.basename(f) in ours_sel:
            os.link(f, out / os.path.basename(f))
    paths = {os.path.basename(f): device_path(f) for f in google if device_path(f)}
    google_sel = {n for n in paths if chosen[n] == 'google'}
    (out / 'modules.dep').write_text(build_modules_dep(mods, chosen, dropped, kernel, paths))
    depmod_dir = Path(next(d for d, _, files in os.walk(ours_root) if 'modules.alias' in files))
    google_dirs = sorted({os.path.dirname(f) for f in google if device_path(f)})
    for index in ('modules.alias', 'modules.softdep'):
        theirs = ''.join(filter_module_lines(_read(Path(d) / index), google_sel) for d in google_dirs)
        (out / index).write_text(theirs + filter_module_lines((depmod_dir / index).read_text(), ours_sel))
    vendor_dir = next(d for d in google_dirs if 'vendor_dlkm' in d)
    for index in GOOGLE_INDEX_COPIES:
        (out / index).write_text(_read(Path(vendor_dir) / index))
    print(f'{out}: {len(ours_sel)} of ours, {len(google_sel)} of Google\'s by path')


def _read(path):
    return path.read_text() if path.exists() else ''


if __name__ == '__main__':
    sys.exit(main())
