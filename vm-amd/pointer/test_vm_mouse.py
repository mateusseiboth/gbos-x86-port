import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = shutil.which("cc")
SLAM = 32000  # vm_mouse.c: beyond any display, Android clamps the cursor to the edge
EDGE = 64


@unittest.skipUnless(CC, "needs a host C compiler")
class TrackerTest(unittest.TestCase):
    """vm-mouse --track W H: stdin "x y" pixel targets -> stdout "dx dy" relative moves."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.exe = cls.tmp / "vm-mouse"
        subprocess.run([CC, "-O2", "-Wall", "-Wextra", "-Werror", "-DVM_MOUSE_HOST_TEST",
                        "-o", str(cls.exe), str(HERE / "vm_mouse.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def track(self, w, h, points):
        stdin = "".join(f"{x} {y}\n" for x, y in points)
        out = subprocess.run([str(self.exe), "--track", str(w), str(h)], input=stdin, capture_output=True,
                             text=True, check=True).stdout
        return [tuple(map(int, line.split())) for line in out.splitlines()]

    def test_first_move_parks_the_cursor_in_the_corner_then_goes_to_the_target(self):
        self.assertEqual(self.track(1920, 1080, [(500, 300)]), [(-SLAM, -SLAM), (500, 300)])

    def test_later_moves_are_deltas(self):
        self.assertEqual(self.track(1920, 1080, [(500, 300), (510, 290)])[2:], [(10, -10)])

    def test_no_motion_no_move(self):
        self.assertEqual(self.track(1920, 1080, [(500, 300), (500, 300)])[2:], [])

    def test_touching_an_edge_overshoots_so_android_clamps_and_both_realign(self):
        moves = self.track(1920, 1080, [(500, 300), (0, 300), (1919, 1079)])[2:]
        self.assertEqual(moves, [(-500 - EDGE, 0), (1919 + EDGE, 779 + EDGE)])


@unittest.skipUnless(CC, "needs a host C compiler")
class ScaleTest(unittest.TestCase):
    def test_tablet_range_maps_to_display_pixels(self):
        exe = Path(tempfile.mkdtemp()) / "vm-mouse"
        subprocess.run([CC, "-O2", "-DVM_MOUSE_HOST_TEST", "-o", str(exe), str(HERE / "vm_mouse.c")], check=True)
        out = subprocess.run([str(exe), "--scale", "0", "32767", "1920"], input="0\n16384\n32767\n",
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out, ["0", "959", "1919"])
        shutil.rmtree(exe.parent)


if __name__ == "__main__":
    unittest.main()
