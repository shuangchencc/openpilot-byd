"""Read-only P7 metadata/artifact inspection; deliberately contains no launcher.

This module accepts *synthetic* inventory records, not a device address. A good
result establishes that local candidate files match a caller-supplied manifest;
it neither authenticates installed firmware nor grants installation or CAN TX.
The outer maintenance command binds the manifest and evidence to downstream-lock.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

BASELINE_SHA = "35e78a9d9518855ec0dacec919e719624a28131f"
PROTOCOL = "f512bcff76f2ab75"
DENIED_PERMISSIONS = dict.fromkeys((
  "device_access", "real_can_tx", "runtime_launch", "system_write", "installation",
  "panda_reset", "firmware_flash", "bootstub_replace", "network_access",
  "auto_build", "auto_download", "usb_role_write", "dfu",
), False)
REQUIRED_CASE_IDS = (
  "A00", "A01", "A02", "A10", "A11", "A12", "A13", "A14", "A15",
  "A20", "A21", "A22", "A23", "A24", "A30", "A31", "A32", "A33", "A34",
  "A40", "A41", "A42", "A43", "A44", "A45", "A46", "A50", "A51", "A52",
  "A53", "A54", "A55", "A60", "A61", "A62", "A63", "A64", "A65", "A70", "A71", "A72",
)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")
_KINDS = frozenset(("firmware-elf", "firmware-bin", "firmware-signed-bin", "firmware-map", "host-executable", "metadata"))
_ARTIFACT_KEYS = frozenset(("path", "sha256", "size_bytes", "kind", "mcu", "build_variant", "protocol", "signature", "deployable"))
_REQUIRED_ARTIFACTS = frozenset((mcu, variant, kind) for mcu in ("F4", "H7") for variant in ("debug", "release")
                                for kind in ("firmware-elf", "firmware-bin", "firmware-signed-bin"))
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


def _exact_keys(value: object, keys: set | frozenset) -> bool:
  return type(value) is dict and value.keys() == keys


def _hash(value: object) -> bool:
  return type(value) is str and _HEX64.fullmatch(value) is not None


def _permissions(value: object) -> bool:
  return _exact_keys(value, DENIED_PERMISSIONS.keys()) and all(v is False for v in value.values())


def synthetic_inventory(dos_slot: int = 0) -> dict:
  """Construct explicitly synthetic metadata for tests, never discover hardware."""
  if type(dos_slot) is not int or dos_slot not in (0, 1):
    raise ValueError("dos_slot must be integer 0 or 1")
  slots = []
  for slot in range(2):
    dos = slot == dos_slot
    slots.append({
      "slot": slot,
      "identity": "fixture:dos" if dos else "fixture:external",
      "board": "DOS" if dos else "RED_PANDA",
      "mcu": "F4" if dos else "H7",
      "exact_mcu": "fixture:unknown-F4" if dos else "fixture:unknown-H7",
      "transport": "USB",
      "protocol": PROTOCOL,
      "signature": "fixture:dos-app" if dos else "fixture:red-app",
    })
  return {"schema": "songplus-maintenance-inventory/v1", "synthetic": True, "slots": slots}


def _inspect_inventory(inventory: object) -> list[str]:
  if not _exact_keys(inventory, {"schema", "synthetic", "slots"}) or inventory["schema"] != "songplus-maintenance-inventory/v1":
    return ["INVENTORY_SCHEMA_INVALID"]
  if inventory["synthetic"] is not True:
    return ["PHYSICAL_INVENTORY_NOT_AUTHORIZED"]
  slots = inventory["slots"]
  if type(slots) is not list or len(slots) != 2:
    return ["COMPLETE_FIXED_GROUP_REQUIRED"]
  seen = set()
  reasons = []
  template = synthetic_inventory()["slots"]
  for index, item in enumerate(slots):
    if not _exact_keys(item, template[0].keys()):
      reasons.append(f"SLOT_{index}_SCHEMA_INVALID")
      continue
    if type(item["slot"]) is not int or item["slot"] != index:
      reasons.append(f"SLOT_{index}_INDEX_INVALID")
    identity = item["identity"]
    if type(identity) is not str or identity not in ("fixture:dos", "fixture:external") or identity in seen:
      reasons.append(f"SLOT_{index}_IDENTITY_UNKNOWN_OR_DUPLICATE")
      continue
    seen.add(identity)
    expected = template[0 if identity == "fixture:dos" else 1]
    for field in ("board", "mcu", "exact_mcu", "protocol", "signature"):
      if type(item[field]) is not str or item[field] != expected[field]:
        reasons.append(f"SLOT_{index}_{field.upper()}_UNKNOWN")
    transports = ("USB",) if identity == "fixture:dos" else ("USB", "SPI")
    if type(item["transport"]) is not str or item["transport"] not in transports:
      reasons.append(f"SLOT_{index}_TRANSPORT_UNKNOWN")
  return reasons


def _relative_parts(name: object) -> list[str] | None:
  if type(name) is not str or not name or len(name) > 512 or "\\" in name or "\x00" in name:
    return None
  parts = name.split("/")
  if any(part in ("", ".", "..") for part in parts) or any(":" in part for part in parts):
    return None
  return parts


def _open_root(root: str | Path) -> int:
  """Open only real directories, rejecting every symlink component."""
  absolute = os.path.abspath(os.fspath(root))
  # The caller provides an ordinary local build directory, never a device tree.
  if absolute in ("/", "/dev", "/proc", "/sys") or absolute.startswith(("/dev/", "/proc/", "/sys/")):
    raise ValueError("unsafe artifact root")
  flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
  fd = os.open("/", flags)
  try:
    for part in absolute.split("/")[1:]:
      next_fd = os.open(part, flags, dir_fd=fd)
      os.close(fd)
      fd = next_fd
    return fd
  except BaseException:
    os.close(fd)
    raise


def _hash_regular_file(root_fd: int, parts: list[str], size: int) -> str:
  """Read a bounded regular file; never follow links or open a special node."""
  parent_fd = os.dup(root_fd)
  file_fd = None
  try:
    for component in parts[:-1]:
      next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
      os.close(parent_fd)
      parent_fd = next_fd
    before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
      raise ValueError("not a unique regular file of the declared size")
    file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    opened = os.fstat(file_fd)
    if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_nlink) != (
        before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_nlink):
      raise ValueError("artifact changed while opening")
    digest = hashlib.sha256()
    remaining = size
    while remaining:
      chunk = os.read(file_fd, min(remaining, 1024 * 1024))
      if not chunk:
        raise ValueError("artifact truncated while reading")
      digest.update(chunk)
      remaining -= len(chunk)
    if os.read(file_fd, 1):
      raise ValueError("artifact grew while reading")
    after = os.fstat(file_fd)
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
      raise ValueError("artifact modified while reading")
    return digest.hexdigest()
  finally:
    if file_fd is not None:
      os.close(file_fd)
    os.close(parent_fd)


def inspect_runtime(inventory: object, artifacts_manifest: object, artifact_root: str | Path) -> dict:
  """Inspect local files and metadata, with no execution/download/device paths.

  `offline_artifacts_available` is a hash/shape check only. `runtime_available`
  and every permission are false even when all declared candidate files match.
  Metadata is not an installed-firmware signature verification mechanism.
  """
  reasons = _inspect_inventory(inventory)
  verified = []
  manifest_keys = {"schema", "baseline_sha", "deployable", "permissions", "artifacts"}
  if not _exact_keys(artifacts_manifest, manifest_keys) or artifacts_manifest["schema"] != "songplus-maintenance-artifacts/v1":
    reasons.append("ARTIFACT_MANIFEST_SCHEMA_INVALID")
    records = []
  else:
    if artifacts_manifest["baseline_sha"] != BASELINE_SHA:
      reasons.append("BASELINE_PIN_MISMATCH")
    if artifacts_manifest["deployable"] is not False or not _permissions(artifacts_manifest["permissions"]):
      reasons.append("ARTIFACT_PERMISSION_ESCALATION")
    records = artifacts_manifest["artifacts"]
    if type(records) is not list or not 1 <= len(records) <= 64:
      reasons.append("ARTIFACT_LIST_INVALID")
      records = []
  candidates = []
  paths = set()
  identities = set()
  for index, item in enumerate(records):
    code = f"ARTIFACT_{index}"
    if not _exact_keys(item, _ARTIFACT_KEYS):
      reasons.append(code + "_SCHEMA_INVALID")
      continue
    parts = _relative_parts(item["path"])
    if parts is None or item["path"] in paths:
      reasons.append(code + "_PATH_INVALID_OR_DUPLICATE")
      continue
    paths.add(item["path"])
    if not _hash(item["sha256"]) or type(item["size_bytes"]) is not int or not 0 < item["size_bytes"] <= _MAX_ARTIFACT_BYTES:
      reasons.append(code + "_HASH_OR_SIZE_INVALID")
      continue
    if any(type(item[field]) is not str for field in ("kind", "mcu", "build_variant", "protocol", "signature")):
      reasons.append(code + "_IDENTITY_INVALID")
      continue
    identity = (item["mcu"], item["build_variant"], item["kind"])
    firmware = item["kind"].startswith("firmware-")
    if item["kind"] not in _KINDS or (firmware and (item["mcu"] not in ("F4", "H7") or item["build_variant"] not in ("debug", "release"))) or (
        not firmware and (item["mcu"] != "HOST" or item["build_variant"] != "host")):
      reasons.append(code + "_IDENTITY_INVALID")
      continue
    expected_signature = "public-development-key" if item["kind"] == "firmware-signed-bin" else "unsigned" if firmware else "not-applicable"
    if item["signature"] != expected_signature or item["protocol"] != PROTOCOL or item["deployable"] is not False:
      reasons.append(code + "_PROTOCOL_SIGNATURE_OR_PERMISSION_INVALID")
      continue
    if firmware and identity in identities:
      reasons.append(code + "_MCU_VARIANT_KIND_DUPLICATE")
      continue
    identities.add(identity)
    candidates.append((index, item, parts))
  if not _REQUIRED_ARTIFACTS.issubset(identities):
    reasons.append("COMPLETE_F4_H7_DEBUG_RELEASE_ARTIFACTS_REQUIRED")
  # A malformed identity/manifest is denied before reading any payload.
  if not reasons:
    root_fd = None
    try:
      root_fd = _open_root(artifact_root)
      for index, item, parts in candidates:
        try:
          actual_hash = _hash_regular_file(root_fd, parts, item["size_bytes"])
        except (OSError, ValueError):
          reasons.append(f"ARTIFACT_{index}_MISSING_UNSAFE_OR_CHANGED")
          continue
        if actual_hash != item["sha256"]:
          reasons.append(f"ARTIFACT_{index}_HASH_MISMATCH")
        else:
          verified.append({key: item[key] for key in sorted(_ARTIFACT_KEYS)})
    except (OSError, ValueError, TypeError):
      reasons.append("ARTIFACT_ROOT_MISSING_OR_UNSAFE")
    finally:
      if root_fd is not None:
        os.close(root_fd)
  return {
    "schema": "songplus-maintenance-inspection/v1",
    "inspection_valid": not reasons,
    "offline_artifacts_available": not reasons,
    "runtime_available": False,
    "permissions": dict(DENIED_PERMISSIONS),
    "reasons": reasons,
    "artifacts": verified,
    "scope": "synthetic metadata and local candidate file hashes only; no installed-firmware attestation",
  }


def evaluate_release(offline_cases: object, *, evidence_context: object = None) -> dict:
  """Require all 41 cases from one verified run; never authorize deployment.

  The maintenance CLI validates evidence-file hashes against downstream-lock
  before constructing these records. This pure evaluator verifies accounting
  and batch binding; it does not turn arbitrary JSON claims into new test runs.
  Context keys other than run_id/lock_sha256 carry no authority.
  """
  reasons = []
  if type(evidence_context) is not dict or type(evidence_context.get("run_id")) is not str or not _RUN_ID.fullmatch(evidence_context["run_id"]) or not _hash(evidence_context.get("lock_sha256")):
    reasons.append("CURRENT_RUN_CONTEXT_REQUIRED")
    context = {}
  else:
    context = evidence_context
  if type(offline_cases) is not list or len(offline_cases) != len(REQUIRED_CASE_IDS):
    reasons.append("ALL_REQUIRED_OFFLINE_CASES_REQUIRED")
    cases = offline_cases if type(offline_cases) is list and len(offline_cases) <= 128 else []
  else:
    cases = offline_cases
  seen = set()
  passed = []
  case_keys = {"id", "status", "executed", "failed", "skipped", "evidence_sha256", "run_id", "lock_sha256"}
  for index, case in enumerate(cases):
    if not _exact_keys(case, case_keys):
      reasons.append(f"CASE_{index}_SCHEMA_INVALID")
      continue
    case_id = case["id"]
    if type(case_id) is not str or case_id not in REQUIRED_CASE_IDS or case_id in seen:
      reasons.append(f"CASE_{index}_UNKNOWN_OR_DUPLICATE")
      continue
    seen.add(case_id)
    if case["status"] != "PASS" or type(case["executed"]) is not int or case["executed"] < 1 or any(type(case[field]) is not int or case[field] != 0 for field in ("failed", "skipped")) or not _hash(case["evidence_sha256"]):
      reasons.append(f"{case_id}_NOT_EXECUTED_AND_PASSED")
      continue
    if case["run_id"] != context.get("run_id") or case["lock_sha256"] != context.get("lock_sha256"):
      reasons.append(f"{case_id}_STALE_OR_FOREIGN_EVIDENCE")
      continue
    passed.append(case_id)
  missing = sorted(set(REQUIRED_CASE_IDS) - seen)
  if missing:
    reasons.append("MISSING_CASES:" + ",".join(missing))
  # Policy is explanatory data only. Its contents cannot remove blockers or
  # grant capabilities: the executable deny contract is fixed below.
  blockers = [
    {"id": "PHYSICAL_IDENTITY", "status": "UNVERIFIED", "detail": "DOS/F413 and external Panda physical identities and wiring remain unconfirmed."},
    {"id": "TARGET_RUNTIME", "status": "NOT_TESTED", "detail": "C3XL OS/camera/USB/SPI/thermal runtime has not executed these candidates."},
    {"id": "RECOVERY_CHAIN", "status": "UNVERIFIED", "detail": "Installed bootstub/app ABI, trusted signing and independently tested recovery are not established."},
    {"id": "DRIVER_MONITORING", "status": "MISSING_HARDWARE", "detail": "C3XL was sold without a driver camera; equivalent validated driver monitoring is unresolved."},
    {"id": "AUDIBLE_ALERTS", "status": "UNVERIFIED", "detail": "Required audible alert output and failure handling remain unvalidated."},
    {"id": "VEHICLE_CONTROL_SAFETY", "status": "UNAVAILABLE", "detail": "Song PLUS controller, signal validation and dedicated independent safety are not implemented or vehicle-validated."},
    {"id": "RESOURCE_RUNTIME_LIMITS", "status": "UNRESOLVED", "detail": "Worst-case nested stacks, physical MCU capacity and inherited H7 cookie/microphone overlap require separate resolution."},
    {"id": "CANDIDATE_TRUST", "status": "NONDEPLOYABLE", "detail": "Both debug and nondebug candidates use the public development key; no deployment trust chain or install package exists."},
  ]
  complete = not reasons and len(passed) == len(REQUIRED_CASE_IDS)
  return {
    "schema": "songplus-maintenance-release-gate/v1",
    "offline_compatibility_complete": complete,
    "offline_cases_passed": len(passed),
    "offline_cases_required": len(REQUIRED_CASE_IDS),
    "offline_evidence_errors": reasons,
    "run_id": context.get("run_id"),
    "lock_sha256": context.get("lock_sha256"),
    "scopes": {"offline_host_and_arm_build": complete, "mcu_hardware_execution": False, "target_runtime": False, "recovery": False, "vehicle_validation": False},
    "deployment_ready": False,
    "release_installable": False,
    "permissions": dict(DENIED_PERMISSIONS),
    "blockers": blockers,
  }


def load_release_policy() -> dict:
  """Read versioned documentation; it is not an authorization input."""
  return json.loads(Path(__file__).with_name("release_policy.json").read_text())
