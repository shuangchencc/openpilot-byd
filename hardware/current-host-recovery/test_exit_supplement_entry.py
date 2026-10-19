"""Offline supplement host validation; never SSH or read authentication keys."""
import base64
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock

ENTRY = Path(__file__).resolve().parents[2] / "tools/inspect_current_exit_supplement.py"
SPEC = importlib.util.spec_from_file_location("exit_supplement_entry_under_test", ENTRY)
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


def fixture():
    stdout = (b"Type=oneshot\nUser=comma\nExecStartPre=\nExecStartPost=\nExecStopPost=\n"
              b"ExecStart={ path=/private-command; argv[]=private-secret; }\n")
    meta = types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_dev=1, st_ino=2,
                                 st_uid=1000, st_gid=1000, st_size=0, st_mtime_ns=1, st_ctime_ns=2)
    return entry.collector.collect("/offline-fixture", runner=lambda: {"stdout": stdout, "stderr": b"", "returncode": 0},
                                    lstat=lambda path: meta)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "payload.partial"
        self.data = fixture()

    def check(self, data=None, result_override=None):
        raw = json.dumps(self.data if data is None else data).encode()
        self.path.write_bytes(raw)
        result = {"exit_code": 0, "bytes": len(raw), "sha256": entry.sha(raw), "stderr": b""}
        result.update(result_override or {})
        return entry.validate_payload(result, self.path)

    def test_complete_validation_recomputes_and_redacts_private_query(self):
        out = self.check()
        text = json.dumps(out)
        self.assertTrue(out["boundary_verified"])
        self.assertTrue(out["systemd_loaded"]["same_output_across_collection"])
        for value in ("base64", "private-command", "private-secret", '"argv"'):
            self.assertNotIn(value, text)

    def test_transport_hash_or_exit_or_remote_stderr_rejected(self):
        for override in ({"exit_code": 1}, {"sha256": "0" * 64}, {"bytes": 0}, {"stderr": b"private error"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.check(result_override=override)

    def test_schema_profile_and_boundary_violations_rejected(self):
        variants = []
        a = copy.deepcopy(self.data); a["schema"] = "wrong"; variants.append(a)
        a = copy.deepcopy(self.data); a["boundary"]["profile"] = "BOUND"; variants.append(a)
        for key in ("can_tx", "services_changed", "boot_marker_content_read", "target_files_written"):
            a = copy.deepcopy(self.data); a["boundary"][key] = True; variants.append(a)
        for value in variants:
            with self.subTest(value=value.get("schema")), self.assertRaises(ValueError):
                self.check(value)

    def test_fixed_query_argv_and_exit_code_checked(self):
        for key, value in (("argv", ["/bin/systemctl", "stop", "comma.service"]), ("returncode", 1), ("status", "query_failed")):
            changed = copy.deepcopy(self.data); changed["systemd_loaded"]["before"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(changed)

    def test_base64_length_digest_and_projection_are_checked(self):
        cases = (("stdout_base64", "%%invalid%%"), ("stdout_bytes", 1), ("stdout_sha256", "0" * 64),
                 ("projection", {"forged": True}))
        for key, value in cases:
            changed = copy.deepcopy(self.data); changed["systemd_loaded"]["before"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(changed)

    def test_reported_consistency_false_is_not_accepted(self):
        self.data["systemd_loaded"]["same_output_across_collection"] = False
        with self.assertRaisesRegex(ValueError, "reported_query_consistency"):
            self.check()

    def test_changed_loaded_state_even_honestly_reported_is_rejected(self):
        item = self.data["systemd_loaded"]["after"]
        raw = b"Type=simple\nUser=comma\n"
        item.update(stdout_base64=base64.b64encode(raw).decode(), stdout_bytes=len(raw), stdout_sha256=entry.sha(raw),
                    projection=entry.collector.systemd_projection(raw))
        self.data["systemd_loaded"]["same_output_across_collection"] = False
        with self.assertRaisesRegex(ValueError, "loaded_state_changed"):
            self.check()

    def test_marker_content_follow_and_path_violations_rejected(self):
        for key, value in (("content_read", True), ("symlink_followed", True), ("path", "/private-marker")):
            changed = copy.deepcopy(self.data); changed["boot_marker"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(changed)

    def test_marker_metadata_types_and_kind_validated(self):
        for key, value in (("kind", "symlink"), ("uid", "1000"), ("inode", -1)):
            changed = copy.deepcopy(self.data); changed["boot_marker"]["metadata"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(changed)

    def test_missing_marker_preserved_as_unknown_without_source_content(self):
        self.data["boot_marker"] = {"status": "unknown", "error": "FileNotFoundError"}
        out = self.check()
        self.assertEqual(out["boot_marker"], self.data["boot_marker"])
        self.data["boot_marker"]["private_content"] = "secret"
        with self.assertRaises(ValueError):
            self.check()

    def test_atomic_or_exit_state_claim_rejected(self):
        self.data["consistency"]["process_cleanup_or_board_exit_state_verified"] = True
        with self.assertRaisesRegex(ValueError, "consistency_claim"):
            self.check()

    def test_timestamp_requires_timezone_and_order(self):
        self.data["collected_utc"] = "2026-10-20T01:00:00"
        with self.assertRaises(ValueError):
            self.check()


class MainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "engineering"
        self.root.mkdir()
        self.destination = self.base / "private"
        self.destination.mkdir(mode=0o700)
        self.sources = {}
        for attr, name in (("COLLECTOR_PATH", "collector.py"), ("WRAPPER_PATH", "wrapper.py"), ("SHARED_PATH", "helper.py")):
            path = self.root / name
            path.write_bytes(("# " + name + "\n").encode())
            self.sources[attr] = path
            patcher = mock.patch.object(entry, attr, path); patcher.start(); self.addCleanup(patcher.stop)
        for attr, value in (("ROOT", self.root),):
            patcher = mock.patch.object(entry, attr, value); patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(entry, "ssh_command", return_value=["fake-ssh"])
        self.command = patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(entry.os, "statvfs", return_value=types.SimpleNamespace(f_bavail=1024**3, f_frsize=1))
        patcher.start(); self.addCleanup(patcher.stop)

    def run_main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            status = entry.main(argv)
        return status, json.loads(output.getvalue())

    def receive(self, command, source, output, maximum, timeout, object_id):
        raw = json.dumps(fixture()).encode()
        entry.private_write(output, raw)
        return {"exit_code": 0, "bytes": len(raw), "sha256": entry.sha(raw), "stderr": b"", "elapsed_seconds": 0.01}

    def test_preview_has_no_connection_read_or_destination_mutation(self):
        with mock.patch.object(entry, "receive", side_effect=AssertionError("receive")):
            status, out = self.run_main([])
        self.assertEqual(status, 0)
        self.assertEqual(out["mode"], "PREVIEW_NO_CONNECTION")
        self.assertFalse(out["config_read"])
        self.command.assert_not_called()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_private_external_destination_required_before_ssh(self):
        self.root.chmod(0o700)
        for destination in (None, self.root):
            argv = ["--connect"] + (["--destination", str(destination)] if destination else [])
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.run_main(argv)
        self.command.assert_not_called()

    def test_exact_0700_mode_required(self):
        self.destination.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private_directory_mode"):
            self.run_main(["--connect", "--destination", str(self.destination)])
        self.command.assert_not_called()

    def test_destination_symlink_rejected(self):
        alias = self.base / "alias"; alias.symlink_to(self.destination)
        with self.assertRaisesRegex(ValueError, "symlink_destination"):
            self.run_main(["--connect", "--destination", str(alias)])
        self.command.assert_not_called()

    def test_existing_capture_cannot_be_overwritten(self):
        (self.destination / "loaded-exit").mkdir()
        with self.assertRaisesRegex(ValueError, "destination_already_exists"):
            self.run_main(["--connect", "--destination", str(self.destination)])
        self.command.assert_not_called()

    def test_success_saves_three_sources_private_payload_and_sanitized_receipt(self):
        with mock.patch.object(entry, "receive", side_effect=self.receive):
            status, out = self.run_main(["--connect", "--destination", str(self.destination)])
        self.assertEqual(status, 0)
        directory = self.destination / "loaded-exit"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ("collector.py", "wrapper.py", "shared_capture_helper.py", "loaded-exit.json", "receipt.json"):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        receipt = json.loads((directory / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "VERIFIED_PASSIVE_EXIT_SUPPLEMENT")
        for name, digest in receipt["source_sha256"].items():
            self.assertEqual(entry.sha((directory / name).read_bytes()), digest)
        self.assertNotIn("private-secret", json.dumps(receipt))
        self.assertNotIn("base64", json.dumps(receipt))
        self.assertNotIn('"argv"', json.dumps(receipt))

    def test_source_change_marks_partial_failed(self):
        def receive_changed(*args):
            out = self.receive(*args)
            self.sources["SHARED_PATH"].write_bytes(b"# changed\n")
            return out
        with mock.patch.object(entry, "receive", side_effect=receive_changed):
            status, out = self.run_main(["--connect", "--destination", str(self.destination)])
        self.assertEqual(status, 2)
        directory = self.destination / "loaded-exit"
        self.assertTrue((directory / "loaded-exit.json.partial").exists())
        self.assertFalse((directory / "loaded-exit.json").exists())

    def test_receive_failure_retains_sanitized_receipt_without_retry(self):
        failure = entry.shared.ReceiveError("local_ssh_timeout", {"exit_code": None, "bytes": 0,
            "sha256": entry.sha(b""), "stderr": b"private diagnostic", "elapsed_seconds": 45,
            "receive_failure": "local_ssh_timeout"})
        with mock.patch.object(entry, "receive", side_effect=failure) as received:
            status, out = self.run_main(["--connect", "--destination", str(self.destination)])
        self.assertEqual(status, 2)
        self.assertEqual(received.call_count, 1)
        receipt = (self.destination / "loaded-exit/receipt.json").read_text()
        self.assertNotIn("private diagnostic", receipt)
        self.assertEqual(json.loads(receipt)["automatic_retries"], 0)


if __name__ == "__main__":
    unittest.main()
