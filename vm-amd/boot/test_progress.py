import io
import unittest

from progress import find_phase, follow_phases


class FindPhaseTest(unittest.TestCase):
    def test_kernel_start(self):
        line = "[    0.000000][    T0] Linux version 6.18.21-android17-5-g3938c8d97142-ab16302624 (x@y)"
        self.assertEqual(find_phase(line), "kernel started")

    def test_first_android_log_line_means_userspace_is_up(self):
        line = "11-19 18:16:20.449   295   303 W libprocessgroup: A JoinCgroup action"
        self.assertEqual(find_phase(line), "Android userspace running")

    def test_boot_animation_and_completion(self):
        self.assertEqual(find_phase("11-19 18:16:36.166   570   570 I SurfaceFlinger: Enter boot animation"),
                         "boot animation on screen")
        self.assertEqual(find_phase("11-19 18:17:22.474   532   698 I SurfaceFlinger: Boot is finished (78306 ms)"),
                         "boot completed")

    def test_reboot_and_panic(self):
        self.assertEqual(find_phase("[  420.1][ T1] reboot: Restarting system"), "guest is rebooting")
        self.assertEqual(find_phase("[    3.2][ T1] Kernel panic - not syncing: VFS"), "KERNEL PANIC (see the serial log)")

    def test_other_lines_are_not_phases(self):
        self.assertIsNone(find_phase("[    0.311443][    T1] kvm_intel: VMX not supported by CPU 0"))
        self.assertIsNone(find_phase("partial line without newline"))


class FollowPhasesTest(unittest.TestCase):
    def test_reports_each_phase_once_in_order(self):
        log = io.StringIO(
            "[    0.000000][    T0] Linux version 6.18\n"
            "11-19 18:16:20.449   295   303 W libprocessgroup: x\n"
            "11-19 18:16:21.000   296   303 E other: y\n"
            "11-19 18:16:36.166   570   570 I SurfaceFlinger: Enter boot animation\n"
            "11-19 18:17:22.474   532   698 I SurfaceFlinger: Boot is finished (78306 ms)\n"
        )
        self.assertEqual(list(follow_phases(log)), ["kernel started", "Android userspace running",
                                                    "boot animation on screen", "boot completed"])

    def test_a_reboot_starts_the_phases_over(self):
        log = io.StringIO(
            "[    0.0][ T0] Linux version 6.18\n"
            "[  420.1][ T1] reboot: Restarting system\n"
            "[    0.0][ T0] Linux version 6.18\n"
        )
        self.assertEqual(list(follow_phases(log)), ["kernel started", "guest is rebooting", "kernel started"])


if __name__ == "__main__":
    unittest.main()
