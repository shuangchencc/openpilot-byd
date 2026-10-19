"""Offline only: temporary regular fixtures and mocks; no real device reads."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("p10_io", Path(__file__).with_name("io_collector.py"))
io = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(io)


def fake_stat(rdev, inode=30, fsdev=None, mode=stat.S_IFCHR | 0o660):
    return types.SimpleNamespace(st_mode=mode, st_rdev=os.makedev(*rdev),
                                 st_dev=fsdev or os.makedev(0, 7), st_ino=inode,
                                 st_uid=0, st_gid=0)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = io.Collector(self.root, sleep=lambda _: None)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, path, value):
        p = self.root / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value)

    def test_constructor_inert_and_explicit_root(self):
        with mock.patch.object(io.os, "open", side_effect=AssertionError("opened")), mock.patch.object(io.os, "scandir", side_effect=AssertionError("listed")):
            io.Collector(self.root)
            with self.assertRaises(ValueError):
                io.Collector(None)

    def test_proc_stat_parentheses_and_spaces(self):
        self.assertEqual(io.proc_starttime("77 (a tricky ) comm) S " + "0 " * 18 + "12345 7"), 12345)
        with self.assertRaises(ValueError):
            io.proc_starttime("truncated")

    def test_fdinfo_ignores_unrequested_fields(self):
        raw = "pos:\t22\nflags:\t0100002\nmnt_id:\t7\nino:\t9\nsecret: private\nflags: invalid"
        self.assertEqual(io.fdinfo_selected(raw), {"flags": "0100002", "mnt_id": "7", "ino": "9"})

    def test_serial_hash_never_retains_plaintext(self):
        value = io.serial_digest(b"serial-secret\n")
        self.assertEqual(value["bytes"], 13)
        self.assertEqual(len(value["sha256"]), 64)
        self.assertNotIn("serial-secret", json.dumps(value))

    def test_lock_key_is_filesystem_dev_inode(self):
        self.assertEqual(io.lock_key("1: FLOCK ADVISORY WRITE 876 00:07:30 0 EOF"), (0, 7, 30))
        self.assertEqual(io.lock_key("1: -> FLOCK ADVISORY WRITE 876 00:07:30 0 EOF"), (0, 7, 30))
        self.assertIsNone(io.lock_key("not a lock"))

    def test_rdev_requires_character_node(self):
        self.assertEqual(io.rdev_key(fake_stat((153, 0))), (153, 0))
        self.assertIsNone(io.rdev_key(fake_stat((153, 0), mode=stat.S_IFREG)))

    def test_fixed_read_whitelist_and_symlink_no_open(self):
        for path in ("/dev/spidev0.0", "/proc/1/fd/3", "/proc/1/cmdline", "/proc/1/environ", "/persist/secret"):
            with mock.patch.object(io.os, "open", side_effect=AssertionError("opened")):
                with self.assertRaises(ValueError):
                    self.c.read(path)
        self.put("/VERSION", b"fake-os")
        self.assertEqual(self.c.read("/VERSION"), b"fake-os")
        (self.root / "VERSION").unlink()
        (self.root / "VERSION").symlink_to("other")
        with mock.patch.object(io.os, "open", side_effect=AssertionError("opened")):
            with self.assertRaises(ValueError):
                self.c.read("/VERSION")

    def test_character_file_never_opened(self):
        with mock.patch.object(io.os, "lstat", return_value=fake_stat((153, 0))), mock.patch.object(io.os, "open", side_effect=AssertionError("opened")):
            with self.assertRaises(ValueError):
                self.c.read("/VERSION")

    def test_read_limits_and_redacted_errors(self):
        self.put("/VERSION", b"12345")
        with self.assertRaises(io.LimitReached):
            self.c.read("/VERSION", 4)
        with mock.patch.object(self.c, "read", side_effect=PermissionError("sensitive path contents")):
            self.assertEqual(self.c.text("/VERSION"), {"error": "PermissionError", "value": "unknown"})
        self.assertNotIn("sensitive", json.dumps(self.c.errors))

    def test_node_stat_target_and_alias_mapping(self):
        with mock.patch.object(io.os, "stat", return_value=fake_stat((153, 0))), mock.patch.object(io.os, "open", side_effect=AssertionError("opened")):
            target = self.c.node("/dev/spidev0.0", target=True)
            alias = self.c.node("/dev/alias")
        self.assertEqual(target["rdev"], [153, 0])
        self.assertEqual(alias["inode"], 30)
        self.assertEqual(self.c.targets, {(153, 0): ["/dev/spidev0.0"]})
        self.assertEqual(self.c.lock_nodes, {(0, 7, 30): (153, 0)})

    def test_locks_only_matching_node_inode(self):
        self.c.lock_nodes[(0, 7, 30)] = (153, 0)
        self.put("/proc/locks", b"1: FLOCK ADVISORY WRITE 876 00:07:30 0 EOF\n2: FLOCK ADVISORY WRITE 999 00:07:31 0 EOF\n")
        locks = self.c.lock_sample()
        self.assertEqual(len(locks["matching_locks"]), 1)
        self.assertEqual(locks["matching_locks"][0]["target_rdev"], [153, 0])
        self.assertNotIn("999", json.dumps(locks))
        self.assertIn("does not prove", locks["limitation"])

    def test_collect_fake_tree_is_unbound_no_secret(self):
        self.put("/VERSION", b"fake-version")
        self.put("/proc/version", b"fake-kernel")
        self.put("/proc/cmdline", b"root=/dev/fake androidboot.slot_suffix=_a serialno=secret androidboot.verifiedbootstate=green")
        self.put("/proc/locks", b"")
        for name in ("sys/bus/usb/devices", "sys/bus/spi/devices", "sys/class/spidev", "dev"):
            (self.root / name).mkdir(parents=True)
        report = self.c.collect()
        self.assertEqual(report["profile_binding"], "UNBOUND")
        self.assertEqual(len(report["process_samples"]), 2)
        self.assertNotIn("serialno", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))
        self.assertIn("firmware_signature", report["identity_unknown"])

    def test_permission_gap_counted_no_message_leak(self):
        with mock.patch.object(io.os, "scandir", side_effect=PermissionError("private")):
            report = self.c.process_sample()
        self.assertEqual(report["gaps"]["list:PermissionError"], 1)
        self.assertNotIn("private", json.dumps(report))

    def test_dt_u32_and_missing_is_not_runtime(self):
        base = "/sys/bus/spi/devices/spi0.0"
        self.put(base + "/of_node/spi-max-frequency", b"\x00\x98\x96\x80")
        self.put(base + "/of_node/reg", b"\x00\x00\x00\x00")
        self.put(base + "/of_node/compatible", b"comma,panda\x00")
        (self.root / "sys/class/spidev").mkdir(parents=True)
        record = self.c.spi()["devices"][0]
        self.assertEqual(record["device_tree"]["spi-max-frequency"], [10000000])
        self.assertEqual(record["current_ioctl_settings"]["speed_hz"], "unknown")
        self.assertEqual(record["device_tree"]["spi-cpol"]["present"], False)

    def test_usb_only_candidate_serial_read_keeps_vendor(self):
        for name, vendor, productid, product in (("1-1", "3801", "ddcc", "panda"), ("1-2", "1234", "5678", "other")):
            base = "/sys/bus/usb/devices/" + name
            for key, value in (("idVendor", vendor), ("idProduct", productid), ("product", product), ("serial", "secret-" + name)):
                self.put(base + "/" + key, value.encode())
        read = self.c.read
        calls = []
        def record(path, limit=4096):
            calls.append(path)
            return read(path, limit)
        with mock.patch.object(self.c, "read", side_effect=record):
            devices = self.c.usb()["devices"]
        self.assertEqual(devices[0]["idVendor"], "3801")
        self.assertTrue(devices[0]["panda_descriptor_candidate"])
        self.assertFalse(devices[1]["serial"]["read"])
        self.assertNotIn("/sys/bus/usb/devices/1-2/serial", calls)
        self.assertNotIn("secret-", json.dumps(devices))

    def make_process(self):
        raw = ("77 (pandad) S " + "0 " * 18 + "12345 7").encode()
        self.put("/proc/77/stat", raw)
        self.put("/proc/77/comm", b"pandad\n")
        self.put("/proc/77/fdinfo/3", b"flags:\t0100002\nmnt_id:\t7\nino:\t30\nsecret: hidden")
        self.put("/proc/locks", b"1: FLOCK ADVISORY WRITE 77 00:07:30 0 EOF\n")
        (self.root / "proc/77/fd").mkdir()
        (self.root / "proc/77/fd/3").symlink_to("/dev/spidev0.0")
        (self.root / "proc/77/exe").symlink_to("/usr/bin/pandad")
        self.c.targets[(153, 0)] = ["/dev/spidev0.0"]

    def test_fd_holder_and_lock_are_matched_by_stat_without_open(self):
        self.make_process()
        with mock.patch.object(io.os, "stat", return_value=fake_stat((153, 0))):
            report = self.c.process_sample()
        self.assertEqual(report["pids_completed"], 1)
        self.assertTrue(report["visible_snapshot_complete"])
        self.assertEqual(report["holders"][0]["starttime_ticks"], 12345)
        self.assertEqual(report["holders"][0]["fds"][0]["fdinfo"]["flags"], "0100002")
        self.assertEqual(len(report["locks"]["matching_locks"]), 1)
        self.assertNotIn("hidden", json.dumps(report))

    def test_pid_reuse_discards_holder(self):
        self.make_process()
        read = self.c.read
        seen = [0]
        def change_start(path, limit=4096):
            value = read(path, limit)
            if path == "/proc/77/stat":
                seen[0] += 1
                if seen[0] == 2:
                    return value.replace(b"12345", b"12346")
            return value
        with mock.patch.object(io.os, "stat", return_value=fake_stat((153, 0))), mock.patch.object(self.c, "read", side_effect=change_start):
            report = self.c.process_sample()
        self.assertEqual(report["holders"], [])
        self.assertEqual(report["gaps"]["process:PidReused"], 1)
        self.assertFalse(report["visible_snapshot_complete"])

    def test_inaccessible_fd_never_counts_pid_complete_or_idle(self):
        self.make_process()
        with mock.patch.object(io.os, "stat", side_effect=PermissionError("hidden")):
            report = self.c.process_sample()
        self.assertEqual(report["holders"], [])
        self.assertEqual(report["pids_completed"], 0)
        self.assertEqual(report["gaps"]["proc_fd:PermissionError"], 1)
        self.assertFalse(report["visible_snapshot_complete"])
        self.assertNotIn("hidden", json.dumps(report))

    def test_inaccessible_fd_directory_not_counted_complete(self):
        self.make_process()
        scandir = io.os.scandir
        def deny_fd(path):
            if str(path).endswith("/proc/77/fd"):
                raise PermissionError("hidden")
            return scandir(path)
        with mock.patch.object(io.os, "scandir", side_effect=deny_fd):
            report = self.c.process_sample()
        self.assertEqual(report["pids_completed"], 0)
        self.assertEqual(report["gaps"]["list:PermissionError"], 1)
        self.assertFalse(report["visible_snapshot_complete"])
        self.assertNotIn("hidden", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
