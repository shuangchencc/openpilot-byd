"""Offline fixtures and injected syscalls only; no SSH, device or systemctl."""
import ast
import base64
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name("startup_collector.py")
SPEC = importlib.util.spec_from_file_location("current_startup_collector", SOURCE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def meta(mode=stat.S_IFREG | 0o644, inode=50, size=3, mtime=1):
    return types.SimpleNamespace(st_mode=mode, st_dev=7, st_ino=inode, st_size=size, st_uid=1000, st_gid=1001,
                                st_mtime_ns=mtime, st_ctime_ns=2)


def query(stdout=b"ActiveState=active\nExecMainPID=7\n", stderr=b"", returncode=0):
    return {"stdout": stdout, "stderr": stderr, "returncode": returncode}


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = module.Collector(self.root, runner=lambda: query())

    def test_import_constructor_inert_and_explicit_root(self):
        with mock.patch.object(module.os, "open", side_effect=AssertionError("open")), \
                mock.patch.object(module.subprocess, "Popen", side_effect=AssertionError("exec")):
            fresh = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(fresh)
            fresh.Collector(self.root)
            for root in (None, "relative"):
                with self.assertRaises(ValueError):
                    fresh.Collector(root)

    def test_denies_every_non_source_content_path_before_any_pin(self):
        paths = ("/dev/spidev0.0", "/dev/sda", "/dev/null", "/proc/1/environ", "/proc/1/cmdline",
                 "/data/params/d/key", "/home/comma/.ssh/id_rsa", "/etc/shadow", "/data/custom_op_script.sh",
                 "/data/openpilot/../params/d/key", "/data/openpilot/custom.py")
        with mock.patch.object(self.c, "_pin", side_effect=AssertionError("pin")):
            for path in paths:
                with self.subTest(path=path), self.assertRaises(module.Unavailable):
                    self.c.source(path)

    def test_pin_allowlist_rejects_before_os_open(self):
        with mock.patch.object(module.os, "open", side_effect=AssertionError("open")):
            with self.assertRaises(module.Unavailable):
                self.c._pin("/etc/passwd")

    def test_pinned_regular_read_returns_exact_private_bytes(self):
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11) as opened, \
                mock.patch.object(module.os, "fstat", side_effect=[meta()] * 3), \
                mock.patch.object(module.os, "read", side_effect=[b"abc", b""]), mock.patch.object(module.os, "close") as closed:
            result = self.c.source("/data/continue.sh")
        self.assertEqual(base64.b64decode(result["content_base64"]), b"abc")
        self.assertEqual(result["sha256"], module.sha256(b"abc"))
        self.assertEqual(result["metadata"]["uid"], 1000)
        self.assertEqual(result["metadata"]["gid"], 1001)
        self.assertTrue(result["stable_during_read"])
        self.assertFalse(result["executed"])
        self.assertEqual(opened.call_args.args[0], "/proc/self/fd/10")
        self.assertEqual([c.args[0] for c in closed.call_args_list], [11, 10])

    def test_device_fifo_symlink_rejected_without_data_open(self):
        for mode in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFLNK, stat.S_IFDIR):
            with self.subTest(mode=mode), mock.patch.object(self.c, "_pin", return_value=10), \
                    mock.patch.object(module.os, "fstat", return_value=meta(mode=mode)), \
                    mock.patch.object(module.os, "open", side_effect=AssertionError("data read")), mock.patch.object(module.os, "close"):
                with self.assertRaises(module.Unavailable):
                    self.c.source("/data/continue.sh")

    def test_per_file_size_rejected_before_content_open(self):
        with mock.patch.object(self.c, "_pin", return_value=10), \
                mock.patch.object(module.os, "fstat", return_value=meta(size=module.LIMITS["file_bytes"] + 1)), \
                mock.patch.object(module.os, "open", side_effect=AssertionError("data read")), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.source("/data/continue.sh")

    def test_total_budget_rejected_before_content_open(self):
        self.c.bytes_read = module.LIMITS["total_read_bytes"] - 2
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "fstat", return_value=meta()), \
                mock.patch.object(module.os, "open", side_effect=AssertionError("data read")), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.source("/data/continue.sh")

    def test_inode_replacement_rejected_before_read(self):
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11), \
                mock.patch.object(module.os, "fstat", side_effect=[meta(), meta(inode=51)]), \
                mock.patch.object(module.os, "read", side_effect=AssertionError("read")), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.source("/data/continue.sh")

    def test_mutation_during_read_rejected(self):
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11), \
                mock.patch.object(module.os, "fstat", side_effect=[meta(), meta(), meta(mtime=2)]), \
                mock.patch.object(module.os, "read", side_effect=[b"abc", b""]), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.source("/data/continue.sh")

    def test_short_read_rejected_even_if_metadata_same(self):
        with mock.patch.object(self.c, "_pin", return_value=10), mock.patch.object(module.os, "open", return_value=11), \
                mock.patch.object(module.os, "fstat", side_effect=[meta()] * 3), \
                mock.patch.object(module.os, "read", side_effect=[b"ab", b""]), mock.patch.object(module.os, "close"):
            with self.assertRaises(module.Unavailable):
                self.c.source("/data/continue.sh")

    def test_dynamic_metadata_never_content_opens_or_follows_symlink(self):
        with mock.patch.object(self.c, "_pin", return_value=10), \
                mock.patch.object(module.os, "fstat", return_value=meta(mode=stat.S_IFLNK)), \
                mock.patch.object(module.os, "open", side_effect=AssertionError("read")), mock.patch.object(module.os, "close"):
            result = self.c.dynamic_metadata("/data/custom_op_script.sh")
        self.assertEqual(result["metadata"]["kind"], "symlink")
        self.assertEqual(result["metadata"]["uid"], 1000)
        self.assertEqual(result["metadata"]["gid"], 1001)
        self.assertFalse(result["content_read"])
        self.assertFalse(result["symlink_followed"])

    def test_missing_linux_safe_pin_fails_closed(self):
        with mock.patch.object(module.os, "O_PATH", 0x200000, create=True):
            del module.os.O_PATH
            try:
                with mock.patch.object(module.os, "open", side_effect=AssertionError("open")), self.assertRaises(module.Unavailable):
                    self.c._pin("/data/continue.sh")
            finally:
                module.os.O_PATH = 0x200000

    def test_parent_symlink_failure_closes_pins_without_following(self):
        with mock.patch.object(module.os, "O_PATH", 0x200000, create=True), \
                mock.patch.object(module.os, "open", side_effect=[10, OSError("symlink")]) as opened, \
                mock.patch.object(module.os, "close") as closed:
            with self.assertRaises(OSError):
                self.c._pin("/data/continue.sh")
        self.assertTrue(opened.call_args.args[1] & module.os.O_NOFOLLOW)
        closed.assert_called_once_with(10)

    def test_projection_preserves_known_exit_semantics_redacts_arguments(self):
        raw = (b"KillMode=control-group\nKillSignal=9\nRestart=always\nTimeoutStopUSec=1min 30s\n"
               b"ExecStop={ path=/secret-hook ; argv[]=token-private; }\nDropInPaths=/private-secret/unit.conf\n"
               b"ControlGroup=/system.slice/comma.service\nFragmentPath=/usr/lib/systemd/system/comma.service\n"
               b"Environment=TOKEN=private-secret\n")
        result = module.systemd_projection(raw)
        self.assertEqual(result["fields"]["KillSignal"]["value"], 9)
        self.assertEqual(result["fields"]["KillMode"]["value"], "control-group")
        self.assertEqual(result["fields"]["TimeoutStopUSec"]["value"], "1min 30s")
        self.assertNotIn("value", result["fields"]["ExecStop"])
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertNotIn("secret-hook", json.dumps(result))
        self.assertNotIn("Environment", result["fields"])

    def test_projection_duplicates_and_missing_are_reported(self):
        result = module.systemd_projection(b"ActiveState=active\nActiveState=inactive\nExecStop=\n")
        self.assertEqual(result["duplicate_properties"], ["ActiveState"])
        self.assertIn("KillMode", result["missing_properties"])
        self.assertTrue(result["fields"]["ExecStop"]["empty"])

    def test_injected_runner_captures_full_private_output(self):
        result = self.c.query()
        self.assertEqual(base64.b64decode(result["stdout_base64"]), query()["stdout"])
        self.assertEqual(result["argv"], list(module.SYSTEMCTL_ARGV))
        self.assertEqual(result["projection"]["fields"]["ExecMainPID"]["value"], 7)

    def test_fixture_without_runner_never_uses_host_systemctl(self):
        with mock.patch.object(module, "run_systemctl_query", side_effect=AssertionError("query")):
            with self.assertRaises(module.Unavailable):
                module.Collector(self.root).query()

    def test_runner_output_cap_and_types_fail_closed(self):
        values = [query(stdout=b"x" * (module.LIMITS["query_output_bytes"] + 1)),
                  {"stdout": "not-bytes", "stderr": b"", "returncode": 0},
                  {"stdout": b"", "stderr": b"", "returncode": True}, {}]
        for value in values:
            with self.subTest(value_type=type(value)), self.assertRaises(module.Unavailable):
                module.Collector(self.root, runner=lambda: value).query()

    def test_nonzero_query_has_no_loaded_projection(self):
        result = module.Collector(self.root, runner=lambda: query(stderr=b"secret diagnostic", returncode=1)).query()
        self.assertEqual(result["status"], "query_failed")
        self.assertIsNone(result["projection"])
        self.assertEqual(base64.b64decode(result["stderr_base64"]), b"secret diagnostic")

    def test_collection_brackets_sources_without_asserting_atomicity(self):
        order = []
        def run():
            order.append("query")
            return query()
        c = module.Collector(self.root, runner=run)
        with mock.patch.object(c, "source", side_effect=lambda p: order.append(p) or {"status": "collected"}), \
                mock.patch.object(c, "dynamic_metadata", return_value={"status": "metadata_only"}):
            result = c.collect()
        self.assertEqual(order[0], "query")
        self.assertEqual(order[-1], "query")
        self.assertEqual(order[1:-1], list(module.SOURCE_PATHS))
        self.assertTrue(result["systemd_loaded"]["same_output_across_collection"])
        self.assertFalse(result["consistency"]["atomic_across_objects"])
        self.assertEqual(result["boundary"]["profile"], "UNBOUND")

    def test_changed_loaded_state_detected(self):
        results = iter([query(), query(stdout=b"ActiveState=inactive\nExecMainPID=0\n")])
        c = module.Collector(self.root, runner=lambda: next(results))
        with mock.patch.object(c, "source", return_value={"status": "collected"}), \
                mock.patch.object(c, "dynamic_metadata", return_value={"status": "metadata_only"}):
            result = c.collect()
        self.assertFalse(result["systemd_loaded"]["same_output_across_collection"])

    def test_permission_errors_are_gaps_without_private_paths_in_error(self):
        with mock.patch.object(self.c, "source", side_effect=PermissionError("private path")), \
                mock.patch.object(self.c, "dynamic_metadata", side_effect=FileNotFoundError("private path")):
            result = self.c.collect()
        self.assertEqual(result["gaps"]["source:PermissionError"], len(module.SOURCE_PATHS))
        self.assertNotIn("private path", json.dumps(result))

    def test_expired_total_budget_does_not_query(self):
        self.c.deadline = 1
        with mock.patch.object(self.c, "monotonic", return_value=2), \
                mock.patch.object(self.c, "runner", side_effect=AssertionError("query")):
            with self.assertRaises(module.Unavailable):
                self.c.query()

    def test_fixed_query_subprocess_no_shell_no_inherited_environment(self):
        proc = mock.Mock()
        proc.stdout, proc.stderr = mock.Mock(), mock.Mock()
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        selector = mock.Mock()
        selector.get_map.return_value = {}
        with mock.patch.object(module.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(module.selectors, "DefaultSelector", return_value=selector):
            result = module.run_systemctl_query()
        self.assertEqual(result, query(stdout=b"", stderr=b""))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], list(module.SYSTEMCTL_ARGV))
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.assertNotIn("HOME", kwargs["env"])
        self.assertEqual(kwargs["stdin"], module.subprocess.DEVNULL)
        proc.kill.assert_not_called()

    def test_query_timeout_kills_and_reaps_only_owned_query(self):
        proc = mock.Mock()
        proc.stdout, proc.stderr = mock.Mock(), mock.Mock()
        proc.poll.return_value = None
        selector = mock.Mock()
        selector.get_map.return_value = {1: object()}
        with mock.patch.object(module.subprocess, "Popen", return_value=proc), \
                mock.patch.object(module.selectors, "DefaultSelector", return_value=selector), \
                mock.patch.object(module.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaises(module.Unavailable):
                module.run_systemctl_query()
        proc.kill.assert_called_once_with()
        proc.wait.assert_called_once_with(timeout=1)

    def test_query_output_overflow_fails_and_kills_only_query(self):
        proc = mock.Mock()
        proc.stdout, proc.stderr = mock.Mock(), mock.Mock()
        proc.poll.return_value = None
        selector = mock.Mock()
        selector.get_map.return_value = {1: object()}
        selector.select.return_value = [(types.SimpleNamespace(fd=1, data="stdout"), 1)]
        with mock.patch.object(module.subprocess, "Popen", return_value=proc), \
                mock.patch.object(module.selectors, "DefaultSelector", return_value=selector), \
                mock.patch.object(module.os, "read", return_value=b"x" * (module.LIMITS["query_output_bytes"] + 1)):
            with self.assertRaises(module.Unavailable):
                module.run_systemctl_query()
        proc.kill.assert_called_once_with()

    def test_python38_and_no_mutating_or_target_execution_apis(self):
        tree = ast.parse(SOURCE.read_text(), feature_version=8)
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertTrue(calls.isdisjoint({"write", "pwrite", "ioctl", "system", "execv", "execve", "chmod", "unlink", "mkdir"}))
        self.assertNotIn("start", module.SYSTEMCTL_ARGV)
        self.assertNotIn("stop", module.SYSTEMCTL_ARGV)
        self.assertNotIn("daemon-reload", module.SYSTEMCTL_ARGV)
        self.assertNotIn("Environment", module.PROPERTIES)
        self.assertNotIn("EnvironmentFiles", module.PROPERTIES)


if __name__ == "__main__":
    unittest.main()
