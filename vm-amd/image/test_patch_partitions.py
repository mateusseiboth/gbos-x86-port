import unittest

from patch_partitions import FAKE_BATTERY_RC, POINTER_BRIDGE_RC, disable_native_bridge, disable_service, drop_avb


class DisableNativeBridgeTest(unittest.TestCase):
    def test_replaces_existing_value(self):
        prop = "ro.foo=1\nro.dalvik.vm.native.bridge=libhoudini.so\nro.bar=2\n"
        self.assertEqual(
            disable_native_bridge(prop),
            "ro.foo=1\nro.dalvik.vm.native.bridge=0\nro.bar=2\n",
        )

    def test_appends_when_absent(self):
        self.assertEqual(
            disable_native_bridge("ro.foo=1\n"),
            "ro.foo=1\nro.dalvik.vm.native.bridge=0\n",
        )

    def test_is_idempotent(self):
        once = disable_native_bridge("ro.dalvik.vm.native.bridge=libhoudini.so\n")
        self.assertEqual(disable_native_bridge(once), once)

    def test_appends_newline_to_unterminated_input(self):
        self.assertEqual(
            disable_native_bridge("ro.foo=1"),
            "ro.foo=1\nro.dalvik.vm.native.bridge=0\n",
        )


class DropAvbTest(unittest.TestCase):
    FSTAB = (
        "system /system erofs ro wait,logical,first_stage_mount,slotselect,avb=vbmeta\n"
        "vendor /vendor erofs ro wait,logical,first_stage_mount,slotselect\n"
        "product /product erofs ro wait,logical,first_stage_mount,slotselect,avb=vbmeta\n"
        "/dev/block/by-name/boot /boot emmc defaults slotselect,avb=boot,first_stage_mount\n"
    )

    def test_removes_avb_only_from_named_mounts(self):
        out = drop_avb(self.FSTAB, {"system"})
        self.assertIn("system /system erofs ro wait,logical,first_stage_mount,slotselect\n", out)
        self.assertIn("product /product erofs ro wait,logical,first_stage_mount,slotselect,avb=vbmeta\n", out)
        self.assertIn("slotselect,avb=boot,first_stage_mount\n", out)

    def test_keeps_other_lines_untouched(self):
        self.assertEqual(drop_avb(self.FSTAB, set()), self.FSTAB)

    def test_does_not_match_by_prefix(self):
        line = "system_dlkm /system_dlkm erofs ro wait,logical,slotselect,avb=vbmeta\n"
        self.assertEqual(drop_avb(line, {"system"}), line)


class DisableServiceTest(unittest.TestCase):
    RC = (
        "on post-fs-data\n"
        "    mkdir /data/misc/x 0750 system system\n\n"
        "service timberslide /system/bin/timberslide --period-ms 4000\n"
        "   user system\n"
        "   class core\n"
    )

    def test_adds_disabled_right_after_service_line(self):
        out = disable_service(self.RC)
        self.assertIn("service timberslide /system/bin/timberslide --period-ms 4000\n    disabled\n   user system\n", out)

    def test_keeps_actions_untouched(self):
        self.assertTrue(disable_service(self.RC).startswith("on post-fs-data\n    mkdir /data/misc/x 0750 system system\n"))

    def test_is_idempotent(self):
        once = disable_service(self.RC)
        self.assertEqual(disable_service(once), once)

    def test_disables_every_service_in_file(self):
        rc = "service a /bin/a\n    user x\nservice b /bin/b\n    user y\n"
        self.assertEqual(
            disable_service(rc),
            "service a /bin/a\n    disabled\n    user x\nservice b /bin/b\n    disabled\n    user y\n",
        )


class FakeBatteryRcTest(unittest.TestCase):
    def test_runs_as_shell_oneshot(self):
        for line in (b"    user shell\n", b"    oneshot\n", b"    disabled\n"):
            self.assertIn(line, FAKE_BATTERY_RC)

    def test_reapplies_after_system_server_restart(self):
        # The fake values live in BatteryService (system_server). When system_server dies, init
        # restarts zygote while sys.boot_completed stays 1, so a boot-only trigger never fires again.
        self.assertIn(
            b"on property:sys.boot_completed=1 && property:init.svc.zygote=running\n"
            b"    start vm-fake-battery\n",
            FAKE_BATTERY_RC,
        )

    def test_waits_long_enough_for_a_slow_battery_service(self):
        self.assertIn(b"for i in $$(seq 1 60); do", FAKE_BATTERY_RC)
        self.assertIn(b"sleep 5", FAKE_BATTERY_RC)


class PointerBridgeRcTest(unittest.TestCase):
    def test_runs_the_vendor_binary_as_a_late_start_service(self):
        self.assertIn(b"service vm-pointer-bridge /vendor/bin/vm-pointer-bridge\n", POINTER_BRIDGE_RC)
        self.assertIn(b"    class late_start\n", POINTER_BRIDGE_RC)

    def test_can_read_input_and_create_uinput_devices(self):
        self.assertIn(b"    user root\n", POINTER_BRIDGE_RC)
        self.assertIn(b"    group root input uhid\n", POINTER_BRIDGE_RC)


if __name__ == "__main__":
    unittest.main()
