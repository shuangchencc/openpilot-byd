"""Pinned native-provider acceptance. No device-opening or deployment entry."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import uuid

from maintenance import core as old
from maintenance import integration as parent

ROOT = old.ROOT
LOCK = "native_maintenance/lock.json"
MANIFEST = "patches/openpilot/p9/manifest.json"
PARENT = "patches/openpilot/p8/manifest.json"
SCOPE = {"real_device_discovery": False, "real_device_io": False, "real_can_tx": False,
         "profile_bound_to_real_hardware": False, "deployment_ready": False}
require, sha, read_json, write_json, safe_path = old.require, old.sha, old.read_json, old.write_json, old.safe_path


def now():
  return datetime.now(timezone.utc).isoformat()


def protected_paths(root=ROOT):
  directories = ("native_maintenance", "patches/openpilot/p9")
  paths = {str(p.relative_to(root)) for d in directories for p in (root / d).rglob("*")
           if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
  paths.discard(LOCK)
  paths.update(("tools/build_p9_native.py", "tools/validate_p9_native.py", "tools/songplus_native.py",
                "tools/check_songplus_native_ci.py", ".github/workflows/songplus-native-offline.yml"))
  return paths


def verify_lock(root=ROOT, metadata_only=False):
  root = Path(root).resolve()
  parent_result = parent.verify_lock(root, metadata_only=metadata_only)
  lock = read_json(root / LOCK)
  require(lock["schema"] == "songplus-native-p9-lock/v1", "Unknown native lock")
  require(lock["parent_lock_sha256"] == sha(root / parent.LOCK), "P8 lock changed")
  require(lock["baselines"] == read_json(root / parent.LOCK)["baselines"], "Official baseline changed")
  require(lock["scope"] == SCOPE, "Native lock cannot grant hardware permissions")
  require(set(lock["files"]) == protected_paths(root), "Native protected file inventory changed")
  for name, expected in lock["files"].items():
    require(sha(safe_path(root, name)) == expected, "Native file drift: " + name)
  manifest = read_json(root / MANIFEST)
  require(manifest["baselines"] == lock["baselines"] and manifest["parent_manifest_sha256"] == sha(root / PARENT), "Native parent chain changed")
  require(manifest["parent_commit"] == read_json(root / PARENT)["result_commit"], "Native parent commit changed")
  series = old.normalized_series(root, MANIFEST)
  require(series == lock["series"] and series and all(p["stage"] == "P9" for p in series), "Native series changed")
  require(manifest["result_commit"] == series[-1]["commit"], "Native result commit differs from applied series")
  require(all(lock["files"].get(p["file"]) == p["sha256"] for p in series), "Native patch outside lock")
  require(manifest["device_access_enabled"] is False and manifest["can_tx_enabled"] is False
          and manifest["firmware_install_enabled"] is False, "Patch permissions changed")
  for platform in ("macos", "linux"):
    groups = manifest["required_groups"][platform]
    require(groups and len(groups) == len(set(groups)) and all(isinstance(x, str) and x for x in groups), "Invalid group inventory")
  require(set(manifest["required_groups"]) == {"macos", "linux"}, "Native platform scope changed")
  require(lock["required_tests"] and lock["required_tests"] == sorted(set(lock["required_tests"]))
          and all(isinstance(test, str) and test.startswith("test_") for test in lock["required_tests"]), "Invalid native maintenance test inventory")
  prerequisite = lock["prerequisite"]
  require(prerequisite["reused_only"] is True and prerequisite["newly_executed"] is False
          and prerequisite["report"] == "results/songplus-integration-p8-full-002/validation/result.json", "Historical P8 prerequisite identity changed")
  if not metadata_only:
    require(sha(safe_path(root, prerequisite["report"])) == prerequisite["sha256"], "Retained P8 evidence changed")
  return {"status": "PASS", "lock_sha256": sha(root / LOCK), "parent_lock_sha256": sha(root / parent.LOCK),
          "metadata_only": metadata_only, "protected_files": len(lock["files"]), "parent": parent_result, **SCOPE}


def expected_source(root=ROOT):
  prior = read_json(root / "patches/openpilot/p6/manifest.json")
  sources = {p.removeprefix("upstream-openpilot/"): h for p, h in prior["baseline_inputs"].items()}
  sources.update(prior["result_files"])
  for path in (PARENT, MANIFEST):
    manifest = read_json(root / path)
    sources.update({p.removeprefix("upstream-openpilot/"): h for p, h in manifest.get("additional_baseline_inputs", {}).items()})
    sources.update(manifest["result_files"])
  return sources


def replay(output, root=ROOT):
  root = Path(root).resolve()
  verify_lock(root)
  output = old.new_output(output)
  source = output / "source"
  source.mkdir()
  prior = read_json(root / "patches/openpilot/p6/manifest.json")
  inputs = dict(prior["baseline_inputs"])
  for path in (PARENT, MANIFEST):
    inputs.update(read_json(root / path).get("additional_baseline_inputs", {}))
  for name, expected in inputs.items():
    data = safe_path(root, name).read_bytes()
    require(sha(root / name) == expected, "Baseline source drift")
    dest = safe_path(source, name.removeprefix("upstream-openpilot/"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
  old.capture(["git", "-c", "init.templateDir=", "init", "-q"], source)
  for path in ("patches/openpilot/p6/manifest.json", PARENT, MANIFEST):
    for patch in old.normalized_series(root, path):
      old.capture(["git", "apply", "--check", root / patch["file"]], source)
      old.capture(["git", "apply", root / patch["file"]], source)
  files = old.tree_hashes(source)
  require(files == expected_source(root), "P9 replay source mismatch")
  with tarfile.open(output / "source.tar", "w", format=tarfile.USTAR_FORMAT) as archive:
    for name in sorted(files):
      data = (source / name).read_bytes()
      info = tarfile.TarInfo(name)
      info.size, info.mode, info.mtime = len(data), 0o644, 0
      archive.addfile(info, io.BytesIO(data))
  result = {"schema": "songplus-native-p9-replay/v1", "status": "PASS", "lock_sha256": sha(root / LOCK),
            "files": files, "bundle_sha256": sha(output / "source.tar"), **SCOPE}
  write_json(output / "result.json", result)
  return result


def release_check(report_path, root=ROOT):
  from native_maintenance.evidence import validate_native_report, validate_contract
  root, report_path = Path(root).resolve(), Path(report_path).resolve()
  verify_lock(root, metadata_only=True)
  report = read_json(report_path)
  base, lock = report_path.parent, read_json(root / LOCK)
  require(report["schema"] == "songplus-native-p9-acceptance/v1" and report["status"] == "PASS_OFFLINE_NATIVE", "Native acceptance incomplete")
  require(report["lock_sha256"] == sha(root / LOCK) and report["parent_lock_sha256"] == sha(root / parent.LOCK), "Native acceptance lock changed")
  require(all(report[k] is v for k, v in SCOPE.items()), "Acceptance scope changed")
  require(report["prerequisite"] == lock["prerequisite"], "Historical prerequisite misrepresented")
  prerequisite = safe_path(root, lock["prerequisite"]["report"])
  require(sha(prerequisite) == lock["prerequisite"]["sha256"], "P8 prerequisite hash changed")
  prior_gate = parent.release_check(prerequisite)
  require(prior_gate["offline_integration_complete"] is True and prior_gate["deployment_ready"] is False, "P8 prerequisite gate invalid")
  files = set(lock["files"]) | {LOCK}
  require(report["protected_files_match_commit"] is True, "Acceptance not committed")
  for name in files:
    require(old.capture(["git", "show", report["maintenance_commit"] + ":" + name], root) == safe_path(root, name).read_bytes(), "Accepted source commit drift: " + name)
  require(set(report["evidence"]) == {"native", "contract", "replay-1", "replay-2"}, "Missing evidence category")
  evidence = {}
  for name, item in report["evidence"].items():
    path = safe_path(base, item["path"])
    require(sha(path) == item["sha256"], "Evidence report drift: " + name)
    evidence[name] = (read_json(path), path.parent)
  native = validate_native_report(*evidence["native"], root=root)
  contract = validate_contract(*evidence["contract"], root=root)
  for name in ("replay-1", "replay-2"):
    item, folder = evidence[name]
    require(item["status"] == "PASS" and item["lock_sha256"] == report["lock_sha256"]
            and item["files"] == expected_source(root) == old.tree_hashes(folder / "source")
            and item["bundle_sha256"] == sha(folder / "source.tar"), "Native replay evidence changed")
  require(evidence["replay-1"][0]["bundle_sha256"] == evidence["replay-2"][0]["bundle_sha256"], "Native source reproducibility failed")
  from maintenance.integration_evidence import check_commands
  check_commands(report["commands"], base, {"source-contract", "native-validation"})
  start, end = datetime.fromisoformat(report["started_at_utc"]), datetime.fromisoformat(report["completed_at_utc"])
  require(start <= end, "Invalid acceptance time interval")
  child = evidence["native"][0]
  require(start <= datetime.fromisoformat(child["started_at"]) <= datetime.fromisoformat(child["completed_at"]) <= end,
          "Native execution outside acceptance interval")
  expected = {"fresh_native": native, "fresh_source_contract": contract, "fresh_source_replays": 2,
              "reused_p8_prerequisite": True, "fresh_arm_builds": 0, "fresh_vehicle_replay_segments": 0}
  require(report["counts"] == expected, "Acceptance count/evidence mismatch")
  return {"status": "BLOCKED_FOR_DEPLOYMENT", "offline_native_complete": True,
          "p8_prerequisite_verified": True, "p8_arm_and_vehicle_newly_executed": False,
          "counts": expected, **SCOPE, "deployment_blockers": [
            "Real USB/SPI identity, F4/DOS board revision, exact firmware/ABI and wiring remain unverified; shipped profile stays unbound.",
            "Native OS permissions, kernel/libusb timing, disconnect behavior and recovery need later authorized hardware acceptance.",
            "No target runtime, driver monitoring, full stack bound, vehicle controller or vehicle safety acceptance."]}


def validate(output, linux_instance, linux_root, root=ROOT):
  from native_maintenance.evidence import validate_native_report, validate_contract
  root = Path(root).resolve()
  verify_lock(root)
  lock = read_json(root / LOCK)
  commit = old.capture(["git", "rev-parse", "HEAD"], root).decode().strip()
  for name in set(lock["files"]) | {LOCK}:
    require(old.capture(["git", "show", commit + ":" + name], root) == safe_path(root, name).read_bytes(), "Commit protected source before full acceptance: " + name)
  parent.release_check(root / lock["prerequisite"]["report"])
  out = old.new_output(output)
  started, commands = now(), []

  def run(name, argv, env=None):
    result = subprocess.run(list(map(str, argv)), cwd=root, env=env, capture_output=True, timeout=7200)
    for stream, data in (("stdout", result.stdout), ("stderr", result.stderr)):
      (out / (name + "." + stream)).write_bytes(data)
    commands.append({"name": name, "exit_code": result.returncode, "expected_exit_code": 0,
                     "stdout_sha256": sha(out / (name + ".stdout")), "stderr_sha256": sha(out / (name + ".stderr"))})
    require(result.returncode == 0, name + " failed; see retained logs")

  try:
    replays = [replay(out / name, root) for name in ("replay-1", "replay-2")]
    require(replays[0]["bundle_sha256"] == replays[1]["bundle_sha256"], "Source replay nondeterministic")
    python = root / "upstream-openpilot/.venv/bin/python"
    run("source-contract", ["/usr/bin/sandbox-exec", "-f", root / "tools/panda_firmware_offline.sb",
                            python, "-B", root / "tools/check_songplus_native_ci.py", "--mode", "contract", "--output", out / "contract"],
        dict(os.environ, P2_NETWORK_DEVICE_SANDBOX="macos-seatbelt", PYTHONDONTWRITEBYTECODE="1"))
    run("native-validation", [python, "-B", root / "tools/validate_p9_native.py", "--output", out / "native",
                              "--linux-instance", linux_instance, "--linux-root", linux_root])
    native = validate_native_report(read_json(out / "native/result.json"), out / "native", root)
    contract = validate_contract(read_json(out / "contract/result.json"), out / "contract", root)
    verify_lock(root)
    report = {"schema": "songplus-native-p9-acceptance/v1", "status": "PASS_OFFLINE_NATIVE",
              "run_id": "p9-" + uuid.uuid4().hex, "started_at_utc": started, "completed_at_utc": now(),
              "lock_sha256": sha(root / LOCK), "parent_lock_sha256": sha(root / parent.LOCK),
              "maintenance_commit": commit, "protected_files_match_commit": True, "prerequisite": lock["prerequisite"],
              "commands": commands, "evidence": {name: {"path": name + "/result.json", "sha256": sha(out / name / "result.json")}
                                                  for name in ("native", "contract", "replay-1", "replay-2")},
              "counts": {"fresh_native": native, "fresh_source_contract": contract, "fresh_source_replays": 2,
                         "reused_p8_prerequisite": True, "fresh_arm_builds": 0, "fresh_vehicle_replay_segments": 0}, **SCOPE}
    write_json(out / "result.json", report)
    gate = release_check(out / "result.json", root)
    write_json(out / "release.json", gate)
    return report
  except Exception as error:
    write_json(out / "result.json", {"status": "FAIL", "error": str(error), "started_at_utc": started,
                                    "completed_at_utc": now(), "commands": commands, **SCOPE})
    raise
