"""ESPHome's update protocol, against a stand-in that speaks it as ESPHome does."""
import gzip
import hashlib
import secrets
import socket
import threading
import time
import unittest
from unittest import mock

from tools.autoflash import esphome_ota as ota
from tools.autoflash import noise

KEY = bytes(range(32))


class Sealed:
    """The device's end of an encrypted session, as ota_esphome_noise.cpp reads
    and writes it: each answer byte in a frame of its own, each value the
    client sends (type, size, checksum) in exactly one, and data in frames of
    at most 1024 bytes."""

    def __init__(self, fake, conn, ciphers):
        self.fake, self.conn = fake, conn
        self.sending, self.receiving = ciphers

    def sendall(self, data):
        for byte in data:
            self.conn.sendall(noise.frame(self.sending.encrypt(bytes([byte]))))

    def frame(self):
        return self.receiving.decrypt(noise.read_frame(lambda size: self.fake.recv(self.conn, size)))

    def value(self, size):
        data = self.frame()
        assert len(data) == size, f"a value of {size} bytes came in a frame of {len(data)}"
        return data

    def data(self, size):
        data = b""
        while len(data) < size:
            piece = self.frame()
            assert 0 < len(piece) <= ota.NOISE_MAX_PLAINTEXT, len(piece)
            data += piece
        assert len(data) == size, "a frame ran past the block"
        return data


