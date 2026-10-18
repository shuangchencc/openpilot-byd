#!/usr/bin/env python3
"""Replay P0–P6 patches and validate daemon, per-board control barriers and message consumers offline.

No fetching, dependency installation, ARM rebuilds or hardware entry points.
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
PATCH_ROOT = ROOT / "patches/openpilot/p6"
HOST_DEPENDENCIES = ROOT / "patches/openpilot/p4/host-dependencies.json"
P2_HOST_DEPENDENCIES = ROOT / "patches/openpilot/p2/host-dependencies.json"
PREFIX = "openpilot/selfdrive/pandad/compatibility/"
RUNTIME_PATHS = {
  "openpilot/selfdrive/pandad/main.cc", "openpilot/selfdrive/pandad/SConscript",
  "openpilot/selfdrive/pandad/pandad.py", "openpilot/selfdrive/pandad/__init__.py",
  "openpilot/selfdrive/selfdrived/selfdrived.py", "openpilot/system/hardware/hardwared.py",
  "openpilot/selfdrive/car/card.py", "openpilot/selfdrive/ui/ui_state.py",
}
ARCHIVE_PATHS = [
  "openpilot/selfdrive/pandad", "openpilot/common", "openpilot/cereal",
  "openpilot/selfdrive/selfdrived/selfdrived.py", "openpilot/system/hardware/hardwared.py",
  "openpilot/selfdrive/car/card.py", "openpilot/selfdrive/ui/ui_state.py",
]
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
  output_parent = ROOT / "results/panda-compat-p6"
  output_parent.mkdir(parents=True, exist_ok=True)
  output = Path(tempfile.mkdtemp(prefix="run-", dir=output_parent))
  RUN_OUTPUT = output
  (output / "result.json").write_text(json.dumps({"status": "RUNNING"}) + "\n")
  (output_parent / "latest.json").write_text(json.dumps({"status": "RUNNING", "result": str(output / "result.json")}) + "\n")
  manifest = json.loads((PATCH_ROOT / "manifest.json").read_text())
  for tool, expected in manifest["validation_tools"].items():
    require(tool.startswith("tools/") and ".." not in Path(tool).parts and digest((ROOT / tool).read_bytes()) == expected, "Validation tool drift: " + tool)
  prior_manifest = json.loads((ROOT / "patches/openpilot/p0p1/manifest.json").read_text())
  p2_manifest_bytes = (ROOT / "patches/openpilot/p2/manifest.json").read_bytes()
  p2_manifest = json.loads(p2_manifest_bytes)
  p3_manifest_bytes = (ROOT / "patches/openpilot/p3/manifest.json").read_bytes()
  p3_manifest = json.loads(p3_manifest_bytes)
  require(all(manifest["baselines"].get(path) == commit for path, commit in p3_manifest["baselines"].items()), "P4 baseline differs from P3")
  require("upstream-openpilot/msgq_repo" in manifest["baselines"], "Full daemon msgq baseline missing")
  require(all(manifest["baseline_inputs"].get(path) == expected for path, expected in p3_manifest["baseline_inputs"].items()), "P3 baseline protection drift")
  p4_path = ROOT / "patches/openpilot/p4/manifest.json"
  p4_manifest = json.loads(p4_path.read_text())
  require(digest(p4_path.read_bytes()) == manifest["depends_on_manifest_sha256"], "P4 dependency drift")
  p5_path = ROOT / "patches/panda/p5/manifest.json"
  p5_manifest = json.loads(p5_path.read_text())
  require(digest(p5_path.read_bytes()) == manifest["p5_manifest_sha256"], "P5 dependency drift")
  require(p5_manifest["host_p0_p4_manifest_sha256"] == digest(p4_path.read_bytes()), "P5 host baseline drift")
  require(manifest["baselines"] == p5_manifest["baselines"] == p4_manifest["baselines"], "P6 full-stack pins drift")
  changed = manifest["changed_runtime_files"]
  require(set(changed) <= RUNTIME_PATHS, "Undeclared P4 runtime scope")
  require(all(manifest["result_files"].get(path) == expected for path, expected in changed.items()), "Changed runtime hashes must match result tree")
  require(not (set(changed) & set(manifest["preserved_runtime_files"])), "Runtime file cannot be both changed and preserved")
  for basename in ["spi.cc", "panda_comms.h", "panda.cc", "panda.h", "pandad.cc", "pandad.h", "panda_safety.cc"]:
    path = "openpilot/selfdrive/pandad/" + basename
    require(path in manifest["preserved_runtime_files"] and
            manifest["preserved_runtime_files"][path] == manifest["baseline_inputs"].get("upstream-openpilot/" + path),
            "Required preserved runtime file missing: " + path)
  for path, expected in p3_manifest["preserved_runtime_files"].items():
    require(path in changed or manifest["preserved_runtime_files"].get(path) == expected, "P3 preserved runtime drift: " + path)
  require(len(manifest["series"]) == 6 and manifest["series"][5]["stage"] == "P6", "Invalid P6 host patch suffix")
  require(manifest["series"][:5] == [{**p, "file": os.path.relpath((p4_path.parent / p["file"]).resolve(), PATCH_ROOT)} for p in p4_manifest["series"]], "P4 patch dependency changed")
  require(digest(p3_manifest_bytes) == p4_manifest["depends_on_manifest_sha256"], "P3 dependency manifest drift")
  require(digest(p2_manifest_bytes) == p3_manifest["depends_on_manifest_sha256"], "P2 dependency manifest drift")
  require(digest((ROOT / "patches/openpilot/p0p1/manifest.json").read_bytes()) == p2_manifest["depends_on_manifest_sha256"], "P0/P1 dependency manifest drift")
  require(manifest["series"][:4] == [{**p, "file": os.path.relpath((ROOT / "patches/openpilot/p3" / p["file"]).resolve(), PATCH_ROOT)} for p in p3_manifest["series"]], "P3 patch dependency changed")
  require(p3_manifest["series"][:3] == [{**p, "file": os.path.relpath((ROOT / "patches/openpilot/p2" / p["file"]).resolve(), ROOT / "patches/openpilot/p3")} for p in p2_manifest["series"]], "P2 patch dependency changed")
  require(p2_manifest["series"][:2] == [{**p, "file": "../p0p1/" + p["file"]} for p in prior_manifest["series"]], "P0/P1 patch dependency changed")
  require(digest(P2_HOST_DEPENDENCIES.read_bytes()) == p2_manifest["host_dependencies_sha256"] == p3_manifest["host_dependencies_sha256"], "P2 host dependency lock drift")
  require(digest(HOST_DEPENDENCIES.read_bytes()) == manifest["host_dependencies_sha256"], "P4 host dependency lock drift")
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
  require(all((p.startswith(PREFIX) or p in changed) and ".." not in Path(p).parts for p in manifest["result_files"]), "Patch scope escape")

  source = output / "source"
  source.mkdir()
  run_capture(["git", "-c", "init.defaultBranch=offline-validation", "init", "--quiet"], source)
  baseline = manifest["baselines"]["upstream-openpilot"]
  def extract_git_archive(repo, commit, paths, prefix=""):
    archive = run_capture(["git", "archive", commit, *paths], repo).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
      for member in tar.getmembers():
        target = (source / prefix / member.name).resolve()
        require(target.is_relative_to(source), "Archive path escape")
        if member.isdir():
          target.mkdir(parents=True, exist_ok=True)
        else:
          require(member.isfile(), "Unexpected archive link")
          target.parent.mkdir(parents=True, exist_ok=True)
          target.write_bytes(tar.extractfile(member).read())
  extract_git_archive(ROOT / "upstream-openpilot", baseline, ARCHIVE_PATHS)
  extract_git_archive(ROOT / "upstream-openpilot/msgq_repo", manifest["baselines"]["upstream-openpilot/msgq_repo"], [], "msgq_repo")
  for relative, expected in manifest["baseline_inputs"].items():
    target = source / relative.removeprefix("upstream-openpilot/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((ROOT / relative).read_bytes())
  staged_baseline = {"upstream-openpilot/" + str(path.relative_to(source)): digest(path.read_bytes())
                     for path in source.rglob("*") if path.is_file() and ".git" not in path.relative_to(source).parts}
  require(staged_baseline == manifest["baseline_inputs"], "Full staged baseline is not covered exactly by locked inputs")
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
  files.update({path: digest((source / path).read_bytes()) for path in changed})
  require(files == manifest["result_files"], "Applied source tree differs from manifest")

  # Replay the unchanged P5 firmware separately. P4's old source oracle retains
  # its own pinned baseline inputs; the P6 oracle executes these P5 sources.
  panda = source / "panda_p5"
  dbc = source / "opendbc_full"
  extract_git_archive(ROOT / "upstream-openpilot/panda", manifest["baselines"]["upstream-openpilot/panda"], [], "panda_p5")
  extract_git_archive(ROOT / "upstream-openpilot/opendbc_repo", manifest["baselines"]["upstream-openpilot/opendbc_repo"], [], "opendbc_full")
  run_capture(["git", "init", "--quiet"], panda)
  for patch in p5_manifest["series"]:
    path = (p5_path.parent / patch["file"]).resolve()
    require(path.is_relative_to(p5_path.parent) and digest(path.read_bytes()) == patch["sha256"], "P5 patch drift")
    run_capture(["git", "apply", "--check", str(path)], panda)
    run_capture(["git", "apply", str(path)], panda)
  def tree_hashes(directory):
    return {str(p.relative_to(directory)): digest(p.read_bytes()) for p in directory.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(directory).parts}
  require(tree_hashes(panda) == p5_manifest["result_tree"], "P5 complete replay tree mismatch")
  dbc_hashes = tree_hashes(dbc)

  # Production Python modules are deliberately standalone stdlib logic. Tests
  # may execute the host probes, but production modules cannot import hardware.
  allowed_imports = {"__future__", "collections", "dataclasses", "enum", "json", "math", "os", "re", "struct", "typing", "types", "compatibility"}
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
    sandbox = ["/usr/bin/sandbox-exec", "-f", str(ROOT / "tools/panda_firmware_offline.sb")]
  env = dict(os.environ, PYTHONPATH=str(package.parent), PYTHONDONTWRITEBYTECODE="1",
             PYTHONNOUSERSITE="1", PYTHONOPTIMIZE="0",
             ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1",
             UV_OFFLINE="1", PIP_NO_INDEX="1",
             SONGPLUS_SCHEMA_ROOT=str(source / "openpilot/cereal"),
             SONGPLUS_CAR_SCHEMA_ROOT=str(source / "opendbc_repo/opendbc/car"))
  host_deps = json.loads(HOST_DEPENDENCIES.read_text())
  p2_host_deps = json.loads(P2_HOST_DEPENDENCIES.read_text())
  require(host_deps["macos"] == p2_host_deps["macos"] and host_deps["linux"] == p2_host_deps["linux"], "P2 native dependency contract changed")
  mac_daemon_deps = host_deps["daemon"]["macos"]
  linux_daemon_deps = host_deps["daemon"]["linux"]
  require(mac_daemon_deps["files"] and linux_daemon_deps["files"], "Daemon dependencies must be fully pinned")
  mac_daemon_prefix = Path(mac_daemon_deps["prefix"]).resolve()
  for dep in mac_daemon_deps["files"]:
    path = Path(dep["path"])
    require(path.is_absolute() and path.resolve().is_relative_to(mac_daemon_prefix), "macOS daemon dependency path escape")
    require(digest(path.read_bytes()) == dep["sha256"], "macOS daemon dependency drift: " + str(path))
  for dep in host_deps["macos"]["files"]:
    require(digest(Path(dep["path"]).read_bytes()) == dep["sha256"], "macOS dependency drift")
  commands = []

  def checked(name, command, capture_json=False):
    full = sandbox + list(map(str, command))
    try:
      result = run_capture(full, source, env=env, timeout=1200)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
      (output / f"{name}.stdout").write_bytes(exc.stdout or b"")
      (output / f"{name}.stderr").write_bytes(exc.stderr or b"")
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
          "-I", source / "panda/board", "-I", source / "opendbc_repo",
          package / "tests/health_layout_probe.cc", "-o", probe])
  health_abi = checked("health-layout", [probe], capture_json=True)
  fixture = output / "health-layout.json"
  fixture.write_text(json.dumps(health_abi, indent=2) + "\n")
  env["TEST_HEALTH_LAYOUT_JSON"] = str(fixture)
  python_report = output / "python-tests.json"
  checked("python-tests", [python, package / "tests/run_python_tests.py", python_report])
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
  env["P2_NETWORK_DEVICE_SANDBOX"] = "macos-seatbelt"
  checked("p6-macos", [python, "-S", ROOT / "tools/run_panda_p6_host.py",
      "--source", source, "--output", native_host, "--compiler", compiler,
      "--usb-include", host_deps["macos"]["usb_include"], "--usb-library", host_deps["macos"]["usb_library"]])
  mac_report = json.loads((native_host / "result.json").read_text())
  daemon_macos = output / "daemon-macos"
  checked("p6-daemon-macos", [python, "-S", ROOT / "tools/build_panda_daemon_p6.py",
      "--source", source, "--output", daemon_macos, "--prefix", mac_daemon_prefix,
      "--compiler", compiler, "--entry-test", package / "tests/test_pandad_runtime.cc"])
  mac_daemon_report = json.loads((daemon_macos / "result.json").read_text())
  checked("p6-safety-macos", [python, "-S", package / "tests/run_p6_safety.py",
      "--panda", panda, "--opendbc", dbc, "--output", output / "safety-macos", "--compiler", "clang"])
  checked("p5-f4-capability-wire", [python, "-S", panda / "tests/p5/run_capabilities.py", "--panda", panda, "--output", output / "firmware-capabilities"])
  firmware_capabilities = json.loads((output / "firmware-capabilities/result.json").read_text())
  require(firmware_capabilities["status"].startswith("PASS") and firmware_capabilities["tests_run"] == 12, "Actual P5 capability wire regression missing")
  selected_dbc = output / "selected-opendbc"
  shutil.copytree(dbc, selected_dbc)
  checked("p5-selected-safety", [python, panda / "tests/p5/run_safety.py", "--opendbc", selected_dbc, "--output", output / "selected-safety"])
  selected_safety = json.loads((output / "selected-safety/safety.json").read_text())
  require(selected_safety["tests_run"] == 49 and selected_safety["status"].startswith("PASS"), "Selected upstream safety regression missing")
  linux_dir = args.linux_root.rstrip("/") + "/" + output.name
  require(args.linux_instance and all(c.isalnum() or c in "_-" for c in args.linux_instance), "Invalid local VM name")
  require(all(c.isalnum() or c in "/_.-" for c in linux_dir) and ".." not in Path(linux_dir).parts and linux_dir.startswith("/"), "Invalid local VM task path")
  bundle = output / "linux-input.tar"
  with tarfile.open(bundle, "w") as tar:
    for path in source.rglob("*"):
      if path.is_file() and ".git" not in path.relative_to(source).parts:
        tar.add(path, arcname="source/" + str(path.relative_to(source)), recursive=False)
    for name in ["run_panda_p6_host.py", "build_panda_daemon_p6.py", "panda_transport_linux_sandbox.sh"]:
      tar.add(ROOT / "tools" / name, arcname=name)
    tar.add(HOST_DEPENDENCIES, arcname="host-dependencies.json")
  def local_vm(name, argv):
    try:
      result = run_capture(argv, timeout=1200)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
      (output / (name + ".stdout")).write_bytes(exc.stdout or b"")
      (output / (name + ".stderr")).write_bytes(exc.stderr or b"")
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
  daemon_dependency_prefix = linux_daemon_deps["prefix"]
  require(isinstance(daemon_dependency_prefix, str) and daemon_dependency_prefix.startswith("/") and
          all(c.isalnum() or c in "/_.-" for c in daemon_dependency_prefix) and
          ".." not in Path(daemon_dependency_prefix).parts, "Invalid prepared daemon dependency prefix")
  for dep in linux_daemon_deps["files"]:
    require(not Path(dep["path"]).is_absolute() and ".." not in Path(dep["path"]).parts, "Linux daemon dependency path escape")
  # Hash all already-prepared files in one bounded read-only guest command.
  dependency_check = """import hashlib,json,pathlib,sys
