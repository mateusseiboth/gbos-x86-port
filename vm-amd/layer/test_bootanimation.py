import io
import unittest
import zipfile

from PIL import Image

from bootanimation import build_bootanimation


def png(w, h, color=(255, 255, 255)):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def stock_zip():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("desc.txt", "100 50 30\nc 1 0 part0")
        z.writestr("part0/a.png", png(40, 50, (0, 0, 0)))
        z.writestr("part0/b.png", png(40, 50))
        z.writestr("part0/trim.txt", "40x50+30+0\n40x50+31+0\n")
    return out.getvalue()


class BuildBootanimationTest(unittest.TestCase):
    def setUp(self):
        self.zip = zipfile.ZipFile(io.BytesIO(build_bootanimation(stock_zip(), "part0/b.png", "Starting up")))

    def test_stock_parts_play_first_then_a_held_frame_loops_until_boot_completes(self):
        self.assertEqual(self.zip.read("desc.txt").decode(), "100 50 30\nc 1 0 part0\np 0 0 part1\n")
        self.assertEqual(self.zip.read("part0/b.png"), stock_zip_entry("part0/b.png"))

    def test_the_held_frame_keeps_the_source_frame_geometry(self):
        self.assertEqual(self.zip.read("part1/trim.txt").decode(), "40x50+31+0\n")
        self.assertEqual(Image.open(io.BytesIO(self.zip.read("part1/hold.png"))).size, (40, 50))

    def test_the_text_is_drawn_on_the_held_frame(self):
        held = Image.open(io.BytesIO(self.zip.read("part1/hold.png"))).convert("RGB")
        self.assertNotEqual(held.getextrema(), ((255, 255), (255, 255), (255, 255)))

    def test_every_entry_is_stored_uncompressed(self):
        # bootanimation maps frames straight out of the zip.
        self.assertEqual({i.compress_type for i in self.zip.infolist()}, {zipfile.ZIP_STORED})


def stock_zip_entry(name):
    return zipfile.ZipFile(io.BytesIO(stock_zip())).read(name)


if __name__ == "__main__":
    unittest.main()
