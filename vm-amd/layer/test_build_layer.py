import unittest

from build_layer import FAKE_BATTERY_RC, added_lines, ancestor_dirs, layer_path, odm_build_prop, prop_overrides


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


class FakeBatteryTriggerTest(unittest.TestCase):
    def test_starts_as_soon_as_zygote_runs(self):
        # The setup wizard checks the battery before boot_completed; the service itself retries
        # `cmd battery` until BatteryService answers.
        self.assertIn(b"on property:init.svc.zygote=running\n    start vm-fake-battery\n", FAKE_BATTERY_RC)
        self.assertNotIn(b"sys.boot_completed", FAKE_BATTERY_RC)


if __name__ == "__main__":
    unittest.main()
