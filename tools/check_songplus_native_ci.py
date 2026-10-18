#!/usr/bin/env python3
"""Shared offline CI for additive native discovery and USB/SPI providers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import check_panda_ci as parent

ROOT = parent.ROOT
WORKFLOW = ".github/workflows/songplus-native-offline.yml"


class Runner(parent.Runner):
  def run(self, name, argv, expected=0):
    result = super().run(name, argv, expected)
    for stream in ("stdout", "stderr"):
      self.commands[-1][stream + "_sha256"] = hashlib.sha256((self.output / (name + "." + stream + ".log")).read_bytes()).hexdigest()
    return result


def workflow_text():
  # Reuse the reviewed isolation/runner boundary, with this stage's own entry.
  text = parent.workflow_text().replace("tools/check_panda_ci.py", "tools/check_songplus_native_ci.py")
  text = text.replace("name: Panda compatibility offline maintenance", "name: Song PLUS native providers")
  text = text.replace("--output results/panda-ci-hosted", "--output results/songplus-native-ci-hosted")
  text = text.replace('results/panda-ci-$CI_RUN_ID', 'results/songplus-native-ci-$CI_RUN_ID')
  text = text.replace("timeout-minutes: 60", "timeout-minutes: 120")
  # The same concurrency group prevents two full jobs using one prepared VM.
  return text


def check_workflow(root):
  parent.require((root / WORKFLOW).read_text() == workflow_text(), "Integration workflow drift")


def contract_names():
  return ["native-metadata", "help-root", "help-verify-lock", "help-replay", "help-validate", "help-release-check", "native-tests"]


def contract_commands(run, python):
  cli = [python, "-B", "tools/songplus_native.py"]
  run("native-metadata", cli + ["verify-lock", "--metadata-only"])
  for command in (None, "verify-lock", "replay", "validate", "release-check"):
    run("help-" + (command or "root"), cli + ([command] if command else []) + ["--help"])
  run("native-tests", [python, "-B", "-m", "unittest", "discover", "-s", "native_maintenance", "-p", "test_*.py", "-v"])


def full_commands(run, python, output, linux_instance, linux_root):
  parent.require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", linux_instance) is not None, "Invalid local VM")
  parent.require(linux_root.startswith("/home/") and ".." not in Path(linux_root).parts
                 and "\n" not in linux_root and "\0" not in linux_root, "Invalid prepared Linux root")
  cli = [python, "-B", "tools/songplus_native.py"]
  run("verify-lock", cli + ["verify-lock"])
  run("validate", cli + ["validate", "--output", str(output / "validation"),
                         "--linux-instance", linux_instance, "--linux-root", linux_root])
  gate = json.loads(run("release-check", cli + ["release-check", "--report", str(output / "validation/result.json")], expected=3))
  parent.require(isinstance(gate, dict) and gate.get("offline_native_complete") is True
                 and gate.get("p8_prerequisite_verified") is True
                 and gate.get("deployment_ready") is False and gate.get("p8_arm_and_vehicle_newly_executed") is False
                 and gate.get("status") == "BLOCKED_FOR_DEPLOYMENT", "Invalid integration release conclusion")
  parent.require(isinstance(gate.get("deployment_blockers"), list) and bool(gate["deployment_blockers"])
                 and all(isinstance(b, (str, dict)) and bool(b) for b in gate["deployment_blockers"]), "Missing deployment blockers")
  return gate


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--mode", choices=("contract", "full"), default="contract")
  parser.add_argument("--output", type=Path)
  parser.add_argument("--linux-instance")
  parser.add_argument("--linux-root")
  parser.add_argument("--expected-commit")
  parser.add_argument("--print-workflow", action="store_true")
  args = parser.parse_args()
  if args.print_workflow:
    print(workflow_text(), end="")
    return 0
  parent.require(args.output is not None, "Output must be explicit")
  output = args.output.absolute()
  parent.require(output.is_relative_to(ROOT / "results") and output.resolve() == output
                 and not output.is_symlink(), "Output must be a new unaliased results directory")
  output.mkdir(parents=True, exist_ok=False)
  runner = Runner(ROOT, output)
  report = {"schema": "songplus-native-ci/v1", "mode": args.mode, "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "commands": runner.commands, "status": "RUNNING", "device_access": False, "deployment_ready": False,
            "remote_ci_executed": False}
  try:
    check_workflow(ROOT)
    if args.expected_commit is not None:
      parent.check_prepared_commit(runner.run, args.expected_commit)
    if args.mode == "contract":
      parent.require(args.linux_instance is None and args.linux_root is None, "Contract must not select a VM")
      marker = os.environ.get("P2_NETWORK_DEVICE_SANDBOX")
      parent.require(marker in ("macos-seatbelt", "linux-user-net-mount"), "Contract requires network/device isolation")
      report["sandbox"] = marker
      contract_commands(runner.run, sys.executable)
    else:
      parent.require(args.linux_instance is not None and args.linux_root is not None, "Full mode requires prepared VM parameters")
      report["release"] = full_commands(runner.run, sys.executable, output, args.linux_instance, args.linux_root)
    report.update(status="PASS", passed_commands=len(runner.commands), failed=0, skipped=0)
    if args.mode == "contract":
      from native_maintenance.evidence import validate_contract
      validate_contract(report, output)
  except (ValueError, KeyError, TypeError, OSError, parent.subprocess.SubprocessError) as error:
    report.update(status="FAIL", error=str(error), failed=1)
  finally:
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
  print(json.dumps(report, indent=2))
  return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
  sys.exit(main())
