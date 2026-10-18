#!/usr/bin/env python3
"""Shared, stdlib-only P7 CI contract and preprovisioned offline runner.

This never fetches, installs, dispatches GitHub Actions, uploads, or operates a
device. The caller supplies the OS sandbox for the contract job. Full validation
uses the existing orchestrator's isolated macOS/Linux workers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/panda-compat-offline.yml"
CHECKOUT_SHA = "d23441a48e516b6c34aea4fa41551a30e30af803"


def workflow_text() -> str:
    """The intentionally small, exact CI surface; review changes with its tests."""
    return f'''# Generated contract: python3 tools/check_panda_ci.py --print-workflow
name: Panda compatibility offline maintenance

on:
  pull_request:
  push:
    branches: [songplus-offline-research]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  source-contract:
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@{CHECKOUT_SHA} # Existing project pin, v6
        with:
          submodules: false
          lfs: false
          persist-credentials: false
      - name: Offline metadata, release-gate and reproducibility contract
        run: >-
          sudo -n -- unshare --user --map-root-user --net --mount --fork env P2_IN_NAMESPACE=1
          sh tools/panda_transport_linux_sandbox.sh
          python3 -B tools/check_panda_ci.py --mode contract
          --output results/panda-ci-hosted

  prepared-offline:
    if: >-
      github.event_name == 'workflow_dispatch' &&
      github.ref == 'refs/heads/songplus-offline-research' &&
      github.repository == vars.PANDA_MAINTENANCE_REPOSITORY
    needs: source-contract
    environment: panda-offline-maintainers
    runs-on: [self-hosted, macOS, ARM64, songplus-panda-offline]
    timeout-minutes: 60
    concurrency:
      group: panda-prepared-offline
      cancel-in-progress: false
    env:
      PANDA_WORKSPACE: ${{{{ vars.PANDA_WORKSPACE }}}}
      PANDA_LINUX_INSTANCE: ${{{{ vars.PANDA_LINUX_INSTANCE }}}}
      PANDA_LINUX_ROOT: ${{{{ vars.PANDA_LINUX_ROOT }}}}
      EXPECTED_COMMIT: ${{{{ github.sha }}}}
      CI_RUN_ID: ${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}
    defaults:
      run:
        working-directory: ${{{{ vars.PANDA_WORKSPACE }}}}
    steps:
      - name: Verify prepared checkout and run complete offline acceptance
        run: >-
          upstream-openpilot/.venv/bin/python -B tools/check_panda_ci.py
          --mode full --expected-commit "$EXPECTED_COMMIT"
          --linux-instance "$PANDA_LINUX_INSTANCE"
          --linux-root "$PANDA_LINUX_ROOT"
          --output "results/panda-ci-$CI_RUN_ID"
'''


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def check_workflow(root: Path) -> None:
    require((root / WORKFLOW).read_text() == workflow_text(),
            "CI workflow differs from the reviewed, pinned offline contract")


def offline_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.update(UV_OFFLINE="1", PIP_NO_INDEX="1", PYTHONDONTWRITEBYTECODE="1",
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    return env


class Runner:
    def __init__(self, root: Path, output: Path):
        self.root, self.output = root, output
        self.commands: list[dict] = []

    def run(self, name: str, argv: list[str], expected: int = 0) -> str:
        result = subprocess.run(argv, cwd=self.root, env=offline_environment(),
                                capture_output=True, text=True, timeout=3000, check=False)
        (self.output / (name + ".stdout.log")).write_text(result.stdout)
        (self.output / (name + ".stderr.log")).write_text(result.stderr)
        self.commands.append({"name": name, "argv": argv,
                              "returncode": result.returncode,
                              "expected_returncode": expected})
        require(result.returncode == expected,
                f"{name}: exit {result.returncode}, expected {expected}; see {self.output}")
        if name.endswith("-tests"):
            count = re.search(r"Ran ([0-9]+) tests? in ", result.stdout + result.stderr)
            require(count is not None and int(count.group(1)) > 0,
                    name + ": missing or empty unittest result")
            require("skipped=" not in result.stdout + result.stderr,
                    name + ": skipped acceptance tests are not permitted")
            self.commands[-1]["tests_passed"] = int(count.group(1))
        return result.stdout


def check_prepared_commit(run, expected_commit: str) -> None:
    require(re.fullmatch(r"[0-9a-f]{40}", expected_commit) is not None,
            "Expected commit must be a complete lowercase Git SHA")
    observed = run("prepared-head", ["git", "rev-parse", "HEAD"]).strip()
    require(observed == expected_commit, "Prepared checkout does not match the dispatched commit")
    dirty = run("prepared-status", ["git", "status", "--porcelain=v1", "--untracked-files=no"])
    require(not dirty.strip(), "Prepared checkout has tracked modifications")


def contract_commands(run, python: str) -> None:
    cli = [python, "-B", "tools/songplus_compat.py"]
    run("metadata-lock", cli + ["verify-lock", "--metadata-only"])
    for command in (None, "verify-lock", "replay", "validate", "release-check", "inspect-runtime"):
        name = command or "root"
        run("help-" + name, cli + ([command] if command else []) + ["--help"])
    run("maintenance-tests", [python, "-B", "-m", "unittest", "discover",
                              "-s", "maintenance", "-p", "test_*.py", "-v"])
    run("ci-tests", [python, "-B", "-m", "unittest", "discover",
                     "-s", "tests", "-p", "test_panda_ci.py", "-v"])


def full_commands(run, python: str, output: Path, linux_instance: str,
                  linux_root: str) -> dict:
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", linux_instance) is not None,
            "An explicit prepared local Lima instance is required")
    require(linux_root.startswith("/") and "\n" not in linux_root and "\0" not in linux_root,
            "An absolute prepared Linux dependency root is required")
    cli = [python, "-B", "tools/songplus_compat.py"]
    run("verify-lock", cli + ["verify-lock"])
    run("replay", cli + ["replay", "--output", str(output / "replay")])
    run("validate", cli + ["validate", "--linux-instance", linux_instance,
                           "--linux-root", linux_root,
                           "--output", str(output / "validation")])
    text = run("release-check", cli + ["release-check", "--report",
                str(output / "validation/result.json")], expected=3)
    gate = json.loads(text)
    require(isinstance(gate, dict), "Release gate must produce a JSON object")
    require(gate.get("offline_compatibility_complete") is True,
            "Release gate did not confirm complete offline evidence")
    require(gate.get("deployment_ready") is False,
            "Offline CI must never authorize deployment")
    blockers = gate.get("deployment_blockers")
    require(isinstance(blockers, list) and len(blockers) > 0 and
            all(isinstance(x, (str, dict)) and bool(x) for x in blockers),
            "Release gate must retain explicit deployment blockers")
    require(gate.get("status") != "INVALID_EVIDENCE", "Release evidence is invalid")
    return gate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("contract", "full"), default="contract")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--linux-instance")
    parser.add_argument("--linux-root")
    parser.add_argument("--expected-commit", help="Required by the protected hosted dispatch; optional locally")
    parser.add_argument("--print-workflow", action="store_true")
    args = parser.parse_args(argv)
    if args.print_workflow:
        print(workflow_text(), end="")
        return 0
    parser.error("--output must name a new results directory") if args.output is None else None
    output = args.output.absolute()
    # Never replace prior logs or allow this entry to write into source trees.
    require(output.is_relative_to((ROOT / "results").resolve()), "Output must be below repository results/")
    require(not output.is_symlink() and output.resolve() == output, "Output path must not contain symlinks")
    output.mkdir(parents=True, exist_ok=False)
    runner = Runner(ROOT, output)
    report = {"schema": "songplus-panda-ci/v1", "mode": args.mode,
              "execution": "local shared CI entry (not proof of a GitHub Actions run)",
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "RUNNING", "deployment_ready": False,
              "commands": runner.commands}
    try:
        check_workflow(ROOT)
        if args.expected_commit is not None:
            check_prepared_commit(runner.run, args.expected_commit)
        if args.mode == "contract":
            require(args.linux_instance is None and args.linux_root is None,
                    "Contract mode must not select a VM")
            marker = os.environ.get("P2_NETWORK_DEVICE_SANDBOX")
            require(marker in ("macos-seatbelt", "linux-user-net-mount"),
                    "Contract mode requires the caller's no-network/no-device sandbox")
            report["sandbox"] = marker
            contract_commands(runner.run, sys.executable)
        else:
            require(args.linux_instance is not None and args.linux_root is not None,
                    "Full mode requires explicit prepared Linux instance and root")
            report["release_gate"] = full_commands(runner.run, sys.executable, output,
                                                    args.linux_instance, args.linux_root)
        report["status"] = "PASS"
        report["failed"] = report["skipped"] = 0
        report["passed_commands"] = len(runner.commands)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        report["status"] = "FAIL"
        report["error"] = str(exc)
        report["failed"] = 1
    finally:
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}))
        raise SystemExit(2)
