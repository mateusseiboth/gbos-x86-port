import unittest

from fedora_drivers import build_fragment, core_drift, decide, parse_kconfig, parse_selects, selects_closure


class DecideTest(unittest.TestCase):
    def test_tristate_driver_built_in_on_fedora_becomes_module(self):
        self.assertEqual(decide("DRM_AMDGPU", "y", "n", "tristate", "drivers/gpu/drm/amd/amdgpu/Kconfig"), "m")

    def test_tristate_driver_module_on_fedora_stays_module(self):
        self.assertEqual(decide("IWLWIFI", "m", "n", "tristate", "drivers/net/wireless/intel/iwlwifi/Kconfig"), "m")

    def test_never_touches_what_gki_already_sets(self):
        self.assertIsNone(decide("USB_XHCI_HCD", "m", "y", "tristate", "drivers/usb/host/Kconfig"))
        self.assertIsNone(decide("NVME_CORE", "y", "m", "tristate", "drivers/nvme/host/Kconfig"))

    def test_bool_gate_inside_driver_tree_is_enabled(self):
        self.assertEqual(decide("WLAN_VENDOR_REALTEK", "y", "n", "bool", "drivers/net/wireless/realtek/Kconfig"), "y")

    def test_bool_outside_driver_tree_is_left_alone(self):
        self.assertIsNone(decide("NUMA_BALANCING", "y", "n", "bool", "init/Kconfig"))

    def test_tristate_dependency_in_crypto_or_lib_becomes_module(self):
        self.assertEqual(decide("CRYPTO_ARC4", "m", "n", "tristate", "crypto/Kconfig"), "m")
        self.assertEqual(decide("CRC_ITU_T", "m", "n", "tristate", "lib/Kconfig"), "m")

    def test_tristate_outside_allowed_trees_is_left_alone(self):
        self.assertIsNone(decide("BTRFS_FS", "m", "n", "tristate", "fs/btrfs/Kconfig"))

    def test_bool_in_crypto_or_lib_is_left_alone(self):
        self.assertIsNone(decide("CRYPTO_FIPS", "y", "n", "bool", "crypto/Kconfig"))

    def test_disabled_on_fedora_is_ignored(self):
        self.assertIsNone(decide("DRM_NOUVEAU", "n", "n", "tristate", "drivers/gpu/drm/nouveau/Kconfig"))

    def test_debug_and_test_options_are_skipped(self):
        for name in ("DRM_AMDGPU_WERROR_DEBUG", "IWLWIFI_DEBUGFS", "DRM_KUNIT_TEST", "USB_TEST", "SND_DEBUG", "RTW88_DEBUG"):
            self.assertIsNone(decide(name, "y", "n", "bool", "drivers/x/Kconfig"), name)

    def test_werror_options_are_skipped(self):
        self.assertIsNone(decide("DRM_WERROR", "y", "n", "bool", "drivers/gpu/drm/Kconfig"))

    def test_sound_and_wireless_stacks_count_as_driver_trees(self):
        self.assertEqual(decide("SND_HDA_INTEL", "m", "n", "tristate", "sound/hda/controllers/Kconfig"), "m")
        self.assertEqual(decide("MAC80211_MESH", "y", "n", "bool", "net/mac80211/Kconfig"), "y")
        self.assertEqual(decide("BT_HCIBTUSB", "m", "n", "tristate", "drivers/bluetooth/Kconfig"), "m")


class ProtectGkiCoreTest(unittest.TestCase):
    def test_options_that_would_demote_gki_core_are_excluded(self):
        cases = {
            "BLK_DEV_FD": ("drivers/block/Kconfig", "tristate"),  # PKVM_INTEL depends on BLK_DEV_FD=n
            "AGP": ("drivers/char/agp/Kconfig", "tristate"),  # DRM depends on AGP || AGP=n
            "RPMB": ("drivers/misc/Kconfig", "tristate"),  # MMC_BLOCK depends on RPMB || !RPMB
            "DEVMEM": ("drivers/char/Kconfig", "bool"),  # EXCLUSIVE_SYSTEM_RAM needs !DEVMEM
            "INTEL_IOMMU_SVM": ("drivers/iommu/intel/Kconfig", "bool"),  # X86_DISABLED_FEATURE_ENQCMD
            # Google ships its own tpm.ko (vendor_dlkm) for tpm_tis_i2c_cr50; ours differs in tpm_chip.
            "TCG_TPM": ("drivers/char/tpm/Kconfig", "tristate"),
        }
        for name, (path, kind) in cases.items():
            self.assertIsNone(decide(name, "y", "n", kind, path), name)

    def test_options_that_enable_protected_ones_through_defaults_are_excluded(self):
        cases = {
            "NETCONSOLE": "drivers/net/Kconfig",  # NETPOLL is def_bool NETCONSOLE
            "IPVLAN": "drivers/net/Kconfig",  # IPVLAN_L3S is def_bool y and selects NET_L3_MASTER_DEV
            # defaults to OF (on in GKI) and selects OF_DYNAMIC
            "CHROMEOS_OF_HW_PROBER": "drivers/platform/chrome/Kconfig",
        }
        for name, path in cases.items():
            self.assertIsNone(decide(name, "m", "n", "tristate", path), name)

    def test_console_fonts_are_excluded(self):
        self.assertIsNone(decide("FONT_8x8", "y", "n", "bool", "lib/fonts/Kconfig"))


