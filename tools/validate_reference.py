#!/usr/bin/env python3
"""Validate the historical research baseline, independently of live capture tools."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--with-upstream", action="store_true", help="Also run pinned upstream BYD/parser host tests")
  args = parser.parse_args()
  output_dir = ROOT / "results/reference"
  output_dir.mkdir(parents=True, exist_ok=True)
  os.environ.pop("PYTHONPATH", None)
  os.environ.update(UV_OFFLINE="1", PIP_NO_INDEX="1", GIT_TERMINAL_PROMPT="0",
                    UV_PYTHON_INSTALL_DIR=str(ROOT / ".bootstrap/python"))
  baseline = json.loads((ROOT / "baseline-lock.json").read_text())
  checks = []

  def run(name, arguments, *, tests=False, json_check=None, executable=None):
    command = [executable or sys.executable, *arguments]
    try:
      result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
      (output_dir / f"{name}.stdout.log").write_text(result.stdout)
      (output_dir / f"{name}.stderr.log").write_text(result.stderr)
      item = {"name": name, "command": command, "exit_code": result.returncode,
              "passed": result.returncode == 0,
              "stdout": f"results/reference/{name}.stdout.log", "stderr": f"results/reference/{name}.stderr.log"}
      if tests:
        match = re.search(r"Ran (\d+) tests? in", result.stderr)
        skipped = re.search(r"OK \(skipped=(\d+)\)", result.stderr)
        item["tests_run"] = int(match[1]) if match else 0
        item["skipped"] = int(skipped[1]) if skipped else 0
        item["passed"] &= item["tests_run"] > 0
      if json_check is not None:
        report = json.loads(result.stdout)
        item["passed"] &= bool(json_check(report))
        (output_dir / f"{name}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
      item = {"name": name, "command": command, "passed": False, "error": str(error)}
    checks.append(item)
    print(f"{'PASS' if item['passed'] else 'FAIL'} {name}", flush=True)

  lock_hash = hashlib.sha256((ROOT / "upstream-opendbc/uv.lock").read_bytes()).hexdigest()
  checks.append({"name": "dependency_lock", "passed": lock_hash == baseline["uv_lock_sha256"], "sha256": lock_hash})
  run("uv_version", ["-c", "import subprocess; v=subprocess.check_output(['.bootstrap/bin/uv', '--version'], text=True); print(v,end=''); assert v.split()[1]=='0.13.1'"])
  # Check the installed environment without downloading or changing packages.
  run("installed_dependency_lock", ["sync", "--project", "upstream-opendbc", "--frozen", "--extra", "testing",
                                    "--no-default-groups", "--check", "--offline", "--python", "3.12.7"],
      executable=str(ROOT / ".bootstrap/bin/uv"))
  run("pinned_reference", ["-c", "from decoding.decoder import load_reference; load_reference('official_atto3_reference')"])
  # Never continue with parser tests against an unpinned or modified dependency.
  if all(item["passed"] for item in checks):
    run("lab_tests", ["-m", "unittest", "discover", "-s", "tests", "-v"], tests=True)
    run("replay_tests", ["-m", "unittest", "discover", "-s", "replay/tests", "-v"], tests=True)
    run("protocol_evidence", ["spec/evidence_gate.py"], json_check=lambda r: r["actuation_allowed"] is False)
    run("hardware_evidence", ["hardware/verify_source_evidence.py"],
        json_check=lambda r: r["status"] == "PASS" and r["hardware_compatibility_confirmed"] is False)
    run("synthetic_summary", ["replay/canlog.py", "summarize", "--manifest",
                               "replay/fixtures/synthetic-can-data.manifest.json"],
        json_check=lambda r: r["validation"] == "passed")
    run("synthetic_decode", ["-m", "decoding.decoder", "decoding/fixtures/synthetic-measurements.manifest.json",
                              "--profile", "official_atto3_reference", "--bus", "0"],
        json_check=lambda r: r["data_kind"] == "synthetic" and r["actuation_allowed"] is False
          and r["counts"] == {"decoded_candidate": 2, "rejected_by_reference_parser": 1, "length_mismatch": 1})
    if args.with_upstream:
      run("upstream_tests", ["-m", "unittest", "opendbc.safety.tests.test_byd",
                             "opendbc.can.tests.test_packer_parser", "opendbc.can.tests.test_dbc_parser"], tests=True)

  report = {
    "schema": "historical-reference-validation/v1", "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "python": sys.version, "python_executable": sys.executable,
    "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
    "checks": checks,
    "tests_run_this_invocation": sum(item.get("tests_run", 0) for item in checks),
    "tests_skipped_this_invocation": sum(item.get("skipped", 0) for item in checks),
    "upstream_tests_requested_this_invocation": args.with_upstream,
    "initial_upstream_baseline": baseline["initial_upstream_test_result"],
    "actuation_allowed": False, "vehicle_compatibility": "unverified",
    "hardware_compatibility": "unverified",
    "meaning": "Host-side tests and evidence consistency only. Synthetic inputs do not confirm vehicle protocols.",
  }
  (output_dir / "validation-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
  print(f"{report['status']}: {report['tests_run_this_invocation']} tests, "
        f"{report['tests_skipped_this_invocation']} skipped; results/reference/validation-result.json")
  return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
  raise SystemExit(main())