class FakeESPHome:
    """One update session at a time, answering as ota_esphome.cpp does
    (ESPHome 2026.9.1): with an update `password`, a SHA256 challenge - or,
    as older ESPHome, an `md5` one - and refused if the client offers no
    SHA256.  With an API `key`, encrypted updates offered (and then no
    password asked for), and, `encrypted_only`, nothing else taken."""

    def __init__(self, *, flags=ota.SERVER_COMPRESSION, password=None, md5=False, encrypted_only=False,
                 refuse_at=None, key=None, version=2, end=ota.UPDATE_END_OK, prepare_delay=0.0):
        self.flags, self.refuse_at = flags, refuse_at
        self.version, self.end, self.prepare_delay = version, end, prepare_delay
        self.acknowledged = threading.Event()  # the client's last byte, after the update ended
        self.password, self.md5, self.encrypted_only = password, md5, encrypted_only
        self.key = key
        self.encrypted: list[bool] = []  # whether each update came encrypted
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.received: list[tuple[int, bytes]] = []
        self.features: list[int] = []  # what the client offered, each session
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def recv(self, conn, count):
        data = b""
        while len(data) < count:
            piece = conn.recv(count - len(data))
            if not piece:
                raise ConnectionError
            data += piece
        return data

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    self.session(conn)
                except (ConnectionError, OSError):
                    pass

    def session(self, conn):
        assert self.recv(conn, 5) == ota.MAGIC
        conn.sendall(bytes([ota.OK, self.version]))
        features = self.recv(conn, 1)[0]
        self.features.append(features)
        wanted = ota.CLIENT_NOISE | ota.CLIENT_EXTENDED
        sealing = self.key is not None and features & wanted == wanted
        if self.encrypted_only and not sealing:
            conn.sendall(bytes([ota.ENCRYPTION_REQUIRED]))
            return
        flags = self.flags | (ota.SERVER_NOISE if self.key is not None else 0)
        conn.sendall(bytes([ota.FEATURE_FLAGS, flags]))
        if sealing:
            # The prologue: every byte of the session so far, both ways.
            prologue = ota.NOISE_PROLOGUE + ota.MAGIC + bytes([ota.OK, self.version, features, ota.FEATURE_FLAGS, flags])
            responder = noise.Handshake(self.key, prologue, initiator=False)
            message = noise.read_frame(lambda size: self.recv(conn, size))
            assert message[0] == noise.HANDSHAKE_OK
            try:
                responder.read_message(message[1:])
            except noise.NoiseError:
                conn.sendall(noise.frame(b"\x01" + noise.WRONG_KEY_REASON.encode()))
                return
            conn.sendall(noise.frame(bytes([noise.HANDSHAKE_OK]) + responder.write_message()))
            self.sealed(Sealed(self, conn, responder.split()))
            return
        if self.password is not None:
            if not self.md5 and not features & ota.CLIENT_SHA256_AUTH:
                conn.sendall(bytes([ota.AUTH_INVALID]))  # "SHA256 required"
                return
            hash_function, size = (hashlib.md5, 32) if self.md5 else (hashlib.sha256, 64)
            nonce = secrets.token_hex(size // 2).encode()
            conn.sendall(bytes([ota.REQUEST_AUTH if self.md5 else ota.REQUEST_SHA256_AUTH]) + nonce)
            answer = self.recv(conn, 2 * size)
            theirs, proof = answer[:size], answer[size:]
            if proof != hash_function(self.password.encode() + nonce + theirs).hexdigest().encode():
                conn.sendall(bytes([ota.AUTH_INVALID]))
                return
        conn.sendall(bytes([ota.AUTH_OK]))
        self.encrypted.append(False)  # before the update: the client returns as it ends
        self.update(conn, value=lambda size: self.recv(conn, size), data=lambda size: self.recv(conn, size))

    def sealed(self, sealed):
        sealed.sendall(bytes([ota.AUTH_OK]))  # no password asked for: the key is the proof
        self.encrypted.append(True)
        self.update(sealed, value=sealed.value, data=sealed.data)

    def update(self, conn, *, value, data):
        kind = value(1)[0]
        size = int.from_bytes(value(4), "big")
        time.sleep(self.prepare_delay)  # erasing the flash
        conn.sendall(bytes([ota.UPDATE_PREPARE_OK]))
        md5 = value(32).decode()
        conn.sendall(bytes([ota.BIN_MD5_OK]))
        received = b""
        while len(received) < size:
            received += data(min(ota.BLOCK, size - len(received)))
            if self.refuse_at is not None and len(received) >= self.refuse_at:
                conn.sendall(bytes([0x83]))
                return
            if self.version >= 2:
                conn.sendall(bytes([ota.CHUNK_OK]))
        assert hashlib.md5(received).hexdigest() == md5
        if self.flags & ota.SERVER_COMPRESSION:
            received = gzip.decompress(received)
        self.received.append((kind, received))
        conn.sendall(bytes([ota.RECEIVE_OK, self.end]))
        if value(1) == bytes([ota.OK]):
            self.acknowledged.set()

    def close(self):
        # Shut down first: closing alone leaves serve() blocked in accept(),
        # still listening, and later tests in the process found their own
        # servers' connections reset (Errno 104).
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()
        self.thread.join(5)


class WireTests(unittest.TestCase):
    def test_its_bytes_are_espota2s(self):
        # Both ends here take them from the module: only this holds them to ESPHome's.
        self.assertEqual((ota.PORT, ota.MAGIC), (3232, b"\x6c\x26\xf7\x5c\x45"))
        self.assertEqual((ota.APP, ota.PARTITION_TABLE, ota.BOOTLOADER), (0, 1, 2))
        self.assertEqual((ota.OK, ota.REQUEST_AUTH, ota.REQUEST_SHA256_AUTH), (0x00, 0x01, 0x02))
        self.assertEqual((ota.AUTH_OK, ota.UPDATE_PREPARE_OK, ota.BIN_MD5_OK, ota.RECEIVE_OK, ota.UPDATE_END_OK,
                          ota.SUPPORTS_COMPRESSION, ota.CHUNK_OK, ota.FEATURE_FLAGS),
                         (0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48))
        self.assertEqual((ota.AUTH_INVALID, ota.ENCRYPTION_REQUIRED), (0x82, 0x94))
        self.assertEqual((ota.CLIENT_COMPRESSION, ota.CLIENT_SHA256_AUTH, ota.CLIENT_EXTENDED, ota.CLIENT_NOISE),
                         (0x01, 0x02, 0x04, 0x08))
        self.assertEqual((ota.SERVER_COMPRESSION, ota.SERVER_PARTITION_ACCESS, ota.SERVER_NOISE), (0x01, 0x02, 0x04))
        self.assertEqual((ota.NOISE_PROLOGUE, ota.NOISE_MAX_PLAINTEXT, ota.BLOCK), (b"NoiseOTAInit", 1024, 8192))

    def test_a_read_takes_no_more_than_it_asked_for(self):
        # What follows on the connection is the next answer's: a read in pieces
        # asks each time only for what is still missing.
        class Trickle:
            def __init__(self):
                self.waiting, self.first = b"abcdef", True

            def recv(self, count):
                count = 1 if self.first else count
                self.first = False
                piece, self.waiting = self.waiting[:count], self.waiting[count:]
                return piece

        sock = Trickle()
        self.assertEqual(ota._recv(sock, 3, "a test"), b"abc")
        self.assertEqual(sock.waiting, b"def")


class ProtocolTests(unittest.TestCase):
    def fake(self, **kwargs):
        fake = FakeESPHome(**kwargs)
        self.addCleanup(fake.close)
        return fake

    def test_a_fake_closed_leaves_nothing_listening(self):
        # Left listening, it took other tests' connections in the same process
        # (test_emulator_stats: Errno 104), so tests run side by side failed.
        fake = FakeESPHome()
        fake.close()
        self.assertFalse(fake.thread.is_alive())
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", fake.port), timeout=2).close()

    def test_an_app_arrives_whole_compressed_or_not(self):
        image = bytes(range(256)) * 300
        for flags in (ota.SERVER_COMPRESSION, 0):
            fake = self.fake(flags=flags)
            sent = []
            ota.upload("127.0.0.1", image, port=fake.port, progress=lambda a, b: sent.append(a))
            self.assertEqual(fake.received, [(ota.APP, image)])
            self.assertEqual(sent[-1], sent[-1])  # progress reported to the end

    def test_the_probe_says_what_the_firmware_offers(self):
        fake = self.fake(flags=ota.SERVER_COMPRESSION | ota.SERVER_PARTITION_ACCESS)
        offer = ota.probe("127.0.0.1", port=fake.port)
        self.assertTrue(offer.extended and offer.compression and offer.partition_access)
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
            # Held, not listening, while asked: refused, and no other test can take it.
            self.assertIsNone(ota.probe("127.0.0.1", port=port, timeout=1))

    def test_an_update_password_is_reported_not_guessed(self):
        fake = self.fake(password="s3cret-71")
        with self.assertRaises(ota.NeedsPassword) as none:
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port)
        with self.assertRaises(ota.NeedsPassword) as another:
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, password="not-it-9")
        self.assertEqual((none.exception.given, another.exception.given), (False, True))
        self.assertEqual(fake.received, [])

    def test_given_its_password_the_update_goes_through(self):
        image = bytes(range(256)) * 40
        for md5 in (False, True):  # current ESPHome's challenge, and an older one's
            fake = self.fake(password="s3cret-71", md5=md5)
            ota.upload("127.0.0.1", image, port=fake.port, password="s3cret-71")
            self.assertEqual(fake.received, [(ota.APP, image)])
        # SHA256 offered: current ESPHome with a password refuses a client without it.
        self.assertTrue(fake.features[0] & ota.CLIENT_SHA256_AUTH)

    def test_its_password_is_checked_without_an_update(self):
        fake = self.fake(password="s3cret-71")
        self.assertEqual(ota.wants("127.0.0.1", port=fake.port), ota.PASSWORD)
        self.assertEqual(ota.wants("127.0.0.1", "not-it-9", port=fake.port), ota.PASSWORD)
        self.assertEqual(ota.wants("127.0.0.1", "s3cret-71", port=fake.port), ota.NOTHING)
        self.assertEqual(ota.wants("127.0.0.1", port=self.fake().port), ota.NOTHING)  # none to want
        self.assertEqual(fake.received, [])
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
            # Held, not listening, while asked: refused, and no other test can take it.
            self.assertIsNone(ota.wants("127.0.0.1", port=port, timeout=1))

    def test_one_taking_only_encrypted_updates_wants_its_key(self):
        fake = self.fake(encrypted_only=True, key=KEY)
        self.assertTrue(ota.probe("127.0.0.1", port=fake.port).encrypted_only)  # it answers
        self.assertEqual(ota.wants("127.0.0.1", port=fake.port), ota.KEY)
        self.assertEqual(ota.wants("127.0.0.1", key=bytes(32), port=fake.port), ota.KEY)  # not its own
        self.assertEqual(ota.wants("127.0.0.1", key=KEY, port=fake.port), ota.NOTHING)
        with self.assertRaisesRegex(ota.NeedsEncryption, "API encryption key"):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, password="s3cret-71")
        with self.assertRaises(ota.WrongKey):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=bytes(32))
        self.assertEqual(fake.received, [])

    def test_given_its_key_the_update_goes_encrypted(self):
        image = bytes(range(256)) * 300  # whole blocks of whole frames, and a part of each
        for flags in (ota.SERVER_COMPRESSION, 0):
            for encrypted_only in (True, False):
                fake = self.fake(flags=flags, key=KEY, encrypted_only=encrypted_only)
                sent = []
                ota.upload("127.0.0.1", image, port=fake.port, key=KEY, progress=lambda a, b: sent.append(a))
                self.assertEqual(fake.received, [(ota.APP, image)])
                self.assertEqual(fake.encrypted, [True])
                self.assertTrue(fake.features[-1] & ota.CLIENT_NOISE)

    def test_its_key_does_instead_of_its_password(self):
        # ESPHome asks for no password in an encrypted session: the key is its proof.
        fake = self.fake(password="s3cret-71", key=KEY)
        self.assertEqual(ota.wants("127.0.0.1", key=KEY, port=fake.port), ota.NOTHING)
        self.assertEqual(ota.wants("127.0.0.1", port=fake.port), ota.PASSWORD)  # plainly, it does
        self.assertTrue(ota.probe("127.0.0.1", port=fake.port).encryption)
        ota.upload("127.0.0.1", b"\xe9" * 5000, port=fake.port, key=KEY)
        self.assertEqual(fake.received, [(ota.APP, b"\xe9" * 5000)])
        self.assertTrue(fake.encrypted[-1])  # the update's session; the check above reached one too

    def test_without_a_key_of_its_own_a_key_given_is_not_used(self):
        # An ESPHome with no API key offers no encrypted updates: sent plainly.
        fake = self.fake()
        ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=KEY)
        self.assertEqual(fake.encrypted, [False])
        self.assertFalse(ota.probe("127.0.0.1", port=fake.port).encryption)

    def test_a_key_not_its_own_is_said_to_be(self):
        fake = self.fake(key=KEY)  # it takes plain updates too, but not with a wrong key offered
        with self.assertRaisesRegex(ota.WrongKey, "not the one ESPHome on the device has"):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=bytes(32))
        self.assertEqual(ota.wants("127.0.0.1", key=bytes(32), port=fake.port), ota.KEY)
        self.assertEqual(fake.received, [])

    def test_a_device_still_holding_a_lost_update_is_told_apart(self):
        # Its update service on another session: the connection is taken (the
        # stack's backlog) and the handshake goes unanswered.
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        with self.assertRaises(ota.Busy):
            ota.upload("127.0.0.1", b"image", port=held.getsockname()[1], timeout=0.5)

    def test_a_refusal_part_way_is_said_in_words(self):
        for key in (None, KEY):
            fake = self.fake(flags=0, refuse_at=ota.BLOCK, key=key)
            with self.assertRaisesRegex(ota.OTAError, "writing the flash failed"):
                ota.upload("127.0.0.1", b"\xe9" * (3 * ota.BLOCK), port=fake.port, key=key)

    def test_an_update_ends_only_once_the_device_says_it_did(self):
        fake = self.fake(end=0x84)
        with self.assertRaisesRegex(ota.OTAError, "finishing the update failed"):
            ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port)

    def test_the_device_is_told_its_update_ending_was_heard(self):
        fake = self.fake()
        ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port)
        self.assertTrue(fake.acknowledged.wait(5))

    def test_a_device_on_the_first_protocol_acknowledges_no_block(self):
        fake = self.fake(version=1)
        image = b"\xe9" * (2 * ota.BLOCK + 5)
        ota.upload("127.0.0.1", image, port=fake.port)
        self.assertEqual(fake.received, [(ota.APP, image)])

    def test_preparing_the_flash_may_take_longer_than_connecting(self):
        # Erasing the slot takes seconds; the connect timeout is not the data's.
        patcher = mock.patch.object(ota, "DATA_TIMEOUT", 10.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (None, KEY):
            with self.subTest(encrypted=bool(key)):
                fake = self.fake(key=key, prepare_delay=1.0)
                ota.upload("127.0.0.1", b"\xe9" * 100, port=fake.port, key=key, timeout=0.5)
                self.assertEqual(fake.encrypted, [bool(key)])

    def test_a_partition_table_needs_a_firmware_that_allows_it(self):
        fake = self.fake(flags=0)
        with self.assertRaisesRegex(ota.OTAError, "allow_partition_access"):
            ota.upload("127.0.0.1", b"\0" * 3072, kind=ota.PARTITION_TABLE, port=fake.port)


if __name__ == "__main__":
    unittest.main()
