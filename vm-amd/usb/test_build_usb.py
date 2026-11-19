import unittest

from build_usb import ESP_TYPE, Part, android_cmdline, layout

MIB = 2048  # sectors


def stock(name, start, sectors, ptype="T", guid="G", attrs=0):
    return Part(name=name, type=ptype, guid=guid, attrs=attrs, start=start, sectors=sectors)


class LayoutTest(unittest.TestCase):
    STOCK = [
        stock("ota_recovery_a", 4096, 262144),
        stock("KERN-C", 266240, 1, attrs=1),
        stock("misc", 28270592, 8192),
        stock("userdata", 70803456, 134217728),
        stock("super", 1458176, 25165824, guid="SUPER"),
    ]

    def plan(self, disk_sectors=60_000_000):
        return layout(self.STOCK, esp_sectors=512 * MIB // 2, layer_sectors=1600 * MIB, disk_sectors=disk_sectors)

    def test_esp_first_then_stock_in_original_order_then_layer_then_userdata(self):
        names = [p.name for p in self.plan()]
        self.assertEqual(names, ["GBOS-EFI", "ota_recovery_a", "KERN-C", "super", "misc", "gbos_layer", "userdata"])

    def test_partitions_are_packed_on_mib_boundaries_without_overlap(self):
        parts = self.plan()
        self.assertEqual(parts[0].start, MIB)
        for a, b in zip(parts, parts[1:]):
            self.assertEqual(b.start % MIB, 0)
            self.assertGreaterEqual(b.start, a.start + a.sectors)

    def test_stock_identity_and_source_are_kept(self):
        sup = next(p for p in self.plan() if p.name == "super")
        self.assertEqual((sup.guid, sup.sectors, sup.source), ("SUPER", 25165824, 1458176))
        kern_c = next(p for p in self.plan() if p.name == "KERN-C")
        self.assertEqual(kern_c.attrs, 1)

    def test_userdata_takes_the_rest_and_is_created_empty(self):
        parts = self.plan()
        data = parts[-1]
        self.assertEqual(data.source, None)
        self.assertLessEqual(data.start + data.sectors, 60_000_000 - 33)  # backup GPT
        self.assertGreater(data.start + data.sectors, 60_000_000 - 33 - MIB)

    def test_the_esp_has_the_efi_system_type(self):
        self.assertEqual(self.plan()[0].type, ESP_TYPE)

    def test_a_stick_too_small_for_a_usable_userdata_is_refused(self):
        with self.assertRaises(ValueError):
            self.plan(disk_sectors=27_000_000)


class AndroidCmdlineTest(unittest.TestCase):
    def test_shared_arguments_with_the_screen_as_console(self):
        # The last console= is /dev/console (the screen); ttyS0 is for QEMU tests and does nothing elsewhere.
        # boot/cmdline.txt is shared with run_vm.sh, which adds the serial console instead.
        self.assertEqual(android_cmdline("panic=0\nbinder.impl=rust # comment\n# whole line\n"),
                         "console=ttyS0,115200 console=tty0 panic=0 binder.impl=rust")


if __name__ == "__main__":
    unittest.main()
