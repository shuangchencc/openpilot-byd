"""Offline only: injected systemctl runner, lstat and subprocess/syscall fakes."""
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

SOURCE = Path(__file__).with_name("loaded_exit_collector.py")
SPEC = importlib.util.spec_from_file_location("current_loaded_exit_collector", SOURCE)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def result(stdout=b"Type=oneshot\nUser=comma\nExecStartPre=\nExecStartPost=\nExecStopPost=\n", stderr=b"", returncode=0):
    return {"stdout": stdout, "stderr": stderr, "returncode": returncode}


def meta(mode=stat.S_IFREG | 0o644):
    return types.SimpleNamespace(st_mode=mode, st_dev=7, st_ino=8, st_size=0,
                                st_uid=1000, st_gid=1001, st_mtime_ns=1, st_ctime_ns=2)


class LoadedExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = module.Collector(self.root, runner=lambda: result(), lstat=lambda p: meta())

    def test_import_constructor_inert_explicit_root(self):
        with mock.patch.object(module.os, "open", side_effect=AssertionError("open")), \
                mock.patch.object(module.os, "lstat", side_effect=AssertionError("stat")), \
                mock.patch.object(module.subprocess, "Popen", side_effect=AssertionError("exec")):
            fresh = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(fresh)
            fresh.Collector(self.root)
            for root in (None, "relative"):
                with self.assertRaises(ValueError):
                    fresh.Collector(root)

    def test_all_query_is_fixed_and_exact_property_allowlist(self):
        argv = module.SYSTEMCTL_ARGV
        self.assertEqual(argv[:5], ("/bin/systemctl", "show", "comma.service", "--all", "--no-pager"))
        self.assertEqual(len(argv), 6)
        self.assertEqual(argv[5], "--property=" + ",".join(module.PROPERTIES))
        self.assertTrue({"Type", "User", "ExecStartPre", "ExecStartPost", "ExecStopPost"} <= set(module.PROPERTIES))
        self.assertNotIn("Environment", module.PROPERTIES)
        self.assertNotIn("EnvironmentFiles", module.PROPERTIES)

    def test_empty_hook_is_present_empty_not_missing(self):
        out = self.c.query()
        fields = out["projection"]["fields"]
        for key in ("ExecStartPre", "ExecStartPost", "ExecStopPost"):
            self.assertTrue(fields[key]["empty"])
            self.assertEqual(fields[key]["value"], "")
            self.assertNotIn(key, out["projection"]["missing_properties"])
        self.assertEqual(fields["Type"]["value"], "oneshot")
        self.assertEqual(fields["User"]["value"], "comma")

    def test_missing_hook_remains_unknown_even_with_all_option(self):
        projected = module.systemd_projection(b"Type=oneshot\nUser=comma\n")
        self.assertIn("ExecStopPost", projected["missing_properties"])
        self.assertNotIn("ExecStopPost", projected["fields"])

    def test_unknown_user_and_hook_arguments_redacted_in_projection(self):
        raw = b"User=private-user\nExecStopPost={ path=/private/script; argv[]=token-secret; }\n"
        projected = module.systemd_projection(raw)
        text = json.dumps(projected)
        self.assertNotIn("private-user", text)
        self.assertNotIn("private/script", text)
        self.assertNotIn("token-secret", text)
        self.assertFalse(projected["fields"]["ExecStopPost"]["empty"])

    def test_full_raw_output_only_encoded_private_fields(self):
        out = self.c.query()
        self.assertEqual(base64.b64decode(out["stdout_base64"]), result()["stdout"])
        self.assertEqual(out["stdout_sha256"], module.sha256(result()["stdout"]))
        self.assertEqual(out["stdout_bytes"], len(result()["stdout"]))

    def test_marker_only_lstats_fixed_path_and_preserves_ownership(self):
        with mock.patch.object(self.c, "lstat", return_value=meta()) as called, \
                mock.patch.object(module.os, "open", side_effect=AssertionError("content read")):
            out = self.c.boot_marker()
        called.assert_called_once_with(str(self.root / "tmp/booted"))
        self.assertEqual(out["metadata"]["uid"], 1000)
        self.assertEqual(out["metadata"]["gid"], 1001)
        self.assertFalse(out["content_read"])
        self.assertFalse(out["symlink_followed"])

    def test_symlink_marker_never_follows_or_reads(self):
        with mock.patch.object(self.c, "lstat", return_value=meta(stat.S_IFLNK | 0o777)), \
                mock.patch.object(module.os, "open", side_effect=AssertionError("read")), \
                mock.patch.object(module.os, "readlink", side_effect=AssertionError("follow")):
            out = self.c.boot_marker()
        self.assertEqual(out["metadata"]["kind"], "symlink")
        self.assertFalse(out["symlink_followed"])

    def test_missing_marker_reports_point_in_time_gap(self):
        with mock.patch.object(self.c, "lstat", side_effect=FileNotFoundError("private-path")):
            out = self.c.collect()
        self.assertEqual(out["boot_marker"], {"status": "unknown", "error": "FileNotFoundError"})
        self.assertEqual(out["gaps"]["boot_marker:FileNotFoundError"], 1)
        self.assertNotIn("private-path", json.dumps(out))

    def test_query_brackets_marker_but_not_atomic(self):
        order = []
        c = module.Collector(self.root, runner=lambda: order.append("query") or result(),
                             lstat=lambda p: order.append("lstat") or meta())
        out = c.collect()
        self.assertEqual(order, ["query", "lstat", "query"])
        self.assertTrue(out["systemd_loaded"]["same_output_across_collection"])
        self.assertFalse(out["consistency"]["atomic_snapshot"])
        self.assertFalse(out["consistency"]["process_cleanup_or_board_exit_state_verified"])
        self.assertEqual(out["boundary"]["profile"], "UNBOUND")

    def test_change_between_loaded_queries_detected(self):
        outputs = iter([result(), result(stdout=b"Type=oneshot\nUser=root\n")])
        c = module.Collector(self.root, runner=lambda: next(outputs), lstat=lambda p: meta())
        self.assertFalse(c.collect()["systemd_loaded"]["same_output_across_collection"])

    def test_nonzero_query_is_not_projected_as_loaded_state(self):
        c = module.Collector(self.root, runner=lambda: result(stderr=b"error", returncode=1))
        out = c.query()
        self.assertEqual(out["status"], "query_failed")
        self.assertIsNone(out["projection"])
        self.assertEqual(base64.b64decode(out["stderr_base64"]), b"error")

    def test_fixture_no_runner_never_queries_host(self):
        with mock.patch.object(module, "run_systemctl_query", side_effect=AssertionError("query")):
            with self.assertRaises(module.Unavailable):
                module.Collector(self.root).query()

    def test_runner_types_and_budget_fail_closed(self):
        values = [{}, result(stdout=b"x" * (module.LIMITS["query_output_bytes"] + 1)),
                  result(stdout="not-bytes"), result(returncode=True)]
        for value in values:
            with self.subTest(value_type=type(value)), self.assertRaises(module.Unavailable):
                module.Collector(self.root, runner=lambda: value).query()

    def test_total_budget_prevents_lstat_and_query(self):
        self.c.deadline = 1
        with mock.patch.object(self.c, "monotonic", return_value=2), \
                mock.patch.object(self.c, "lstat", side_effect=AssertionError("lstat")), \
                mock.patch.object(self.c, "runner", side_effect=AssertionError("query")):
            with self.assertRaises(module.Unavailable):
                self.c.boot_marker()
            with self.assertRaises(module.Unavailable):
                self.c.query()

    def test_duplicate_property_is_explicit(self):
        projected = module.systemd_projection(b"Type=oneshot\nType=simple\n")
        self.assertEqual(projected["duplicate_properties"], ["Type"])

    def test_actual_runner_fixed_subprocess_no_shell_no_environment_inheritance(self):
        proc = mock.Mock()
        proc.stdout, proc.stderr = mock.Mock(), mock.Mock()
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        selector = mock.Mock()
        selector.get_map.return_value = {}
        with mock.patch.object(module.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(module.selectors, "DefaultSelector", return_value=selector):
            out = module.run_systemctl_query()
        self.assertEqual(out, result(stdout=b""))
        args, kwargs = popen.call_args
        self.assertEqual(args[0], list(module.SYSTEMCTL_ARGV))
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.assertNotIn("HOME", kwargs["env"])
        proc.kill.assert_not_called()

    def test_timeout_kills_only_collector_owned_query(self):
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

    def test_oversized_query_is_rejected_and_reaped(self):
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

    def test_python38_independent_no_content_read_or_mutating_apis(self):
        tree = ast.parse(SOURCE.read_text(), feature_version=8)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertNotIn("startup_collector", imported)
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertTrue(calls.isdisjoint({"open", "read_text", "read_bytes", "write", "pwrite", "ioctl", "system", "execv", "chmod", "unlink"}))
        self.assertFalse(set(module.SYSTEMCTL_ARGV) & {"start", "stop", "daemon-reload", "restart"})


if __name__ == "__main__":
    unittest.main()
