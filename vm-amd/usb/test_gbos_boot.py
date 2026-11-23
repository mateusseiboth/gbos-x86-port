import hashlib
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = shutil.which("cc")
MAGIC = 0x42414342


def control(slots, nb_slot=2):
    """bootloader_control bytes; slots = [(priority, tries, successful), ...]."""
    info = b"".join(bytes([(p & 0xF) | ((t & 0x7) << 4) | (int(s) << 7), 0]) for p, t, s in slots).ljust(8, b"\0")
    body = b"_a\0\0" + struct.pack("<I", MAGIC) + bytes([1, nb_slot & 0x7, 0, 0]) + info + b"\0" * 8
    return body + struct.pack("<I", zlib.crc32(body))


def vbmeta(auth, aux, fill):
    header = b"AVB0" + b"\0" * 8 + struct.pack(">QQ", auth, aux)
    return header.ljust(256, b"\0") + bytes([fill]) * (auth + aux)


def make_disk(path, ctrl, vbmeta_a, vbmeta_b):
    """GPT disk with misc, vbmeta_a, vbmeta_b and the gbos_layer marker partition."""
    with open(path, "wb") as f:
        f.truncate(8 * 1024 * 1024)
    subprocess.run(["sgdisk", "-o", "-a", "1",
                    "-n", "1:2048:4095", "-c", "1:misc",
                    "-n", "2:4096:4223", "-c", "2:vbmeta_a",
                    "-n", "3:4224:4351", "-c", "3:vbmeta_b",
                    "-n", "4:4352:6143", "-c", "4:gbos_layer", str(path)], check=True, capture_output=True)
    with open(path, "r+b") as f:
        f.seek(2048 * 512 + 2048)
        f.write(ctrl)
        f.seek(4096 * 512)
        f.write(vbmeta_a)
        f.seek(4224 * 512)
        f.write(vbmeta_b)


