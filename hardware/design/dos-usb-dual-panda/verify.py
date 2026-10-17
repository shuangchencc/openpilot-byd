#!/usr/bin/env python3
"""Check the local design package, never import or execute target/Panda code.

Default mode is portable: private archive references are reported as unverified.
--check-local-archives also hashes the four named plaintext historical sources.
This does not run any of the planned compatibility acceptance tests.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ARCHIVE_ROOT = ROOT.parent / "op_rootdir/data/openpilot"
ARCHIVE_FILES = {
  "selfdrive/pandad/pandad.py", "selfdrive/pandad/panda_comms.h",
  "panda/python/__init__.py", "panda/python/constants.py",
}
DP_COMMIT = "94ad85cfda64773a960d048dc11cac02d4aa86a5"


def require(condition, message):
  if not condition:
    raise ValueError(message)


def read_json(path):
  return json.loads(path.read_text())


def sha(data):
  return hashlib.sha256(data).hexdigest()


def keyed(items):
  result = {item["id"]: item for item in items}
  require(len(result) == len(items), "Duplicate ID")
  return result


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--check-local-archives", action="store_true")
  args = parser.parse_args()
  manifest = read_json(HERE / "manifest.json")
  actual_files = {str(p.relative_to(HERE)) for p in HERE.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.name != "manifest.json"}
  require(actual_files == set(manifest["files"]), "Manifest coverage differs from design directory")
  for name, digest in manifest["files"].items():
    path = (HERE / name).resolve()
    require(path.is_relative_to(HERE), "Manifest path escapes design package")
    require(sha(path.read_bytes()) == digest, f"Design hash drift: {name}")

  plan = read_json(HERE / "implementation-plan.json")
  acceptance = read_json(HERE / "offline-acceptance.json")
  profile = read_json(HERE / "profile.template.json")
  assumptions = read_json(HERE / "hardware-assumptions.json")
  host = read_json(HERE / "host-design.json")
  firmware = read_json(HERE / "firmware-design.json")
  review = read_json(HERE / "review-requirements.json")
  locked = {r["path"]: r["commit"] for r in read_json(ROOT / "upstream-lock.json")["repos"]}
  pins = {"openpilot": locked["upstream-openpilot"], "panda": locked["upstream-openpilot/panda"],
          "opendbc": locked["upstream-openpilot/opendbc_repo"]}
  require(plan["official_pins"] == pins, "Plan pins differ from upstream lock")
  for doc in [host, firmware]:
    require(all(doc["baseline"][k] == v for k, v in pins.items()), "Baseline mismatch")
  require(review["official_commit"] == pins["openpilot"], "Review baseline mismatch")
  require(review["dependency_commits"] == {r: locked[r] for r in ["upstream-openpilot/panda", "upstream-openpilot/opendbc_repo"]}, "Review dependencies mismatch")

  checked_paths, skipped_paths = set(), set()
  source_ref_count = 0
  line_range_count = 0

  def check_source(source):
    nonlocal source_ref_count
    source_ref_count += 1
    prefix = source.get("repository_root_relative_to_project", "")
    path = (ROOT / prefix / source["path"]).resolve()
    if not path.is_relative_to(ROOT):
      require(path.is_relative_to(ARCHIVE_ROOT), "Source path outside allowed archive")
      require(str(path.relative_to(ARCHIVE_ROOT)) in ARCHIVE_FILES, "Unexpected archive input")
      require(source.get("commit") is None, "Unproven archive commit")
      if not args.check_local_archives:
        skipped_paths.add(str(path.relative_to(ROOT.parent)))
        return None
    else:
      rel = path.relative_to(ROOT).as_posix()
      matches = [r for r in locked if rel.startswith(r + "/")]
      if matches:
        owner = max(matches, key=len)
        require(source.get("commit") == locked[owner], f"Source commit mismatch: {rel}")
      elif rel.startswith("research/evidence/public-source/dragonpilot-94ad85cf/"):
        require(source.get("commit") in (None, DP_COMMIT), "DP provenance mismatch")
    data = path.read_bytes()
    require(sha(data) == source["sha256"], f"Source hash drift: {source['id']}")
    lines = data.splitlines(keepends=True)
    require(len(lines) == source["line_count"], f"Source line-count drift: {source['id']}")
    checked_paths.add(str(path))
    return lines

  def check_range(lines, start, end, digest=None):
    nonlocal line_range_count
    if lines is None:
      return
    require(1 <= start <= end <= len(lines), "Source range out of bounds")
    if digest:
      require(sha(b"".join(lines[start - 1:end])) == digest, "Source excerpt hash drift")
    line_range_count += 1

  host_sources = keyed(host["source_anchors"])
  for source in host_sources.values():
    lines = check_source(source)
    for start, end in source["line_ranges"]:
      check_range(lines, start, end)
  for doc in [firmware, review]:
    sources = keyed(doc["sources"])
    lines_by_id = {key: check_source(source) for key, source in sources.items()}
    for anchor in doc["anchors"]:
      require(anchor["source_id"] in sources, "Unknown source anchor")
      check_range(lines_by_id[anchor["source_id"]], anchor["start_line"], anchor["end_line"], anchor.get("sha256"))

  stages = keyed(plan["stages"])
  require(list(stages) == [f"P{i}" for i in range(8)], "Missing/reordered main stages")
  visited = set()
  for stage in stages.values():
    require(set(stage["depends_on"]) <= visited, "Dependency is missing, cyclic or out of order")
    require(stage["status"] == "PLANNED" and stage["acceptance_status"] == "NOT_RUN", "Stage status overclaim")
    visited.add(stage["id"])
  cases = keyed(acceptance["cases"])
  require(len(cases) == acceptance["case_count"], "Acceptance count mismatch")
  require(acceptance["status"] == "SPECIFIED_NOT_EXECUTED", "Acceptance status overclaim")
  require({c["stage"] for c in cases.values()} == set(stages), "Acceptance stage coverage gap")
  for case in cases.values():
    require(case["status"] == "NOT_RUN" and case["required"], "Case status or requirement mismatch")
    require(all(case[k] for k in ["inputs", "expected", "required_evidence"]), "Incomplete acceptance case")
  require(not acceptance["device_access_allowed"] and not acceptance["real_can_tx_allowed"], "Unsafe acceptance scope")
  review_sources = keyed(review["sources"])
  for requirement in keyed(review["requirements"]).values():
    require(set(requirement["basis"]) <= set(review_sources), "Unknown review source")
    require(set(requirement["acceptance_ids"]) <= set(cases), "Unmapped review acceptance")
    require(requirement["acceptance_ids"] and requirement["acceptance_execution_status"] == "NOT_RUN", "Review status overclaim")
  require(not review["pending_acceptance_detail_requests"], "Review details unresolved")

  host_cases, host_modules = keyed(host["offline_acceptance_cases"]), keyed(host["design_modules"])
  for fact in keyed(host["facts"]).values():
    require(set(fact["sources"]) <= set(host_sources), "Unknown host fact source")
  for module in host_modules.values():
    require(set(module["sources"]) <= set(host_sources), "Unknown host source")
    require(set(module["acceptance"]) <= set(host_cases), "Unknown host case")
  for case in host_cases.values():
    require(case["design"] in host_modules and case["status"] == "expected_not_executed", "Host case status mismatch")
  fw_modules = keyed(firmware["modules"])
  fw_anchors = keyed(firmware["anchors"])
  for module in fw_modules.values():
    require(set(module["anchors"]) <= set(fw_anchors), "Unknown firmware source anchor")
    require(module["status"] == "planned_not_implemented", "Firmware implementation overclaim")
  for case in keyed(firmware["offline_acceptance"]).values():
    require(set(case["modules"]) <= set(fw_modules), "Unknown firmware module")
    require(case["status"] == "planned_not_run", "Firmware case status overclaim")

  require(profile["binding_status"] == "UNBOUND", "Profile bound prematurely")
  require(profile["expected_board_count"] == 2 and profile["required_slots"] == [0, 1], "Board count mismatch")
  require(profile["bus_stride"] == 4 and profile["ordinary_local_can_buses"] == [0, 1, 2], "Bus contract mismatch")
  require([s["slot"] for s in profile["slots"]] == [0, 1], "Slot mismatch")
  require(all(v is None for slot in profile["slots"] for k, v in slot.items() if k != "slot"), "Invented board identity")
  require(set(profile["roles"]) == {"vehicle_can", "diagnostic", "harness", "ignition_sources", "fan_control", "power_monitor", "ir_control"}, "Missing/unknown role")
  require(all(v is None for v in profile["roles"].values()), "Invented physical roles")
  for key in ["device_access_enabled", "can_tx_enabled", "firmware_install_enabled"]:
    require(profile[key] is False, f"Enabled profile action: {key}")
  for key in ["implementation_complete", "device_access_this_task", "photos_required_to_start_local_work", "patches_applied"]:
    require(plan[key] is False, f"Plan scope overclaim: {key}")
  require(plan["runtime_policy"] == {
    "cereal_schema_change_v1": False, "expected_boards": 2, "all_boards_required": True,
    "partial_operation_v1": False, "autoflash": False, "automatic_board_reset": False,
    "unknown_identity_action": "QUARANTINE", "legacy_protocol_use": "OFFLINE_ONLY",
    "target_protocol_use": "CURRENT_ABI_ON_F4_AND_H7", "real_can_tx_this_scope": False,
  }, "Runtime policy differs from reviewed design")
  for key in ["photos_or_vendor_materials_requested_again", "local_design_blocked", "local_mock_implementation_blocked",
              "device_compatibility_verified", "recovery_verified", "deployment_ready"]:
    require(assumptions[key] is False, f"Hardware assumption mismatch: {key}")

  protocol = firmware["protocol"]
  for generation in ["legacy", "target"]:
    desc = protocol[generation]
    require(struct.calcsize(desc["version_reply_format"]) == desc["version_reply_size"], "Version size mismatch")
    for name in ["health", "can_health"]:
      layout = desc[name]
      require(struct.calcsize(layout["format"]) == layout["size"], "Documented struct size mismatch")
      offset = 0
      for field in layout.get("fields", []):
        require(field["offset"] == offset and field["size"] == struct.calcsize("<" + field["format"]), "Documented field offset mismatch")
        offset += field["size"]
      require(not layout.get("fields") or offset == layout["size"], "Documented field coverage mismatch")
  versions = []
  for name, path in {"health": "upstream-openpilot/panda/board/health.h", "can": "upstream-openpilot/opendbc_repo/opendbc/safety/can.h"}.items():
    digest = sha((ROOT / path).read_bytes().replace(b"\r", b""))
    expected = protocol["target"]["header_hashes"][name]
    version = int.from_bytes(bytes.fromhex(digest[:8]), "little")
    require(digest == expected["normalized_sha256"] and version == expected["version_u32"], "Header protocol hash mismatch")
    versions.append(version)
  require(struct.pack("<II", *versions) == bytes.fromhex(protocol["target"]["version_reply_hex"]), "Version bytes mismatch")

  print(json.dumps({
    "status": "PASS_DESIGN_CHECKS_ONLY", "mode": "with_local_archives" if args.check_local_archives else "portable",
    "design_files_hashed": len(actual_files), "source_references": source_ref_count,
    "distinct_source_files_hashed": len(checked_paths), "source_line_ranges_checked": line_range_count,
    "external_archive_files_skipped": sorted(skipped_paths), "main_acceptance_cases_specified": len(cases),
    "host_detail_cases_specified": len(host_cases), "firmware_detail_cases_specified": len(firmware["offline_acceptance"]),
    "review_requirements_mapped": len(review["requirements"]), "planned_acceptance_tests_executed": 0,
    "source_code_imported_or_executed": False, "device_access": False, "firmware_build": False,
    "implementation_complete": False, "deployment_ready": False,
    "limits": "Checks document integrity, local source hashes/ranges and plan relationships. Does not validate implementation behavior or complete repository Git integrity; CI separately runs lock_upstream.py.",
  }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
  try:
    main()
  except (ValueError, KeyError, OSError, TypeError, struct.error) as exc:
    print(f"FAIL design verification: {exc}", file=sys.stderr)
    sys.exit(1)
