import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from build_layer import (ODM_SEPOLICY, odm_files, SYSTEM_TWEAKS_RC, TEST_POWER_OPTIONS, VM_POWER_RC, Symlink, added_lines, ancestor_dirs, layer_path, module_label,
                         new_dir_meta, odm_build_prop, parse_erofs_ls, prop_overrides, tree_entries, write_tar)


class PropOverridesTest(unittest.TestCase):
    ORIGINAL = "# comment\nro.hardware.egl=angle\nro.foo=1\nro.keep=x\n"

    def test_changed_and_added_keys_are_overrides(self):
        patched = "# comment\nro.hardware.egl=virgl\nro.foo=1\nro.keep=x\nro.new=2\n"
        self.assertEqual(prop_overrides(self.ORIGINAL, patched), {"ro.hardware.egl": "virgl", "ro.new": "2"})

    def test_reordering_and_comments_are_not_overrides(self):
        patched = "ro.keep=x\n# another comment\nro.foo=1\nro.hardware.egl=angle\n"
        self.assertEqual(prop_overrides(self.ORIGINAL, patched), {})

    def test_last_assignment_wins_like_init(self):
        patched = self.ORIGINAL + "ro.foo=2\nro.foo=3\n"
        self.assertEqual(prop_overrides(self.ORIGINAL, patched), {"ro.foo": "3"})


class OdmBuildPropTest(unittest.TestCase):
    def test_lists_overrides_sorted_after_a_header(self):
        text = odm_build_prop({"ro.b": "2", "ro.a": "1"})
        self.assertTrue(text.startswith("#"))
        self.assertTrue(text.endswith("ro.a=1\nro.b=2\n"))


class LayerPathTest(unittest.TestCase):
    def test_vendor_files_go_under_vendor(self):
        self.assertEqual(layer_path("vendor", "etc/init/x.rc"), "vendor/etc/init/x.rc")

    def test_odm_files_go_under_odm(self):
        self.assertEqual(layer_path("odm", "etc/build.prop"), "odm/etc/build.prop")

    def test_rejects_paths_that_escape_the_partition(self):
        for bad in ("../system/bin/sh", "/etc/x", "etc/../../x"):
            with self.assertRaises(ValueError, msg=bad):
                layer_path("vendor", bad)


class AncestorDirsTest(unittest.TestCase):
    def test_lists_every_parent_directory_once(self):
        files = {"etc/gbos/system/lib64/libz.so": 1, "etc/init/a.rc": 1, "build.prop": 1}
        self.assertEqual(
            ancestor_dirs(files),
            {"etc", "etc/gbos", "etc/gbos/system", "etc/gbos/system/lib64", "etc/init"},
        )


class AddedLinesTest(unittest.TestCase):
    def test_returns_lines_only_in_the_patched_file_in_order(self):
        original = "/vendor/bin/a u:object_r:a_exec:s0\n/vendor/bin/b u:object_r:b_exec:s0\n"
        patched = original + "/dev/ttyS0 u:object_r:console_device:s0\n/sys/x u:object_r:sysfs_gpu:s0\n"
        self.assertEqual(
            added_lines(original, patched),
            ["/dev/ttyS0 u:object_r:console_device:s0", "/sys/x u:object_r:sysfs_gpu:s0"],
        )

    def test_ignores_blank_lines_and_comments(self):
        self.assertEqual(added_lines("a\n", "a\n\n# note\n"), [])


class VmPowerTest(unittest.TestCase):
    def test_battery_and_ac_come_from_test_power(self):
        # The health HAL reads /sys/class/power_supply; `cmd battery set` lived only in system_server.
        self.assertIn(b"exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe -d /vendor/lib/modules test_power",
                      VM_POWER_RC)
        self.assertEqual(TEST_POWER_OPTIONS,
                         "options test_power ac_online=on battery_status=full battery_capacity=100\n")
        self.assertNotIn(b"cmd battery", VM_POWER_RC)

    def test_the_guest_never_suspends(self):
        self.assertIn(b"write /sys/power/wake_lock gbos_vm", VM_POWER_RC)
        # Writing wake_lock needs CAP_BLOCK_SUSPEND, which vendor_init lacked (EPERM).
        self.assertIn("(allow vendor_init_202604 self (capability2 (block_suspend)))", ODM_SEPOLICY.decode())

    def test_screen_stays_on_through_settings_the_shell_may_write(self):
        # `svc power stayon` needs WRITE_SETTINGS, which com.android.shell lacks on a user build.
        self.assertIn(b"cmd settings put global stay_on_while_plugged_in 7", VM_POWER_RC)
        self.assertIn(b"cmd settings put system screen_off_timeout 2147483647", VM_POWER_RC)
        self.assertNotIn(b"svc power", VM_POWER_RC)


class GraphicsTest(unittest.TestCase):
    def test_rendering_stays_on_gl_over_virgl(self):
        # Vulkan through the port's Venus driver crashed SurfaceFlinger in
        # vkGetAndroidHardwareBufferPropertiesANDROID (gralloc buffers) and the guest boot-looped.
        prop = odm_files({"debug.hwui.renderer": "skiagl", "ro.hwui.use_vulkan": "false"}, [])["etc/build.prop"][0]
        self.assertIn(b"debug.hwui.renderer=skiagl\n", prop)
        self.assertNotIn(b"skiavk", prop)


