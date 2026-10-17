#!/usr/bin/env python3
"""Verify local text evidence only. This is NOT a deployment-readiness check.

Uses only standard-library file reads and optional JSON report output. Does not
import candidate modules, invoke subprocesses, load pickle/ONNX, access devices,
or open network connections. Source snippets are treated as inert UTF-8 text.
"""

import argparse
import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent


def local_file(value):
  path = Path(value)
  path = (HERE / path).resolve() if not path.is_absolute() else path.resolve()
  if not path.is_relative_to(PROJECT_ROOT):
    raise ValueError(f"evidence path outside project: {path}")
  if not path.is_file():
    raise ValueError(f"not a local regular file: {path}")
  return path


def verify(profile_path):
  errors = []
  checked_files = 0
  checked_anchors = 0
  profile = json.loads(local_file(profile_path).read_text(encoding="utf-8"))

  def require(condition, message):
    if not condition:
      errors.append(message)

  require(profile.get("schema_version") == 1, "unsupported hardware-profile schema")
  require(profile.get("status") == "offline_research_only", "profile is not restricted to offline research")
  require(profile.get("deployment_ready") is False, "profile must explicitly deny deployment readiness")
  require(profile.get("hardware_compatibility_confirmed") is False, "hardware compatibility must remain unconfirmed")
  packaging = profile.get("evidence_packaging", {})
  require(packaging.get("schema") == "self-contained-hardware-evidence/v1", "missing self-contained evidence contract")
  require(packaging.get("external_files_required") is False, "hardware evidence cannot require external files")
  policy = profile.get("execution_policy", {})
  for key in ("device_access", "candidate_import", "candidate_execution", "network", "firmware_flash", "os_update"):
    require(policy.get(key) is False, f"execution_policy.{key} must be false")

  source_map = {}
  for source in profile.get("source_evidence", []):
    source_id = source.get("id")
    require(bool(source_id) and source_id not in source_map, f"duplicate or empty source id: {source_id}")
    source_map[source_id] = source
    try:
      path = local_file(source["local_path"])
      data = path.read_bytes()
      checked_files += 1
      require(hashlib.sha256(data).hexdigest() == source["sha256"], f"SHA-256 mismatch: {source_id}")
      require(len(data) == source["bytes"], f"byte count mismatch: {source_id}")
      text = data.decode("utf-8")
      lines = text.splitlines()
      for ref in source.get("references", []):
        require(1 <= ref["start_line"] <= ref["end_line"] <= len(lines), f"invalid reference lines: {source_id}")
      if source.get("commit") and source.get("url"):
        require(source["commit"] in source["url"], f"source URL is not fixed to its commit: {source_id}")
      source["_lines"] = lines
    except (KeyError, OSError, ValueError, UnicodeError) as exc:
      errors.append(f"cannot verify {source_id}: {exc}")

  anchors = profile.get("critical_code_anchors", [])
  require(len(anchors) >= 25, "critical code anchors unexpectedly absent")
  anchor_ids = set()
  for anchor in anchors:
    try:
      aid = anchor["id"]
      require(aid not in anchor_ids, f"duplicate anchor: {aid}")
      anchor_ids.add(aid)
      source = source_map[anchor["source_id"]]
      start, end = anchor["start_line"], anchor["end_line"]
      lines = source.get("_lines", [])
      require(1 <= start <= end <= len(lines), f"anchor range invalid: {aid}")
      actual = "\n".join(lines[start - 1:end])
      require(actual == anchor["expected_text"], f"critical code changed or anchor moved: {aid}")
      checked_anchors += 1
    except (KeyError, TypeError) as exc:
      errors.append(f"invalid critical anchor: {exc}")

  patches = profile.get("patch_plan", [])
  required_sources = {a.get("source_id") for a in anchors}
  required_sources.update(s for patch in patches for s in patch.get("source_ids", []))
  require(set(source_map) == required_sources, "evidence must contain exactly the sources used by anchors and patch plan")
  require(len(source_map) == 31, "expected 28 public snapshots and 3 derived historical summaries")
  patch_ids = [patch.get("id") for patch in patches]
  require(len(patch_ids) == len(set(patch_ids)), "duplicate patch id")
  visited = set()
  for patch in patches:
    pid = patch.get("id")
    require(patch.get("implemented") is False, f"patch wrongly marked implemented: {pid}")
    require(bool(patch.get("hardware_confirmation_required")), f"missing hardware conditions: {pid}")
    require(set(patch.get("depends_on", [])) <= visited, f"patch order invalid or dependency unresolved: {pid}")
    for sid in patch.get("source_ids", []):
      require(sid in source_map, f"unknown source in patch {pid}: {sid}")
    for aid in patch.get("anchor_ids", []):
      require(aid in anchor_ids, f"unknown anchor in patch {pid}: {aid}")
    visited.add(pid)

  mapping = profile.get("bus_mapping_candidate", {})
  require(mapping.get("panda_bus_offset") == 4, "candidate PANDA_BUS_OFFSET must be 4")
  require(mapping.get("physical_mapping_confirmed") is False, "physical bus mapping must remain unconfirmed")
  require(mapping.get("conditional_second_panda_host_buses") == [4, 5, 6], "second-Panda candidate mapping inconsistent")
  require(bool(profile.get("unresolved_hardware_requirements")), "unresolved hardware requirements cannot be empty")

  return {
    "status": "PASS" if not errors else "FAIL",
    "meaning": "Local source integrity and exact text anchors only; NOT DEPLOYMENT READINESS.",
    "deployment_ready": False,
    "hardware_compatibility_confirmed": False,
    "device_access_performed": False,
    "candidate_code_imported_or_executed": False,
    "network_used": False,
    "external_files_required": False,
    "profile": str(Path(profile_path).resolve()),
    "source_files_checked": checked_files,
    "critical_code_anchors_checked": checked_anchors,
    "patches_checked": len(patches),
    "errors": errors,
    "remaining_requirements": profile.get("unresolved_hardware_requirements", []),
  }


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--profile", type=Path, default=HERE / "hardware-profile.json")
  parser.add_argument("--report", type=Path, help="Optional JSON output, restricted to this hardware directory")
  args = parser.parse_args()
  try:
    report = verify(args.profile)
  except (OSError, ValueError, TypeError, KeyError) as exc:
    report = {"status": "FAIL", "meaning": "NOT DEPLOYMENT READINESS", "deployment_ready": False, "errors": [str(exc)]}
  rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
  if args.report:
    output = args.report.resolve()
    if not output.is_relative_to(HERE) or output == args.profile.resolve():
      raise SystemExit("Report must be inside this hardware directory and must not overwrite the profile")
    if output.exists():
      raise SystemExit(f"Refusing to overwrite existing report: {output}")
    output.write_text(rendered, encoding="utf-8")
  print(rendered, end="")
  return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
  raise SystemExit(main())