lock=json.loads(pathlib.Path(sys.argv[1]).read_text())['daemon']['linux']
prefix=pathlib.Path(lock['prefix'])
for item in lock['files']:
 actual=hashlib.sha256((prefix/item['path']).read_bytes()).hexdigest()
 if actual != item['sha256']: raise SystemExit('Daemon dependency drift: '+item['path'])
print(json.dumps({'status':'PASS','checked_files':len(lock['files'])}))
"""
  local_vm("linux-daemon-dependency-hashes", ["limactl", "shell", args.linux_instance, "--",
      "python3", "-S", "-c", dependency_check, linux_dir + "/host-dependencies.json"])
  local_vm("p6-linux", ["limactl", "shell", args.linux_instance, "--", "sh", linux_dir + "/panda_transport_linux_sandbox.sh",
      "python3", linux_dir + "/run_panda_p6_host.py", "--source", linux_dir + "/source",
      "--output", linux_dir + "/output", "--compiler", "g++", "--usb-include", dependency_prefix + "/usr/include/libusb-1.0",
      "--usb-library", dependency_prefix + "/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0.3.0"])
  local_vm("linux-copy-results", ["limactl", "copy", "-r", args.linux_instance + ":" + linux_dir + "/output", str(output / "native-linux")])
  linux_report = json.loads((output / "native-linux/result.json").read_text())
  local_vm("p6-daemon-linux", ["limactl", "shell", args.linux_instance, "--", "sh", linux_dir + "/panda_transport_linux_sandbox.sh",
      "python3", linux_dir + "/build_panda_daemon_p6.py", "--source", linux_dir + "/source",
      "--output", linux_dir + "/daemon-output", "--compiler", "g++", "--prefix", daemon_dependency_prefix])
  local_vm("linux-copy-daemon-results", ["limactl", "copy", "-r", args.linux_instance + ":" + linux_dir + "/daemon-output", str(output / "daemon-linux")])
  linux_daemon_report = json.loads((output / "daemon-linux/result.json").read_text())
  local_vm("p6-safety-linux", ["limactl", "shell", args.linux_instance, "--", "sh", linux_dir + "/panda_transport_linux_sandbox.sh",
      "python3", "-S", linux_dir + "/source/" + PREFIX + "tests/run_p6_safety.py",
      "--panda", linux_dir + "/source/panda_p5", "--opendbc", linux_dir + "/source/opendbc_full",
      "--output", linux_dir + "/safety-output", "--compiler", "gcc"])
  local_vm("linux-copy-safety-results", ["limactl", "copy", "-r", args.linux_instance + ":" + linux_dir + "/safety-output", str(output / "safety-linux")])
  safety_reports = {label: json.loads((output / ("safety-"+label) / "result.json").read_text()) for label in ("macos", "linux")}
  safety_runner = package / "tests/run_p6_safety.py"
  safety_fixture = package / "tests/p6_safety/oracle.c"
  safety_lock_path = package / "tests/p6_safety/source-lock.json"
  safety_lock = json.loads(safety_lock_path.read_text())
  require(safety_lock["panda_commit"] == p5_manifest["series"][-1]["commit"] and
          safety_lock["opendbc_commit"] == manifest["baselines"]["upstream-openpilot/opendbc_repo"], "P6 safety source commit drift")
  require(safety_lock["files"] and len({(item["repository"], item["path"]) for item in safety_lock["files"]}) == len(safety_lock["files"]), "P6 safety source inventory empty/duplicated")
  for item in safety_lock["files"]:
    require(item["repository"] in {"panda", "opendbc"} and not Path(item["path"]).is_absolute() and
            ".." not in Path(item["path"]).parts, "P6 safety source inventory path escape")
    root = panda if item["repository"] == "panda" else dbc
    require(digest((root / item["path"]).read_bytes()) == item["sha256"], "P6 safety source hash drift")
  safety_cases = {}
  for node in ast.parse(safety_runner.read_text()).body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {"COMMON", "DEBUG", "RELEASE"}:
      safety_cases[node.targets[0].id] = ast.literal_eval(node.value)
  require(set(safety_cases) == {"COMMON", "DEBUG", "RELEASE"} and
          tuple(map(len, (safety_cases["COMMON"], safety_cases["DEBUG"], safety_cases["RELEASE"]))) == (12, 7, 1), "P6 safety case inventory drift")
  for label, safety_report in safety_reports.items():
    summary = safety_report["summary"]
    require(summary == {"status": "PASS_OFFLINE_SOURCE_ORACLE", "passed": 64, "assertions": 5086,
                        "failed": 0, "skipped": 0, "classic_parity_comparisons": 30}, "Incomplete P6 safety counts: " + label)
    require(safety_report["hardware_access"] is False and safety_report["real_can_tx"] is False,
            "P6 safety oracle escaped offline scope")
    require(safety_report["source_lock"] == safety_lock and
            safety_report["source_lock_sha256"] == digest(safety_lock_path.read_bytes()) and
            safety_report["runner_sha256"] == digest(safety_runner.read_bytes()) and
            safety_report["oracle_sha256"] == digest(safety_fixture.read_bytes()), "P6 safety report/source mismatch")
    require(safety_report["compiler_version"].strip() and len(safety_report["commands"]) == 69 and
            all(command["exit_code"] == 0 for command in safety_report["commands"]), "P6 safety compiler/run evidence missing")
    configurations = safety_report["configurations"]
    require(len(configurations) == 4 and {item["name"] for item in configurations} ==
            {"f4-debug", "h7-debug", "f4-release", "h7-release"}, "P6 safety build matrix incomplete")
    for config in configurations:
      require(config["name"] == config["target_policy"] + "-" + config["variant"], "P6 safety target label mismatch")
      expected = safety_cases["COMMON"] + safety_cases[config["variant"].upper()]
      require(tuple(case["case"] for case in config["cases"]) == expected and
              sum(case["assertions"] for case in config["cases"]) == (1532 if config["variant"] == "debug" else 1011),
              "P6 safety group/assertion evidence incomplete")
      require(all(case["assertions"] > 0 and case["failed"] == case["skipped"] == 0 and case["traces"] for case in config["cases"]),
              "P6 safety group empty/failed/skipped")
      require(len(config["binary_sha256"]) == 64, "P6 safety binary hash missing")
      require(digest((output / ("safety-" + label) / config["name"]).read_bytes()) == config["binary_sha256"],
              "P6 safety executed artifact hash mismatch")
    expected_parity = []
    for variant in ("debug", "release"):
      f4 = next(config for config in configurations if config["name"] == "f4-" + variant)
      h7 = next(config for config in configurations if config["name"] == "h7-" + variant)
      for left, right in zip(f4["cases"], h7["cases"], strict=True):
        if left["case"] != "fd_capability_differs_without_policy_drift":
          require(left == right, "P6 F4/H7 classic decisions diverge")
          expected_parity.append({"variant": variant, "case": left["case"], "equal": True})
    require(safety_report["parity_comparisons"] == expected_parity and len(expected_parity) == 30,
            "P6 classic parity evidence missing")
    extracted = safety_report["source_evidence"]
    pieces = extracted["unmodified_original_ranges"]
    require(len(pieces) == 7 and len({(piece["path"], piece["first_line"], piece["last_line"]) for piece in pieces}) == 7 and
            digest((output / ("safety-" + label) / "firmware_source.inc").read_bytes()) == extracted["generated_sha256"],
            "P6 actual firmware extraction evidence missing")
    for piece in pieces:
      require(piece["path"] in {"board/main.c", "board/main_comms.h", "board/utils.h"} and
              1 <= piece["first_line"] <= piece["last_line"], "P6 actual firmware source range invalid")
      lines = (panda / piece["path"]).read_bytes().splitlines(keepends=True)
      require(digest(b"".join(lines[piece["first_line"] - 1:piece["last_line"]])) == piece["sha256"],
              "P6 extracted firmware differs from pinned source")
    expected_commands = {"compiler-version"}
    for config in configurations:
      expected_commands.add("compile-" + config["name"])
      expected_commands.update(config["name"] + "-" + case["case"] for case in config["cases"])
    require({command["name"] for command in safety_report["commands"]} == expected_commands,
            "P6 safety compiler/case command inventory differs")
  require(safety_reports["macos"]["source_evidence"] == safety_reports["linux"]["source_evidence"],
          "P6 macOS/Linux firmware source extraction differs")
  for mac_config, linux_config in zip(safety_reports["macos"]["configurations"], safety_reports["linux"]["configurations"], strict=True):
    require(mac_config["name"] == linux_config["name"] and mac_config["cases"] == linux_config["cases"],
            "P6 macOS/Linux actual-source decisions differ")
  for label, native_report, daemon_report in [("macos", mac_report, mac_daemon_report), ("linux", linux_report, linux_daemon_report)]:
    require(daemon_report["status"] == "PASS_OFFLINE_HOST_ONLY" and not daemon_report["legacy_main_invoked"] and
            not daemon_report["hardware_open"] and not daemon_report["real_can_tx"], "Daemon execution escaped offline scope")
    require(daemon_report["active_pandad_built"] == (label == "linux"), "Daemon build scope mismatch")
    require(daemon_report["actual_main_cc_compiled_and_linked"] == (label == "linux"), "Daemon entry source not compiled")
    if label == "linux":
      require(daemon_report["unknown_compat_guard"]["exit_code"] == 2 and daemon_report["legacy_panda_spi_compiled_and_linked"], "Linux daemon guard/retained-SPI evidence missing")
      guard_cases = daemon_report["unknown_compat_guard"]["cases"]
      require(len(guard_cases) == 8 and len({case["name"] for case in guard_cases}) == 8 and
              all(case["exit_code"] == 2 for case in guard_cases), "Incomplete native compatibility argument guard matrix")
    for path, expected in daemon_report["source_files_sha256"].items():
      require(".." not in Path(path).parts and not Path(path).is_absolute(), "Daemon compiler input path escape")
      require(path in files or "upstream-openpilot/" + path in manifest["baseline_inputs"], "Unpinned daemon compiler input: " + path)
      require(digest((source / path).read_bytes()) == expected, "Daemon compiler source drift: " + path)
    native_report["suites"]["pandad_runtime"] = {"summary": daemon_report["summary"], "groups": daemon_report["groups"], "traces": daemon_report["traces"]}
    loop = daemon_report["loop_suite"]
    require(loop["summary"]["failed"] == 0 and loop["summary"]["skipped"] == 0 and loop["groups"], "Event cycle evidence missing")
    native_report["suites"]["pandad_loop"] = loop
    native_report["suites"]["p6_faults"] = daemon_report["p6_faults_suite"]
  require(mac_report["status"] == linux_report["status"] == "PASS_OFFLINE_HOST_ONLY", "Native suite failed")
  runtime_message_oracles = {}
  for label, directory in [("macos", daemon_macos), ("linux", output / "daemon-linux")]:
    result = checked("native-cereal-consumers-" + label,
      [python, package / "tests/check_runtime_messages.py", directory / "result.json"], capture_json=True)
    require(result["status"] == "PASS" and result["native_cereal_messages"] == 11 and result["assertions_enabled"], "Native cereal/consumer mismatch")
    runtime_message_oracles[label] = result

  session_oracles = {}
  for label, native_report, native_path in [("macos", mac_report, native_host), ("linux", linux_report, output / "native-linux")]:
    oracle_report = checked("session-independent-oracle-" + label,
      [python, "-S", package / "tests/check_session_oracle.py", native_path / "session-oracle.json"], capture_json=True)
    require(oracle_report["status"] == "PASS" and oracle_report["assertions_enabled"] and oracle_report["fixture_count"] == 8 and oracle_report["route_checks"] == 48, "Session oracle failed")
    session_oracles[label] = oracle_report
  for stage, map_root, stage_manifest in [("P2", ROOT / "patches/openpilot/p2", p2_manifest), ("P3", ROOT / "patches/openpilot/p3", p3_manifest), ("P4", p4_path.parent, p4_manifest), ("P6", PATCH_ROOT, manifest)]:
    map_bytes = (map_root / "acceptance-map.json").read_bytes()
    require(digest(map_bytes) == stage_manifest["acceptance_map_sha256"], stage + " mapping drift")
    mapping = json.loads(map_bytes)
    require(set(stage_manifest["acceptance_ids"]) == {c["id"] for c in mapping["cases"]} == {c["id"] for c in design_cases if c["stage"] == stage}, stage + " coverage drift")
    for case in mapping["cases"]:
      python_evidence = set()
      for selector in case.get("required_python_selectors", []):
        matches = {test for test in unit_result["passed_ids"] if fnmatch.fnmatchcase(test, selector)}
        require(matches, "Required P4 Python selector missing: " + selector)
        python_evidence.update(matches)
      for platform_report in [mac_report, linux_report]:
        for suite, groups in case["required_groups"].items():
          require(set(groups) <= set(platform_report["suites"][suite]["groups"]), "Missing evidence for " + case["id"])
      for platform_report in safety_reports.values() if "safety_evidence" in case else []:
        actual = {c["case"] for cfg in platform_report["configurations"] for c in cfg["cases"]}
        require(set(case["safety_evidence"]["required_cases"]) <= actual, "Missing P6 safety evidence for " + case["id"])
      acceptance_results.append({"id": case["id"], "stage": stage, "status": "PASS_OFFLINE_DECLARED_SCOPE",
                                 "evidence": case["required_groups"], "python_evidence": sorted(python_evidence), "scope": case["scope"], "safety_evidence": case.get("safety_evidence")})
  for relative, expected in manifest["preserved_runtime_files"].items():
    require(digest((source / relative).read_bytes()) == expected, "Original runtime SPI path changed")
  require(tree_hashes(panda) == p5_manifest["result_tree"] and tree_hashes(dbc) == dbc_hashes, "Firmware/safety source changed during validation")
  require(all(digest((selected_dbc / p).read_bytes()) == h for p, h in dbc_hashes.items()), "Selected safety source modified")
  require(all(digest((source / p).read_bytes()) == h for p, h in files.items()), "Host source changed during validation")
  for tool, expected in manifest["validation_tools"].items():
    require(digest((ROOT / tool).read_bytes()) == expected, "Validation tool changed during run")
  report = {
    "schema": "songplus-panda-p6-validation/v1",
    "validation_tools": manifest["validation_tools"], "status": "PASS_OFFLINE_HOST_ONLY",
    "tested_at_utc": datetime.now(timezone.utc).isoformat(), "baselines": manifest["baselines"],
    "patch_commits": [p["commit"] for p in manifest["series"]],
    "manifest_sha256": digest((PATCH_ROOT / "manifest.json").read_bytes()),
    "validator_sha256": digest(Path(__file__).read_bytes()),
    "sandbox_sha256": digest((ROOT / "tools/panda_firmware_offline.sb").read_bytes()),
    "platform": platform.platform(), "python": python_version, "compiler": compiler,
    "compiler_version": run_capture([compiler, "--version"]).stdout.decode(),
    "macos_network_and_device_node_sandbox": bool(sandbox), "sanitizers": ["address", "undefined"],
    "python_tests": unit_result, "native_can_tests": native_result, "native_summary": native_summary, "independent_oracle": oracle,
    "health_host_abi": health_abi, "commands": commands,
    "acceptance_results": acceptance_results,
    "source_tree_sha256": digest(json.dumps(files, sort_keys=True).encode()),
    "runtime_integrated": "explicit offline entry only", "real_transport_executed": False, "device_access": False,
    "native_macos": mac_report, "native_linux": linux_report, "session_independent_oracles": session_oracles,
    "runtime_message_oracles": runtime_message_oracles,
    "daemon_macos": mac_daemon_report, "daemon_linux": linux_daemon_report,
    "daemon_worker_sha256": digest((ROOT / "tools/build_panda_daemon_p6.py").read_bytes()),
    "real_tx_permission_enabled": False, "p4_group_availability": "per-board health, CAN-health, noOutput readback and fresh re-engagement barriers",
    "host_dependencies_sha256": digest(HOST_DEPENDENCIES.read_bytes()),
    "worker_sha256": digest((ROOT / "tools/run_panda_p6_host.py").read_bytes()),
    "linux_sandbox_sha256": digest((ROOT / "tools/panda_transport_linux_sandbox.sh").read_bytes()),
    "original_spi_path_unchanged": True, "health_heartbeat_runtime_integrated": "offline injected transports only",
    "MCU_build": False, "MCU_ARM_ABI_validated": False, "deployment_ready": False,
    "remaining_stages": ["P7"],
    "p5_manifest_sha256": digest(p5_path.read_bytes()), "p6_safety": safety_reports, "selected_upstream_safety": selected_safety, "firmware_capabilities": firmware_capabilities,
  }
  (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
  (output_parent / "latest.json").write_text(json.dumps({"status": report["status"], "result": str(output / "result.json")}, indent=2) + "\n")
  print(json.dumps({"status": report["status"], "python_tests": {k: unit_result[k] for k in ["tests_run", "passed", "failures", "errors", "skipped"]},
                    "native_can_tests": native_result.strip(), "independent_oracle": oracle,
                    "p6_platforms": {"macos": mac_report["status"], "linux": linux_report["status"]},
                    "result": str(output / "result.json"), "device_access": False}, indent=2))


if __name__ == "__main__":
  try:
    main()
  except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
    if RUN_OUTPUT is not None:
      (RUN_OUTPUT / "result.json").write_text(json.dumps({"status": "FAIL", "error": str(error), "device_access": False}, indent=2) + "\n")
      (RUN_OUTPUT.parent / "latest.json").write_text(json.dumps({"status": "FAIL", "result": str(RUN_OUTPUT / "result.json")}, indent=2) + "\n")
    print(f"FAIL P6 offline validation: {error}", file=sys.stderr)
    sys.exit(1)