class PointerTest(unittest.TestCase):
    def test_the_layer_ships_no_pointer_bridge(self):
        # The VM uses QEMU's usb-mouse (a real relative mouse for Android). The old tablet bridge showed
        # up as a hovering stylus and crashed system_server's InputDispatcher when a window changed under it.
        import build_layer
        self.assertFalse(hasattr(build_layer, "POINTER_BRIDGE_RC"))
        self.assertNotIn(b"pointer_bridge", build_layer.ODM_SEPOLICY)


class SystemTweaksTest(unittest.TestCase):
    def test_boot_animation_is_bound_over_the_stock_one_at_early_init(self):
        early_init = SYSTEM_TWEAKS_RC.split(b"on early-init\n")[1].split(b"\non ")[0]
        self.assertIn(b"mount none /vendor/etc/gbos/system/media/bootanimation.zip "
                      b"/system/media/bootanimation.zip bind", early_init)

    def test_gscutil_is_kept_aside_then_wrapped(self):
        early_init = SYSTEM_TWEAKS_RC.split(b"on early-init\n")[1].split(b"\non ")[0]
        keep = b"mount none /system/bin/gscutil /vendor/etc/gbos/system/bin/gscutil.real bind"
        # /system is a shared mount: without this the wrapper's mount propagates onto the kept copy,
        # the wrapper then execs itself forever and init hangs on `gscutil perform-os-startup`.
        private = b"mount none none /vendor/etc/gbos/system/bin/gscutil.real private"
        wrap = b"mount none /vendor/etc/gbos/system/bin/gscutil /system/bin/gscutil bind"
        self.assertLess(early_init.index(keep), early_init.index(private))
        self.assertLess(early_init.index(private), early_init.index(wrap))


class TreeEntriesTest(unittest.TestCase):
    def test_files_are_read_lazily_and_links_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sub").mkdir()
            (root / "sub" / "a.ko").write_bytes(b"ko")
            (root / "modules.dep").write_text("a.ko:\n")
            (root / "MISSING.txt").write_text("x\n")
            (root / "g.ko").symlink_to("/vendor_dlkm/lib/modules/g.ko")
            entries = tree_entries(root, module_label, skip={"MISSING.txt"})
        self.assertEqual(entries["sub/a.ko"], (root / "sub" / "a.ko", "vendor_kernel_modules", 0o644, 0, 0))
        self.assertEqual(entries["modules.dep"][1], "vendor_file")
        self.assertEqual(entries["g.ko"], (Symlink("/vendor_dlkm/lib/modules/g.ko"), "vendor_file", 0o777, 0, 0))
        self.assertNotIn("MISSING.txt", entries)


class NewDirMetaTest(unittest.TestCase):
    def test_firmware_directories_keep_the_firmware_label(self):
        self.assertEqual(new_dir_meta("firmware/amdgpu"), (0o755, 0, 2000, "vendor_firmware_file"))

    def test_other_directories_are_vendor_file(self):
        self.assertEqual(new_dir_meta("lib/modules"), (0o755, 0, 2000, "vendor_file"))


class ParseErofsLsTest(unittest.TestCase):
    def test_names_and_directory_flags(self):
        text = ("Path : /firmware\nSize: 899  On-disk size: 899  directory\n\n"
                "       NID TYPE  FILENAME\n        321    2  .\n         51    2  ..\n"
                "    3001461    1  regulatory.db\n       400    2  i915\n")
        self.assertEqual(parse_erofs_ls(text), {"regulatory.db": False, "i915": True})


class WriteTarTest(unittest.TestCase):
    def test_dirs_bytes_files_and_links_carry_selinux_labels(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "fw.bin"
            src.write_bytes(b"firmware")
            out = Path(d) / "layer.tar"
            write_tar(out, {"vendor": (0o755, 0, 2000, "vendor_file")}, {
                "vendor/a.rc": (b"on boot\n", "vendor_configs_file", 0o644, 0, 0),
                "vendor/firmware/fw.bin": (src, "vendor_firmware_file", 0o644, 0, 0),
                "vendor/lib/modules/g.ko": (Symlink("/vendor_dlkm/lib/modules/g.ko"), "vendor_file", 0o777, 0, 0),
            })
            with tarfile.open(out) as t:
                members = {m.name: m for m in t.getmembers()}
                self.assertTrue(members["vendor"].isdir())
                self.assertEqual(t.extractfile("vendor/a.rc").read(), b"on boot\n")
                self.assertEqual(t.extractfile("vendor/firmware/fw.bin").read(), b"firmware")
                link = members["vendor/lib/modules/g.ko"]
                self.assertTrue(link.issym())
                self.assertEqual(link.linkname, "/vendor_dlkm/lib/modules/g.ko")
                self.assertEqual(link.pax_headers["SCHILY.xattr.security.selinux"], "u:object_r:vendor_file:s0")


if __name__ == "__main__":
    unittest.main()
