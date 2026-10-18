"""Pinned Song PLUS receive-only sources and explicitly local replay acceptance.

Source contract inspection uses only the standard library and needs neither
opendbc nor private recordings. The full acceptance requires existing local
inputs; it never downloads, enumerates hardware or starts a vehicle process.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOCK = "songplus_readonly/evidence-lock.json"
TARGET_COMMIT = "b3bd79a659dae961af169a3baac723961f720133"
RUNTIME_FILES = (
  "songplus_readonly/__init__.py", "songplus_readonly/runtime.py",
  "songplus_readonly/carstate.py", "songplus_readonly/dbc/byd_song_plus_readonly.dbc",
)
OFFLINE_FILES = (
  "songplus_readonly/replay.py", "songplus_readonly/tests/test_carstate.py",
  "songplus_readonly/tests/test_candidate_fields.py", "songplus_readonly/tests/test_replay.py",
  "songplus_readonly/README.txt", "songplus_readonly/VALIDATION.txt",
  "songplus_readonly/CANDIDATES-VALIDATION.txt", "songplus_readonly/MAINTENANCE.txt",
  "songplus_readonly/field-evidence.json",
)
REQUIRED_MESSAGES = {287: 5, 291: 8, 307: 8, 496: 8, 508: 8, 544: 8, 578: 8, 834: 8}
DENIED = dict.fromkeys(("device_access", "network_access", "real_can_tx", "runtime_launch",
                       "firmware_flash", "system_write", "deployment_ready",
                       "vehicle_semantics_independently_confirmed"), False)
VALIDATOR_FILES = ("maintenance/vehicle_readonly.py", "tools/panda_firmware_offline.sb")


def sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def canonical_hash(value: object) -> str:
  return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   allow_nan=False).encode()).hexdigest()


def require(condition: bool, message: str) -> None:
  if not condition:
    raise ValueError(message)


def read_json(path: Path) -> dict:
  def unique(pairs):
    value = {}
    for key, item in pairs:
      require(key not in value, "duplicate JSON key: " + key)
      value[key] = item
    return value
  def invalid_constant(value):
    raise ValueError("nonfinite JSON number: " + value)
  return json.loads(path.read_text(), object_pairs_hook=unique, parse_constant=invalid_constant)


def reject_symlink_components(path: Path) -> None:
  path = path.absolute()
  for component in (*reversed(path.parents), path):
    require(not component.is_symlink(), "symlink component: " + str(component))
  require(not any(path == Path(prefix) or Path(prefix) in path.parents for prefix in ("/dev", "/proc", "/sys")),
          "device/system path is not an artifact location")


def create_output_directory(path: Path) -> Path:
  path = path.absolute()
  reject_symlink_components(path)
  path.mkdir(parents=True, exist_ok=False)
  return path


def write_new(path: Path, value: object) -> None:
  with path.open("x") as handle:
    json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
    handle.write("\n")


def verify_contract(root: Path = ROOT) -> dict:
  """Verify frozen sources/evidence policy without importing vehicle code."""
  root = Path(root).absolute()
  reject_symlink_components(root)
  reject_symlink_components(root / LOCK)
  lock = read_json(root / LOCK)
  require(lock.get("schema") == "songplus-readonly-evidence-lock/v1", "evidence lock schema")
  require(lock.get("opendbc_commit") == TARGET_COMMIT, "opendbc pin changed")
  require(type(lock.get("permissions")) is dict and set(lock["permissions"]) == set(DENIED)
          and all(v is False for v in lock["permissions"].values()), "read-only permission policy changed")
  files = lock["versioned_files_sha256"]
  require(set(files) == set(RUNTIME_FILES + OFFLINE_FILES), "versioned source inventory changed")
  require(lock["runtime_files"] == list(RUNTIME_FILES), "runtime dependency boundary changed")
  require(lock["offline_only_files"] == list(OFFLINE_FILES), "offline dependency boundary changed")
  for name, expected in files.items():
    path = root / name
    reject_symlink_components(path)
    require(path.is_file() and not path.is_symlink(), "source missing or symlink: " + name)
    require(sha256(path) == expected, "source hash mismatch: " + name)
  actual = {str(p.relative_to(root)) for p in (root / "songplus_readonly").rglob("*")
            if p.is_file() and p.suffix in (".py", ".dbc", ".json", ".txt")}
  require(actual == set(files) | {LOCK}, "unaccounted vehicle source/evidence file")
  evidence = read_json(root / "songplus_readonly/field-evidence.json")
  require(evidence.get("schema") == "songplus-readonly-field-evidence/v1", "field evidence schema")
  require(evidence.get("canValid") is False and evidence.get("vehicle_semantics_independently_confirmed") is False,
          "evidence cannot grant control validity")
  fields = evidence["fields"]
  require(len(fields) == 32, "field inventory changed")
  tiers = Counter(v["tier"] for v in fields.values())
  require(tiers == {"supported_encoding": 5, "empirical_candidate": 10, "unconfirmed": 17}, "evidence tier counts changed")
  for name, item in fields.items():
    require(item.get("vehicle_confirmed") is False and item.get("candidate") is (item["tier"] == "empirical_candidate"),
            "field promoted: " + name)
  for name in ("cruiseState.enabled", "cruiseState.available", "cruiseState.speed"):
    require(fields[name]["tier"] == "unconfirmed", "reserved ACC field promoted")
  tree = ast.parse((root / "songplus_readonly/carstate.py").read_text())
  assignments = [node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Attribute) and target.attr == "canValid" for target in node.targets)]
  require(len(assignments) == 1 and isinstance(assignments[0], ast.Constant) and assignments[0].value is False,
          "canValid must remain explicitly false")
  for name in RUNTIME_FILES:
    if not name.endswith(".py"):
      continue
    source = ast.parse((root / name).read_text())
    imports = [n.module or "" for n in ast.walk(source) if isinstance(n, ast.ImportFrom)]
    imports += [alias.name for n in ast.walk(source) if isinstance(n, ast.Import) for alias in n.names]
    require(not any(module.startswith(("songplus_readonly.replay", "offline_analysis", "panda", "numpy", "socket"))
                    for module in imports), "research or hardware dependency in runtime")
  dbc = (root / RUNTIME_FILES[-1]).read_text()
  messages = re.findall(r"^BO_ (\d+) \w+: (\d+) (\w+)$", dbc, re.M)
  require({int(address): int(length) for address, length, _ in messages} == REQUIRED_MESSAGES
          and len(messages) == len(REQUIRED_MESSAGES), "receive DBC inventory changed")
  external = lock["external_evidence"]
  require(external["raw_logs_committed"] is False and external["research_runtime_dependency"] is False,
          "raw/research boundary changed")
  segments = external["segments"]
  require(len(segments) == 98 and [s["segment"] for s in segments] == list(range(98)), "segment coverage changed")
  for entry in segments:
    for name in ("rlog_sha256", "cache_sha256"):
      require(re.fullmatch(r"[0-9a-f]{64}", entry[name]) is not None, "invalid external digest")
    require(type(entry["rlog_bytes"]) is int and entry["rlog_bytes"] > 0, "invalid rlog size")
  for name, digest in external["file_sha256"].items():
    require(not Path(name).is_absolute() and ".." not in Path(name).parts, "external locator must be bounded")
    require(re.fullmatch(r"[0-9a-f]{64}", digest) is not None, "invalid evidence digest")
  return {"schema": "songplus-readonly-source-contract/v1", "status": "PASS_SOURCE_CONTRACT",
          "source_files": len(files), "source_files_sha256": files, "lock_sha256": sha256(root / LOCK),
          "fields": len(fields), "evidence_tiers": dict(tiers), "segments_manifested": 98,
          "raw_logs_read": False, "replay_executed": False, "permissions": DENIED}


def verify_replay_report(report: dict, lock: dict) -> dict:
  """Fail closed on lost coverage, changed sources, evidence or nullable policy."""
  require(report.get("schema") == "songplus-readonly-carstate-replay/v1"
          and report.get("status") == "completed_offline_replay", "incomplete replay")
  for field in ("vehicle_semantics_independently_confirmed", "actuation_allowed", "network_or_device_access", "panda_safety_validated"):
    require(report.get(field) is False, "replay scope escalation: " + field)
  provenance = report["provenance"]
  external = lock["external_evidence"]
  require(provenance["segments"] == external["segments"], "external segment identity changed")
  require(provenance["rlog_hashes_verified"] == provenance["cache_hashes_verified"] == 98, "unverified input coverage")
  for field, expected in external["provenance_sha256"].items():
    require(provenance[field] == expected, "provenance changed: " + field)
  require(sorted(provenance["schema_sha256"].values()) == sorted(external["source_schema_sha256"].values()), "old schema identity changed")
  research = report["candidate_research"]
  require(research.get("verified") is True and research.get("input_chain_matches_current_route_and_caches") is True,
          "candidate research not verified")
  for field, expected in external["candidate_research"].items():
    require(research[field] == expected, "candidate research changed: " + field)
  require(report.get("runtime_source_unchanged_during_replay") is True, "source changed during replay")
  expected_runtime = {name: digest for name, digest in lock["versioned_files_sha256"].items()
                      if name.endswith((".py", ".dbc")) and "/tests/" not in name}
  require(report["runtime_source_sha256"] == expected_runtime, "replay runtime hash differs from lock")
  reference = lock["historical_replay_expectations"]
  require(report["counts"] == reference["counts"], "replay count regression")
  require(canonical_hash(report["fields"]) == reference["fields_sha256"], "field comparison/evidence regression")
  coverage = report["can_event_coverage"]
  require(len(coverage) == 98 and all(c["omitted_can_events"] == 0 for c in coverage), "CAN Event coverage loss")
  projection = report["official_projection"]
  require(projection["canValid"] is False and projection["unknown_defaults_usable"] is False
          and projection["nullable_sidecar_required"] is True, "nullable/control projection changed")
  unknown = report["reserved_acc_unknown"]
  require(unknown["all_checked_values_none"] is True and unknown["quality_always_unconfirmed"] is True,
          "ACC unknowns promoted")
  require(projection["samples_checked"] == reference["projection_samples"]
          and unknown["snapshots_checked"] == reference["unknown_snapshots"], "projection coverage changed")
  return {"status": "PASS_REPLAY_REGRESSION", "segments": 98, "rlogs_rehashed": 98, "caches_rehashed": 98,
          "original_can_events": report["counts"]["original_can_events"], "omitted_can_events": 0,
          "projection_samples": projection["samples_checked"], "unknown_snapshots": unknown["snapshots_checked"],
          "historical_labels_only": True, "vehicle_semantics_independently_confirmed": False}


def _worker_tests(output: Path) -> None:
  # Only called by the isolated acceptance subprocess with the pinned venv.
  from songplus_readonly.carstate import FIELD_EVIDENCE, RUNTIME
  import numpy
  require(FIELD_EVIDENCE == read_json(ROOT / "songplus_readonly/field-evidence.json")["fields"],
          "machine-readable field evidence differs from decoder")
  suite = unittest.defaultTestLoader.discover(str(ROOT / "songplus_readonly/tests"))
  class RecordedResult(unittest.TextTestResult):
    def startTest(self, test):
      ids.append(test.id())
      super().startTest(test)
  ids = []
  result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
  report = {"tests_run": result.testsRun, "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
            "failed": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped), "test_ids": ids,
            "runtime": RUNTIME, "python_version": sys.version, "numpy_version": numpy.__version__}
  write_new(output, report)
  require(result.testsRun == 55 and result.wasSuccessful() and not result.skipped, "55 mandatory vehicle tests required")


def validate_acceptance_report(report: dict, output: Path, *, root: Path = ROOT) -> dict:
  """Validate a recorded batch against present locked sources and local outputs.

  This does not rerun the suite and cannot establish physical validation. The
  outer maintenance lock must additionally bind this validator's own sources.
  """
  root, output = Path(root).absolute(), Path(output).absolute()
  reject_symlink_components(output)
  require(report.get("schema") == "songplus-readonly-maintenance-acceptance/v1"
          and report.get("status") == "PASS_OFFLINE_ONLY", "acceptance batch incomplete")
  require(report.get("permissions") == DENIED
          and all(value is False for value in report["permissions"].values()), "acceptance scope changed")
  require(report.get("sandbox") == "macos-seatbelt-network-device-iokit-denied", "acceptance isolation changed")
  require(report["contract"] == verify_contract(root), "acceptance source contract drift")
  validator = {name: sha256(root / name) for name in VALIDATOR_FILES}
  require(report["validator_source_sha256"] == validator, "acceptance validator source drift")
  require(set(report["artifacts_sha256"]) == {"tests.json", "replay.json"}, "acceptance output inventory")
  for name, expected in report["artifacts_sha256"].items():
    reject_symlink_components(output / name)
    require(sha256(output / name) == expected, "acceptance output drift: " + name)
  tests = read_json(output / "tests.json")
  require(report["tests"] == tests, "acceptance tests differ from output")
  require(tests["tests_run"] == tests["passed"] == 55 and tests["failed"] == tests["errors"] == tests["skipped"] == 0,
          "mandatory vehicle tests incomplete")
  require(type(tests["test_ids"]) is list and len(tests["test_ids"]) == len(set(tests["test_ids"])) == 55,
          "vehicle test inventory incomplete")
  require(tests["runtime"]["opendbc_commit"] == TARGET_COMMIT and tests["runtime"]["tracked_source_clean"] is True,
          "vehicle target runtime changed")
  commands = report["commands"]
  require(len(commands) == 2 and [command["name"] for command in commands] == ["vehicle-tests", "vehicle-replay"],
          "vehicle command inventory changed")
  for command in commands:
    require(command["returncode"] == 0 and type(command["returncode"]) is int, "vehicle command failed")
    argv = command["argv"]
    require(argv[:6] == ["/usr/bin/sandbox-exec", "-f", str(root / VALIDATOR_FILES[1]),
                         str(root / "upstream-openpilot/.venv/bin/python"), "-I", "-B"], "vehicle command sandbox changed")
    require(sha256(output / (command["name"] + ".log")) == command["log_sha256"], "vehicle command log drift")
  replay = verify_replay_report(read_json(output / "replay.json"), read_json(root / LOCK))
  require(report["replay"] == replay, "acceptance replay differs from output")
  return {**replay, "status": "PASS_RECORDED_OFFLINE_BATCH", "tests_passed": 55, "failed": 0, "skipped": 0}


def acceptance(output: Path, *, root: Path = ROOT, analysis_manifest: Path | None = None,
               candidate_research_manifest: Path | None = None, historical_replay_source: Path | None = None) -> dict:
  """Run existing tests and full route under the device/network-denying sandbox.

  Complete replay currently uses the private manifests' original absolute
  paths. It is a mandatory local-data gate, not a falsely portable public CI
  job. Source-only contract tests are portable and require no private data.
  """
  root = Path(root).absolute()
  contract = verify_contract(root)
  validators = {name: sha256(root / name) for name in VALIDATOR_FILES}
  require(root == ROOT, "acceptance must use this checkout")
  require(sys.platform == "darwin", "full local replay requires the prepared macOS sandbox/runtime")
  python = root / "upstream-openpilot/.venv/bin/python"
  require(python.is_file(), "missing prepared target Python; automatic download prohibited")
  lock = read_json(root / LOCK)
  defaults = lock["local_replay_arguments"]
  selected = {
    "analysis_manifest": analysis_manifest or root / defaults["analysis_manifest"],
    "candidate_research_manifest": candidate_research_manifest or root / defaults["candidate_research_manifest"],
    "historical_replay_source": historical_replay_source or root / defaults["historical_replay_source"],
  }
  # Check frozen historical reports/scripts before executing the replay runner.
  for name, expected in lock["external_evidence"]["file_sha256"].items():
    path = root / name
    reject_symlink_components(path)
    require(path.is_file() and sha256(path) == expected, "missing/drifted local evidence: " + name)
  for role, path in selected.items():
    expected = lock["external_evidence"]["file_sha256"][defaults[role]]
    require(Path(path).is_file() and sha256(Path(path)) == expected, "wrong local replay input: " + role)
  output = create_output_directory(Path(output))
  commands = []

  def run(name: str, module: str, arguments: list[str]) -> None:
    code = "import runpy,sys;sys.path.insert(0," + repr(str(root)) + ");runpy.run_module(" + repr(module) + ",run_name='__main__')"
    argv = ["/usr/bin/sandbox-exec", "-f", str(root / "tools/panda_firmware_offline.sb"),
            str(python), "-I", "-B", "-c", code, *arguments]
    log = output / (name + ".log")
    start = time.monotonic()
    with log.open("x") as handle:
      proc = subprocess.run(argv, cwd=root, stdout=handle, stderr=subprocess.STDOUT, timeout=7200,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    commands.append({"name": name, "argv": argv, "returncode": proc.returncode,
                     "elapsed_seconds": round(time.monotonic() - start, 3), "log_sha256": sha256(log)})
    require(proc.returncode == 0, "vehicle acceptance command failed: " + name)

  try:
    run("vehicle-tests", "maintenance.vehicle_readonly", ["_worker-tests", "--out", str(output / "tests.json")])
    run("vehicle-replay", "songplus_readonly.replay", ["--analysis-manifest", str(selected["analysis_manifest"]),
        "--candidate-research-manifest", str(selected["candidate_research_manifest"]),
        "--historical-replay-source", str(selected["historical_replay_source"]), "--out", str(output / "replay.json")])
    verify_contract(root)
    require(validators == {name: sha256(root / name) for name in VALIDATOR_FILES}, "validator source changed during acceptance")
    tests = read_json(output / "tests.json")
    require(tests["tests_run"] == tests["passed"] == 55 and tests["failed"] == tests["errors"] == tests["skipped"] == 0,
            "mandatory vehicle tests incomplete")
    replay = verify_replay_report(read_json(output / "replay.json"), lock)
    result = {"schema": "songplus-readonly-maintenance-acceptance/v1", "status": "PASS_OFFLINE_ONLY",
              "created_at_utc": datetime.now(timezone.utc).isoformat(), "contract": contract, "tests": tests,
              "replay": replay, "commands": commands, "permissions": DENIED,
              "validator_source_sha256": validators,
              "sandbox": "macos-seatbelt-network-device-iokit-denied",
              "artifacts_sha256": {name: sha256(output / name) for name in ("tests.json", "replay.json")}}
    validate_acceptance_report(result, output, root=root)
    write_new(output / "acceptance.json", result)
    return result
  except BaseException as error:
    write_new(output / "failure.json", {"status": "FAIL", "error": str(error), "commands": commands, "permissions": DENIED})
    raise


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("mode", choices=("source-check", "acceptance", "_worker-tests"))
  parser.add_argument("--out", type=Path, required=True)
  parser.add_argument("--analysis-manifest", type=Path)
  parser.add_argument("--candidate-research-manifest", type=Path)
  parser.add_argument("--historical-replay-source", type=Path)
  args = parser.parse_args()
  if args.mode == "source-check":
    write_new(args.out, verify_contract())
  elif args.mode == "_worker-tests":
    _worker_tests(args.out)
  else:
    acceptance(args.out, analysis_manifest=args.analysis_manifest,
               candidate_research_manifest=args.candidate_research_manifest,
               historical_replay_source=args.historical_replay_source)


if __name__ == "__main__":
  main()
