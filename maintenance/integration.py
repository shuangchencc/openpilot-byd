"""Append-only P8 integration maintenance; no device entry or deployment path.

P7 remains a separately replayable, frozen contract. This lock binds its exact
bytes, additive host/firmware patches, and the receive-only vehicle source.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from maintenance import core

ROOT = core.ROOT
LOCK = "maintenance/integration-lock.json"
MANIFESTS = {"host": "patches/openpilot/p8/manifest.json", "panda": "patches/panda/p8/manifest.json"}
PARENTS = {"host": core.HOST, "panda": core.PANDA}
CASE_IDS = ("B80", "B81", "B82", "B83", "B84", "B85", "B86")
DENIED = {"device_access": False, "real_can_tx": False, "deployment_ready": False,
          "actual_mcu_executed": False, "vehicle_validated": False}


def now():
  return datetime.now(timezone.utc).isoformat()


def cover_discovered_sources(root, locked):
  discovered = {str(p.relative_to(root)) for p in (root / "maintenance").rglob("*.py") if "__pycache__" not in p.parts}
  core.require(discovered <= set(locked), "Unpinned maintenance module/test would execute: " + ", ".join(sorted(discovered - set(locked))))


def verify_lock(root=ROOT, metadata_only=False):
  root = Path(root).resolve()
  parent = core.verify_lock(root, metadata_only=metadata_only)
  lock = core.read_json(root / LOCK)
  core.require(lock["schema"] == "songplus-integration-lock/v1", "Unknown integration lock")
  core.require(lock["parent_lock_sha256"] == core.sha(root / core.LOCK), "P7 parent lock drift")
  core.require(lock["baselines"] == core.read_json(root / core.LOCK)["baselines"], "Integration baseline drift")
  core.require(lock["permissions"] == DENIED, "Integration cannot grant hardware permissions")
  core.require(lock["required_cases"] == list(CASE_IDS), "Integration case set drift")
  required = {"maintenance/integration.py", "maintenance/test_integration.py", "maintenance/INTEGRATION.md",
              "tools/songplus_integration.py", "tools/check_songplus_integration_ci.py",
              "tests/test_songplus_integration_ci.py", ".github/workflows/songplus-integration-offline.yml",
              "maintenance/vehicle_readonly.py", "maintenance/test_vehicle_readonly.py", "maintenance/integration_evidence.py",
              "tools/validate_p8_runtime.py", "tools/build_p8_runtime.py", "tools/validate_p8_resources.py",
              "maintenance/python-dependencies.json", "maintenance/integration-test-inventory.json",
              "maintenance/opendbc-source-tree.json",
              "tools/p8_runtime_linux_sandbox.sh",
              "songplus_readonly/evidence-lock.json", *MANIFESTS.values()}
  core.require(required <= set(lock["files"]), "Required integration file omitted")
  for name, expected in lock["files"].items():
    core.require(core.sha(core.safe_path(root, name)) == expected, "Integration file drift: " + name)
  cover_discovered_sources(root, set(lock["files"]) | set(core.read_json(root / core.LOCK)["files"]))
  dbc_tree = core.read_json(root / "maintenance/opendbc-source-tree.json")
  core.require(dbc_tree["schema"] == "songplus-locked-opendbc-tree/v1" and dbc_tree["files"]
               and dbc_tree["commit"] == lock["baselines"]["upstream-openpilot/opendbc_repo"], "opendbc tree baseline mismatch")
  python_deps = core.read_json(root / "maintenance/python-dependencies.json")
  core.require(python_deps["schema"] == "songplus-integration-python-dependencies/v1" and python_deps["files"], "Missing Python byte lock")
  if not metadata_only:
    for name, expected in python_deps["files"].items():
      core.require(core.sha(core.safe_path(root, name)) == expected, "Python dependency drift: " + name)
    for package in python_deps["package_roots"]:
      path = core.safe_path(root, package)
      observed = {str(p.relative_to(root)) for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
      core.require(observed == {p for p in python_deps["files"] if p.startswith(package + "/")}, "Python package inventory drift: " + package)
  # Do not let newly added executable code silently escape the source contract.
  vehicle_files = {str(p.relative_to(root)) for p in (root / "songplus_readonly").rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".dbc", ".json", ".txt", ".md")}
  core.require(vehicle_files <= set(lock["files"]), "Unlocked vehicle source/evidence")
  for name, path in MANIFESTS.items():
    manifest = core.read_json(root / path)
    core.require(manifest["baselines"] == lock["baselines"], "Patch baseline disagreement: " + name)
    core.require(manifest["parent_manifest_sha256"] == core.sha(root / PARENTS[name]), "Patch parent drift: " + name)
    series = core.normalized_series(root, path)
    core.require(series == lock["series"][name] and len(series) > 0, "Integration patch order drift: " + name)
    core.require(all(p["stage"] == "P8" for p in series), "Only additive P8 patches belong here")
    for patch in series:
      core.require(lock["files"].get(patch["file"]) == patch["sha256"], "Patch outside integration lock")
    for relative, expected in manifest.get("additional_baseline_inputs", {}).items():
      core.require(relative.startswith("upstream-openpilot/"), "Additional baseline path invalid")
      if not metadata_only:
        core.require(core.sha(core.safe_path(root, relative)) == expected, "Additional baseline source drift")
  from maintenance.vehicle_readonly import verify_contract
  vehicle = verify_contract(root=root)
  return {"status": "PASS", "metadata_only": metadata_only, "lock_sha256": core.sha(root / LOCK),
          "parent": parent, "vehicle": vehicle, "locked_files": len(lock["files"]), **DENIED}


def bundle_sources(output, trees):
  """Content-only archive, deliberately excluding permissions, .git and inputs."""
  target = output / "source.tar"
  with tarfile.open(target, "w", format=tarfile.USTAR_FORMAT) as archive:
    for name, entries in sorted(trees.items()):
      for relative in sorted(entries):
        data = core.safe_path(output / name, relative).read_bytes()
        info = tarfile.TarInfo(name + "/" + relative)
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        archive.addfile(info, io.BytesIO(data))
  return core.sha(target)


def replay(output, root=ROOT):
  root = Path(root).resolve()
  verified = verify_lock(root)
  out = core.new_output(output)
  parent = core.replay(out / "parent-p7", root)
  commands = []
  for name in ("host", "panda", "opendbc"):
    shutil.copytree(out / "parent-p7" / name, out / name, ignore=shutil.ignore_patterns(".git"))
  for name, manifest_path in MANIFESTS.items():
    manifest = core.read_json(root / manifest_path)
    directory = out / name
    expected = dict(parent["files"][name])
    for relative, digest in manifest.get("additional_baseline_inputs", {}).items():
      target_name = relative.removeprefix("upstream-openpilot/")
      target = core.safe_path(directory, target_name)
      core.require(target_name not in expected or expected[target_name] == digest, "Conflicting baseline input")
      target.parent.mkdir(parents=True, exist_ok=True)
      target.write_bytes(core.safe_path(root, relative).read_bytes())
      expected[target_name] = digest
    core.capture(["git", "-c", "init.templateDir=", "init", "-q", directory], root)
    for patch in core.normalized_series(root, manifest_path):
      # Every changed path must have an independently locked expected result.
      stats = core.capture(["git", "apply", "--numstat", root / patch["file"]], directory).decode().splitlines()
      result_files = manifest.get("result_files", manifest.get("result_tree", {}))
      for stat in stats:
        path = stat.split("\t")[2]
        core.require(path in result_files, "Undeclared integration patch path: " + path)
      for check in (["--check"], []):
        argv = ["git", "apply", *check, str(root / patch["file"])]
        core.capture(argv, directory)
        commands.append({"cwd": name, "argv": argv})
    if "result_tree" in manifest:
      expected = manifest["result_tree"]
    else:
      expected.update(manifest["result_files"])
    core.require(core.tree_hashes(directory) == expected, "P8 replay result mismatch: " + name)
  for relative in core.read_json(root / LOCK)["files"]:
    if relative.startswith("songplus_readonly/"):
      target = core.safe_path(out / "vehicle", relative)
      target.parent.mkdir(parents=True, exist_ok=True)
      target.write_bytes(core.safe_path(root, relative).read_bytes())
  trees = {name: core.tree_hashes(out / name) for name in ("host", "panda", "opendbc", "vehicle")}
  core.require(trees["opendbc"] == core.read_json(root / "maintenance/opendbc-source-tree.json")["files"], "opendbc archive tree mismatch")
  result = {"status": "PASS", "lock_sha256": verified["lock_sha256"], "files": trees,
            "bundle_sha256": bundle_sources(out, trees), "commands": commands,
            "scope": "P7 declared source inputs plus P8 additive patches and receive-only source; not an installer", **DENIED}
  core.write_json(out / "replay.json", result)
  return result


def check_report_context(report, lock_hash):
  core.require(report["schema"] == "songplus-integration-validation/v1", "Unknown integration report")
  core.require(report["status"] == "PASS_OFFLINE_INTEGRATION", "Integration run incomplete")
  core.require(report["lock_sha256"] == lock_hash, "Integration evidence belongs to another lock")
  core.require(all(report.get(k) is v for k, v in DENIED.items()), "Integration evidence exceeds execution scope")
  core.require(report["failed"] == report["skipped"] == 0, "Failed/skipped integration evidence")
  cases = report["acceptance"]
  core.require(len(cases) == len(CASE_IDS) and [c["id"] for c in cases] == list(CASE_IDS), "Integration acceptance incomplete")
  for case in cases:
    core.require(case["status"] == "PASS" and type(case["executed"]) is int and case["executed"] > 0
                 and case["failed"] == case["skipped"] == 0, "Empty/failed integration case")
    core.require(case["run_id"] == report["run_id"] and case["lock_sha256"] == lock_hash, "Stale integration case")


def release_check(report_path):
  verify_lock(metadata_only=True)
  path = Path(report_path).resolve()
  report = core.read_json(path)
  check_report_context(report, core.sha(ROOT / LOCK))
  start, end = (datetime.fromisoformat(report[k]) for k in ("started_at_utc", "completed_at_utc"))
  core.require(start <= end, "Invalid integration time interval")
  expected = {"p7", "runtime", "resources", "vehicle", "contract", "source_replay"}
  core.require(set(report["evidence"]) == expected, "Integration evidence registry incomplete")
  evidence = {}
  for name, item in report["evidence"].items():
    target = core.safe_path(path.parent, item["path"])
    core.require(core.sha(target) == item["sha256"], "Integration child hash mismatch: " + name)
    core.require(item["run_id"] == report["run_id"] and item["lock_sha256"] == report["lock_sha256"], "Child context mismatch")
    evidence[name] = core.read_json(target)
  for name, field in (("p7", "completed_at_utc"), ("runtime", "completed_at"), ("resources", "utc"),
                      ("vehicle", "created_at_utc"), ("contract", "completed_at_utc")):
    core.require(start <= datetime.fromisoformat(evidence[name][field]) <= end, "Historical child cannot count in this batch: " + name)
  from maintenance import integration_evidence as checks
  from maintenance.vehicle_readonly import validate_acceptance_report
  bases = {name: core.safe_path(path.parent, item["path"]).parent for name, item in report["evidence"].items()}
  checks.resources(evidence["resources"], bases["resources"])
  checks.runtime(evidence["runtime"], bases["runtime"])
  checks.contract(evidence["contract"], bases["contract"])
  validate_acceptance_report(evidence["vehicle"], bases["vehicle"])
  for item in report["commands"]:
    core.require(item["exit_code"] == 0, "Integration command failure")
    core.require(core.sha(core.safe_path(path.parent, item["name"] + ".log")) == item["log_sha256"], "Integration command log drift")
  core.require(len(report["commands"]) == 5 and {c["name"] for c in report["commands"]} == {"contract", "p7", "vehicle", "resources", "runtime"},
               "Integration command inventory incomplete")
  source = evidence["source_replay"]
  core.require(source["lock_sha256"] == report["lock_sha256"], "Source replay lock drift")
  core.require(set(source["files"]) == {"host", "panda", "opendbc", "vehicle"} and all(source["files"].values())
               and source["source_counts"] == {k: len(v) for k, v in source["files"].items()}, "Source replay inventory incomplete")
  prior, host = core.read_json(ROOT / core.HOST), core.read_json(ROOT / MANIFESTS["host"])
  host_files = {k.removeprefix("upstream-openpilot/"): v for k, v in prior["baseline_inputs"].items()}
  host_files.update(prior["result_files"])
  host_files.update({k.removeprefix("upstream-openpilot/"): v for k, v in host.get("additional_baseline_inputs", {}).items()})
  host_files.update(host["result_files"])
  core.require(source["files"]["host"] == host_files and source["files"]["panda"] == core.read_json(ROOT / MANIFESTS["panda"])["result_tree"],
               "Source replay does not represent locked patches")
  core.require(source["files"]["opendbc"] == core.read_json(ROOT / "maintenance/opendbc-source-tree.json")["files"], "Source package opendbc baseline changed")
  vehicle_files = {k: v for k, v in core.read_json(ROOT / LOCK)["files"].items() if k.startswith("songplus_readonly/")}
  core.require(source["files"]["vehicle"] == vehicle_files, "Source package vehicle boundary changed")
  for replay_dir in ("replay", "replay-check"):
    base = core.safe_path(path.parent, replay_dir)
    core.require(core.sha(base / "source.tar") == source["bundle_sha256"], "Retained source bundle drift")
    core.require({name: core.tree_hashes(base / name) for name in source["files"]} == source["files"], "Retained source tree drift")
  # P7 validates retained artifacts and re-derives all 41 historical cases.
  from maintenance.validation import release_check as p7_release
  p7 = p7_release(core.safe_path(path.parent, report["evidence"]["p7"]["path"]))
  core.require(p7["offline_compatibility_complete"] is True, "P0-P7 regression incomplete")
  derived = derive_cases(evidence, report["run_id"], report["lock_sha256"])
  core.require(derived == report["acceptance"], "Integration cases differ from actual child evidence")
  blockers = [dict(item) for item in p7["deployment_blockers"]]
  for item in blockers:
    if item["id"] == "RESOURCE_RUNTIME_LIMITS":
      item["detail"] = "P8 removes the candidate H7 cookie/buffer overlap. Physical MCU capacity and complete nested stack/target runtime proof remain unverified."
  return {"status": "BLOCKED_FOR_DEPLOYMENT", "offline_integration_complete": True,
          "offline_compatibility_complete": True, "deployment_ready": False,
          "deployment_blockers": blockers,
          "runtime_resource_proof_complete": False,
          "note": "Offline integration and layout fixes do not prove target runtime, worst-case stack, identity or vehicle semantics."}


def derive_cases(evidence, run_id, lock_hash):
  """Filled from the executed validators, never a static count of planned cases."""
  # Child-specific contracts are below, kept separate from the source lock.
  counts = child_counts(evidence)
  return [{"id": name, "status": "PASS", "executed": counts[name], "failed": 0, "skipped": 0,
           "run_id": run_id, "lock_sha256": lock_hash} for name in CASE_IDS]


def child_counts(evidence):
  """Derive case counts from bounded executed groups; file checks occur separately."""
  from maintenance.integration_evidence import groups_passed
  replay = evidence["source_replay"]
  core.require(replay["status"] == "PASS" and replay["bundles_equal"] is True and replay["trees_equal"] is True,
               "Independent source replays differ")
  vehicle = evidence["vehicle"]
  core.require(vehicle["status"] == "PASS_OFFLINE_ONLY" and vehicle["tests"]["passed"] == vehicle["tests"]["tests_run"] == 55
               and vehicle["tests"]["failed"] == vehicle["tests"]["errors"] == vehicle["tests"]["skipped"] == 0, "Vehicle tests incomplete")
  core.require(vehicle["replay"]["segments"] == vehicle["replay"]["rlogs_rehashed"] == vehicle["replay"]["caches_rehashed"] == 98
               and vehicle["replay"]["omitted_can_events"] == 0, "Vehicle replay coverage incomplete")
  runtime = evidence["runtime"]
  core.require(runtime["status"] == "PASS_OFFLINE_HOST_ONLY" and set(runtime["platforms"]) == {"macos", "linux"}, "Runtime platforms incomplete")
  runtime_count = sum(groups_passed(suite) for suite in runtime["platforms"].values())
  resources = evidence["resources"]
  core.require(resources["status"] == "PASS_OFFLINE_CANDIDATE" and resources["summary"]["arm_builds"] == 8
               and resources["summary"]["failed"] == resources["summary"]["skipped"] == 0, "Resource builds incomplete")
  faults = resources["reports"]["resource-faults"]
  core.require(faults["passed"] == 23 and faults["runtime_proof_completed"] is False
               and resources["runtime_resource_gate"] == "BLOCKED", "Resource rejection gate incomplete")
  contract = evidence["contract"]
  core.require(contract["status"] == "PASS" and contract["passed_commands"] == 8
               and contract["failed"] == contract["skipped"] == 0, "CI contract incomplete")
  parent = evidence["p7"]
  core.require(parent["status"] == "PASS_OFFLINE_COMPATIBILITY" and len(parent["acceptance"]) == 41
               and parent["release"]["offline_compatibility_complete"] is True, "Fresh P0-P7 prerequisite incomplete")
  return {"B80": 2, "B81": vehicle["tests"]["passed"], "B82": vehicle["replay"]["segments"],
          "B83": runtime_count, "B84": resources["summary"]["arm_builds"], "B85": faults["passed"],
          "B86": contract["passed_commands"]}


def validate(output, linux_instance, linux_root):
  import platform
  import sys
  core.require(platform.system() == "Darwin" and not sys.flags.optimize, "Prepared macOS orchestrator without -O required")
  core.require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", linux_instance) is not None, "Invalid local VM")
  core.require(linux_root.startswith("/home/") and ".." not in Path(linux_root).parts
               and "\n" not in linux_root and "\0" not in linux_root, "Invalid Linux dependency root")
  core.require(Path(os.path.abspath(output)).is_relative_to(ROOT / "results"), "Output must be under results/")
  verified = verify_lock()
  protected = [LOCK, *core.read_json(ROOT / LOCK)["files"]]
  core.capture(["git", "ls-files", "--error-unmatch", *protected])
  core.capture(["git", "diff", "--exit-code", "HEAD", "--", *protected])
  commit = core.capture(["git", "rev-parse", "HEAD"]).decode().strip()
  out = core.new_output(output)
  started, run_id, lock_hash = now(), "p8-" + uuid.uuid4().hex, verified["lock_sha256"]
  python = ROOT / "upstream-openpilot/.venv/bin/python"
  sandbox = ["/usr/bin/sandbox-exec", "-f", str(ROOT / "tools/panda_firmware_offline.sb")]
  env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONOPTIMIZE="0",
             UV_OFFLINE="1", PIP_NO_INDEX="1", P2_NETWORK_DEVICE_SANDBOX="macos-seatbelt")
  commands = []
  core.write_json(out / "result.json", {"status": "RUNNING", "run_id": run_id, **DENIED})
  def run(name, argv, isolated=True):
    full = [*sandbox, *map(str, argv)] if isolated else list(map(str, argv))
    proc = subprocess.run(full, cwd=ROOT, env=env, capture_output=True, timeout=7200)
    log = out / (name + ".log")
    log.write_bytes(proc.stdout + proc.stderr)
    commands.append({"name": name, "argv": full, "exit_code": proc.returncode, "log_sha256": core.sha(log)})
    core.require(proc.returncode == 0, name + " failed; see " + str(log))
  try:
    first, second = replay(out / "replay"), replay(out / "replay-check")
    source = {"status": "PASS", "lock_sha256": lock_hash, "bundles_equal": first["bundle_sha256"] == second["bundle_sha256"],
              "bundle_sha256": first["bundle_sha256"], "trees_equal": first["files"] == second["files"],
              "files": first["files"], "source_counts": {k: len(v) for k, v in first["files"].items()}}
    core.require(source["bundles_equal"] and source["trees_equal"], "Source package mismatch")
    core.write_json(out / "source-replay.json", source)
    run("contract", [python, "-B", ROOT / "tools/check_songplus_integration_ci.py", "--mode", "contract", "--output", out / "contract"])
    # Only the P7 and P8 runtime orchestration needs local Lima IPC. All workers
    # deny network/device access; these two VM build phases are serialized.
    with ThreadPoolExecutor(max_workers=3) as pool:
      parent = pool.submit(run, "p7", [python, "-B", ROOT / "tools/check_panda_ci.py", "--mode", "full",
          "--linux-instance", linux_instance, "--linux-root", linux_root, "--output", out / "p7"], False)
      vehicle = pool.submit(run, "vehicle", [python, "-B", "-m", "maintenance.vehicle_readonly", "acceptance", "--out", out / "vehicle"], False)
      resources = pool.submit(run, "resources", [python, "-B", ROOT / "tools/validate_p8_resources.py", "--output", out / "resources"], False)
      parent.result()
      run("runtime", [python, "-B", ROOT / "tools/validate_p8_runtime.py", "--output", out / "runtime",
                      "--linux-instance", linux_instance, "--linux-root", linux_root], False)
      vehicle.result()
      resources.result()
    evidence_paths = {"p7": "p7/validation/result.json", "runtime": "runtime/result.json", "resources": "resources/result.json",
                      "vehicle": "vehicle/acceptance.json", "contract": "contract/result.json", "source_replay": "source-replay.json"}
    evidence = {k: core.read_json(out / p) for k, p in evidence_paths.items()}
    core.require(all(core.tree_hashes(out / "replay" / name) == entries for name, entries in first["files"].items()), "Replay source mutated")
    verify_lock()
    registry = {name: {"path": path, "sha256": core.sha(out / path), "run_id": run_id, "lock_sha256": lock_hash}
                for name, path in evidence_paths.items()}
    result = {"schema": "songplus-integration-validation/v1", "status": "PASS_OFFLINE_INTEGRATION", "run_id": run_id,
              "started_at_utc": started, "completed_at_utc": now(), "lock_sha256": lock_hash,
              "parent_lock_sha256": core.sha(ROOT / core.LOCK), "maintenance_commit": commit,
              "protected_files_match_commit": True, "baselines": core.read_json(ROOT / LOCK)["baselines"],
              "acceptance": derive_cases(evidence, run_id, lock_hash), "evidence": registry,
              "commands": sorted(commands, key=lambda c: c["name"]), "failed": 0, "skipped": 0,
              "remote_ci_executed": False, **DENIED}
    core.write_json(out / "result.json", result)
    gate = release_check(out / "result.json")
    core.write_json(out / "release.json", gate)
    core.write_json(ROOT / "results/songplus-integration-p8/latest.json", {"status": result["status"], "result": str(out / "result.json")})
    return result
  except Exception as error:
    core.write_json(out / "result.json", {"status": "FAIL", "error": str(error), "run_id": run_id,
        "started_at_utc": started, "completed_at_utc": now(), "commands": commands, **DENIED})
    raise