@unittest.skipUnless(CC and shutil.which("sgdisk"), "needs cc and sgdisk")
class GbosBootPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.exe = cls.tmp / "gbos-boot"
        subprocess.run([CC, "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.exe),
                        str(HERE / "gbos_boot.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def plan(self, disk, *flags):
        out = subprocess.run([str(self.exe), "--plan", str(disk), *flags], capture_output=True, text=True,
                             check=True).stdout
        return dict(line.split("=", 1) for line in out.split())

    def test_picks_the_highest_priority_bootable_slot_and_its_vbmeta(self):
        disk = self.tmp / "d1.img"
        va, vb = vbmeta(64, 32, 0x11), vbmeta(128, 16, 0x22)
        make_disk(disk, control([(14, 0, True), (15, 0, True)]), va, vb)
        self.assertEqual(self.plan(disk), {
            "SLOT": "_b", "VBMETA_SIZE": str(256 + 128 + 16),
            "VBMETA_DIGEST": hashlib.sha256(vb).hexdigest()})

    def test_spends_a_try_on_an_unverified_slot_only_when_asked_to_write(self):
        disk = self.tmp / "d2.img"
        make_disk(disk, control([(14, 0, True), (15, 3, False)]), vbmeta(1, 1, 1), vbmeta(1, 1, 2))
        self.assertEqual(self.plan(disk)["SLOT"], "_b")
        self.assertEqual(disk.read_bytes()[2048 * 512 + 2048:][:32], control([(14, 0, True), (15, 3, False)]))
        self.plan(disk, "--write")
        self.assertEqual(disk.read_bytes()[2048 * 512 + 2048:][:32], control([(14, 0, True), (15, 2, False)]))

    def test_a_slot_out_of_tries_falls_back_to_the_other(self):
        disk = self.tmp / "d3.img"
        make_disk(disk, control([(14, 0, True), (15, 0, False)]), vbmeta(1, 1, 1), vbmeta(1, 1, 2))
        self.assertEqual(self.plan(disk)["SLOT"], "_a")

    def test_missing_or_corrupt_control_boots_slot_a(self):
        disk = self.tmp / "d4.img"
        make_disk(disk, b"\0" * 32, vbmeta(1, 1, 1), vbmeta(1, 1, 2))
        self.assertEqual(self.plan(disk)["SLOT"], "_a")

    def boot_device(self, syspath):
        return subprocess.run([str(self.exe), "--boot-device", syspath], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_boot_device_is_the_root_port_above_the_disk(self):
        # androidboot.boot_devices: first stage (init/devices.cpp FindPciDevicePrefix) links
        # /dev/block/by-name for disks whose path starts with pciDDDD:BB/<first PCI device>; anything
        # deeper (a controller behind a bridge, as on Ryzen) is not compared.
        self.assertEqual(self.boot_device(
            "/sys/devices/pci0000:00/0000:00:08.1/0000:06:00.3/usb4/4-1/4-1:1.0/host6/"
            "target6:0:0/6:0:0:0/block/sdc"), "pci0000:00/0000:00:08.1")

    def test_boot_device_of_a_controller_on_the_root_bus(self):
        self.assertEqual(self.boot_device(
            "/sys/devices/pci0000:00/0000:00:04.0/usb2/2-1/2-1:1.0/host0/target0:0:0/0:0:0:0/block/sda"),
            "pci0000:00/0000:00:04.0")


def pci(root, slot, vendor, cls):
    d = root / slot
    d.mkdir(parents=True)
    (d / "vendor").write_text(f"0x{vendor:04x}\n")
    (d / "class").write_text(f"0x{cls:06x}\n")


@unittest.skipUnless(CC, "needs cc")
class GpuDetectionTest(unittest.TestCase):
    # androidboot.gbos.gpu picks the layer's graphics stack (/odm/etc/gbos/gpu_<stack>.prop).
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.exe = cls.tmp / "gbos-boot"
        subprocess.run([CC, "-O2", "-o", str(cls.exe), str(HERE / "gbos_boot.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def gpu(self, devices):
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        for slot, vendor, cls in devices:
            pci(root, slot, vendor, cls)
        return subprocess.run([str(self.exe), "--gpu", str(root)], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_amd_apu(self):
        self.assertEqual(self.gpu([("0000:00:00.0", 0x1022, 0x060000), ("0000:0a:00.0", 0x1002, 0x030000)]), "amd")

    def test_intel_wins_over_a_discrete_nvidia(self):
        self.assertEqual(self.gpu([("0000:01:00.0", 0x10de, 0x030200), ("0000:00:02.0", 0x8086, 0x030000)]),
                         "intel")

    def test_qemu_virtio_gpu(self):
        self.assertEqual(self.gpu([("0000:00:01.0", 0x1af4, 0x038000)]), "virgl")

    def test_anything_else_renders_in_software(self):
        self.assertEqual(self.gpu([("0000:01:00.0", 0x10de, 0x030000)]), "swiftshader")
        self.assertEqual(self.gpu([("0000:00:1f.0", 0x8086, 0x060100)]), "swiftshader")


@unittest.skipUnless(CC, "needs cc")
class BatteryDetectionTest(unittest.TestCase):
    # A desktop has no battery: Android then reads 0 % and setup refuses to go on, so the layer loads
    # a fake full battery on AC (test_power) on androidboot.gbos.battery=none.
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.exe = cls.tmp / "gbos-boot"
        subprocess.run([CC, "-O2", "-o", str(cls.exe), str(HERE / "gbos_boot.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def battery(self, supplies):
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        for name, kind, *scope in supplies:
            (root / name).mkdir()
            (root / name / "type").write_text(kind + "\n")
            if scope:
                (root / name / "scope").write_text(scope[0] + "\n")
        return subprocess.run([str(self.exe), "--battery", str(root)], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_a_desktop_has_none(self):
        self.assertEqual(self.battery([]), "none")
        # A wireless mouse's battery is a Battery of scope Device, not the system's.
        self.assertEqual(self.battery([("ADP1", "Mains"), ("hidpp_battery_0", "Battery", "Device")]), "none")

    def test_a_laptop_has_one(self):
        self.assertEqual(self.battery([("AC", "Mains"), ("BAT0", "Battery")]), "present")


if __name__ == "__main__":
    unittest.main()
