import unittest

from collect_firmware import find_missing, strip_compression


class StripCompressionTest(unittest.TestCase):
    def test_removes_xz_and_zst(self):
        self.assertEqual(strip_compression("amdgpu/navi10_pfp.bin.xz"), "amdgpu/navi10_pfp.bin")
        self.assertEqual(strip_compression("iwlwifi-so-a0-gf-a0-89.ucode.zst"), "iwlwifi-so-a0-gf-a0-89.ucode")

    def test_keeps_uncompressed_names(self):
        self.assertEqual(strip_compression("rtw88/rtw8822c_fw.bin"), "rtw88/rtw8822c_fw.bin")

    def test_only_strips_a_real_suffix(self):
        self.assertEqual(strip_compression("vendor/xzfile.bin"), "vendor/xzfile.bin")


class FindMissingTest(unittest.TestCase):
    AVAILABLE = {"amdgpu/navi10_pfp.bin", "rtw89/rtw8852b_fw-1.bin", "iwlwifi-cc-a0-77.ucode"}

    def test_reports_only_declared_names_that_are_absent(self):
        declared = {"amdgpu/navi10_pfp.bin", "amdgpu/navi10_me.bin"}
        self.assertEqual(find_missing(declared, self.AVAILABLE), {"amdgpu/navi10_me.bin"})

    def test_wildcards_match_any_available_file(self):
        self.assertEqual(find_missing({"rtw89/rtw8852b_fw*.bin"}, self.AVAILABLE), set())
        self.assertEqual(find_missing({"rtw89/rtw8922a_fw*.bin"}, self.AVAILABLE), {"rtw89/rtw8922a_fw*.bin"})

    def test_nothing_declared_means_nothing_missing(self):
        self.assertEqual(find_missing(set(), self.AVAILABLE), set())


if __name__ == "__main__":
    unittest.main()
