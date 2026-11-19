import unittest

from initrd import LAYER_ENTRIES, add_layer_entries

STOCK = (
    "system /system erofs ro wait,logical,first_stage_mount,slotselect,avb=vbmeta\n"
    "vendor /vendor erofs ro wait,logical,first_stage_mount,slotselect,avb=vbmeta\n"
    "/dev/block/by-name/metadata /metadata ext4 noatime wait,formattable,first_stage_mount,check\n"
)


class AddLayerEntriesTest(unittest.TestCase):
    def test_appends_layer_mount_and_overlays(self):
        out = add_layer_entries(STOCK)
        self.assertTrue(out.startswith(STOCK))
        self.assertIn("/dev/block/by-name/gbos_layer /mnt/vendor/gbos erofs ro wait,first_stage_mount\n", out)
        self.assertIn("overlay /vendor overlay ro,lowerdir=/mnt/vendor/gbos/vendor:/vendor first_stage_mount\n", out)
        self.assertIn("overlay /odm overlay ro,lowerdir=/mnt/vendor/gbos/odm:/odm first_stage_mount\n", out)

    def test_keeps_verity_on_stock_partitions(self):
        out = add_layer_entries(STOCK)
        self.assertIn("slotselect,avb=vbmeta\nvendor", out)

    def test_is_idempotent(self):
        once = add_layer_entries(STOCK)
        self.assertEqual(add_layer_entries(once), once)

    def test_layer_is_mounted_before_the_overlays(self):
        lines = LAYER_ENTRIES.splitlines()
        self.assertTrue(lines[0].startswith("/dev/block/by-name/gbos_layer "))


if __name__ == "__main__":
    unittest.main()
