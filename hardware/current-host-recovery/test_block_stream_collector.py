"""All acquisition tests use in-memory fake OS descriptors; no device enumeration."""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import types
import unittest
from unittest import mock
import zlib

PATH = Path(__file__).with_name("block_stream_collector.py")
SPEC = importlib.util.spec_from_file_location("block_stream_collector_test_subject", PATH)
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeIO:
    def __init__(self):
        self.handles = {}
        self.next_fd = 10
        self.opened = []
        self.closed = []
        self.preads = []
        self.sysfs = {}
        self.metadata = {}
        self.clock = 0
        self.tick = 0
        self.hook = None
        self.open_hook = None
        self.stat_hook = None
        self.node_numbers = {}
        for index, (lun, size) in enumerate(C.LUN_BYTES.items()):
            self.node_numbers[lun] = [8, index * 64]
            self.sysfs["/sys/class/block/" + lun + "/dev"] = "8:" + str(index * 64)
            self.sysfs["/sys/class/block/" + lun + "/size"] = str(size // 512)
            self.sysfs["/sys/class/block/" + lun + "/queue/logical_block_size"] = "4096"
            table = bytearray(128 * 128)
            for name, node, amount, start in C.PARTITIONS:
                if node[:3] != lun:
                    continue
                number = int(node[3:])
                self.node_numbers[node] = [8, index * 64 + number]
                base = "/sys/class/block/" + node
                self.sysfs[base + "/dev"] = "8:" + str(index * 64 + number)
                self.sysfs[base + "/size"] = str(amount // 512)
                self.sysfs[base + "/start"] = str(start * 8)
                self.sysfs[base + "/partition"] = str(number)
                offset = (number - 1) * 128
                table[offset:offset + 16] = b"T" * 16
                table[offset + 16:offset + 32] = b"U" * 16
                struct.pack_into("<QQQ", table, offset + 32, start, start + amount // C.SECTOR - 1, 0)
                encoded = name.encode("utf-16-le")
                table[offset + 56:offset + 56 + len(encoded)] = encoded
            total = size // C.SECTOR
            crc = zlib.crc32(table) & 0xffffffff
            headers = []
            for secondary in (False, True):
                data = bytearray(C.SECTOR)
                data[:8] = b"EFI PART"
                struct.pack_into("<IIII", data, 8, 65536, 92, 0, 0)
                struct.pack_into("<QQQQ", data, 24, total - 1 if secondary else 1,
                                 1 if secondary else total - 1, 6, total - 6)
                struct.pack_into("<QIII", data, 72, total - 5 if secondary else 2, 128, 128, crc)
                struct.pack_into("<I", data, 16, zlib.crc32(data[:92]) & 0xffffffff)
                headers.append(bytes(data))
            self.metadata[lun] = [bytes(C.SECTOR) + headers[0] + table,
                                  bytes(table) + headers[1]]

    def open(self, path, flags):
        if self.open_hook:
            self.open_hook(path, flags)
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        assert path.startswith("/sys/class/block/") or path.startswith("/dev/sd")
        if path.startswith("/sys/") and path not in self.sysfs:
            raise FileNotFoundError("synthetic missing")
        fd = self.next_fd
        self.next_fd += 1
        self.handles[fd] = path
        self.opened.append((path, flags))
        return fd

    def close(self, fd):
        self.closed.append(fd)
        del self.handles[fd]

    def fstat(self, fd):
        path = self.handles[fd]
        node = path[5:] if path.startswith("/dev/") else None
        mode = stat.S_IFBLK | 0o660 if node else stat.S_IFREG | 0o444
        rdev = os.makedev(*self.node_numbers[node]) if node else 0
        result = types.SimpleNamespace(st_mode=mode, st_rdev=rdev, st_ino=100,
                                       st_dev=200, st_uid=0, st_gid=1000,
                                       st_size=0, st_ctime_ns=1, st_mtime_ns=1)
        if self.stat_hook:
            self.stat_hook(path, result)
        return result

    def read(self, fd, count):
        value = self.sysfs[self.handles[fd]]
        value = value if isinstance(value, bytes) else (value + "\n").encode("ascii")
        return value[:count]

    def pread(self, fd, count, offset):
        node = self.handles[fd][5:]
        self.preads.append((node, count, offset))
        if node in C.LUN_BYTES:
            head, tail = self.metadata[node]
            size = C.LUN_BYTES[node]
            if offset < len(head):
                result = bytes(head[offset:offset + count])
            elif offset >= size - len(tail):
                start = offset - (size - len(tail))
                result = bytes(tail[start:start + count])
            else:
                raise AssertionError("read beyond approved GPT metadata")
        else:
            obj = next(x for x in C.OBJECTS.values() if x["node"] == node)
            assert 0 <= offset and offset + count <= obj["bytes"]
            result = bytes([0x5a]) * count
        if self.hook:
            result = self.hook(node, count, offset, result)
        return result

    def monotonic(self):
        self.clock += self.tick
        return self.clock

    def mutate_header(self, lun, secondary, field, value, fmt="I"):
        area = bytearray(self.metadata[lun][int(secondary)])
        start = len(area) - C.SECTOR if secondary else C.SECTOR
        struct.pack_into("<" + fmt, area, start + field, value)
        struct.pack_into("<I", area, start + 16, 0)
        crc = zlib.crc32(area[start:start + 92]) & 0xffffffff
        struct.pack_into("<I", area, start + 16, crc)
        self.metadata[lun][int(secondary)] = bytes(area)


class CollectorTests(unittest.TestCase):
    def run_stream(self, object_id="partition_qupfw_a", fake=None, stdout=None):
        fake = FakeIO() if fake is None else fake
        stdout = io.BytesIO() if stdout is None else stdout
        stderr = io.StringIO()
        code = C.stream(object_id, stdout, stderr, fake)
        lines = [json.loads(x) for x in stderr.getvalue().splitlines()]
        self.assertFalse(fake.handles)
        self.assertLessEqual(len(stderr.getvalue()), C.MAX_STDERR)
        return code, stdout, lines, fake

    def failed(self, code, fake, object_id="partition_qupfw_a"):
        status, output, records, fake = self.run_stream(object_id, fake)
        self.assertEqual(status, 2)
        self.assertEqual(records[-1]["status"], "FAILED")
        self.assertEqual(records[-1]["error_code"], code)
        return output, records, fake

    def test_partition_stream_and_protocol(self):
        code, output, records, fake = self.run_stream()
        self.assertEqual(code, 0)
        self.assertEqual([x["event"] for x in records], ["begin", "end"])
        raw = gzip.decompress(output.getvalue())
        self.assertEqual(raw, b"Z" * 65536)
        self.assertEqual(records[-1]["actual_bytes"], len(raw))
        self.assertEqual(records[-1]["raw_sha256"], sha(raw))
        self.assertEqual(records[-1]["gzip_sha256"], sha(output.getvalue()))
        self.assertEqual(records[-1]["gzip_bytes"], len(output.getvalue()))
        self.assertFalse(records[-1]["content_stability_proven"])
        self.assertEqual(output.getvalue()[4:8], b"\0" * 4)
        self.assertEqual(records[0]["before"], records[-1]["after"])
        self.assertTrue(all(flags & os.O_ACCMODE == os.O_RDONLY for _, flags in fake.opened))

    def test_all_metadata_objects_bounded_and_preserved(self):
        for object_id, obj in C.OBJECTS.items():
            if obj["kind"] != "gpt_metadata":
                continue
            with self.subTest(object_id=object_id):
                code, output, records, fake = self.run_stream(object_id)
                self.assertEqual(code, 0)
                expected = fake.metadata[obj["lun"]][0 if obj["offset"] == 0 else 1]
                self.assertEqual(gzip.decompress(output.getvalue()), bytes(expected))
                self.assertEqual(records[-1]["actual_bytes"], obj["bytes"])
                self.assertEqual({x[0] for x in fake.preads}, {obj["lun"]})

    def test_scope_exactly_matches_reviewed_manifest(self):
        matrix = json.loads(PATH.with_name("capture-scope.json").read_text())
        for item in matrix["partition_objects"] + matrix["gpt_metadata_objects"]:
            obj = C.OBJECTS[item["id"]]
            self.assertEqual("/dev/" + obj["node"], item["node"])
            self.assertEqual(obj["bytes"], item["expected_bytes"])
            self.assertEqual(obj["offset"], item["source_offset_bytes"])
            self.assertEqual(C.LUN_BYTES[obj["lun"]], item["lun_bytes"])
            if obj["kind"] == "partition":
                self.assertEqual(obj["start_lba"], item["first_lba"])
        self.assertEqual(len(C.OBJECTS), 54)
        self.assertEqual(sum(x["bytes"] for x in C.OBJECTS.values()), 25463984128)

    def test_unknown_id_rejected_before_io_and_not_echoed(self):
        value = "SECRET /dev/sda10\n--offset=0"
        output, records, fake = self.failed("OBJECT_NOT_ALLOWED", FakeIO(), value)
        self.assertEqual(output.getvalue(), b"")
        self.assertFalse(fake.opened)
        self.assertNotIn("SECRET", json.dumps(records))
        self.assertIsNone(records[-1]["object_id"])

    def test_unique_and_mutable_partitions_not_allowed(self):
        for name in ("keymaster_a", "keymaster_b", "persist", "misc", "userdata", "systemrw",
                     "cdt", "ddr", "modemst1", "modemst2", "frp", "keystore", "sec", "logdump"):
            with self.subTest(name=name):
                _, _, fake = self.failed("OBJECT_NOT_ALLOWED", FakeIO(), "partition_" + name)
                self.assertFalse(fake.opened)

    def test_wrong_parent_size_fails_before_block_open(self):
        fake = FakeIO()
        fake.sysfs["/sys/class/block/sde/size"] = "0"
        _, _, fake = self.failed("PARENT_GEOMETRY", fake)
        self.assertFalse(any(path.startswith("/dev/") for path, _ in fake.opened))

    def test_wrong_logical_sector_fails_closed(self):
        fake = FakeIO()
        fake.sysfs["/sys/class/block/sde/queue/logical_block_size"] = "512"
        self.failed("PARENT_GEOMETRY", fake)

    def test_wrong_partition_size_start_number_fail(self):
        for suffix in ("size", "start", "partition"):
            with self.subTest(suffix=suffix):
                fake = FakeIO()
                fake.sysfs["/sys/class/block/sde15/" + suffix] = "1"
                self.failed("PARTITION_GEOMETRY", fake)

    def test_sysfs_invalid_integer(self):
        for value in ("-1", "1 2", "1x", "9" * 21):
            with self.subTest(value=value):
                fake = FakeIO()
                fake.sysfs["/sys/class/block/sde/size"] = value
                self.failed("SYSFS_INTEGER", fake)

    def test_sysfs_oversize(self):
        fake = FakeIO()
        fake.sysfs["/sys/class/block/sde/size"] = "0" * 129
        self.failed("SYSFS_LIMIT", fake)

    def test_sysfs_nonascii(self):
        fake = FakeIO()
        fake.sysfs["/sys/class/block/sde/size"] = b"\xff"
        self.failed("SYSFS_ENCODING", fake)

    def test_sysfs_empty(self):
        fake = FakeIO()
        fake.sysfs["/sys/class/block/sde/size"] = ""
        self.failed("SYSFS_EMPTY", fake)

    def test_sysfs_dev_format(self):
        fake = FakeIO()
        fake.sysfs["/sys/class/block/sde/dev"] = "1:2:3"
        self.failed("SYSFS_DEV", fake)

    def test_sysfs_must_be_regular(self):
        fake = FakeIO()
        fake.stat_hook = lambda path, st: setattr(st, "st_mode", stat.S_IFCHR) if path.startswith("/sys") else None
        self.failed("SYSFS_TYPE", fake)

    def test_block_must_be_block(self):
        fake = FakeIO()
        fake.stat_hook = lambda path, st: setattr(st, "st_mode", stat.S_IFREG) if path.startswith("/dev") else None
        self.failed("NODE_TYPE", fake)

    def test_block_rdev_matches_sysfs(self):
        fake = FakeIO()
        fake.stat_hook = lambda path, st: setattr(st, "st_rdev", os.makedev(99, 99)) if path.startswith("/dev") else None
        self.failed("NODE_IDENTITY", fake)

    def test_permission_gap_has_no_fallback_or_raw_message(self):
        fake = FakeIO()
        def deny(path, flags):
            if path == "/dev/sde":
                raise PermissionError("SECRET credentials should not print")
        fake.open_hook = deny
        output, records, fake = self.failed("PERMISSION_DENIED", fake)
        self.assertFalse(fake.preads)
        self.assertNotIn("SECRET", json.dumps(records))

    def test_no_follow_error_no_alternate_device(self):
        fake = FakeIO()
        def deny(path, flags):
            if path == "/dev/sde15":
                raise OSError(40, "synthetic symlink")
        fake.open_hook = deny
        _, _, fake = self.failed("IO_ERROR", fake)
        self.assertFalse(fake.preads)
        self.assertFalse(any("by-" in path for path, _ in fake.opened))

    def test_short_content_read_fails_without_retry(self):
        fake = FakeIO()
        fake.hook = lambda node, count, offset, data: data[:-1] if node == "sde15" else data
        output, _, fake = self.failed("SHORT_READ", fake)
        self.assertEqual(len([x for x in fake.preads if x[0] == "sde15"]), 1)
        self.assertEqual(output.getvalue(), b"")

    def test_short_metadata_read_fails_before_content(self):
        fake = FakeIO()
        fake.hook = lambda node, count, offset, data: data[:-1]
        _, _, fake = self.failed("SHORT_READ", fake)
        self.assertEqual(len(fake.preads), 1)

    def test_gpt_signature_rejected(self):
        fake = FakeIO()
        head = bytearray(fake.metadata["sde"][0]); head[4096] = 0
        fake.metadata["sde"][0] = bytes(head)
        self.failed("GPT_HEADER", fake)

    def test_gpt_header_crc_rejected(self):
        fake = FakeIO()
        head = bytearray(fake.metadata["sde"][0]); head[4096 + 16] ^= 1
        fake.metadata["sde"][0] = bytes(head)
        self.failed("GPT_HEADER_CRC", fake)

    def test_gpt_usable_bounds_must_not_overlap_fixed_ranges(self):
        for field, value in ((40, 5), (48, C.LUN_BYTES["sde"] // 4096 - 5)):
            with self.subTest(field=field):
                fake = FakeIO(); fake.mutate_header("sde", False, field, value, "Q")
                _, _, fake = self.failed("GPT_SCOPE", fake)
                self.assertFalse(any(count == 24576 for _, count, _ in fake.preads))

    def test_gpt_table_range_not_caller_expandable(self):
        fake = FakeIO(); fake.mutate_header("sde", False, 72, 6, "Q")
        self.failed("GPT_TABLE_SCOPE", fake)

    def test_gpt_disk_guid_must_agree_without_raw_output(self):
        fake = FakeIO(); fake.mutate_header("sde", True, 56, 1234, "Q")
        _, records, _ = self.failed("GPT_HEADER_DISAGREEMENT", fake)
        self.assertNotIn("1234", json.dumps(records))

    def test_gpt_table_shape_must_agree(self):
        fake = FakeIO(); fake.mutate_header("sde", True, 80, 64)
        self.failed("GPT_HEADER_DISAGREEMENT", fake)

    def test_gpt_secondary_table_must_equal_primary(self):
        fake = FakeIO(); tail = bytearray(fake.metadata["sde"][1]); tail[0] ^= 1
        fake.metadata["sde"][1] = bytes(tail)
        self.failed("GPT_TABLE_DISAGREEMENT", fake)

    def test_gpt_primary_entries_crc_required(self):
        fake = FakeIO(); fake.mutate_header("sde", False, 88, 42)
        self.failed("GPT_PRIMARY_ENTRIES_CRC", fake)

    def test_known_sde_secondary_crc_anomaly_preserved(self):
        fake = FakeIO(); fake.mutate_header("sde", True, 88, C.KNOWN_SDE_SECONDARY_STORED_CRC32)
        table = fake.metadata["sde"][0][8192:]
        # Synthetic GPT contains no real identifiers. Patch only test reference
        # fingerprint; production constants remain the independently observed set.
        with mock.patch.object(C, "KNOWN_SDE_TABLE_SHA256", sha(table)), \
                mock.patch.object(C, "KNOWN_SDE_SECONDARY_CALCULATED_CRC32", zlib.crc32(table) & 0xffffffff):
            code, _, records, _ = self.run_stream(fake=fake)
        self.assertEqual(code, 0)
        self.assertFalse(records[0]["before"]["layout"]["secondary_entries_crc_valid"])

    def test_unknown_sde_secondary_crc_anomaly_fails(self):
        for field in ("stored_crc", "calculated_crc", "table_sha"):
            with self.subTest(field=field):
                fake = FakeIO(); fake.mutate_header("sde", True, 88, C.KNOWN_SDE_SECONDARY_STORED_CRC32)
                table = fake.metadata["sde"][0][8192:]
                values = {"KNOWN_SDE_TABLE_SHA256": sha(table),
                          "KNOWN_SDE_SECONDARY_CALCULATED_CRC32": zlib.crc32(table) & 0xffffffff,
                          "KNOWN_SDE_SECONDARY_STORED_CRC32": C.KNOWN_SDE_SECONDARY_STORED_CRC32}
                values[{"stored_crc": "KNOWN_SDE_SECONDARY_STORED_CRC32",
                        "calculated_crc": "KNOWN_SDE_SECONDARY_CALCULATED_CRC32",
                        "table_sha": "KNOWN_SDE_TABLE_SHA256"}[field]] = "wrong" if field == "table_sha" else 1
                with mock.patch.multiple(C, **values):
                    self.failed("GPT_SECONDARY_ENTRIES_CRC", fake)

    def test_other_lun_secondary_crc_anomaly_fails(self):
        fake = FakeIO(); fake.mutate_header("sdb", True, 88, 42)
        self.failed("GPT_SECONDARY_ENTRIES_CRC", fake, "partition_xbl_a")

    def test_gpt_name_range_match_expected_partition(self):
        fake = FakeIO()
        # Rename the selected partition in both copies and update both CRCs.
        for index in (0, 1):
            area = bytearray(fake.metadata["sde"][index])
            table_start = 8192 if index == 0 else 0
            area[table_start + 14 * 128 + 56] ^= 1
            fake.metadata["sde"][index] = bytes(area)
        crc = zlib.crc32(fake.metadata["sde"][0][8192:]) & 0xffffffff
        fake.mutate_header("sde", False, 88, crc); fake.mutate_header("sde", True, 88, crc)
        self.failed("GPT_PARTITION_MAPPING", fake)

    def test_pre_post_geometry_drift_fails(self):
        fake = FakeIO()
        def mutate(node, count, offset, data):
            if node == "sde15":
                fake.sysfs["/sys/class/block/sde15/dev"] = "8:999"
            return data
        fake.hook = mutate
        self.failed("SOURCE_METADATA_DRIFT", fake)

    def test_pre_post_gpt_reserved_bytes_drift_fails(self):
        fake = FakeIO()
        def mutate(node, count, offset, data):
            if node == "sde15":
                head = bytearray(fake.metadata["sde"][0]); head[0] ^= 1
                fake.metadata["sde"][0] = bytes(head)
            return data
        fake.hook = mutate
        self.failed("SOURCE_METADATA_DRIFT", fake)

    def test_gpt_capture_hash_tied_to_before_layout(self):
        fake = FakeIO()
        def mutate(node, count, offset, data):
            if len(fake.preads) == 5:
                changed = bytearray(data); changed[0] ^= 1
                return bytes(changed)
            return data
        fake.hook = mutate
        self.failed("GPT_CONTENT_DRIFT", fake, "gpt_sde_primary")

    def test_time_budget_fails_before_data(self):
        fake = FakeIO(); fake.tick = 200
        output, _, _ = self.failed("TIME_BUDGET", fake)
        self.assertEqual(output.getvalue(), b"")

    def test_numeric_progress_and_stderr_bound(self):
        fake = FakeIO(); fake.tick = 1
        with mock.patch.object(C, "CHUNK", 1024):
            code, _, records, _ = self.run_stream(fake=fake)
        self.assertEqual(code, 0)
        progress = [x for x in records if x["event"] == "progress"]
        self.assertTrue(progress)
        self.assertTrue(all(isinstance(x["actual_bytes"], int) for x in progress))
        self.assertTrue(all(x["actual_bytes"] <= 65536 for x in progress))

    def test_interruption_not_retried(self):
        fake = FakeIO()
        def interrupt(node, count, offset, data):
            if node == "sde15":
                raise KeyboardInterrupt()
            return data
        fake.hook = interrupt
        self.failed("INTERRUPTED", fake)

    def test_output_short_write_is_failure(self):
        class Short(io.BytesIO):
            def write(self, value):
                super().write(value[:-1])
                return len(value) - 1
        code, _, records, _ = self.run_stream(stdout=Short())
        self.assertEqual(code, 2)
        self.assertEqual(records[-1]["error_code"], "OUTPUT_SHORT_WRITE")

    def test_broken_pipe_is_failure(self):
        class Closed(io.BytesIO):
            def write(self, value):
                raise BrokenPipeError("secret")
        code, _, records, _ = self.run_stream(stdout=Closed())
        self.assertEqual(code, 2)
        self.assertEqual(records[-1]["error_code"], "OUTPUT_CLOSED")

    def test_import_inert(self):
        spec = importlib.util.spec_from_file_location("inert_capture", PATH)
        module = importlib.util.module_from_spec(spec)
        with mock.patch("os.open", side_effect=AssertionError("I/O during import")), \
                mock.patch("os.pread", side_effect=AssertionError("I/O during import")), \
                mock.patch("os.read", side_effect=AssertionError("I/O during import")):
            spec.loader.exec_module(module)
        self.assertEqual(len(module.OBJECTS), 54)

    def test_cli_arity_does_not_start_capture(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), mock.patch.object(C, "stream", side_effect=AssertionError("called")):
            self.assertEqual(C.main([]), 2)
            self.assertEqual(C.main(["a", "b"]), 2)
        self.assertEqual(len(stderr.getvalue().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
