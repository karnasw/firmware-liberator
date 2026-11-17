"""NVS partitions built with the standard library, as ESP-IDF's own tool builds them."""
import random
import struct
import unittest

from tools.espressif import nvs_image
from tools.espressif.inspect_stock_nvs import parse

try:
    from tools.espressif.emulator import rewrite_nvs  # ESP-IDF's nvs_partition_gen, a test tool
    import esp_idf_nvs_partition_gen  # noqa: F401
except ImportError:  # pragma: no cover - requirements/ci.txt has it
    rewrite_nvs = None


def made_up(seed):
    """Items of every type, and the CSV rows nvs_partition_gen takes for them."""
    rng = random.Random(seed)
    items, rows = [], []
    for space in range(rng.randint(1, 4)):
        name = f"space{space}"
        rows.append((name, "namespace", "", ""))
        for index in range(rng.randint(1, 12)):
            key, kind = f"k{space}_{index}", rng.choice(["u8", "i32", "string", "blob", "blob"])
            if kind == "u8":
                value = rng.randint(0, 255)
                items.append(nvs_image.Item(name, key, "u8", bytes([value])))
                rows.append((key, "data", "u8", str(value)))
            elif kind == "i32":
                value = rng.randint(-2**31, 2**31 - 1)
                items.append(nvs_image.Item(name, key, "i32", struct.pack("<i", value)))
                rows.append((key, "data", "i32", str(value)))
            elif kind == "string":
                value = "".join(rng.choice("abcdefgh") for _ in range(rng.randint(0, 300)))
                items.append(nvs_image.Item(name, key, "string", value.encode()))
                rows.append((key, "data", "string", value))
            else:  # up to more than a page: split across pages
                value = bytes(rng.randrange(256) for _ in range(rng.randint(1, 6000)))
                items.append(nvs_image.Item(name, key, "blob", value))
                rows.append((key, "data", "hex2bin", value.hex()))
    return items, rows


class NvsImageTests(unittest.TestCase):
    @unittest.skipIf(rewrite_nvs is None, "needs requirements/ci.txt")
    def test_the_same_bytes_as_esp_idfs_own_tool(self):
        for seed in range(8):
            with self.subTest(seed=seed):
                items, rows = made_up(seed)
                self.assertEqual(nvs_image.build(items, 0x10000), rewrite_nvs.rebuild(rows, 0x10000))
        # A blob begun on a page's last entry: there, a chunk of nothing.
        items = [nvs_image.Item("s", f"k{i}", "u8", b"\x01") for i in range(124)]
        rows = [("s", "namespace", "", "")] + [(f"k{i}", "data", "u8", "1") for i in range(124)]
        items.append(nvs_image.Item("s", "blob", "blob", b"\x02" * 100))
        rows.append(("blob", "data", "hex2bin", "02" * 100))
        self.assertEqual(nvs_image.build(items, 0x4000), rewrite_nvs.rebuild(rows, 0x4000))

    def test_what_is_written_reads_back(self):
        items, _ = made_up(3)
        names, values = parse(nvs_image.build(items, 0x10000))
        read = {(names[space], key): value for (space, key), value in values.items()}
        for item in items:
            value = read[(item.namespace, item.key)]
            data = value.data[:value.size] if item.kind == "blob" else value.data
            self.assertEqual(data.rstrip(b"\0") if item.kind == "string" else data, item.data, item.key)

    def test_too_much_for_the_partition_is_refused(self):
        too_much = [nvs_image.Item("s", f"k{i}", "blob", b"\x01" * 3000) for i in range(3)]
        with self.assertRaisesRegex(nvs_image.NvsError, "do not fit"):
            nvs_image.build(too_much, 0x2000)
        with self.assertRaisesRegex(nvs_image.NvsError, "key"):
            nvs_image.build([nvs_image.Item("s", "a" * 16, "u8", b"\0")], 0x2000)

    def test_only_the_pages_used_are_written(self):
        image = nvs_image.build([nvs_image.Item("s", "k", "u8", b"\x05")], 0x10000)
        self.assertEqual(len(nvs_image.used(image)), nvs_image.PAGE)
        self.assertEqual(image[nvs_image.PAGE:], b"\xff" * (0x10000 - nvs_image.PAGE))


if __name__ == "__main__":
    unittest.main()
