"""Synthetic files only; never call collect() or open real device paths."""
import hashlib
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

SOURCE = Path(__file__).resolve().parents[1] / "collector.py"
spec = importlib.util.spec_from_file_location("passive_followup_collector", SOURCE)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)

SECTOR = 512
DISK_BYTES = 64 * SECTOR


def image(*, entry_crc=None, entry_lba=2, count=4, size=128, label="boot_b", header_size=92):
    table = bytearray(512)
    table[:16] = b"\x01" * 16
    struct.pack_into("<QQQ", table, 32, 3, 10, 1 << 50)
    encoded = label.encode("utf-16le")
    table[56:56 + len(encoded)] = encoded
    header = bytearray(SECTOR)
    header[:8] = b"EFI PART"
    struct.pack_into("<III", header, 8, 0x10000, header_size, 0)
    struct.pack_into("<QQQQ", header, 24, 1, 63, 3, 61)
    struct.pack_into("<QIII", header, 72, entry_lba, count, size,
                     zlib.crc32(table) & 0xffffffff if entry_crc is None else entry_crc)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:header_size]) & 0xffffffff)
    disk = bytearray(DISK_BYTES)
    disk[SECTOR:2 * SECTOR] = header
    disk[2 * SECTOR:3 * SECTOR] = table
    return bytes(disk)


class CollectorTests(unittest.TestCase):
    def parse(self, data, lba=1):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-gpt.bin"
            path.write_bytes(data)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            fd = os.open(path, os.O_RDONLY)
            try:
                parsed = collector.gpt(fd, lba, SECTOR, DISK_BYTES)
            finally:
                os.close(fd)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
            return parsed

    def test_valid_header_table_and_attributes_without_mutation(self):
        parsed = self.parse(image())
        self.assertTrue(parsed["header_crc_valid"])
        self.assertTrue(parsed["entries_crc_valid"])
        self.assertEqual(parsed["read_bytes"], 1024)
        self.assertEqual(parsed["partitions"][0]["attributes_hex"], "0x0004000000000000")

    def test_known_style_stale_entry_crc_is_reported_not_repaired(self):
        parsed = self.parse(image(entry_crc=0))
        self.assertTrue(parsed["header_crc_valid"])
        self.assertFalse(parsed["entries_crc_valid"])

    def test_corrupt_header_crc_is_reported(self):
        data = bytearray(image())
        data[SECTOR + 16] ^= 0x80
        self.assertFalse(self.parse(bytes(data))["header_crc_valid"])

    def test_header_lba_outside_declared_disk_rejected(self):
        for lba in (-1, 64):
            with self.subTest(lba=lba), self.assertRaises(ValueError):
                self.parse(image(), lba)

    def test_invalid_signature_and_header_size_rejected(self):
        bad_signature = bytearray(image())
        bad_signature[SECTOR] = 0
        for data in (bytes(bad_signature), image(header_size=91), image(header_size=513)):
            with self.subTest(data=data[SECTOR:SECTOR + 20]), self.assertRaises(ValueError):
                self.parse(data)

    def test_table_bounds_and_dimensions_rejected(self):
        for kwargs in ({"entry_lba": 1}, {"entry_lba": 64}, {"count": 0},
                       {"count": 16385}, {"size": 127}, {"size": 4097}, {"count": 1024, "size": 4096}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.parse(image(**kwargs))

    def test_short_header_and_table_rejected(self):
        for data in (image()[:SECTOR + 40], image()[:2 * SECTOR + 16]):
            with self.subTest(bytes=len(data)), self.assertRaises(ValueError):
                self.parse(data)

    def test_unexpected_partition_label_rejected(self):
        with self.assertRaisesRegex(ValueError, "partition label"):
            self.parse(image(label="not an allowed label"))

    def test_regular_file_read_bound_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ordinary.txt"
            path.write_bytes(b"12345678")
            self.assertEqual(collector.read_bytes(path, 8), b"12345678")
            with self.assertRaisesRegex(ValueError, "read limit"):
                collector.read_bytes(path, 7)

    def test_nonregular_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-fifo"
            os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, "regular/sysfs"):
                collector.read_bytes(path, 8)


if __name__ == "__main__":
    unittest.main()
