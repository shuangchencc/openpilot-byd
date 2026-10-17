"""Portable evidence checks; never import public snapshots or recovered code."""

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestResearchPortability(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name) / "independent-checkout"
    self.root.mkdir()
    for directory in ("research", "hardware", "spec"):
      shutil.copytree(ROOT / directory, self.root / directory,
                      ignore=shutil.ignore_patterns("__pycache__"))
    spec = importlib.util.spec_from_file_location("portable_research_verifier", self.root / "research/verify.py")
    self.verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(self.verifier)

  def rewrite(self, relative, mutate):
    path = self.root / relative
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))

  def test_fresh_checkout_needs_no_sibling_archive_or_candidate_import(self):
    self.assertFalse((self.root / "capture-plan").exists())
    for script in ("research/verify.py", "hardware/verify_source_evidence.py"):
      with self.subTest(script=script):
        completed = subprocess.run([sys.executable, "-I", "-B", str(self.root / script)],
                                   cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual("PASS", report["status"])
        self.assertFalse(report["external_files_required"])
        self.assertFalse(report["device_access_performed"])

  def test_changed_snapshot_rejected(self):
    manifest = json.loads((self.root / "research/evidence-manifest.json").read_text())
    path = self.root / manifest["files"][0]["path"]
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    result = self.verifier.verify(self.root)
    self.assertEqual("FAIL", result["status"])
    self.assertTrue(any("hash mismatch" in e for e in result["errors"]))

  def test_external_or_absolute_manifest_path_rejected(self):
    original = (self.root / "research/evidence-manifest.json").read_text()
    for value in ("../outside.txt", str(ROOT / "research/index.json")):
      with self.subTest(value=value):
        (self.root / "research/evidence-manifest.json").write_text(original)
        self.rewrite("research/evidence-manifest.json", lambda m: m["files"][0].update(path=value))
        self.assertEqual("FAIL", self.verifier.verify(self.root)["status"])

  def test_symlink_escape_rejected(self):
    manifest = json.loads((self.root / "research/evidence-manifest.json").read_text())
    path = self.root / manifest["files"][0]["path"]
    original = ROOT / manifest["files"][0]["path"]
    path.unlink()
    path.symlink_to(original)
    result = self.verifier.verify(self.root)
    self.assertEqual("FAIL", result["status"])
    self.assertTrue(any("escapes project" in e for e in result["errors"]))

  def test_hardware_cannot_read_original_external_archive(self):
    self.rewrite("hardware/hardware-profile.json", lambda p: p["source_evidence"][0].update(
      local_path=str(ROOT / "hardware/hardware-profile.json")))
    result = subprocess.run([sys.executable, "-I", "-B", str(self.root / "hardware/verify_source_evidence.py")],
                            capture_output=True, text=True, timeout=20)
    self.assertNotEqual(0, result.returncode)
    report = json.loads(result.stdout)
    self.assertTrue(any("outside project" in e for e in report["errors"]))

  def test_new_unmanifested_file_rejected(self):
    (self.root / "research/evidence/unreviewed.txt").write_text("not reviewed\n")
    report = self.verifier.verify(self.root)
    self.assertEqual("FAIL", report["status"])
    self.assertIn("evidence directory contains missing or unmanifested files", report["errors"])

  def test_changed_plan_snapshot_rejected(self):
    path = self.root / "research/plans/capture-plan-20261017/FIELD-CARD.txt"
    path.write_bytes(path.read_bytes() + b"\nchanged\n")
    report = self.verifier.verify(self.root)
    self.assertEqual("FAIL", report["status"])
    self.assertTrue(any("hash mismatch" in error for error in report["errors"]))

  def test_live_capture_tool_cannot_silently_enter_snapshot(self):
    path = self.root / "research/plans/capture-plan-20261017/log_export.py"
    path.write_text("# not part of historical plan\n")
    report = self.verifier.verify(self.root)
    self.assertEqual("FAIL", report["status"])
    self.assertIn("plan snapshots contain missing or unmanifested files", report["errors"])

  def test_summary_hash_cannot_be_presented_as_original(self):
    manifest_path = self.root / "research/evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    entry = next(x for x in manifest["files"] if x["content_kind"] == "derived_summary_not_original")
    self.rewrite(entry["path"], lambda s: s.update(content_kind="original_file"))
    result = self.verifier.verify(self.root)
    self.assertEqual("FAIL", result["status"])
    self.assertTrue(any("summary presented as original" in e for e in result["errors"]))

  def test_malformed_manifest_fails_closed(self):
    self.rewrite("research/evidence-manifest.json", lambda m: m.update(files=[None]))
    self.assertEqual("FAIL", self.verifier.verify(self.root)["status"])


if __name__ == "__main__":
  unittest.main()
