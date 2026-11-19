import struct
import unittest
import zlib

from slot import BootControl, choose_slot, parse_control, parse_qemu_io_hexdump, serialize_control, vbmeta_size


def make_control(slots, nb_slot=2):
    """slots: [(priority, tries, successful), ...] in slot order."""
    info = b"".join(
        bytes([(p & 0xF) | ((t & 0x7) << 4) | ((1 if s else 0) << 7), 0]) for p, t, s in slots
    )
    info = info.ljust(8, b"\0")
    body = b"_a\0\0" + struct.pack("<I", 0x42414342) + bytes([1, nb_slot & 0x7, 0, 0]) + info + b"\0" * 8
    return body + struct.pack("<I", zlib.crc32(body))


class ParseControlTest(unittest.TestCase):
    def test_reads_slot_metadata(self):
        ctrl = parse_control(make_control([(15, 0, True), (14, 7, False)]))
        self.assertEqual(ctrl.nb_slot, 2)
        self.assertEqual(ctrl.slots[0], (15, 0, True))
        self.assertEqual(ctrl.slots[1], (14, 7, False))

    def test_rejects_wrong_magic(self):
        raw = bytearray(make_control([(15, 0, True), (14, 7, False)]))
        raw[4] = 0
        self.assertIsNone(parse_control(bytes(raw)))

    def test_rejects_bad_crc(self):
        raw = bytearray(make_control([(15, 0, True), (14, 7, False)]))
        raw[12] ^= 1
        self.assertIsNone(parse_control(bytes(raw)))

    def test_round_trips_through_serialize(self):
        raw = make_control([(15, 0, True), (14, 7, False)])
        self.assertEqual(serialize_control(parse_control(raw), raw), raw)


class ChooseSlotTest(unittest.TestCase):
    def test_no_control_block_boots_slot_a(self):
        self.assertEqual(choose_slot(None), (0, None))

    def test_successful_highest_priority_slot_boots_without_changes(self):
        ctrl = parse_control(make_control([(15, 0, True), (14, 0, True)]))
        self.assertEqual(choose_slot(ctrl), (0, None))

    def test_updated_slot_boots_and_spends_a_try(self):
        # After an OTA update_engine gives the new slot the top priority and some tries.
        ctrl = parse_control(make_control([(14, 0, True), (15, 7, False)]))
        index, updated = choose_slot(ctrl)
        self.assertEqual(index, 1)
        self.assertEqual(updated.slots[1], (15, 6, False))
        self.assertEqual(updated.slots[0], (14, 0, True))

    def test_slot_out_of_tries_falls_back(self):
        ctrl = parse_control(make_control([(14, 0, True), (15, 0, False)]))
        self.assertEqual(choose_slot(ctrl), (0, None))

    def test_zero_priority_slot_is_never_chosen(self):
        ctrl = parse_control(make_control([(0, 7, True), (15, 0, True)]))
        self.assertEqual(choose_slot(ctrl), (1, None))

    def test_nothing_bootable_falls_back_to_slot_a(self):
        ctrl = parse_control(make_control([(0, 0, False), (0, 0, False)]))
        self.assertEqual(choose_slot(ctrl), (0, None))


class BootControlTest(unittest.TestCase):
    def test_suffix_names(self):
        self.assertEqual(BootControl.suffix(0), "_a")
        self.assertEqual(BootControl.suffix(1), "_b")


class VbmetaSizeTest(unittest.TestCase):
    def test_header_plus_auth_and_aux_blocks(self):
        header = b"AVB0" + struct.pack(">II", 1, 0) + struct.pack(">QQ", 576, 5888) + b"\0" * 228
        self.assertEqual(vbmeta_size(header), 256 + 576 + 5888)

    def test_rejects_non_vbmeta(self):
        with self.assertRaises(ValueError):
            vbmeta_size(b"\0" * 256)


class QemuIoHexdumpTest(unittest.TestCase):
    def test_parses_hex_columns_and_ignores_the_ascii_column(self):
        out = (
            "00000800:  5f 61 00 00 42 43 41 42 01 02 00 00 ff 00 0e 87  _a..BCAB........\n"
            "00000810:  00 00 00 00 00 00 00 00 00 00 00 00 12 34 56 78  .............4Vx\n"
            "read 32/32 bytes at offset 0x800\n"
        )
        data = parse_qemu_io_hexdump(out)
        self.assertEqual(len(data), 32)
        self.assertEqual(data[:8], b"_a\0\0BCAB")
        self.assertEqual(data[-4:], bytes.fromhex("12345678"))


class QemuIoLargeOffsetTest(unittest.TestCase):
    def test_offsets_past_4gib_have_more_than_eight_hex_digits(self):
        out = "35ec00800:  5f 61 00 00 42 43 41 42 01 02 00 00 ff 00 0e 87  _a..BCAB........\n"
        self.assertEqual(parse_qemu_io_hexdump(out)[:4], b"_a\0\0")


if __name__ == "__main__":
    unittest.main()
