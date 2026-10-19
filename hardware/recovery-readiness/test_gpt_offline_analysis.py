"""Synthetic-only tests; no device or archive reads and no network imports."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

HERE = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = load("gpt_header_collector")
sys.modules["gpt_header_collector"] = collector
analysis = load("gpt_offline_analysis")


def fixture():
    table = bytearray(4 * 128)
    for index, name in enumerate(("boot_a", "boot_b")):
        off = index * 128
        table[off:off + 16] = bytes([index + 1]) * 16
        table[off + 16:off + 32] = bytes([index + 11]) * 16
        struct.pack_into("<QQQ", table, off + 32, 10 + 10 * index, 19 + 10 * index, 3 + index)
        table[off + 56:off + 56 + len(name) * 2] = name.encode("utf-16le")
    header = bytearray(512)
    header[:8] = b"EFI PART"
    struct.pack_into("<IIIIQQQQ", header, 8, 65536, 92, 0, 0, 99, 1, 3, 97)
    header[56:72] = bytes([71]) * 16
    struct.pack_into("<QIII", header, 72, 98, 4, 128, zlib.crc32(table))
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
    return bytes(header), bytes(table)


def scoped_fixture(label="primary"):
    old_header, old_table = fixture()
    header = bytearray(old_header + bytes(4096 - len(old_header)))
    table = old_table + bytes(8192 - len(old_table))
    primary = label == "primary"
    struct.pack_into("<QQQQ", header, 24, 1 if primary else 1048575,
                     1048575 if primary else 1, 6, 1048570)
    struct.pack_into("<QIII", header, 72, 2 if primary else 1048571, 64, 128, zlib.crc32(table))
    header[16:20] = bytes(4)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]))
    return bytes(header), table


class HeaderTests(unittest.TestCase):
    def test_valid_complete_hash_and_crc(self):
        header, table = fixture()
        record = collector.parse_header(header, 512, 51200, 99)
        self.assertTrue(record["header_crc_valid"])
        self.assertEqual(record["header_sector_sha256"], collector.sha(header))
        self.assertTrue(collector.describe_table(record, table)["entries_crc_valid"])

    def test_crc_mismatch_is_reported(self):
        header, table = fixture()
        changed = bytearray(header)
        changed[88] ^= 1
        record = collector.parse_header(changed, 512, 51200, 99)
        self.assertFalse(record["header_crc_valid"])
        self.assertFalse(collector.describe_table(record, table)["entries_crc_valid"])

    def test_short_header(self):
        with self.assertRaises(ValueError):
            collector.parse_header(fixture()[0][:-1], 512, 51200, 99)

    def test_signature(self):
        with self.assertRaises(ValueError):
            collector.parse_header(bytes(512), 512, 51200, 99)

    def test_geometry(self):
        for sector, disk, lba in ((1024, 51200, 1), (512, 0, 1), (512, 513, 1), (512, 51200, 100)):
            with self.subTest(sector=sector, disk=disk, lba=lba), self.assertRaises(ValueError):
                collector.parse_header(fixture()[0], sector, disk, lba)

    def test_header_length(self):
        for size in (91, 513):
            header = bytearray(fixture()[0]); struct.pack_into("<I", header, 12, size)
            with self.subTest(size=size), self.assertRaises(ValueError):
                collector.parse_header(header, 512, 51200, 99)

    def test_table_budget(self):
        header = bytearray(fixture()[0]); struct.pack_into("<I", header, 80, 1024)
        with self.assertRaises(ValueError):
            collector.parse_header(header, 512, 51200, 99)

    def test_table_bounds(self):
        for lba in (0, 100):
            header = bytearray(fixture()[0]); struct.pack_into("<Q", header, 72, lba)
            with self.subTest(lba=lba), self.assertRaises(ValueError):
                collector.parse_header(header, 512, 51200, 99)

    def test_table_shape(self):
        for count, size in ((0, 128), (4, 129), (4, 64)):
            header = bytearray(fixture()[0]); struct.pack_into("<II", header, 80, count, size)
            with self.subTest(count=count, size=size), self.assertRaises(ValueError):
                collector.parse_header(header, 512, 51200, 99)

    def test_short_table(self):
        header, table = fixture()
        with self.assertRaises(ValueError):
            collector.describe_table(collector.parse_header(header, 512, 51200, 99), table[:-1])

    def test_output_redacted(self):
        header, table = fixture()
        record = collector.parse_header(header, 512, 51200, 99)
        record.update(collector.describe_table(record, table))
        output = json.dumps(record)
        self.assertNotIn(header[56:72].hex(), output)
        self.assertNotIn("boot_a", output)
        self.assertTrue(all(isinstance(v, (int, str, bool)) for v in record.values()))

    def test_import_does_no_io(self):
        with patch("os.open", side_effect=AssertionError("unexpected I/O")), patch("os.pread", side_effect=AssertionError("unexpected I/O")):
            load("gpt_header_collector")


class CollectionScopeTests(unittest.TestCase):
    def test_fixed_metadata_scope_valid(self):
        for label, lba in (("primary", 1), ("secondary", 1048575)):
            record = collector.parse_header(scoped_fixture(label)[0], 4096, 4294967296, lba)
            collector.validate_collection_scope(record, label, 4294967296)

    def test_malformed_scope_rejected_before_io(self):
        for label, lba in (("primary", 1), ("secondary", 1048575)):
            record = collector.parse_header(scoped_fixture(label)[0], 4096, 4294967296, lba)
            cases = {"sector_bytes": 512, "revision": 0, "header_size": 96,
                     "reserved": 1, "observed_lba": 5, "current_lba": 5,
                     "alternate_lba": 100, "first_usable_lba": 1,
                     "last_usable_lba": 1048575, "entries_lba": 32,
                     "entries_count": 65, "entry_size": 256, "entries_bytes": 16384,
                     "header_crc_valid": False}
            for field, value in cases.items():
                changed = dict(record); changed[field] = value
                with self.subTest(label=label, field=field), self.assertRaises(ValueError):
                    collector.validate_collection_scope(changed, label, 4294967296)

    def test_collect_refuses_in_partition_pointer_without_table_pread(self):
        header, _ = scoped_fixture()
        malicious = bytearray(header)
        struct.pack_into("<Q", malicious, 72, 32)
        malicious[16:20] = bytes(4)
        struct.pack_into("<I", malicious, 16, zlib.crc32(malicious[:92]))
        with patch.object(collector, "_geometry_integer", side_effect=[4096, 8388608]), \
             patch("os.open", return_value=9) as opened, \
             patch("os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
             patch("os.close"), patch("os.pread", return_value=malicious) as pread, \
             patch("os.write", side_effect=AssertionError("write forbidden")):
            with self.assertRaises(ValueError):
                collector.collect()
            self.assertEqual(pread.call_args_list[0].args, (9, 4096, 4096))
            self.assertEqual(pread.call_count, 1)
            self.assertEqual(opened.call_args.args[0], "/dev/sde")
            self.assertEqual(opened.call_args.args[1] & os.O_ACCMODE, os.O_RDONLY)
            self.assertTrue(opened.call_args.args[1] & os.O_NOFOLLOW)

    def test_collect_geometry_rejected_before_device_open(self):
        with patch.object(collector, "_geometry_integer", side_effect=[512, 8388608]), \
             patch("os.open", side_effect=AssertionError("device open forbidden")):
            with self.assertRaises(ValueError):
                collector.collect()

    def test_collect_reads_only_fixed_header_and_table_offsets(self):
        primary, table = scoped_fixture("primary")
        secondary, _ = scoped_fixture("secondary")
        values = {(4096, 4096): primary, (4096, 1048575 * 4096): secondary,
                  (8192, 2 * 4096): table, (8192, 1048571 * 4096): table}
        calls = []
        def fake_pread(fd, length, offset):
            self.assertEqual(fd, 9)
            calls.append((length, offset))
            return values[(length, offset)]
        with patch.object(collector, "_geometry_integer", side_effect=[4096, 8388608]), \
             patch("os.open", return_value=9), \
             patch("os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
             patch("os.close"), patch("os.pread", side_effect=fake_pread), \
             patch("os.write", side_effect=AssertionError("write forbidden")):
            result = collector.collect()
        self.assertEqual(calls, [(4096, 4096), (8192, 8192), (4096, 4096),
                                 (4096, 1048575 * 4096), (8192, 1048571 * 4096),
                                 (4096, 1048575 * 4096)])
        self.assertTrue(result["sde"]["primary"]["entries_crc_valid"])
        self.assertTrue(result["sde"]["secondary"]["header_stable_across_table_read"])


class OfflineAnalysisTests(unittest.TestCase):
    def current_table(self):
        header, old = fixture()
        current = bytearray(old)
        current[:16], current[128:144] = old[128:144], old[:16]
        struct.pack_into("<Q", current, 48, 8)
        struct.pack_into("<Q", current, 176, 9)
        return header, old, bytes(current)

    def test_exact_reconstruction_and_allowed_changes(self):
        _, old, current = self.current_table()
        result, summary = analysis.reconstruct_entries(old, analysis.fingerprints(current, 4, 128))
        self.assertEqual(result, current)
        self.assertEqual(summary["type_guid_swap_pairs"], [[1, 2]])
        self.assertEqual(summary["attribute_changed_entries"], 2)
        self.assertFalse(summary["payload_saved"])

    def test_rejects_changed_unique_guid_lba_name_extension(self):
        _, old, current = self.current_table()
        for key in ("unique_guid_sha256", "lba_sha256", "name_bytes_sha256", "extension_sha256"):
            expected = analysis.fingerprints(current, 4, 128)
            expected["used"][0][key] = "0" * 64
            with self.subTest(key=key), self.assertRaises(ValueError):
                analysis.reconstruct_entries(old, expected)

    def test_rejects_whole_table_hash_mismatch(self):
        _, old, current = self.current_table()
        expected = analysis.fingerprints(current, 4, 128); expected["entries_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            analysis.reconstruct_entries(old, expected)

    def test_rejects_unused_entry_change(self):
        _, old, current = self.current_table()
        modified = bytearray(current); modified[300] = 1
        with self.assertRaises(ValueError):
            analysis.reconstruct_entries(old, analysis.fingerprints(modified, 4, 128))

    def test_rejects_duplicate_index(self):
        _, old, current = self.current_table()
        expected = analysis.fingerprints(current, 4, 128)
        expected["used"][1]["partition"] = 1
        with self.assertRaises(ValueError):
            analysis.reconstruct_entries(old, expected)

    def test_rejects_nonreciprocal_type_change(self):
        _, old, current = self.current_table()
        modified = bytearray(current); modified[128:144] = old[128:144]
        with self.assertRaises(ValueError):
            analysis.reconstruct_entries(old, analysis.fingerprints(modified, 4, 128))

    def test_crc_only_changes_two_fields(self):
        header, _, current = self.current_table()
        result = analysis.crc_only_summary(header, current, 512, 51200, 99)
        self.assertTrue(result["all_other_bytes_preserved"])
        self.assertTrue(set(result["changed_byte_offsets_within_header_sector"]) <= {16,17,18,19,88,89,90,91})
        self.assertFalse(result["deployable"])
        self.assertFalse(result["payload_saved"])

    def observed(self):
        header, _, _ = self.current_table()
        record = collector.parse_header(header, 512, 51200, 99)
        record.update({"header_stable_across_table_read": True, "header_after_sha256": collector.sha(header)})
        return header, record

    def test_full_header_reconstruction(self):
        header, record = self.observed()
        self.assertEqual(analysis.reconstruct_header(header, record, 51200), header)

    def test_observed_numeric_changes_require_exact_full_header_hash(self):
        old, _ = self.observed()
        new = bytearray(old)
        struct.pack_into("<I", new, 88, 123456)
        new[16:20] = bytes(4)
        struct.pack_into("<I", new, 16, zlib.crc32(new[:92]))
        observed = collector.parse_header(new, 512, 51200, 99)
        observed.update({"header_stable_across_table_read": True, "header_after_sha256": collector.sha(new)})
        self.assertEqual(analysis.reconstruct_header(old, observed, 51200), new)
        observed["entries_crc32_stored"] ^= 1
        with self.assertRaises(ValueError):
            analysis.reconstruct_header(old, observed, 51200)

    def test_header_unrepresented_byte_change_rejected(self):
        header, record = self.observed()
        for index in (56, 72 + 100):
            old = bytearray(header); old[index] ^= 1
            with self.subTest(index=index), self.assertRaises(ValueError):
                analysis.reconstruct_header(old, record, 51200)

    def test_header_whole_sha_mismatch_rejected(self):
        header, record = self.observed(); record["header_sector_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            analysis.reconstruct_header(header, record, 51200)

    def test_header_changed_during_read_rejected(self):
        header, record = self.observed(); record["header_stable_across_table_read"] = False
        with self.assertRaises(ValueError):
            analysis.reconstruct_header(header, record, 51200)

    def test_layout_bounds_and_overlap(self):
        header, table = fixture(); record = collector.parse_header(header, 512, 51200, 99)
        self.assertTrue(analysis.validate_layout(table, record)["ranges_nonoverlapping"])
        for first, last in ((2, 20), (19, 30), (20, 98), (25, 24)):
            changed = bytearray(table); struct.pack_into("<QQ", changed, 128 + 32, first, last)
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                analysis.validate_layout(changed, record)

    def test_duplicate_unique_guid_rejected(self):
        header, table = fixture(); record = collector.parse_header(header, 512, 51200, 99)
        changed = bytearray(table); changed[144:160] = changed[16:32]
        with self.assertRaises(ValueError):
            analysis.validate_layout(changed, record)

    def test_pair_correspondence(self):
        header, _ = fixture(); secondary = collector.parse_header(header, 512, 51200, 99)
        primary = copy.deepcopy(secondary)
        primary.update({"current_lba": 1, "observed_lba": 1, "alternate_lba": 99, "entries_lba": 2})
        self.assertTrue(analysis.validate_header_pair(primary, secondary, 51200)["reciprocal_header_lbas"])
        for field, value in (("alternate_lba", 98), ("disk_guid_sha256", "0" * 64), ("entries_lba", 3), ("last_usable_lba", 99)):
            changed = copy.deepcopy(primary); changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                analysis.validate_header_pair(changed, secondary, 51200)

    def test_no_binary_values_in_summary(self):
        header, old, current = self.current_table()
        _, summary = analysis.reconstruct_entries(old, analysis.fingerprints(current, 4, 128))
        summary["crc"] = analysis.crc_only_summary(header, current, 512, 51200, 99)
        output = json.dumps(summary)
        self.assertNotIn(old[:16].hex(), output)
        self.assertNotIn(old[16:32].hex(), output)

    def test_analysis_import_no_io(self):
        with patch("os.open", side_effect=AssertionError("unexpected I/O")), patch("os.pread", side_effect=AssertionError("unexpected I/O")):
            load("gpt_offline_analysis")


if __name__ == "__main__":
    unittest.main(verbosity=2)
