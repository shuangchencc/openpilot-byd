"""Offline syscall mocks only: no target connection or real device access."""
import ast
import contextlib
import hashlib
import importlib.util
from pathlib import Path
import stat
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("software_collector.py")
SPEC = importlib.util.spec_from_file_location("software_collector", SOURCE)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)

O_PATH = 0x200000


def metadata(mode=stat.S_IFREG | 0o644, size=3, inode=50, mtime=10, ctime=20):
    return types.SimpleNamespace(st_mode=mode, st_size=size, st_dev=7, st_ino=inode,
                                 st_mtime_ns=mtime, st_ctime_ns=ctime)


class SoftwareCollectorTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(collector.os, "O_PATH", O_PATH, create=True))
        self.stack.enter_context(mock.patch.object(collector.os, "O_CLOEXEC", 0x80000, create=True))
        self.open = self.stack.enter_context(mock.patch.object(collector.os, "open", side_effect=[10, 11]))
        self.fstat = self.stack.enter_context(mock.patch.object(collector.os, "fstat", side_effect=[metadata()] * 3))
        self.read = self.stack.enter_context(mock.patch.object(collector.os, "read", side_effect=[b"abc", b""]))
        self.close = self.stack.enter_context(mock.patch.object(collector.os, "close"))

    def record(self):
        return collector.record(Path("/data/openpilot/panda/board/obj/panda.bin.signed"))

    def test_regular_inode_pinned_then_read_and_hashed(self):
        report = self.record()
        self.assertEqual(report, {"bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest(), "stable_during_read": True})
        self.assertEqual(self.open.call_count, 2)
        original, flags = self.open.call_args_list[0].args
        self.assertEqual(original, "/data/openpilot/panda/board/obj/panda.bin.signed")
        self.assertTrue(flags & O_PATH)
        self.assertTrue(flags & collector.os.O_NOFOLLOW)
        self.assertEqual(self.open.call_args_list[1].args[0], "/proc/self/fd/10")
        self.assertEqual([c.args[0] for c in self.close.call_args_list], [11, 10])

    def test_character_device_never_gets_data_capable_open(self):
        self.fstat.side_effect = [metadata(mode=stat.S_IFCHR | 0o666)]
        self.assertEqual(self.record()["error"], "not_bounded_regular_file")
        self.assertEqual(self.open.call_count, 1)
        self.assertTrue(self.open.call_args.args[1] & O_PATH)
        self.read.assert_not_called()
        self.close.assert_called_once_with(10)

    def test_block_device_never_gets_data_capable_open(self):
        self.fstat.side_effect = [metadata(mode=stat.S_IFBLK | 0o660)]
        self.assertEqual(self.record()["error"], "not_bounded_regular_file")
        self.assertEqual(self.open.call_count, 1)
        self.read.assert_not_called()

    def test_leaf_symlink_not_followed_to_device_or_secret(self):
        self.fstat.side_effect = [metadata(mode=stat.S_IFLNK | 0o777)]
        self.assertEqual(self.record()["error"], "not_bounded_regular_file")
        self.assertTrue(self.open.call_args.args[1] & collector.os.O_NOFOLLOW)
        self.assertEqual(self.open.call_count, 1)
        self.read.assert_not_called()

    def test_fifo_and_directory_rejected_before_data_open(self):
        for mode in (stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o755):
            with self.subTest(mode=mode):
                self.open.reset_mock(side_effect=True)
                self.open.side_effect = [10]
                self.fstat.side_effect = [metadata(mode=mode)]
                self.assertEqual(self.record()["error"], "not_bounded_regular_file")
                self.assertEqual(self.open.call_count, 1)
                self.read.assert_not_called()

    def test_no_original_path_reopen_even_if_replaced_after_pin(self):
        calls = []
        def opened(path, flags):
            calls.append(path)
            if len(calls) == 1:
                return 10  # Original pathname may now refer to a device/symlink.
            self.assertEqual(path, "/proc/self/fd/10")
            return 11
        self.open.side_effect = opened
        self.assertTrue(self.record()["stable_during_read"])
        self.assertEqual(len(calls), 2)

    def test_size_budget_checked_before_data_open(self):
        self.fstat.side_effect = [metadata(size=64 * 1024 * 1024 + 1)]
        self.assertEqual(self.record()["error"], "not_bounded_regular_file")
        self.assertEqual(self.open.call_count, 1)
        self.read.assert_not_called()

    def test_inode_mismatch_rejected_before_read(self):
        self.fstat.side_effect = [metadata(), metadata(inode=51)]
        self.assertEqual(self.record()["error"], "file_changed_before_open")
        self.read.assert_not_called()
        self.assertEqual([c.args[0] for c in self.close.call_args_list], [11, 10])

    def test_change_between_pin_and_read_open_rejected(self):
        self.fstat.side_effect = [metadata(), metadata(ctime=21)]
        self.assertEqual(self.record()["error"], "file_changed_before_open")
        self.read.assert_not_called()

    def test_change_during_read_rejected(self):
        self.fstat.side_effect = [metadata(), metadata(), metadata(mtime=11)]
        report = self.record()
        self.assertEqual(report, {"error": "file_changed_during_read", "bytes_read": 3})
        self.assertNotIn("sha256", report)

    def test_short_and_grown_file_rejected(self):
        for chunks in ([b"ab", b""], [b"abc", b"d"]):
            with self.subTest(chunks=chunks):
                self.open.side_effect = [10, 11]
                self.fstat.side_effect = [metadata()] * 3
                self.read.side_effect = chunks
                self.assertEqual(self.record()["error"], "file_changed_during_read")

    def test_empty_file_hash(self):
        self.fstat.side_effect = [metadata(size=0)] * 3
        self.read.side_effect = [b""]
        report = self.record()
        self.assertEqual(report["bytes"], 0)
        self.assertEqual(report["sha256"], hashlib.sha256(b"").hexdigest())

    def test_metadata_open_permission_error_redacted(self):
        self.open.side_effect = PermissionError("secret path")
        self.assertEqual(self.record(), {"error": "PermissionError"})
        self.read.assert_not_called()
        self.close.assert_not_called()

    def test_proc_fd_open_failure_closes_metadata_without_fallback(self):
        self.open.side_effect = [10, FileNotFoundError("secret path")]
        self.assertEqual(self.record(), {"error": "FileNotFoundError"})
        self.assertEqual(self.open.call_count, 2)
        self.read.assert_not_called()
        self.close.assert_called_once_with(10)

    def test_read_failure_closes_both_fds(self):
        self.read.side_effect = PermissionError("secret")
        self.assertEqual(self.record(), {"error": "PermissionError"})
        self.assertEqual([c.args[0] for c in self.close.call_args_list], [11, 10])

    def test_missing_o_path_fails_closed(self):
        del collector.os.O_PATH
        try:
            self.assertEqual(self.record(), {"error": "safe_metadata_pinning_unavailable"})
            self.open.assert_not_called()
            self.read.assert_not_called()
        finally:
            collector.os.O_PATH = O_PATH

    def test_python38_grammar_and_only_stdlib_imports(self):
        tree = ast.parse(SOURCE.read_text(), feature_version=8)
        imported = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"datetime", "hashlib", "json", "os", "pathlib", "stat"})


if __name__ == "__main__":
    unittest.main()
