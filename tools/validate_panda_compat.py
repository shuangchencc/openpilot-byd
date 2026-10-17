#!/usr/bin/env python3
"""Reapply pinned P0/P1 patches and run offline host checks, never device code.

No fetching, dependency installation, MCU builds or hardware entry points.
Requires Python >=3.11, local Git sources and a C++17 compiler with ASan/UBSan.
"""
import ast
import argparse
from datetime import datetime, timezone
import hashlib
import fnmatch
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PATCH_ROOT = ROOT / "patches/openpilot/p0p1"
PREFIX = "openpilot/selfdrive/pandad/compatibility/"
RUN_OUTPUT = None


def require(condition, message):
  if not condition:
    raise ValueError(message)


def digest(data):
  return hashlib.sha256(data).hexdigest()


def run_capture(command, cwd=ROOT, timeout=60, env=None):
  return subprocess.run(command, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=timeout)


def main():
  global RUN_OUTPUT
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--python", type=Path, default=ROOT / "upstream-openpilot/.venv/bin/python",
                      help="Already-installed locked interpreter; never installed automatically")
  args = parser.parse_args()
  output_parent = ROOT / "results/panda-compat-p0p1"
  output_parent.mkdir(parents=True, exist_ok=True)
  output = Path(tempfile.mkdtemp(prefix="run-", dir=output_parent))
  RUN_OUTPUT = output
  (output / "result.json").write_text(json.dumps({"status": "RUNNING"}) + "\n")
  (output_parent / "latest.json").write_text(json.dumps({"status": "RUNNING", "result": str(output / "result.json")}) + "\n")
  manifest = json.loads((PATCH_ROOT / "manifest.json").read_text())
  coverage_bytes = (PATCH_ROOT / "acceptance-map.json").read_bytes()
  require(digest(coverage_bytes) == manifest["acceptance_map_sha256"], "Acceptance mapping hash drift")
  coverage = json.loads(coverage_bytes)
  design_cases = json.loads((ROOT / "hardware/design/dos-usb-dual-panda/offline-acceptance.json").read_text())["cases"]
  expected_ids = {c["id"] for c in design_cases if c["stage"] in ("P0", "P1")}
  require(set(manifest["acceptance_ids"]) == expected_ids == {c["id"] for c in coverage["cases"]}, "P0/P1 coverage differs from frozen design")
  lock = json.loads((ROOT / "upstream-lock.json").read_text())
  locked = {r["path"]: r["commit"] for r in lock["repos"]}
  python = str(args.python.absolute())
  require(Path(python).exists(), "Locked Python missing; explicitly prepare it or pass --python")
  python_version = run_capture([python, "--version"]).stdout.decode().strip()
  require(python_version == "Python " + lock["python_dependencies"]["python_version"], "Test Python differs from upstream lock")
  for path, commit in manifest["baselines"].items():
    require(locked[path] == commit, f"Lock mismatch: {path}")
    observed = run_capture(["git", "rev-parse", "HEAD"], ROOT / path).stdout.decode().strip()
    require(observed == commit, f"Checkout drift: {path}")
  for path, expected in manifest["baseline_inputs"].items():
    require(digest((ROOT / path).read_bytes()) == expected, f"Baseline input drift: {path}")
  for patch in manifest["series"]:
    require(Path(patch["file"]).name == patch["file"], "Invalid patch filename")
    require(digest((PATCH_ROOT / patch["file"]).read_bytes()) == patch["sha256"], "Patch hash drift")
  require(all(p.startswith(PREFIX) and ".." not in Path(p).parts for p in manifest["result_files"]), "Patch scope escape")

  source = output / "source"
  source.mkdir()
  run_capture(["git", "-c", "init.defaultBranch=offline-validation", "init", "--quiet"], source)
  baseline = manifest["baselines"]["upstream-openpilot"]
  archive = run_capture(["git", "archive", baseline, "openpilot/selfdrive/pandad"], ROOT / "upstream-openpilot").stdout
  with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    for member in tar.getmembers():
      target = (source / member.name).resolve()
      require(target.is_relative_to(source), "Archive path escape")
      if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
      else:
        require(member.isfile(), "Unexpected archive link")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(tar.extractfile(member).read())
  for patch in manifest["series"]:
    path = str(PATCH_ROOT / patch["file"])
    stats = run_capture(["git", "apply", "--numstat", path], source).stdout.decode().splitlines()
    for stat in stats:
      _, _, changed_path = stat.split("\t")
      require(changed_path in manifest["result_files"], "Patch changes a path outside its declared scope")
    run_capture(["git", "apply", "--check", path], source)
    run_capture(["git", "apply", path], source)
  package = source / PREFIX
  require(not package.is_symlink() and all(not p.is_symlink() for p in package.rglob("*")), "Symlinks are forbidden in the patch package")
  files = {str(p.relative_to(source)): digest(p.read_bytes()) for p in package.rglob("*") if p.is_file()}
  require(files == manifest["result_files"], "Applied source tree differs from manifest")

  # Production Python modules are deliberately standalone stdlib logic. Tests
  # may execute the host probes, but production modules cannot import hardware.
  allowed_imports = {"__future__", "collections", "dataclasses", "enum", "json", "math", "re", "struct", "typing", "types", "compatibility"}
  for path in package.glob("*.py"):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
      if isinstance(node, ast.Import):
        names = [n.name.split(".")[0] for n in node.names]
      elif isinstance(node, ast.ImportFrom):
        names = [] if node.level else [(node.module or "").split(".")[0]]
      else:
        continue
      require(set(names) <= allowed_imports, f"Unexpected production import in {path.name}: {names}")

  compiler = shutil.which(os.environ.get("CXX", "clang++"))
  require(compiler is not None, "Install a local clang++/C++17 compiler explicitly first")
  sandbox = []
  if platform.system() == "Darwin":
    require(Path("/usr/bin/sandbox-exec").exists(), "macOS sandbox-exec missing")
    sandbox = ["/usr/bin/sandbox-exec", "-f", str(ROOT / "tools/panda_compat_offline.sb")]
  env = dict(os.environ, PYTHONPATH=str(package.parent), PYTHONDONTWRITEBYTECODE="1",
             PYTHONNOUSERSITE="1", PYTHONOPTIMIZE="0",
             ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1",
             UV_OFFLINE="1", PIP_NO_INDEX="1")
  commands = []

  def checked(name, command, capture_json=False):
    full = sandbox + list(map(str, command))
    try:
      result = run_capture(full, source, env=env)
    except subprocess.CalledProcessError as exc:
      (output / f"{name}.stdout").write_bytes(exc.stdout)
      (output / f"{name}.stderr").write_bytes(exc.stderr)
      raise
    (output / f"{name}.stdout").write_bytes(result.stdout)
    (output / f"{name}.stderr").write_bytes(result.stderr)
    commands.append({"name": name, "command": full, "exit_code": result.returncode})
    return json.loads(result.stdout) if capture_json else result.stdout.decode()

  flags = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-g", "-fno-omit-frame-pointer",
           "-fsanitize=address,undefined"]
  native = output / "test_can_codec"
  checked("compile-can-sanitized", [compiler, *flags, "-I", package,
          package / "can_codec.cc", package / "tests/test_can_codec.cc", "-o", native])
  native_result = checked("native-can-tests", [native])
  probe = output / "health_layout_probe"
  checked("compile-health-probe-sanitized", [compiler, *flags,
          "-I", ROOT / "upstream-openpilot/panda/board", "-I", ROOT / "upstream-openpilot/opendbc_repo",
          package / "tests/health_layout_probe.cc", "-o", probe])
  health_abi = checked("health-layout", [probe], capture_json=True)
  fixture = output / "health-layout.json"
  fixture.write_text(json.dumps(health_abi, indent=2) + "\n")
  env["TEST_HEALTH_LAYOUT_JSON"] = str(fixture)
  python_report = output / "python-tests.json"
  checked("python-tests", [python, "-S", package / "tests/run_python_tests.py", python_report])
  unit_result = json.loads(python_report.read_text())
  oracle = checked("can-independent-oracle", [python, "-S", package / "tests/check_can_oracle.py", native,
                    "--health-fixture", fixture], capture_json=True)
  native_summary = json.loads(native_result.splitlines()[-1])
  native_groups = {line.removeprefix("PASS ") for line in native_result.splitlines() if line.startswith("PASS ")}
  require(native_summary["failed"] == 0 and native_summary["passed"] == len(native_groups), "Native result mismatch")
  require(unit_result["tests_run"] == unit_result["passed"] and unit_result["tests_run"] > 0
          and not any(unit_result[k] for k in ("failures", "errors", "skipped")), "Python result mismatch")
  acceptance_results = []
  for case in coverage["cases"]:
    matched = set()
    for selector in case["required_python_selectors"]:
      matches = {test for test in unit_result["passed_ids"] if fnmatch.fnmatchcase(test, selector)}
      require(matches, f"Required test selector has no passing evidence: {selector}")
      matched.update(matches)
    require(set(case["required_native_groups"]) <= native_groups, f"Native evidence gap: {case['id']}")
    if case["requires_independent_oracle"]:
      require(oracle["status"] == "PASS" and oracle["baseline_c_layout_vector_checked"] and oracle["assertions_enabled"], "Independent oracle evidence gap")
    acceptance_results.append({"id": case["id"], "stage": case["stage"], "status": "PASS_OFFLINE_HOST_ONLY",
                               "python_evidence": sorted(matched), "native_evidence": case["required_native_groups"],
                               "independent_oracle": case["requires_independent_oracle"]})
  report = {
    "schema": "songplus-panda-p0p1-validation/v1", "status": "PASS_OFFLINE_HOST_ONLY",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(), "baselines": manifest["baselines"],
    "patch_commits": [p["commit"] for p in manifest["series"]],
    "manifest_sha256": digest((PATCH_ROOT / "manifest.json").read_bytes()),
    "validator_sha256": digest(Path(__file__).read_bytes()),
    "sandbox_sha256": digest((ROOT / "tools/panda_compat_offline.sb").read_bytes()),
    "platform": platform.platform(), "python": python_version, "compiler": compiler,
    "compiler_version": run_capture([compiler, "--version"]).stdout.decode(),
    "macos_network_and_device_node_sandbox": bool(sandbox), "sanitizers": ["address", "undefined"],
    "python_tests": unit_result, "native_can_tests": native_result, "native_summary": native_summary, "independent_oracle": oracle,
    "health_host_abi": health_abi, "commands": commands,
    "acceptance_results": acceptance_results,
    "source_tree_sha256": digest(json.dumps(files, sort_keys=True).encode()),
    "runtime_integrated": False, "real_transport": False, "device_access": False,
    "MCU_build": False, "ARM_ABI_validated": False, "deployment_ready": False,
    "remaining_stages": ["P2", "P3", "P4", "P5", "P6", "P7"],
  }
  (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
  (output_parent / "latest.json").write_text(json.dumps({"status": report["status"], "result": str(output / "result.json")}, indent=2) + "\n")
  print(json.dumps({"status": report["status"], "python_tests": {k: unit_result[k] for k in ["tests_run", "passed", "failures", "errors", "skipped"]},
                    "native_can_tests": native_result.strip(), "independent_oracle": oracle,
                    "result": str(output / "result.json"), "device_access": False}, indent=2))


if __name__ == "__main__":
  try:
    main()
  except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
    if RUN_OUTPUT is not None:
      (RUN_OUTPUT / "result.json").write_text(json.dumps({"status": "FAIL", "error": str(error), "device_access": False}, indent=2) + "\n")
      (RUN_OUTPUT.parent / "latest.json").write_text(json.dumps({"status": "FAIL", "result": str(RUN_OUTPUT / "result.json")}, indent=2) + "\n")
    print(f"FAIL P0/P1 offline validation: {error}", file=sys.stderr)
    sys.exit(1)
