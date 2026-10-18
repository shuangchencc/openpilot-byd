"""Metadata/replay boundary tests with entirely synthetic local inputs."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core
from runtime_guard import REQUIRED_CASE_IDS


def write(root, name, content):
  path = root / name
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(content if isinstance(content, bytes) else content.encode())
  return path


def write_json(root, name, value):
  return write(root, name, json.dumps(value))


def lock_fixture(root):
  """Construct a complete metadata-only lock without an official checkout."""
  baseline = {
    "upstream-openpilot": "1" * 40,
    "upstream-openpilot/panda": "2" * 40,
    "upstream-openpilot/opendbc_repo": "3" * 40,
    "upstream-openpilot/msgq_repo": "4" * 40,
  }
  host_series = []
  for index, stage in enumerate(("P0", "P1", "P2", "P3", "P4", "P6")):
    name = f"fixture-{stage}.patch"
    commit = str(index + 1) * 40
    path = write(root, "patches/openpilot/p6/" + name,
                 f"From {commit} Mon Sep 17 00:00:00 2001\nSubject: fixture\n")
    host_series.append({"file": name, "sha256": core.sha(path), "commit": commit, "stage": stage})
  panda_path = write(root, "patches/panda/p5/fixture-P5.patch", f"From {'7' * 40} Mon Sep 17 00:00:00 2001\nSubject: fixture\n")
  panda_series = [{"file": panda_path.name, "sha256": core.sha(panda_path), "commit": "7" * 40, "stage": "P5"}]
  prior_names = ["patches/openpilot/p0p1/manifest.json", "patches/openpilot/p2/manifest.json",
                 "patches/openpilot/p3/manifest.json", "patches/openpilot/p4/manifest.json"]
  previous = None
  for name in prior_names:
    value = {"baselines": baseline, "series": []}
    if previous:
      value["depends_on_manifest_sha256"] = core.sha(root / previous)
    write_json(root, name, value)
    previous = name
  panda = {"baselines": baseline, "series": panda_series,
           "host_p0_p4_manifest_sha256": core.sha(root / previous), "baseline_inputs": {}, "dependencies": {}}
  write_json(root, core.PANDA, panda)
  host = {"baselines": baseline, "series": host_series, "baseline_inputs": {},
          "depends_on_manifest_sha256": core.sha(root / previous), "p5_manifest_sha256": core.sha(root / core.PANDA)}
  write_json(root, core.HOST, host)
  write_json(root, core.DESIGN, {"cases": [{"id": name, "required": True} for name in REQUIRED_CASE_IDS]})
  write_json(root, "upstream-lock.json", {"repos": [{"path": path, "commit": commit} for path, commit in baseline.items()]})
  files = set(prior_names) | {core.HOST, core.PANDA, core.DESIGN, "upstream-lock.json"}
  for name in ("tools/songplus_compat.py", "maintenance/core.py", "maintenance/validation.py", "maintenance/runtime_guard.py", "maintenance/child_evidence.py",
               "maintenance/reproducibility.py", "maintenance/release_policy.json", "tools/validate_panda_p6.py",
               "tools/validate_panda_firmware.py", "tools/check_panda_ci.py", ".github/workflows/panda-compat-offline.yml"):
    write(root, name, "synthetic locked file\n")
    files.add(name)
  series = {"host": core.normalized_series(root, core.HOST), "panda": core.normalized_series(root, core.PANDA), "opendbc": []}
  files.update(item["file"] for group in series.values() for item in group)
  lock = {"schema": "songplus-maintenance-lock/v1", "device_access": False, "deployment_ready": False,
          "files": {name: core.sha(root / name) for name in sorted(files)}, "baselines": baseline,
          "series": series, "required_case_ids": list(REQUIRED_CASE_IDS)}
  write_json(root, core.LOCK, lock)
  return lock


def tar_bytes(entries):
  data = io.BytesIO()
  with tarfile.open(fileobj=data, mode="w") as tar:
    for name, kind, content in entries:
      item = tarfile.TarInfo(name)
      item.mode = 0o644
      if kind == "file":
        payload = content.encode()
        item.size = len(payload)
        tar.addfile(item, io.BytesIO(payload))
      else:
        item.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE,
                     "char": tarfile.CHRTYPE, "directory": tarfile.DIRTYPE}[kind]
        if kind in ("symlink", "hardlink"):
          item.linkname = content
        tar.addfile(item)
  return data.getvalue()


class CoreBoundaryTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory(prefix="songplus-p7-core-")
    self.root = Path(self.temp.name).resolve()

  def tearDown(self):
    self.temp.cleanup()

  def test_json_duplicate_top_level_and_nested_keys_rejected(self):
    for value in ('{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}'):
      source = write(self.root, "duplicate.json", value)
      with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
        core.read_json(source)

  def test_safe_path_positive_and_traversal_rejection(self):
    self.assertEqual(core.safe_path(self.root, "normal/source.cc"), self.root / "normal/source.cc")
    for name in ("../escape", "normal/../../escape", "/tmp/escape", "", "."):
      with self.subTest(name=name), self.assertRaises(ValueError):
        core.safe_path(self.root, name)

  def test_safe_path_rejects_internal_and_external_symlink(self):
    write(self.root, "real/file", "fixture")
    (self.root / "internal").symlink_to(self.root / "real", target_is_directory=True)
    (self.root / "external").symlink_to(self.root.parent, target_is_directory=True)
    for name in ("internal/file", "external/file"):
      with self.assertRaises(ValueError):
        core.safe_path(self.root, name)

  def test_new_output_is_new_and_rejects_existing_files_directories_links(self):
    result = core.new_output(self.root / "new/out")
    self.assertTrue(result.is_dir())
    existing_file = write(self.root, "existing", "keep")
    (self.root / "dangling").symlink_to(self.root / "not-found")
    (self.root / "parent-alias").symlink_to(self.root / "new", target_is_directory=True)
    for path in (result, existing_file, self.root / "dangling", self.root / "parent-alias/child"):
      with self.assertRaises(ValueError):
        core.new_output(path)
    self.assertEqual(existing_file.read_text(), "keep")

  def test_tree_hash_inventory_is_order_independent_excludes_git_and_rejects_link(self):
    write(self.root, "b", "B")
    write(self.root, "a", "A")
    write(self.root, ".git/config", "not-source")
    self.assertEqual(core.tree_hashes(self.root), {name: hashlib.sha256(value).hexdigest() for name, value in (("a", b"A"), ("b", b"B"))})
    (self.root / "link").symlink_to(self.root / "a")
    with self.assertRaises(ValueError):
      core.tree_hashes(self.root)

  def test_complete_metadata_fixture_needs_no_subprocess_or_checkouts(self):
    lock_fixture(self.root)
    with patch.object(core, "capture", side_effect=AssertionError("metadata check must not run processes")):
      result = core.verify_lock(self.root, metadata_only=True)
    self.assertEqual(result["status"], "PASS")
    self.assertTrue(result["metadata_only"])
    self.assertEqual(result["checks"]["source_files"], 0)
    self.assertEqual(result["checks"]["dependency_files"], 0)
    self.assertFalse(result["device_access"])
    self.assertFalse(result["deployment_ready"])

  def test_locked_file_drift_and_missing_file_fail(self):
    lock_fixture(self.root)
    target = self.root / "maintenance/runtime_guard.py"
    target.write_text("changed")
    with self.assertRaisesRegex(ValueError, "Locked file drift"):
      core.verify_lock(self.root, metadata_only=True)
    target.unlink()
    with self.assertRaises(OSError):
      core.verify_lock(self.root, metadata_only=True)

  def test_required_locked_file_cannot_be_omitted(self):
    lock = lock_fixture(self.root)
    del lock["files"]["maintenance/runtime_guard.py"]
    write_json(self.root, core.LOCK, lock)
    with self.assertRaisesRegex(ValueError, "Required locked file absent"):
      core.verify_lock(self.root, metadata_only=True)

  def test_lock_scope_and_patch_order_cannot_be_promoted(self):
    original = lock_fixture(self.root)
    for field in ("device_access", "deployment_ready"):
      lock = copy.deepcopy(original)
      lock[field] = True
      write_json(self.root, core.LOCK, lock)
      with self.assertRaises(ValueError):
        core.verify_lock(self.root, metadata_only=True)
    lock = copy.deepcopy(original)
    lock["series"]["host"] = list(reversed(lock["series"]["host"]))
    write_json(self.root, core.LOCK, lock)
    with self.assertRaisesRegex(ValueError, "patch ordering drift"):
      core.verify_lock(self.root, metadata_only=True)

  def test_current_lock_cannot_hide_manifest_baseline_disagreement(self):
    lock = lock_fixture(self.root)
    host = core.read_json(self.root / core.HOST)
    host["baselines"]["upstream-openpilot"] = "9" * 40
    write_json(self.root, core.HOST, host)
    lock["files"][core.HOST] = core.sha(self.root / core.HOST)
    write_json(self.root, core.LOCK, lock)
    with self.assertRaisesRegex(ValueError, "Baseline manifest disagreement"):
      core.verify_lock(self.root, metadata_only=True)

  def test_patch_path_escape_fails_without_reading_outside(self):
    lock_fixture(self.root)
    host = core.read_json(self.root / core.HOST)
    host["series"][0]["file"] = "../../../../outside.patch"
    write_json(self.root, core.HOST, host)
    with self.assertRaisesRegex(ValueError, "Patch path escape"):
      core.normalized_series(self.root, core.HOST)

  def test_patch_hash_and_declared_commit_are_separately_checked(self):
    lock_fixture(self.root)
    host = core.read_json(self.root / core.HOST)
    first = self.root / "patches/openpilot/p6" / host["series"][0]["file"]
    first.write_text("changed")
    with self.assertRaisesRegex(ValueError, "Patch drift"):
      core.normalized_series(self.root, core.HOST)
    host["series"][0]["sha256"] = core.sha(first)
    first.write_text(f"From {'9' * 40} Mon Sep 17 00:00:00 2001\n")
    host["series"][0]["sha256"] = core.sha(first)
    write_json(self.root, core.HOST, host)
    with self.assertRaisesRegex(ValueError, "Patch commit mismatch"):
      core.normalized_series(self.root, core.HOST)

  def test_malformed_patch_headers_produce_structured_valueerror(self):
    lock_fixture(self.root)
    host = core.read_json(self.root / core.HOST)
    first = self.root / "patches/openpilot/p6" / host["series"][0]["file"]
    for content in (b"", b"From\n", b"\xff \xfe\n", b"garbage " + host["series"][0]["commit"].encode() + b"\n"):
      with self.subTest(content=content):
        first.write_bytes(content)
        host["series"][0]["sha256"] = core.sha(first)
        write_json(self.root, core.HOST, host)
        with self.assertRaises(ValueError):
          core.normalized_series(self.root, core.HOST)

  def test_frozen_case_duplicate_cannot_be_accepted(self):
    lock = lock_fixture(self.root)
    design = core.read_json(self.root / core.DESIGN)
    design["cases"][-1]["id"] = design["cases"][0]["id"]
    write_json(self.root, core.DESIGN, design)
    lock["files"][core.DESIGN] = core.sha(self.root / core.DESIGN)
    lock["required_case_ids"] = [case["id"] for case in design["cases"]]
    write_json(self.root, core.LOCK, lock)
    with self.assertRaisesRegex(ValueError, "Frozen acceptance IDs drift"):
      core.verify_lock(self.root, metadata_only=True)

  def test_archive_extracts_only_declared_regular_files_and_directories(self):
    data = tar_bytes([("src/", "directory", ""), ("src/file.c", "file", "code\n")])
    with patch.object(core, "capture", return_value=data) as called:
      core.archive(self.root / "fake-repo", "1" * 40, self.root / "output")
    self.assertEqual((self.root / "output/src/file.c").read_text(), "code\n")
    self.assertEqual(called.call_args.args, (["git", "archive", "1" * 40], self.root / "fake-repo"))

  def test_archive_rejects_traversal_links_fifo_and_device_members(self):
    for index, entry in enumerate((("../escape", "file", "bad"), ("/absolute", "file", "bad"),
                                   ("alias", "symlink", "../escape"), ("hard", "hardlink", "other"),
                                   ("fifo", "fifo", ""), ("device", "char", ""))):
      with self.subTest(entry=entry), patch.object(core, "capture", return_value=tar_bytes([entry])), self.assertRaises(ValueError):
        core.archive(self.root / "fake-repo", "1" * 40, self.root / f"out-{index}")
    self.assertFalse((self.root.parent / "escape").exists())

  def test_archive_duplicate_member_is_rejected_without_overwrite(self):
    data = tar_bytes([("source.c", "file", "original"), ("source.c", "file", "replacement")])
    with patch.object(core, "capture", return_value=data), self.assertRaises(ValueError):
      core.archive(self.root / "fake-repo", "1" * 40, self.root / "output")
    self.assertEqual((self.root / "output/source.c").read_text(), "original")

  def test_archive_existing_output_cannot_be_reused(self):
    output = self.root / "output"
    output.mkdir()
    with patch.object(core, "capture", side_effect=AssertionError("must fail before git")), self.assertRaises(FileExistsError):
      core.archive(self.root / "fake-repo", "1" * 40, output)


if __name__ == "__main__":
  unittest.main(verbosity=2)