class ParseKconfigTest(unittest.TestCase):
    def test_reads_types_of_config_and_menuconfig(self):
        text = (
            "menuconfig WLAN_VENDOR_REALTEK\n"
            '\tbool "Realtek devices"\n'
            "\tdefault y\n\n"
            "config RTW88_8822CE\n"
            '\ttristate "Realtek 8822CE"\n'
            "\tdepends on PCI\n"
            "config RTW88_CORE\n"
            "\tdef_tristate n\n"
        )
        self.assertEqual(
            parse_kconfig(text),
            {"WLAN_VENDOR_REALTEK": "bool", "RTW88_8822CE": "tristate", "RTW88_CORE": "tristate"},
        )

    def test_ignores_untyped_and_non_boolean_entries(self):
        text = "config LOG_BUF_SHIFT\n\tint \"Log size\"\nconfig CMDLINE\n\tstring\nconfig FOO\n\tselect BAR\n"
        self.assertEqual(parse_kconfig(text), {})


class SelectsTest(unittest.TestCase):
    def test_reads_select_lines_with_and_without_condition(self):
        text = (
            "config NVME_TCP_TLS\n\tbool \"TLS\"\n\tselect NET_HANDSHAKE\n\tselect TLS if NET\n"
            "config OTHER\n\ttristate \"x\"\n"
        )
        self.assertEqual(parse_selects(text), {"NVME_TCP_TLS": {"NET_HANDSHAKE", "TLS"}})

    def test_closure_follows_selects_transitively(self):
        selects = {"A": {"B"}, "B": {"C"}, "C": set(), "D": {"A"}}
        self.assertEqual(selects_closure("D", selects), {"A", "B", "C"})

    def test_closure_survives_cycles(self):
        self.assertEqual(selects_closure("A", {"A": {"B"}, "B": {"A"}}), {"A", "B"})


class ProtectedCoreTest(unittest.TestCase):
    SYMBOLS = {
        "NETCONSOLE": ("tristate", "drivers/net/Kconfig"),
        "NETPOLL": ("bool", "net/Kconfig"),
        "E1000E": ("tristate", "drivers/net/ethernet/intel/Kconfig"),
        "PCIE_PTM": ("bool", "drivers/pci/pcie/Kconfig"),
    }

    def test_drivers_that_select_a_protected_option_are_dropped(self):
        lines = build_fragment(
            {"NETCONSOLE": "m", "E1000E": "m"}, {}, self.SYMBOLS,
            selects={"NETCONSOLE": {"NETPOLL"}}, protected={"NETPOLL"},
        )
        self.assertIn("CONFIG_E1000E=m", lines)
        self.assertNotIn("CONFIG_NETCONSOLE=m", lines)

    def test_protected_options_off_in_gki_are_pinned_off(self):
        lines = build_fragment({"PCIE_PTM": "y"}, {"PCIE_PTM": "n"}, self.SYMBOLS, selects={}, protected={"PCIE_PTM"})
        self.assertEqual(lines, ["# CONFIG_PCIE_PTM is not set"])

    def test_protected_options_on_in_gki_are_left_alone(self):
        lines = build_fragment({}, {"PCIE_PTM": "y"}, self.SYMBOLS, selects={}, protected={"PCIE_PTM"})
        self.assertEqual(lines, [])


class VmExtrasTest(unittest.TestCase):
    def test_the_vm_battery_module_is_always_built(self):
        # test_power: battery + AC power supplies the health HAL reads (QEMU has no ACPI battery).
        symbols = {"TEST_POWER": ("tristate", "drivers/power/supply/Kconfig")}
        self.assertEqual(build_fragment({}, {}, symbols), ["CONFIG_TEST_POWER=m"])

    def test_extras_unknown_to_the_tree_are_skipped(self):
        self.assertEqual(build_fragment({}, {}, {}), [])


class CoreDriftTest(unittest.TestCase):
    def test_reports_gki_options_that_changed(self):
        self.assertEqual(core_drift({"DRM": "y", "X": "n"}, {"DRM": "m", "X": "n"}, set()), {"DRM": ("y", "m")})

    def test_reports_protected_options_turned_on(self):
        self.assertEqual(core_drift({}, {"NETPOLL": "y"}, {"NETPOLL"}), {"NETPOLL": ("n", "y")})

    def test_console_font_autoselect_is_tolerated(self):
        # Only picks the built-in console font; no exported type changes.
        self.assertEqual(core_drift({"FONT_AUTOSELECT": "y"}, {}, set()), {})

    def test_new_driver_modules_are_not_drift(self):
        self.assertEqual(core_drift({}, {"E1000E": "m"}, {"NETPOLL"}), {})


if __name__ == "__main__":
    unittest.main()
