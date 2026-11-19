import io
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

from build_layer import (ODM_SEPOLICY, VM_MOUSE_RC, odm_files, patch_minigbm, SYSTEM_TWEAKS_RC, TEST_POWER_OPTIONS, VM_POWER_RC, Symlink, added_lines, ancestor_dirs, layer_path, module_label,
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
    # The graphics stack is chosen at boot: androidboot.gbos.gpu (run_vm.sh, usb/gbos_boot.c) selects
    # /odm/etc/gbos/gpu_<stack>.prop, imported by /odm/etc/build.prop. One layer for VM and PCs.
    PORT = {"debug.hwui.renderer": "skiagl", "ro.hardware.egl": "virgl", "ro.hardware.vulkan": "virtio",
            "ro.gfx.angle.supported": "false", "ro.surface_flinger.has_HDR_display": "false"}

    def files(self):
        return odm_files(self.PORT, [])

    def prop(self, stack):
        return self.files()[f"etc/gbos/gpu_{stack}.prop"][0].decode()

    def test_build_prop_imports_the_stack_of_the_detected_gpu(self):
        base = self.files()["etc/build.prop"][0].decode()
        self.assertTrue(base.rstrip().endswith("import /odm/etc/gbos/gpu_${ro.boot.gbos.gpu:-virgl}.prop"))
        self.assertIn("ro.surface_flinger.has_HDR_display=false\n", base)
        self.assertNotIn("ro.hardware.egl", base)

    def test_virgl_keeps_the_security_ports_gl_stack(self):
        self.assertIn("ro.hardware.egl=virgl\n", self.prop("virgl"))
        self.assertIn("debug.hwui.renderer=skiagl\n", self.prop("virgl"))

    def test_venus_is_virgl_with_vulkan_rendering(self):
        # Needs the patched virglrenderer (vm-amd/virgl): GBOS_VULKAN=1 run_vm.sh.
        prop = self.prop("venus")
        for line in ("ro.hardware.egl=virgl", "debug.hwui.renderer=skiavk", "ro.hwui.use_vulkan=true",
                     "debug.renderengine.backend=skiavkthreaded", "ro.hardware.vulkan=virtio"):
            self.assertIn(line + "\n", prop)
        self.assertNotIn("skiagl", prop)

    def test_amd_renders_on_the_gpu_with_radv_under_angle(self):
        # RADV (graphics/build_radv.sh) as the Vulkan HAL; ANGLE (in /system) gives GLES on top of it.
        prop = self.prop("amd")
        for line in ("ro.hardware.egl=angle", "ro.hardware.vulkan=radv", "ro.gfx.angle.supported=true",
                     "debug.hwui.renderer=skiagl", "debug.renderengine.backend=skiaglthreaded"):
            self.assertIn(line + "\n", prop)

    def test_unknown_gpus_render_in_software(self):
        # ANGLE on SwiftShader Vulkan (vulkan.pastel.so, from Cuttlefish): the last resort.
        prop = self.prop("swiftshader")
        for line in ("ro.hardware.egl=angle", "ro.hardware.vulkan=pastel"):
            self.assertIn(line + "\n", prop)

    def test_intel_keeps_the_stock_drivers(self):
        self.assertNotIn("ro.hardware.egl", self.prop("intel"))


class MinigbmTest(unittest.TestCase):
    def test_amdgpu_takes_the_dumb_radeon_backend(self):
        # Google's minigbm has no amdgpu backend; its "radeon" one is the generic dumb-buffer backend,
        # which amdgpu supports. Same length, so the driver table just gets the other name.
        lib = b"\x00i915\x00radeon\x00udl\x00"
        self.assertEqual(patch_minigbm(lib), b"\x00i915\x00amdgpu\x00udl\x00")

    def test_refuses_an_unexpected_library(self):
        with self.assertRaises(ValueError):
            patch_minigbm(b"\x00radeon\x00\x00radeon\x00")
        with self.assertRaises(ValueError):
            patch_minigbm(b"\x00i915\x00")


class GpuSysfsLabelTest(unittest.TestCase):
    # libdrm/Mesa read the GPU's PCI identity and drm/ nodes; the port labeled only 0000:00:01.0
    # (the VM's slot), so a GPU anywhere else (a real PC, behind a bridge) was "no DRM device".
    def patterns(self):
        text = odm_files({}, [])["etc/selinux/odm_file_contexts"][0].decode()
        return [line.split()[0] for line in text.splitlines() if line.endswith("u:object_r:sysfs_gpu:s0")]

    def labeled(self, path):
        return any(re.fullmatch(p, path) for p in self.patterns())

    def test_gpu_behind_a_bridge(self):
        self.assertTrue(self.labeled("/sys/devices/pci0000:00/0000:00:08.1/0000:0a:00.0/uevent"))
        self.assertTrue(self.labeled("/sys/devices/pci0000:00/0000:00:08.1/0000:0a:00.0/drm/card0/dev"))

    def test_gpu_on_the_root_bus(self):
        self.assertTrue(self.labeled("/sys/devices/pci0000:00/0000:00:03.0/device"))
        self.assertTrue(self.labeled("/sys/devices/pci0000:00/0000:00:03.0/drm"))

    def test_other_device_attributes_keep_their_labels(self):
        self.assertFalse(self.labeled("/sys/devices/pci0000:00/0000:00:03.0/power/control"))
        self.assertFalse(self.labeled("/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/uevent"))


class VmOnlyTest(unittest.TestCase):
    # run_vm.sh passes androidboot.gbos.vm=1; on a real PC (the pendrive) none of this may run: a fake
    # battery, no suspend and a tablet bridge only make sense in QEMU.
    def test_power_tweaks_run_only_in_the_vm(self):
        for trigger in (b"on boot && property:ro.boot.gbos.vm=1\n",
                        b"on property:sys.boot_completed=1 && property:ro.boot.gbos.vm=1\n"):
            self.assertIn(trigger, VM_POWER_RC)
        self.assertNotIn(b"\non boot\n", VM_POWER_RC)

    def test_the_fake_battery_loads_wherever_there_is_no_battery(self):
        # The VM and desktops (usb/gbos_boot.c sets androidboot.gbos.battery=none); never on a laptop.
        self.assertIn(b"on early-boot && property:ro.boot.gbos.battery=none\n"
                      b"    exec u:r:vendor_modprobe:s0 -- /vendor/bin/modprobe -d /vendor/lib64/modules test_power\n"
                      .replace(b"lib64/modules", b"lib/modules"), VM_POWER_RC)
        self.assertNotIn(b"\non early-boot\n", VM_POWER_RC)

    def test_vm_mouse_starts_only_in_the_vm(self):
        self.assertIn(b"service vm-mouse /vendor/bin/vm-mouse\n", VM_MOUSE_RC)
        self.assertIn(b"    disabled\n", VM_MOUSE_RC)
        self.assertIn(b"on boot && property:ro.boot.gbos.vm=1\n    start vm-mouse\n", VM_MOUSE_RC)

    def test_pointer_acceleration_is_off_so_the_cursor_follows_the_host_pointer(self):
        self.assertIn(b"cmd settings put system mouse_pointer_acceleration_enabled 0", VM_POWER_RC)

    def test_vm_mouse_has_its_own_domain(self):
        self.assertIn(b"(type gbos_vm_mouse)", ODM_SEPOLICY)
        self.assertIn(b"(typetransition init_202604 gbos_vm_mouse_exec process gbos_vm_mouse)", ODM_SEPOLICY)
        self.assertNotIn(b"pointer_bridge", ODM_SEPOLICY)


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
