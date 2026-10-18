"""Source-only tests for deterministic packaging and fail-closed evidence."""
import copy
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from maintenance import core, integration as stage


def report_fixture():
  report = {"schema": "songplus-integration-validation/v1", "status": "PASS_OFFLINE_INTEGRATION",
            "lock_sha256": "a" * 64, "run_id": "fixture", "failed": 0, "skipped": 0, **stage.DENIED}
  report["acceptance"] = [{"id": name, "status": "PASS", "executed": 1, "failed": 0, "skipped": 0,
                           "run_id": report["run_id"], "lock_sha256": report["lock_sha256"]} for name in stage.CASE_IDS]
  return report


class IntegrationEvidenceTests(unittest.TestCase):
  def test_untracked_discoverable_test_or_module_is_rejected(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      (root / "maintenance").mkdir()
      (root / "maintenance/known.py").write_text("# locked")
      stage.cover_discovered_sources(root, {"maintenance/known.py"})
      for name in ("test_extra.py", "injected_helper.py"):
        extra = root / "maintenance" / name
        extra.write_text("raise AssertionError('must not execute')")
        with self.assertRaisesRegex(ValueError, "Unpinned maintenance"):
          stage.cover_discovered_sources(root, {"maintenance/known.py"})
        extra.unlink()

  def test_complete_context_is_only_offline(self):
    report = report_fixture()
    stage.check_report_context(report, "a" * 64)
    self.assertFalse(report["deployment_ready"])

  def test_no_missing_duplicated_or_reordered_cases(self):
    for index in range(len(stage.CASE_IDS)):
      report = report_fixture()
      del report["acceptance"][index]
      with self.subTest(missing=index), self.assertRaises(ValueError):
        stage.check_report_context(report, "a" * 64)
    for replace in (lambda r: r["acceptance"].reverse(), lambda r: r["acceptance"].append(r["acceptance"][0])):
      report = report_fixture()
      replace(report)
      with self.assertRaises(ValueError):
        stage.check_report_context(report, "a" * 64)

  def test_no_empty_failed_skipped_or_planned_acceptance(self):
    for field, value in (("executed", 0), ("executed", True), ("executed", -1), ("executed", "1"),
                         ("failed", 1), ("skipped", 1), ("status", "PLANNED")):
      report = report_fixture()
      report["acceptance"][0][field] = value
      with self.subTest(field=field, value=value), self.assertRaises(ValueError):
        stage.check_report_context(report, "a" * 64)

  def test_scope_cannot_be_promoted_by_report_flags(self):
    for key in stage.DENIED:
      for value in (True, 0, "false", None):
        report = report_fixture()
        report[key] = value
        with self.subTest(key=key, value=value), self.assertRaises(ValueError):
          stage.check_report_context(report, "a" * 64)

  def test_old_report_or_case_context_fails(self):
    for scope, field in (("report", "lock_sha256"), ("case", "lock_sha256"), ("case", "run_id")):
      report = report_fixture()
      target = report if scope == "report" else report["acceptance"][0]
      target[field] = "historical"
      with self.subTest(scope=scope, field=field), self.assertRaises(ValueError):
        stage.check_report_context(report, "a" * 64)

  def test_report_status_and_aggregate_failures_fail(self):
    for field, value in (("status", "RUNNING"), ("failed", 1), ("skipped", 1), ("schema", "other")):
      report = report_fixture()
      report[field] = value
      with self.subTest(field=field), self.assertRaises(ValueError):
        stage.check_report_context(report, "a" * 64)

  def test_deterministic_source_bundle_excludes_git_and_normalizes_metadata(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      hashes = []
      for dirname in ("left", "right"):
        out = root / dirname
        (out / "host/.git").mkdir(parents=True)
        (out / "host/.git/private").write_text("not bundled")
        (out / "host/source.py").write_text("print('offline')\n")
        (out / "host/source.py").chmod(0o755 if dirname == "left" else 0o600)
        hashes.append(stage.bundle_sources(out, {"host": core.tree_hashes(out / "host")}))
        with tarfile.open(out / "source.tar") as bundle:
          members = bundle.getmembers()
          self.assertEqual([m.name for m in members], ["host/source.py"])
          self.assertEqual((members[0].mode, members[0].mtime, members[0].uid, members[0].gid), (0o644, 0, 0, 0))
      self.assertEqual(hashes[0], hashes[1])

  def test_bundle_rejects_path_escape_and_symlinks(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = Path(tmp)
      (root / "host").mkdir()
      (root / "outside").write_text("private")
      (root / "host/link").symlink_to(root / "outside")
      for relative in ("../outside", "link"):
        with self.subTest(relative=relative), self.assertRaises(ValueError):
          stage.bundle_sources(root, {"host": {relative: "unused"}})


if __name__ == "__main__":
  unittest.main()
