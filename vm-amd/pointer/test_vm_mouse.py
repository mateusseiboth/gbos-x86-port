import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = shutil.which("cc")
SLAM = 32000  # vm_mouse.c: beyond any display, Android clamps the cursor to the edge


@unittest.skipUnless(CC, "needs a host C compiler")
class VmMouseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.exe = cls.tmp / "vm-mouse"
        subprocess.run([CC, "-O2", "-Wall", "-Wextra", "-Werror", "-DVM_MOUSE_HOST_TEST",
                        "-o", str(cls.exe), str(HERE / "vm_mouse.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_mode(self, args, stdin):
        return subprocess.run([str(self.exe), *args], input=stdin, capture_output=True, text=True,
                              check=True).stdout.split("\n")[:-1]

    def test_tablet_range_maps_to_display_pixels(self):
        self.assertEqual(self.run_mode(["--scale", "0", "32767", "1920"], "0\n16384\n32767\n"),
                         ["0", "959", "1919"])

    def test_wheel_cursor_is_parked_then_moved_under_the_pointer(self):
        # Scroll goes where Android's mouse cursor is: corner first (clamped), then the exact pixel,
        # so no drift can build up between scrolls.
        self.assertEqual(self.run_mode(["--wheel-moves", "500", "300"], ""), [f"{-SLAM} {-SLAM}", "500 300"])

    def test_right_click_becomes_a_long_press(self):
        # A quick right click still holds the touch long enough for Android's long-press (context menu).
        self.assertEqual(self.run_mode(["--release-at", "1000", "1100"], ""), ["1650"])
        self.assertEqual(self.run_mode(["--release-at", "1000", "2000"], ""), ["2000"])


if __name__ == "__main__":
    unittest.main()
