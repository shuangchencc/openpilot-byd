#!/usr/bin/env python3
"""Replay P0/P1/P2/P3 patches and validate session/queue/routing behavior offline.

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
PATCH_ROOT = ROOT / "patches/openpilot/p3"
HOST_DEPENDENCIES = ROOT / "patches/openpilot/p2/host-dependencies.json"
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
  parser.add_argument("--linux-instance", required=True, help="Already-running local Lima build VM; never C3XL")
  parser.add_argument("--linux-root", default="/home/shuang.guest/songplus-p2-build", help="Prepared task directory containing cached deps")
  args = parser.parse_args()
  require(platform.system() == "Darwin", "Use the macOS orchestrator with an explicitly prepared local Linux VM")
  output_parent = ROOT / "results/panda-compat-p3"
  output_parent.mkdir(parents=True, exist_ok=True)
  output = Path(tempfile.mkdtemp(prefix="run-", dir=output_parent))
  RUN_OUTPUT = output
  (output / "result.json").write_text(json.dumps({"status": "RUNNING"}) + "\n")
  (output_parent / "latest.json").write_text(json.dumps({"status": "RUNNING", "result": str(output / "result.json")}) + "\n")
  manifest = json.loads((PATCH_ROOT / "manifest.json").read_text())
  prior_manifest = json.loads((ROOT / "patches/openpilot/p0p1/manifest.json").read_text())
  p2_manifest_bytes = (ROOT / "patches/openpilot/p2/manifest.json").read_bytes()
  p2_manifest = json.loads(p2_manifest_bytes)
  require(manifest["baselines"] == p2_manifest["baselines"], "P3 baseline differs from P2")
  for section in ["baseline_inputs", "preserved_runtime_files"]:
    require(all(manifest[section].get(path) == expected for path, expected in p2_manifest[section].items()), "P2 inherited protection drift: " + section)
  require(len(manifest["series"]) > 3 and all(p["stage"] == "P3" for p in manifest["series"][3:]), "Invalid P3 patch suffix")
  require(digest(p2_manifest_bytes) == manifest["depends_on_manifest_sha256"], "P2 dependency manifest drift")
  require(digest((ROOT / "patches/openpilot/p0p1/manifest.json").read_bytes()) == p2_manifest["depends_on_manifest_sha256"], "P0/P1 dependency manifest drift")
  require(manifest["series"][:3] == [{**p, "file": os.path.relpath((ROOT / "patches/openpilot/p2" / p["file"]).resolve(), PATCH_ROOT)} for p in p2_manifest["series"]], "P2 patch dependency changed")
  require(p2_manifest["series"][:2] == [{**p, "file": "../p0p1/" + p["file"]} for p in prior_manifest["series"]], "P0/P1 patch dependency changed")
  require(digest(HOST_DEPENDENCIES.read_bytes()) == manifest["host_dependencies_sha256"] == p2_manifest["host_dependencies_sha256"], "Host dependency lock drift")
  coverage_bytes = (ROOT / "patches/openpilot/p0p1/acceptance-map.json").read_bytes()
  require(digest(coverage_bytes) == prior_manifest["acceptance_map_sha256"], "Acceptance mapping hash drift")
  coverage = json.loads(coverage_bytes)
  design_cases = json.loads((ROOT / "hardware/design/dos-usb-dual-panda/offline-acceptance.json").read_text())["cases"]
  expected_ids = {c["id"] for c in design_cases if c["stage"] in ("P0", "P1")}
  require(set(prior_manifest["acceptance_ids"]) == expected_ids == {c["id"] for c in coverage["cases"]}, "P0/P1 coverage differs from frozen design")
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
    require(path.startswith("upstream-openpilot/") and ".." not in Path(path).parts, "Baseline input path escape")
    require(digest((ROOT / path).read_bytes()) == expected, f"Baseline input drift: {path}")
  for patch in manifest["series"]:
    require((PATCH_ROOT / patch["file"]).resolve().is_relative_to(ROOT / "patches/openpilot"), "Invalid patch filename")
    patch_bytes = (PATCH_ROOT / patch["file"]).read_bytes()
    require(digest(patch_bytes) == patch["sha256"], "Patch hash drift")
    require(patch_bytes.splitlines()[0].split()[1].decode() == patch["commit"], "Patch commit metadata mismatch")
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
  for relative, expected in manifest["baseline_inputs"].items():
    target = source / relative.removeprefix("upstream-openpilot/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((ROOT / relative).read_bytes())
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
      result = run_capture(full, source, env=env, timeout=180)
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
  # Compile the actual native adapters, but never initialize/open them. The
  # Linux worker runs in private user/network/mount namespaces with empty /dev.
  native_host = output / "native-macos"
  host_deps = json.loads(HOST_DEPENDENCIES.read_text())
  for dep in host_deps["macos"]["files"]:
    require(digest(Path(dep["path"]).read_bytes()) == dep["sha256"], "macOS dependency drift")
  env["P2_NETWORK_DEVICE_SANDBOX"] = "macos-seatbelt"
  checked("p3-macos", [python, "-S", ROOT / "tools/run_panda_sessions_host.py",
      "--source", source, "--output", native_host, "--compiler", compiler,
      "--usb-include", host_deps["macos"]["usb_include"], "--usb-library", host_deps["macos"]["usb_library"]])
  mac_report = json.loads((native_host / "result.json").read_text())
  linux_dir = args.linux_root.rstrip("/") + "/" + output.name
  require(args.linux_instance and all(c.isalnum() or c in "_-" for c in args.linux_instance), "Invalid local VM name")
  require(all(c.isalnum() or c in "/_.-" for c in linux_dir) and ".." not in Path(linux_dir).parts and linux_dir.startswith("/"), "Invalid local VM task path")
  bundle = output / "linux-input.tar"
  with tarfile.open(bundle, "w") as tar:
    for path in source.rglob("*"):
      if path.is_file() and ".git" not in path.relative_to(source).parts:
        tar.add(path, arcname="source/" + str(path.relative_to(source)), recursive=False)
    for name in ["run_panda_sessions_host.py", "panda_transport_linux_sandbox.sh"]:
      tar.add(ROOT / "tools" / name, arcname=name)
    tar.add(HOST_DEPENDENCIES, arcname="host-dependencies.json")
  def local_vm(name, argv):
    try:
      result = run_capture(argv, timeout=360)
    except subprocess.CalledProcessError as exc:
      (output / (name + ".stdout")).write_bytes(exc.stdout)
      (output / (name + ".stderr")).write_bytes(exc.stderr)
      raise
    (output / (name + ".stdout")).write_bytes(result.stdout)
    (output / (name + ".stderr")).write_bytes(result.stderr)
    commands.append({"name": name, "command": list(map(str, argv)), "exit_code": result.returncode})
    return result
  local_vm("linux-prepare-directory", ["limactl", "shell", args.linux_instance, "--", "mkdir", "-p", linux_dir])
  local_vm("linux-copy-input", ["limactl", "copy", str(bundle), args.linux_instance + ":" + linux_dir + "/input.tar"])
  remote_hash = local_vm("linux-input-hash", ["limactl", "shell", args.linux_instance, "--", "sha256sum", linux_dir + "/input.tar"]).stdout.decode().split()[0]
  require(remote_hash == digest(bundle.read_bytes()), "Linux transfer hash mismatch")
  local_vm("linux-extract", ["limactl", "shell", args.linux_instance, "--", "tar", "-xf", linux_dir + "/input.tar", "-C", linux_dir])
  dependency_prefix = args.linux_root.rstrip("/") + "/deps/prefix"
  for dep in host_deps["linux"]["files"]:
    actual = local_vm("linux-dep-" + Path(dep["path"]).name, ["limactl", "shell", args.linux_instance, "--", "sha256sum", dependency_prefix + "/" + dep["path"]]).stdout.decode().split()[0]
    require(actual == dep["sha256"], "Linux dependency drift")
  local_vm("p3-linux", ["limactl", "shell", args.linux_instance, "--", "sh", linux_dir + "/panda_transport_linux_sandbox.sh",
      "python3", linux_dir + "/run_panda_sessions_host.py", "--source", linux_dir + "/source",
      "--output", linux_dir + "/output", "--compiler", "g++", "--usb-include", dependency_prefix + "/usr/include/libusb-1.0",
      "--usb-library", dependency_prefix + "/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0.3.0"])
  local_vm("linux-copy-results", ["limactl", "copy", "-r", args.linux_instance + ":" + linux_dir + "/output", str(output / "native-linux")])
  linux_report = json.loads((output / "native-linux/result.json").read_text())
  require(mac_report["status"] == linux_report["status"] == "PASS_OFFLINE_HOST_ONLY", "Native suite failed")
  session_oracles = {}
  for label, native_report, native_path in [("macos", mac_report, native_host), ("linux", linux_report, output / "native-linux")]:
    oracle_report = checked("session-independent-oracle-" + label,
      [python, "-S", package / "tests/check_session_oracle.py", native_path / "session-oracle.json"], capture_json=True)
    require(oracle_report["status"] == "PASS" and oracle_report["assertions_enabled"] and oracle_report["fixture_count"] == 8 and oracle_report["route_checks"] == 48, "Session oracle failed")
    session_oracles[label] = oracle_report
  for stage, map_root, stage_manifest in [("P2", ROOT / "patches/openpilot/p2", p2_manifest), ("P3", PATCH_ROOT, manifest)]:
    map_bytes = (map_root / "acceptance-map.json").read_bytes()
    require(digest(map_bytes) == stage_manifest["acceptance_map_sha256"], stage + " mapping drift")
    mapping = json.loads(map_bytes)
    require(set(stage_manifest["acceptance_ids"]) == {c["id"] for c in mapping["cases"]} == {c["id"] for c in design_cases if c["stage"] == stage}, stage + " coverage drift")
    for case in mapping["cases"]:
      for platform_report in [mac_report, linux_report]:
        for suite, groups in case["required_groups"].items():
          require(set(groups) <= set(platform_report["suites"][suite]["groups"]), "Missing evidence for " + case["id"])
      acceptance_results.append({"id": case["id"], "stage": stage, "status": "PASS_OFFLINE_DECLARED_SCOPE",
                                 "evidence": case["required_groups"], "scope": case["scope"]})
  for relative, expected in manifest["preserved_runtime_files"].items():
    require(digest((source / relative).read_bytes()) == expected, "Original runtime SPI path changed")
  report = {
    "schema": "songplus-panda-p3-validation/v1", "status": "PASS_OFFLINE_HOST_ONLY",
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
    "runtime_integrated": False, "real_transport_executed": False, "device_access": False,
    "native_macos": mac_report, "native_linux": linux_report, "session_independent_oracles": session_oracles,
    "real_tx_permission_enabled": False, "p3_group_availability": "structural offline only; P4 safety/freshness barriers not integrated",
    "host_dependencies_sha256": digest(HOST_DEPENDENCIES.read_bytes()),
    "worker_sha256": digest((ROOT / "tools/run_panda_sessions_host.py").read_bytes()),
    "linux_sandbox_sha256": digest((ROOT / "tools/panda_transport_linux_sandbox.sh").read_bytes()),
    "original_spi_path_unchanged": True, "health_heartbeat_runtime_integrated": False,
    "MCU_build": False, "MCU_ARM_ABI_validated": False, "deployment_ready": False,
    "remaining_stages": ["P4", "P5", "P6", "P7"],
  }
  (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
  (output_parent / "latest.json").write_text(json.dumps({"status": report["status"], "result": str(output / "result.json")}, indent=2) + "\n")
  print(json.dumps({"status": report["status"], "python_tests": {k: unit_result[k] for k in ["tests_run", "passed", "failures", "errors", "skipped"]},
                    "native_can_tests": native_result.strip(), "independent_oracle": oracle,
                    "p3_platforms": {"macos": mac_report["status"], "linux": linux_report["status"]},
                    "result": str(output / "result.json"), "device_access": False}, indent=2))


if __name__ == "__main__":
  try:
    main()
  except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
    if RUN_OUTPUT is not None:
      (RUN_OUTPUT / "result.json").write_text(json.dumps({"status": "FAIL", "error": str(error), "device_access": False}, indent=2) + "\n")
      (RUN_OUTPUT.parent / "latest.json").write_text(json.dumps({"status": "FAIL", "result": str(RUN_OUTPUT / "result.json")}, indent=2) + "\n")
    print(f"FAIL P3 offline validation: {error}", file=sys.stderr)
    sys.exit(1)
