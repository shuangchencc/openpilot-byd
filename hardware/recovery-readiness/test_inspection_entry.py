"""Offline entry tests: fake SSH only, temporary local files, no credentials."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "recovery_entry", Path(__file__).resolve().parents[2] / "tools/inspect_c3xl_recovery_readiness.py")
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "results").mkdir()
        self.source = b"# inert synthetic collector\n"
        for path, unused in entry.COLLECTORS.values():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.source)
        # Deliberately fake material: the entry may stat, but must not read it.
        self.key = self.root / "synthetic-key"
        self.key.write_text("not a key")
        self.config = self.root / "connection.json"
        self.configuration = {"host": "192.0.2.7", "user": "comma", "port": 22,
                              "identity_file": str(self.key)}
        self.config.write_text(json.dumps(self.configuration))
        self.patch_root = patch.object(entry, "ROOT", self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def invoke(self, arguments, runner=None):
        with patch("sys.argv", ["entry"] + arguments), patch.object(entry.subprocess, "run", runner) as run, contextlib.redirect_stdout(io.StringIO()) as output:
            result = entry.main()
        return result, output.getvalue(), run

    def args(self, phase="supervisor"):
        return [phase, "--connect", "--connection", str(self.config)]

    def response(self, phase="supervisor", code=0, payload=None):
        if payload is None:
            payload = {"schema": entry.COLLECTORS[phase][1], "collected_utc": "synthetic"}
        return subprocess.CompletedProcess([], code, json.dumps(payload).encode(), b"")

    def receipts(self):
        return [json.loads(p.read_text()) for p in (self.root / "results").glob("*/receipt.json")]

    def test_preview_never_reads_configuration_or_connects(self):
        self.config.unlink()
        for phase in entry.COLLECTORS:
            result, raw, unused = self.invoke([phase, "--connection", str(self.config)],
                                             lambda *a, **k: self.fail("SSH in preview"))
            self.assertEqual(result, 0)
            self.assertFalse(json.loads(raw)["config_read"])
        self.assertEqual(self.receipts(), [])

    def test_connect_is_fixed_stdin_python_with_strict_ssh(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            self.assertEqual(kwargs["input"], self.source)
            self.assertEqual(kwargs["timeout"], 45)
            self.assertEqual(command[-1], "/usr/bin/python3 -I -S -B -")
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("ClearAllForwardings=yes", command)
            self.assertIn("ControlPath=none", command)
            self.assertNotIn("sudo", command)
            return self.response()
        original_read_bytes = Path.read_bytes
        def guarded_read(path):
            self.assertNotEqual(path, self.key)
            return original_read_bytes(path)
        with patch.object(Path, "read_bytes", guarded_read):
            result, unused, unused_run = self.invoke(self.args(), run)
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        receipt = self.receipts()[0]
        self.assertTrue(receipt["source_unchanged"])
        for field in ("services_controlled", "target_writes_requested", "usb_spi_control_io", "can_tx", "profile_binding_changed"):
            self.assertFalse(receipt[field])

    def test_gpt_schema_selected(self):
        result, unused, unused_run = self.invoke(self.args("gpt-header"), lambda *a, **k: self.response("gpt-header"))
        self.assertEqual(result, 0)
        self.assertEqual(self.receipts()[0]["phase"], "gpt-header")

    def test_invalid_endpoint_rejected_before_ssh(self):
        for field, value in (("host", "host; command"), ("user", "root"), ("port", True), ("port", 0), ("identity_file", "relative")):
            self.config.write_text(json.dumps(dict(self.configuration, **{field: value})))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.invoke(self.args(), lambda *a, **k: self.fail("unexpected SSH"))
        self.assertEqual(self.receipts(), [])

    def test_strict_json(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                entry.strict_json(raw)

    def test_wrong_schema_or_exit_rejected(self):
        for response in (self.response("gpt-header"), self.response(code=1), self.response(payload={"schema": entry.COLLECTORS["supervisor"][1]})):
            result, unused, unused_run = self.invoke(self.args(), lambda *a, **k: response)
            self.assertEqual(result, 2)
        self.assertTrue(all(r["status"] == "FAILED" for r in self.receipts()))

    def test_timeout_preserves_partial_without_retry(self):
        calls = []
        def run(*args, **kwargs):
            calls.append(1)
            raise subprocess.TimeoutExpired("fake", 45, output=b"{", stderr=b"partial")
        result, unused, unused_run = self.invoke(self.args(), run)
        self.assertEqual(result, 2)
        self.assertEqual(calls, [1])
        self.assertEqual(self.receipts()[0]["status"], "TIMEOUT")

    def test_changed_source_invalidates_receipt(self):
        def run(*args, **kwargs):
            (self.root / entry.COLLECTORS["supervisor"][0]).write_bytes(b"changed")
            return self.response()
        result, unused, unused_run = self.invoke(self.args(), run)
        self.assertEqual(result, 2)
        self.assertEqual(self.receipts()[0]["status"], "SOURCE_CHANGED")

    def test_oversize_or_duplicate_payload_rejected(self):
        for raw in (b" " * (1024 * 1024 + 1), b'{"schema":"x","schema":"y"}'):
            result, unused, unused_run = self.invoke(self.args(), lambda *a, **k: subprocess.CompletedProcess([], 0, raw, b""))
            self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
