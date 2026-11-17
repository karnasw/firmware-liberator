"""cloud_push.py, a whole conversion of the emulated device through the stub."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fota_wrap as fota
from tests.beken.test_rbl import rbl_header


class CloudPushTests(unittest.TestCase):
    RBL = rbl_header(version=b"1.18.319") + bytes(range(256)) * 4

    def test_it_announces_the_version_the_device_takes(self):
        sent = []

        class Session:
            def __init__(self, *args, **kwargs):
                self.stub = SimpleNamespace(heard=lambda message: None)
                self.stublog = []

            def run(self, secs, until=None):
                self.stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up"})
                self.stub.heard({"action": "update", "deviceid": "1000abcdef"})

            def flash(self):
                return bytes(0x400000)

            def online(self):
                return True, True, True, True

        online = SimpleNamespace(SSID=b"net", PW=b"password", DISPATCH_IP="192.0.2.1", DUMP="dump.bin",
                                 CloudSession=Session,
                                 ss=SimpleNamespace(send_to_device=lambda device, command: sent.append(command) or True))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"cloud_online": online}):
            sys.modules.pop("cloud_push", None)
            import cloud_push
            image, dump = Path(tmp, "image.rbl"), Path(tmp, "dump.bin")
            image.write_bytes(self.RBL[:96].replace(b"1.18.319", b"1.0.0\0\0\0") + bytes(256))
            dump.write_bytes(bytes(0x400000))
            with mock.patch.object(cloud_push.os, "_exit", side_effect=SystemExit), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cloud_push.main([str(image), str(Path(tmp, "out.bin")), "--dump", str(dump)])
            sys.modules.pop("cloud_push", None)
        self.assertEqual(sent[0]["params"]["version"], fota.ANNOUNCED_VERSION)


if __name__ == "__main__":
    unittest.main()
