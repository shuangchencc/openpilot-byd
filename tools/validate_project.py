#!/usr/bin/env python3
"""Run the prepared project's local verification. Never bootstrap or contact a device."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--sources-only", action="store_true",
                      help="Verify pinned sources and evidence only; do not claim build/test validation")
  args = parser.parse_args()
  output = ROOT / "results/project"
  output.mkdir(parents=True, exist_ok=True)
  env = dict(os.environ, UV_OFFLINE="1", PIP_NO_INDEX="1", GIT_TERMINAL_PROMPT="0",
             PYTHONDONTWRITEBYTECODE="1")
  checks = []

  def run(name: str, command: list[str], timeout: int = 1800) -> None:
    log_path = output / f"{name}.log"
    item = {"name": name, "command": command, "log": str(log_path.relative_to(ROOT)),
            "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
      with log_path.open("w") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
      item.update(exit_code=result.returncode, status="PASS" if result.returncode == 0 else "FAIL")
    except (OSError, subprocess.SubprocessError) as error:
      item.update(status="FAIL", error=str(error))
    checks.append(item)
    print(f"{item['status']} {name}: {item['log']}", flush=True)

  run("official_source_lock", [sys.executable, "tools/lock_upstream.py", "--verify"])
  run("hardware_evidence", [sys.executable, "hardware/verify_source_evidence.py"])
  run("research_index", [sys.executable, "research/verify.py"])
  if not args.sources_only and all(check["status"] == "PASS" for check in checks):
    lab_python = ROOT / "upstream-opendbc/.venv/bin/python"
    run("research_lab", ["/usr/bin/sandbox-exec", "-f", "tools/offline-network.sb",
                         str(lab_python), "tools/validate_reference.py", "--with-upstream"])
    if checks[-1]["status"] == "PASS":
      run("official_host", [sys.executable, "tools/validate_upstream.py"])

  expected_count = 3 if args.sources_only else 5
  passed = len(checks) == expected_count and all(check["status"] == "PASS" for check in checks)
  report = {
    "schema": "songplus-project-validation/v1",
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "scope": "sources_only" if args.sources_only else "prepared_local_host_build_and_tests",
    "status": "PASS" if passed else "FAIL",
    "checks": checks,
    "device_access_performed": False,
    "can_transmit": False,
    "firmware_flash": False,
    "os_update": False,
    "deployment_ready": False,
    "limitations": [
      "A source-only run does not perform any build or runtime validation.",
      "Selected official host checks and research-tool tests do not validate Song PLUS control.",
      "C3XL Linux/QCOM, camera, actual Panda boards, recovery and vehicle behavior remain unverified.",
      "Dependencies must be prepared separately; this command does not fetch or install them.",
    ],
  }
  (output / "validation-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
  print(f"{report['status']} ({report['scope']}): results/project/validation-result.json", flush=True)
  return 0 if passed else 1


if __name__ == "__main__":
  raise SystemExit(main())
