"""Offline fixtures/syscall mocks only; never connects or opens real devices."""
import ast
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock

SOURCE = Path(__file__).with_name("supervisor_collector.py")
SPEC = importlib.util.spec_from_file_location("supervisor_collector", SOURCE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def raw_stat(pid, comm="pandad", parent=1, start=123):
    return ("%d (%s) S %d " % (pid, comm, parent) + "0 " * 17 + "%d 0" % start).encode()


def meta(mode=stat.S_IFREG | 0o644, inode=50, size=3):
    return types.SimpleNamespace(st_mode=mode, st_dev=7, st_ino=inode, st_size=size,
                                st_mtime_ns=1, st_ctime_ns=2)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = module.Collector(self.root)

    def test_import_and_constructor_inert_explicit_root(self):
        with mock.patch.object(module.os, "open", side_effect=AssertionError("open")), mock.patch.object(module.os, "scandir", side_effect=AssertionError("scan")):
            fresh = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(fresh)
            fresh.Collector(self.root)
            with self.assertRaises(ValueError):
                fresh.Collector(None)
            with self.assertRaises(ValueError):
                fresh.Collector("relative")

    def test_stat_parentheses_ppid_and_starttime(self):
        result = module.parse_stat(raw_stat(7, "a ) b", 3, 999).decode())
        self.assertEqual(result, {"pid": 7, "ppid": 3, "comm": "a ) b", "starttime_ticks": 999})
        with self.assertRaises(ValueError):
            module.parse_stat("bad")

    def test_cgroup_known_unit_and_unknown_path_privacy(self):
        result = module.cgroup_fields("1:name=systemd:/system.slice/comma.service\n0::/private-secret/token\n")
        self.assertEqual(result[0]["path"], "/system.slice/comma.service")
        self.assertEqual(result[1]["path"], "redacted_nonallowlisted_cgroup_path")
        self.assertNotIn("private-secret", json.dumps(result))

    def test_cgroup_session_is_metadata_not_supervisor_proof(self):
        result = module.cgroup_fields("1:name=systemd:/user.slice/user-1000.slice/session-c1.scope")
        self.assertIn("session-c1.scope", result[0]["path"])

    def test_unit_only_safe_exec_paths_and_fields(self):
        raw = (b"[Service]\nType=simple\nRestart=always\nUser=comma\nEnvironment=TOKEN=private-secret\n"
               b"ExecStart=/bin/bash /usr/comma/comma.sh private-secret\nExecStop=/unknown/secret --token=private-secret\n")
        result = module.startup_fields(module.UNIT_PATHS[0], raw)
        self.assertEqual(result["selected_unit_fields"]["Restart"], "always")
        self.assertEqual(result["commands_not_executed"][0]["known_executable"], "/bin/bash")
        self.assertEqual(result["commands_not_executed"][0]["known_literal_script_arguments"], ["/usr/comma/comma.sh"])
        self.assertNotIn("known_executable", result["commands_not_executed"][1])
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertNotIn("/unknown/secret", json.dumps(result))

    def test_script_only_hash_markers_never_custom_content(self):
        raw = b"source /data/custom_op_script\nTOKEN=private-secret\nexec /data/continue.sh\ntmux new-session"
        result = module.startup_fields("/usr/comma/comma.sh", raw)
        self.assertTrue(result["dynamic_custom_script_reference"])
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIn("/data/continue.sh", result["known_path_markers"])
        self.assertIn("tmux", result["static_token_markers"])
        self.assertNotIn("private-secret", json.dumps(result))

    def test_read_allowlist_excludes_devices_configs_keys_and_dynamic_scripts(self):
        for path in ("/dev/spidev0.0", "/dev/sda", "/proc/7/fd/3", "/proc/7/cmdline", "/proc/7/environ",
                     "/data/params/d/secret", "/data/custom_op_script", "/home/comma/.ssh/id_rsa", "/etc/shadow"):
            self.assertFalse(self.c.allowed_read(path), path)
            with mock.patch.object(self.c, "_pin", side_effect=AssertionError("pin")):
                with self.assertRaises(module.Unavailable):
                    self.c.read(path)

    def test_allowlist_accepts_fixed_sources(self):
        for path in ("/proc/7/stat", "/proc/7/cgroup", "/proc/self/mountinfo", "/sys/class/block/sde26/size",
                     "/sys/class/block/sda/queue/logical_block_size", "/sys/bus/usb/devices/1-1.2/idVendor") + module.STARTUP_FILES:
            self.assertTrue(self.c.allowed_read(path), path)

    def test_mount_fields_redact_nonpartition_sources_and_options(self):
        raw = "12 1 8:6 / / rw,secret=token - ext4 /dev/sda6 rw\n13 1 0:1 / /data rw - ext4 user:password@server rw\n"
        result = module.parse_mounts(raw)
        self.assertEqual(result[0]["source"], "/dev/sda6")
        self.assertEqual(result[1]["source"], "redacted_non_partition_source")
        self.assertNotIn("password", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_pinned_regular_read_does_not_reopen_original_path(self):
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11) as opened, \
                mock.patch.object(module.os, "fstat", side_effect=[meta()] * 3), \
                mock.patch.object(module.os, "read", side_effect=[b"abc", b""]), mock.patch.object(module.os, "close") as close:
            self.assertEqual(self.c.read("/data/continue.sh"), b"abc")
            self.assertEqual(opened.call_args.args[0], "/proc/self/fd/10")
            self.assertEqual([c.args[0] for c in close.call_args_list], [11, 10])

    def test_device_or_symlink_pin_never_data_opened(self):
        for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFLNK, stat.S_IFIFO):
            with self.subTest(mode=mode), mock.patch.object(self.c, "_pin", return_value=10), \
                    mock.patch.object(module.os, "fstat", return_value=meta(mode=mode)), \
                    mock.patch.object(module.os, "open", side_effect=AssertionError("data open")), \
                    mock.patch.object(module.os, "close") as close:
                with self.assertRaises(module.Unavailable):
                    self.c.read("/data/continue.sh")
                close.assert_called_once_with(10)

    def test_inode_change_rejected_and_fds_closed(self):
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11), \
                mock.patch.object(module.os, "fstat", side_effect=[meta(), meta(inode=51)]), \
                mock.patch.object(module.os, "read", side_effect=AssertionError("read")), mock.patch.object(module.os, "close") as close:
            with self.assertRaises(module.Unavailable):
                self.c.read("/data/continue.sh")
            self.assertEqual([c.args[0] for c in close.call_args_list], [11, 10])

    def test_aggregate_read_budget_never_reads_past_limit(self):
        self.c.bytes_read = module.LIMITS["read_bytes"]
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11), \
                mock.patch.object(module.os, "fstat", side_effect=[meta()] * 2), \
                mock.patch.object(module.os, "read", side_effect=AssertionError("read beyond budget")), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.read("/data/continue.sh")

    def test_oversize_file_rejected_before_data_open(self):
        with mock.patch.object(self.c, "_pin", return_value=10), \
                mock.patch.object(module.os, "fstat", return_value=meta(size=4097)), \
                mock.patch.object(module.os, "open", side_effect=AssertionError("data open")), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.read("/data/continue.sh", 4096)

    def test_missing_safe_pinning_no_fallback(self):
        with mock.patch.object(module.os, "O_PATH", 0x200000, create=True):
            del module.os.O_PATH
            try:
                with mock.patch.object(module.os, "open", side_effect=AssertionError("open")):
                    with self.assertRaises(module.Unavailable):
                        self.c._pin("/data/continue.sh")
            finally:
                module.os.O_PATH = 0x200000

    def test_parent_directory_pins_close_on_rejected_symlink(self):
        with mock.patch.object(module.os, "O_PATH", 0x200000, create=True), \
                mock.patch.object(module.os, "open", side_effect=[10, OSError("symlink parent")]) as opened, \
                mock.patch.object(module.os, "close") as close:
            with self.assertRaises(OSError):
                self.c._pin("/data/continue.sh")
            self.assertTrue(opened.call_args.args[1] & module.os.O_NOFOLLOW)
            close.assert_called_once_with(10)

    def test_changed_process_identity_is_rejected(self):
        with mock.patch.object(self.c, "read", side_effect=[raw_stat(7), b"0::/\n", raw_stat(7, start=124)]):
            with self.assertRaises(module.Unavailable):
                self.c.process(7)

    def test_cgroup_permission_gap_does_not_hide_process_identity(self):
        with mock.patch.object(self.c, "read", side_effect=[raw_stat(7), PermissionError("private"), raw_stat(7)]):
            result = self.c.process(7)
        self.assertEqual(result["pid"], 7)
        self.assertEqual(result["cgroup"]["error"], "PermissionError")
        self.assertNotIn("private", json.dumps(result))

    def test_actual_rdev_holder_and_parent_candidate_chain(self):
        self.c.targets[(153, 0)] = ["/dev/spidev0.0"]
        stats = {7: raw_stat(7, parent=5), 5: raw_stat(5, "tmux: server", 1), 1: raw_stat(1, "systemd", 0)}
        def read(path, limit=4096):
            pid = int(path.split("/")[2])
            return b"1:name=systemd:/system.slice/comma.service\n" if path.endswith("/cgroup") else stats[pid]
        def names(path, limit):
            if path == "/proc":
                return ["7", "5", "1"]
            return ["3"] if path == "/proc/7/fd" else []
        fdstat = types.SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=module.os.makedev(153, 0))
        with mock.patch.object(self.c, "read", side_effect=read), mock.patch.object(self.c, "names", side_effect=names), \
                mock.patch.object(self.c, "target_nodes", return_value=[]), mock.patch.object(module.os, "stat", return_value=fdstat):
            result = self.c.processes()
        obs = {o["pid"]: o for o in result["observations"]}
        self.assertEqual(obs[7]["selection"], "observed_device_holder")
        self.assertEqual(obs[5]["selection"], "known_comm_candidate_only")
        self.assertEqual(result["ancestors"][0]["chain"], [{"pid": 7, "starttime_ticks": 123}, {"pid": 5, "starttime_ticks": 123}, {"pid": 1, "starttime_ticks": 123}])
        self.assertFalse(result["scan"]["all_accessors_proven"])

    def test_pid_scan_permission_error_is_gap_not_complete_proof(self):
        with mock.patch.object(self.c, "target_nodes", return_value=[]), \
                mock.patch.object(module.os, "scandir", side_effect=PermissionError("private")):
            result = self.c.processes()
        self.assertEqual(result["scan"]["gaps"]["list:PermissionError"], 1)
        self.assertFalse(result["scan"]["all_accessors_proven"])

    def test_scan_time_budget_stops(self):
        with mock.patch.object(self.c, "target_nodes", return_value=[]), \
                mock.patch.object(self.c, "names", return_value=["7"]), \
                mock.patch.object(self.c, "monotonic", side_effect=[0, 7, 7]):
            result = self.c.processes()
        self.assertTrue(result["scan"]["truncated"])
        self.assertEqual(result["scan"]["pids_seen"], 0)

    def test_storage_only_uses_statvfs_and_sysfs_reads(self):
        vfs = types.SimpleNamespace(f_blocks=100, f_frsize=4096, f_bfree=50, f_bavail=40, f_flag=1)
        def read(path, limit=4096):
            if path == "/proc/self/mountinfo":
                return b"12 1 8:6 / / rw - ext4 /dev/sda6 rw\n"
            return {"size": b"100", "dev": b"8:0", "ro": b"0", "logical_block_size": b"4096"}[path.rsplit("/", 1)[1]]
        with mock.patch.object(module.os, "statvfs", return_value=vfs), mock.patch.object(self.c, "names", return_value=["sda", "sda6", "loop0"]), \
                mock.patch.object(self.c, "read", side_effect=read):
            result = self.c.storage()
        self.assertEqual(result["statvfs"]["/data"]["available_bytes"], 40 * 4096)
        self.assertEqual(result["block_devices"]["sda"]["capacity_bytes"], 51200)
        self.assertNotIn("loop0", result["block_devices"])

    def test_python38_and_no_execution_or_sensitive_read_apis(self):
        tree = ast.parse(SOURCE.read_text(), feature_version=8)
        imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
        self.assertNotIn("subprocess", imports)
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertTrue(calls.isdisjoint({"write", "pwrite", "ioctl", "system", "popen", "execv", "execve"}))


if __name__ == "__main__":
    unittest.main()
