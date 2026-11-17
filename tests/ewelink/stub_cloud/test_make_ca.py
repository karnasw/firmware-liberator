"""make_ca.py, the stub's certificate authority and server certificate."""

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

STUB = Path(__file__).resolve().parents[3] / "tools" / "ewelink" / "stub_cloud"


class RunIsolatedTests(unittest.TestCase):
    """Run as emu.sh runs it: in isolated mode (-I), where only its own path
    puts the repository on sys.path."""

    def test_it_imports_the_repository(self):
        done = subprocess.run([sys.executable, "-I", str(STUB / "make_ca.py"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the cryptography package")
class RsaCaTests(unittest.TestCase):
    def test_an_rsa_ca_is_eWeLinks_size_fits_the_slot_and_can_be_trusted(self):
        from cryptography import x509
        from tools.ewelink.stub_cloud import make_ca
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(make_ca.main(["--out-dir", folder, "--rsa"]), 0)
            pem = (out / "ca.crt").read_bytes()
            self.assertLess(len(pem), make_ca.SLOT_BYTES)
            self.assertEqual(x509.load_pem_x509_certificate(pem).public_key().key_size, make_ca.RSA_CA_BITS)
            leaf = x509.load_pem_x509_certificate((out / "server.crt").read_bytes())
            leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(pem))
            self.assertIn("SECLEVEL=1", (out / "openssl.cnf").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
