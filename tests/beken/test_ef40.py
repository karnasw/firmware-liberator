"""The EasyFlash KV40 store as tools/beken/ef40.py reads and writes it: its
nodes byte for byte, the last write winning, and a store provisioned."""
import struct
import unittest
import zlib

from tools.beken import ef40


def kv40_store(nodes, size=0x1000):
    """An erased store holding these (name, value) nodes in order, 4-byte aligned."""
    store = bytearray(b"\xff" * size)
    pos = 0
    for name, value in nodes:
        node = ef40.encode_node(name, value)
        store[pos:pos + len(node)] = node
        pos += (len(node) + 3) & ~3
    return store


class Kv40NodeTests(unittest.TestCase):
    def test_a_node_byte_for_byte(self):
        body = b"\x01\xff\xff\xff" + struct.pack("<I", 1) + b"k" + b"v"
        expected = (b"KV40" + struct.pack("<I", 30) + struct.pack("<I", zlib.crc32(body))
                    + body + b"\x00\x00" + b"\xff" * 6)
        self.assertEqual(ef40.encode_node("k", "v"), expected)
        self.assertEqual(ef40.encode_node(b"k", b"v"), expected)

    def test_a_store_reads_back_with_the_last_write_winning(self):
        nodes = [("wifi-ssid", b"OldNet\x00"), ("elecCalibCoef", bytes(12)), ("wifi-ssid", b"HomeNet\x00"),
                 ("k", b"v"), ("n" * 64, b"the longest name"), ("blob", bytes(0x2000))]   # the largest value
        store = kv40_store(nodes, size=0x3000)
        kv = ef40.parse_store(store, region=(0, len(store)))
        self.assertEqual(kv, {**dict(nodes[1:]), "wifi-ssid": b"HomeNet\x00"})

    def test_only_the_region_is_read(self):
        store = kv40_store([("wifi-ssid", b"HomeNet")])
        flash = bytes(store) + bytes(store)
        self.assertEqual(ef40.parse_store(flash, region=(len(store), 2 * len(store))),
                         {"wifi-ssid": b"HomeNet"})
        self.assertEqual(ef40.parse_store(flash, region=(0, 8)), {})

    def test_an_erased_store_has_no_keys(self):
        self.assertEqual(ef40.parse_store(b"\xff" * 0x100, region=(0, 0x100)), {})

    def test_a_corrupt_node_is_skipped(self):
        store = kv40_store([("wifi-ssid", b"HomeNet"), ("wifi-pass", b"secret")])
        store[8] ^= 0x01                 # a bit of the first node's stored CRC flipped
        self.assertEqual(ef40.parse_store(store, region=(0, len(store))), {"wifi-pass": b"secret"})

    def test_nonsense_lengths_are_skipped(self):
        for name_len, value_len in ((0, 1), (65, 1), (1, 0x2001)):
            node = bytearray(ef40.encode_node("k", "v"))
            node[12] = name_len
            struct.pack_into("<I", node, 16, value_len)
            store = bytes(node) + ef40.encode_node("ok", "yes")
            self.assertEqual(ef40.parse_store(store, region=(0, len(store))), {"ok": b"yes"},
                             (name_len, value_len))

    def test_a_node_cut_off_by_the_end_of_the_store_is_skipped(self):
        node = ef40.encode_node("wifi-ssid", b"HomeNet")
        for cut in (len(node) - 10, 19, 4):
            self.assertEqual(ef40.parse_store(node[:cut], region=(0, cut)), {}, cut)


class ProvisionTests(unittest.TestCase):
    REGION = (0x100, 0x100 + 0x400)

    def flash(self, nodes):
        data = bytearray(b"\xff" * 0x600)
        store = kv40_store(nodes, size=0x400)
        data[self.REGION[0]:self.REGION[1]] = store
        return data

    def test_the_credentials_follow_the_last_node_without_a_gap(self):
        # Node lengths of 56 and 30 bytes: one already 4-byte aligned, one not;
        # the second has a one-character name. The firmware walks the store
        # node by node, so the new ones must follow on, aligned, with no gap.
        existing = [("elecCalibCoef", bytes(15)), ("k", b"v")]
        data = self.flash(existing)
        before = bytes(data)
        ef40.provision(data, {"wifi-ssid": "HomeNet", "wifi-pass": "secret"}, region=self.REGION)
        kv = ef40.parse_store(data, region=self.REGION)
        self.assertEqual(kv, {**dict(existing), "wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        end = self.REGION[0] + sum((len(ef40.encode_node(*node)) + 3) & ~3 for node in existing)   # 56 + 32
        self.assertEqual(data[:end], before[:end])                                 # nothing before touched
        added = ef40.encode_node("wifi-ssid", "HomeNet")                        # 44 bytes: aligned already
        added += ef40.encode_node("wifi-pass", "secret")                        # 43 bytes, padded to 44
        self.assertEqual(data[end:end + len(added)], added)
        self.assertEqual(data[end + len(added):end + len(added) + 4], b"\xff" * 4)  # erased after them
        self.assertEqual(data[self.REGION[1]:], before[self.REGION[1]:])          # nothing past the store

    def test_an_erased_store_starts_at_its_beginning(self):
        data = self.flash([])
        ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
        self.assertEqual(data[self.REGION[0]:self.REGION[0] + 4], b"KV40")
        self.assertEqual(ef40.parse_store(data, region=self.REGION), {"wifi-ssid": b"HomeNet"})

    def test_kv40_inside_a_value_does_not_move_the_end(self):
        # A value holding the magic, followed by what reads as a huge length:
        # taken as a node, the "end" would land far past the store and the
        # credentials would be written over whatever is there.
        for value_len in (0x7FFFFFF0, 0x1000):   # one absurd, one plausible but past the store's end
            decoy = b"KV40" + b"\xff" * 8 + b"\x08\xff\xff\xff" + struct.pack("<I", value_len) + b"x" * 8
            data = self.flash([("note", decoy)])
            before = bytes(data)
            ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
            self.assertEqual(data[self.REGION[1]:], before[self.REGION[1]:], hex(value_len))
            self.assertEqual(ef40.parse_store(data, region=self.REGION),
                             {"note": decoy, "wifi-ssid": b"HomeNet"}, hex(value_len))

    # A "big" node's length is 31 + its value's; the new wifi-ssid node is 44 bytes.
    def test_a_full_store_refuses_rather_than_writing_past_it(self):
        data = self.flash([("big", bytes(0x400 - 31 - 40))])   # 984 bytes: 40 left, 44 needed
        before = bytes(data)
        with self.assertRaises(ValueError):
            ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
        self.assertEqual(data, before)

    def test_what_exactly_fits_is_written(self):
        data = self.flash([("big", bytes(0x400 - 31 - 44))])   # 980 bytes: 44 left, 44 needed
        ef40.provision(data, {"wifi-ssid": "HomeNet"}, region=self.REGION)
        self.assertEqual(ef40.parse_store(data, region=self.REGION)["wifi-ssid"], b"HomeNet")
        self.assertEqual(data[self.REGION[1] - 44:self.REGION[1]], ef40.encode_node("wifi-ssid", "HomeNet"))


if __name__ == "__main__":
    unittest.main()
