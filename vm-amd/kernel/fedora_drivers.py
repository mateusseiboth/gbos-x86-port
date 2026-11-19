#!/usr/bin/env python3
"""Build a config fragment that adds Fedora's hardware drivers to the GKI kernel as modules.

  fedora_drivers.py FEDORA_CONFIG GKI_CONFIG OUT_FRAGMENT
  fedora_drivers.py check GKI_CONFIG MERGED_CONFIG     (after olddefconfig; exit 1 on core drift)

Only options the GKI config leaves off are touched, so the shipped core (and with it the
symbol CRCs of Google's prebuilt modules) stays as it is; check_crc.py has the last word.
"""
import os
import re
import sys
from pathlib import Path

DRIVER_TREES = ('drivers/', 'sound/', 'net/wireless/', 'net/mac80211/', 'net/bluetooth/', 'net/rfkill/')
DEPENDENCY_TREES = ('crypto/', 'lib/')
SKIPPED = re.compile(r'(DEBUG|_TEST|TEST_|KUNIT|SELFTEST|WERROR)')
# Driver options whose mere presence turns off or demotes a built-in GKI option (found by
# diffing the merged config against the GKI one after olddefconfig).
CORE_CONFLICTS = {
    'BLK_DEV_FD': 'PKVM_INTEL depends on BLK_DEV_FD=n',
    'AGP': 'DRM depends on AGP || AGP=n, so AGP=m forces DRM=m',
    'RPMB': 'MMC_BLOCK depends on RPMB || !RPMB',
    'DEVMEM': 'EXCLUSIVE_SYSTEM_RAM depends on !DEVMEM || STRICT_DEVMEM',
    'INTEL_IOMMU_SVM': 'X86_DISABLED_FEATURE_ENQCMD depends on !INTEL_IOMMU_SVM',
    'TCG_TPM': "Google ships its own tpm.ko for tpm_tis_i2c_cr50; ours differs in tpm_chip (TCG_TPM2_HMAC)",
    # Options that turn a CORE_PROTECTED one on through a default rather than a select.
    'NETCONSOLE': 'NETPOLL is def_bool NETCONSOLE',
    'IPVLAN': 'IPVLAN_L3S is def_bool y and selects NET_L3_MASTER_DEV',
    'CHROMEOS_OF_HW_PROBER': 'defaults to OF (on in GKI) and selects OF_DYNAMIC',
}
# Options that add members to structs reachable from exported symbols (found by diffing
# `pahole --sizes` of the GKI and full vmlinux and mapping the new members to their #if).
# gendwarfksyms --stable walks the whole type graph, so any of them changes thousands of
# CRCs. They keep their GKI value, and drivers that select them are left out.
CORE_PROTECTED = {
    'BLK_DEV_INTEGRITY',            # bio.bi_integrity
    'CDROM',                        # gendisk.cdi
    'DPLL',                         # net_device.dpll_pin
    'FCOE',                         # net_device.fcoe_ddp_xid
    'HYPERV',                       # kvm_arch.hv_*
    'I2C_SLAVE',                    # i2c_client.slave_cb
    'KVM_EXTERNAL_WRITE_TRACKING',  # kvm_arch.track_notifier_head
    'LEDS_BRIGHTNESS_HW_CHANGED',   # led_classdev.brightness_hw_changed
    'NETPOLL', 'NET_POLL_CONTROLLER',  # net_device.npinfo, vlan_dev_priv.netpoll
    'NET_L3_MASTER_DEV',            # net_device.l3mdev_ops
    'NET_SHAPER',                   # net_device.net_shaper_hierarchy
    'OF_DYNAMIC',                   # property._flags
    'PAGE_POOL_STATS',              # page_pool stats
    'PCIE_DPC', 'PCIE_PTM', 'PCIE_TPH', 'PCI_DOE', 'PCI_NPEM',  # pci_dev.*
    'PM_GENERIC_DOMAINS',           # pm_subsys_data.domain_data
    'SND_SEQUENCER',                # snd_rawmidi.seq_dev
    'SND_VERBOSE_PROCFS',           # snd_compr.proc_*
    'TLS',                          # netns_mib.tls_statistics
    'VMD',                          # pci_sysdata.vmd_dev
    # Same-size changes, found by diffing gendwarfksyms --symtypes of exported symbols:
    'BCACHE',                       # task_struct.sequential_io (lives in padding)
    'CRYPTO_USER',                  # net.crypto_nlsk
    'LIBFCOE',                      # net_device_ops.ndo_fcoe_get_wwn
    'MAC80211_MESH', 'HYPERV_NET',  # raise LL_MAX_HEADER -> hh_cache.hh_data
}


def _in(path, trees):
    return path.startswith(trees)


def _tristate(name, fedora, path):
    return 'm' if _in(path, DRIVER_TREES + DEPENDENCY_TREES) else None


def _bool(name, fedora, path):
    return 'y' if _in(path, DRIVER_TREES) else None


BY_TYPE = {'tristate': _tristate, 'bool': _bool}


