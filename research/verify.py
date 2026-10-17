#!/usr/bin/env python3
"""Verify the portable research index and inert evidence using local reads only.

No original archive, recovered module, native library, model, device, network,
or subprocess is accessed. A pass establishes packaging integrity, not the truth
of historical experiments or target/vehicle compatibility.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
HEX256 = re.compile(r"[0-9a-f]{64}\Z")


def local_path(root: Path, value: str, *, evidence_only: bool = False) -> Path:
  path = Path(value)
  if path.is_absolute() or ".." in path.parts:
    raise ValueError(f"nonportable path: {value}")
  if evidence_only and path.parts[:2] != ("research", "evidence"):
    raise ValueError(f"not an evidence path: {value}")
  result = (root / path).resolve()
  if not result.is_relative_to(root.resolve()):
    raise ValueError(f"path escapes project: {value}")
  if not result.is_file():
    raise ValueError(f"missing local file: {value}")
  return result


def _verify(root: Path) -> dict:
  errors = []

  def require(condition, message):
    if not condition:
      errors.append(message)

  def check_file(entry, *, evidence_only=False):
    path = local_path(root, entry["path"], evidence_only=evidence_only)
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == entry["sha256"], f"hash mismatch: {entry['path']}")
    require(len(data) == entry["bytes"], f"size mismatch: {entry['path']}")
    return path

  manifest = json.loads((root / "research/evidence-manifest.json").read_text())
  require(manifest["schema"] == "research-evidence-manifest/v1", "unsupported manifest")
  require(manifest["external_files_required"] is False, "external files must not be required")
  files = manifest["files"]
  paths = set()
  identifiers = set()
  kinds = {"public_source_snapshot", "derived_summary_not_original", "public_license_text", "acquisition_metadata"}
  for entry in files:
    require(entry["path"] not in paths, f"duplicate path: {entry['path']}")
    require(entry["id"] not in identifiers, f"duplicate ID: {entry['id']}")
    paths.add(entry["path"])
    identifiers.add(entry["id"])
    path = check_file(entry, evidence_only=True)
    require(entry["runtime_dependency"] is False, f"runtime evidence dependency: {entry['id']}")
    require(entry["content_kind"] in kinds, f"unknown evidence kind: {entry['id']}")
    require(bool(entry["source_provenance"]), f"missing provenance: {entry['id']}")
    if entry["content_kind"] == "derived_summary_not_original":
      summary = json.loads(path.read_text())
      require(summary["schema"] == "research-evidence-summary/v1", f"invalid summary: {entry['id']}")
      require(summary["content_kind"] == "derived_summary_not_original", f"summary presented as original: {entry['id']}")
      require(summary["runtime_dependency"] is False, f"runtime summary: {entry['id']}")
      require(summary["target_functions_executed_for_this_summary"] is False, f"unexpected target execution: {entry['id']}")
      require(bool(summary["facts"]) and bool(summary["limits"]), f"unscoped summary: {entry['id']}")
      require(bool(summary["source_provenance"]), f"missing summary origins: {entry['id']}")
      for source in summary["source_provenance"]:
        # Original archive paths are citations, never input paths to this verifier.
        original_path = Path(source["path_relative_to_original_workspace"])
        require(not original_path.is_absolute() and ".." not in original_path.parts,
                f"invalid original citation path: {entry['id']}")
        require(source["required_for_offline_validation"] is False, f"external summary dependency: {entry['id']}")
        require(HEX256.fullmatch(source["sha256"]) is not None, f"invalid original hash: {entry['id']}")
        require(source["sha256"] != entry["sha256"], f"summary uses original file hash: {entry['id']}")
        require(isinstance(source["bytes"], int) and source["bytes"] > 0, f"invalid original size: {entry['id']}")

  actual_paths = {str(path.relative_to(root)) for path in (root / "research/evidence").rglob("*") if path.is_file()}
  require(actual_paths == paths, "evidence directory contains missing or unmanifested files")
  plan_manifest = json.loads((root / "research/plan-manifest.json").read_text())
  require(plan_manifest["schema"] == "research-plan-snapshots/v1", "unsupported plan snapshot manifest")
  require(plan_manifest["live_capture_plan_directory_required"] is False, "live capture plan must not be required")
  plan_paths = set()
  for entry in plan_manifest["files"]:
    require(entry["path"] not in plan_paths, f"duplicate plan path: {entry['path']}")
    plan_paths.add(entry["path"])
    require(Path(entry["path"]).parts[:3] == ("research", "plans", "capture-plan-20261017"),
            f"plan snapshot outside pinned directory: {entry['path']}")
    check_file(entry)
    require(entry["content_kind"] == "historical_plan_snapshot", f"not a historical plan snapshot: {entry['path']}")
    require(entry["runtime_dependency"] is False, f"plan became runtime dependency: {entry['path']}")
    require(entry["source_provenance"]["required_for_offline_validation"] is False,
            f"live plan became dependency: {entry['path']}")
  actual_plan_paths = {str(path.relative_to(root)) for path in (root / "research/plans").rglob("*") if path.is_file()}
  require(actual_plan_paths == plan_paths, "plan snapshots contain missing or unmanifested files")
  require(len(plan_paths) == 6, "expected six historical planning files")
  profile = json.loads((root / "hardware/hardware-profile.json").read_text())
  for source in profile["source_evidence"]:
    source_path = (root / "hardware" / source["local_path"]).resolve()
    require(source_path.is_relative_to(root.resolve()), f"hardware source escapes project: {source['id']}")
    portable_path = str(source_path.relative_to(root.resolve()))
    require(portable_path in paths, f"hardware source missing from manifest: {source['id']}")
    matched = [entry for entry in files if entry["path"] == portable_path]
    require(len(matched) == 1 and matched[0]["sha256"] == source["sha256"]
            and matched[0]["bytes"] == source["bytes"], f"hardware/manifest binding mismatch: {source['id']}")
    if source["evidence_kind"] == "public_source_snapshot":
      require(source["commit"] in source["url"], f"unpinned public source: {source['id']}")
      require(source["license_evidence"] in paths, f"missing license evidence: {source['id']}")
      require(source["usage"] == "inert_reference_only_not_runtime_or_production_source", f"unexpected source usage: {source['id']}")

  index = json.loads((root / "research/index.json").read_text())
  require(index["schema"] == "songplus-research-index/v1", "unsupported research index")
  require(index["device_access_performed_by_packager"] is False, "unexpected device access")
  index_ids = set()
  for item in index["items"]:
    require(item["id"] not in index_ids, f"duplicate research item: {item['id']}")
    index_ids.add(item["id"])
    require(item["status"] in {"verified", "partial", "candidate", "missing"}, f"invalid research status: {item['id']}")
    check_file({"path": item["artifact_path"], "sha256": item["sha256"], "bytes": item["bytes"]})
    require(item["runtime_dependency"] is False and item["production_source_included"] is False,
            f"research promoted to production: {item['id']}")
    require(bool(item["validation_basis"]) and bool(item["remaining"]), f"unscoped research status: {item['id']}")

  return {"status": "PASS" if not errors else "FAIL", "meaning": "Portable local packaging integrity only; historical experiments are not rerun and vehicle readiness is not established.",
          "evidence_files_checked": len(files), "plan_snapshot_files_checked": len(plan_paths),
          "research_items_checked": len(index["items"]),
          "external_files_required": False, "device_access_performed": False,
          "target_code_executed": False, "actuation_allowed": False, "errors": errors}


def verify(root: Path = ROOT) -> dict:
  try:
    return _verify(root.resolve())
  except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
    return {"status": "FAIL", "actuation_allowed": False, "errors": [str(exc)]}


if __name__ == "__main__":
  report = verify()
  print(json.dumps(report, ensure_ascii=False, indent=2))
  raise SystemExit(0 if report["status"] == "PASS" else 1)
