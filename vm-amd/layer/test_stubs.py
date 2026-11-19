import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

STUBS = Path(__file__).resolve().parent / "stubs"
CC = shutil.which("cc")


@unittest.skipUnless(CC, "needs a host C compiler")
class GscutilStubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.real = self.tmp / "gscutil.real"
        self.real.write_text('#!/bin/sh\necho "real $*"\nexit 76\n')
        self.real.chmod(0o755)
        self.stub = self.tmp / "gscutil"
        subprocess.run([CC, f'-DGSCUTIL_REAL="{self.real}"', "-o", str(self.stub), str(STUBS / "gscutil.c")],
                       check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_stub(self, *args):
        return subprocess.run([str(self.stub), *args], capture_output=True, text=True)

    def test_post_install_firmware_update_reports_success(self):
        # OTA postinstall: there is no GSC to flash, and its failure made update_engine reject the update.
        r = self.run_stub("perform-post-install", "--fw-zip-mp", "/postinstall/x.zip", "--metadata", "/m")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("real", r.stdout)

    def test_every_other_command_runs_the_real_gscutil(self):
        r = self.run_stub("hwsec-ownership-id")
        self.assertEqual((r.returncode, r.stdout), (76, "real hwsec-ownership-id\n"))


@unittest.skipUnless(CC, "needs a host C compiler")
class GscutilStubLoopTest(unittest.TestCase):
    def test_a_wrapper_that_would_exec_itself_fails_at_once(self):
        # If the kept copy is the wrapper itself (mount propagation), exec'ing it loops forever and
        # init waits on it; fail instead.
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "gscutil"
            subprocess.run([CC, f'-DGSCUTIL_REAL="{stub}"', "-o", str(stub), str(STUBS / "gscutil.c")], check=True)
            r = subprocess.run([str(stub), "perform-os-startup"], capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 127)
        self.assertIn("is this wrapper", r.stderr)


if __name__ == "__main__":
    unittest.main()