def decide(name, fedora, ours, kind, path):
    """New value for CONFIG_<name>, or None to leave the GKI value."""
    if ours != 'n' or fedora not in ('y', 'm') or SKIPPED.search(name) or name in CORE_CONFLICTS:
        return None
    strategy = BY_TYPE.get(kind)
    return strategy(name, fedora, path) if strategy else None


def read_config(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        m = re.match(r'CONFIG_(\w+)=(.*)', line) or re.match(r'# CONFIG_(\w+) is not set', line)
        if m:
            values[m.group(1)] = m.group(2) if m.lastindex == 2 else 'n'
    return values


CONFIG_LINE = re.compile(r'^\s*(?:menu)?config\s+(\w+)\s*$')
TYPE_LINE = re.compile(r'^\s*(?:def_)?(bool|tristate)\b')


def parse_kconfig(text):
    """{symbol: type} for the typed config entries of one Kconfig file."""
    symbols, current = {}, None
    for line in text.splitlines():
        start = CONFIG_LINE.match(line)
        if start:
            current = start.group(1)
            continue
        kind = TYPE_LINE.match(line)
        if current and kind:
            symbols.setdefault(current, kind.group(1))
            current = None
    return symbols


SELECT_LINE = re.compile(r'^\s*select\s+(\w+)')


def parse_selects(text):
    """{symbol: {selected symbols}} for one Kconfig file (conditions are ignored, on purpose)."""
    selects, current = {}, None
    for line in text.splitlines():
        start = CONFIG_LINE.match(line)
        if start:
            current = start.group(1)
            continue
        sel = SELECT_LINE.match(line)
        if current and sel:
            selects.setdefault(current, set()).add(sel.group(1))
    return selects


def selects_closure(name, selects):
    seen, todo = set(), list(selects.get(name, ()))
    while todo:
        sym = todo.pop()
        if sym in seen:
            continue
        seen.add(sym)
        todo.extend(selects.get(sym, ()))
    return seen


def _kconfig_files(srctree):
    root = Path(srctree)
    for kconfig in sorted(root.rglob('Kconfig*')):
        yield str(kconfig.relative_to(root)), kconfig.read_text(errors='replace')


def load_selects(srctree):
    selects = {}
    for _, text in _kconfig_files(srctree):
        for name, targets in parse_selects(text).items():
            selects.setdefault(name, set()).update(targets)
    return selects


def load_symbols(srctree):
    """{symbol: (type, Kconfig path)}; the first typed definition wins."""
    symbols = {}
    for rel, text in _kconfig_files(srctree):
        for name, kind in parse_kconfig(text).items():
            symbols.setdefault(name, (kind, rel))
    return symbols


def _touches_core(name, selects, protected):
    return name in protected or bool(selects_closure(name, selects) & protected)


def build_fragment(fedora, ours, symbols, selects=None, protected=frozenset()):
    selects = selects or {}
    lines = []
    for name in sorted(fedora):
        if name not in symbols or _touches_core(name, selects, protected):
            continue
        kind, path = symbols[name]
        value = decide(name, fedora[name], ours.get(name, 'n'), kind, path)
        if value:
            lines.append(f'CONFIG_{name}={value}')
    # Pin the protected options GKI leaves off, so no `default y if ...` turns them on.
    lines += [f'# CONFIG_{name} is not set' for name in sorted(protected)
              if name in symbols and ours.get(name, 'n') == 'n']
    return lines


TOLERATED_DRIFT = {'FONT_AUTOSELECT'}  # console font choice only


def core_drift(gki, full, protected):
    """{option: (gki, full)} for GKI options that changed and protected options that moved."""
    names = {k for k, v in gki.items() if v != 'n'} | set(protected)
    return {k: (gki.get(k, 'n'), full.get(k, 'n')) for k in sorted(names)
            if gki.get(k, 'n') != full.get(k, 'n') and k not in TOLERATED_DRIFT}


def check(gki_cfg, full_cfg):
    drift = core_drift(read_config(gki_cfg), read_config(full_cfg), CORE_PROTECTED)
    for name, (was, now) in drift.items():
        print(f'CONFIG_{name}: {was} -> {now}')
    print('core unchanged' if not drift else f'{len(drift)} core options changed')
    return 1 if drift else 0


def main():
    if sys.argv[1:2] == ['check']:
        sys.exit(check(*sys.argv[2:4]))
    fedora_cfg, gki_cfg, out = sys.argv[1:4]
    srctree = os.environ.get('GBOS_KSRC', os.path.join(os.environ.get('GBOS_ROOT', '/mnt/dados/gbos'), 'kernel/common'))
    lines = build_fragment(read_config(fedora_cfg), read_config(gki_cfg), load_symbols(srctree),
                           load_selects(srctree), CORE_PROTECTED | set(CORE_CONFLICTS))
    Path(out).write_text('# Generated by fedora_drivers.py from ' + fedora_cfg + '\n' + '\n'.join(lines) + '\n')
    print(f'{len(lines)} options -> {out} ({sum(l.endswith("=m") for l in lines)} modules)')


if __name__ == '__main__':
    main()
