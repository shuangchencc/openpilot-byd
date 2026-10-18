#!/usr/bin/env python3
"""Compile lifecycle providers, exercising only injected USB/SPI calls offline.

The unchanged P9 worker executes first on this same source tree. Real libusb and
Linux spidev implementations are compiled and linked, but test mains supply
fake functions and never enumerate or open the host's hardware.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

SOURCES = ("discovery_scheduler.cc", "spi_freshness.cc", "production_gate.cc", "native_discovery_executor.cc")
SUITES = ("discovery_scheduler", "spi_freshness", "production_gate", "native_discovery_executor", "native_discovery_engine")


def digest(path):
  return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(path):
  return {str(p.relative_to(path)): digest(p) for p in sorted(path.rglob("*"))
          if p.is_file() and ".git" not in p.parts}


def parse_suite(raw, suite):
  lines = raw.splitlines()
  if not lines:
    raise ValueError(suite + " produced no evidence")
  summary = json.loads(lines[-1])
  groups = [line[5:] for line in lines if line.startswith("PASS ")]
  if (not groups or len(groups) != len(set(groups)) or any(not s for s in groups)
      or type(summary.get("passed")) is not int or summary["passed"] != len(groups)
      or type(summary.get("failed")) is not int or summary["failed"] != 0
      or type(summary.get("skipped")) is not int or summary["skipped"] != 0):
    raise ValueError(suite + " invalid case accounting")
  if suite in SUITES and (summary.get("suite") != "p10_" + suite or summary.get("real_device_io") is not False):
    raise ValueError(suite + " identity/device scope differs from the native offline suite")
  return {"summary": summary, "groups": groups}


def expected_commands(linux=False):
  return ["p9-regression", "scan-firmware-oracle", "compile-firmware-oracle", *["scan-" + s for s in SOURCES], *["scan-" + s + "-test" for s in SUITES],
          *["compile-" + s for s in SOURCES],
          *[name for suite in SUITES for name in ("compile-" + suite + "-test", "link-" + suite, "run-" + suite)],
          *(["compile-actual-main", "link-actual-pandad-p10", "actual-pandad-p10-offline", "actual-pandad-p10-owner", "actual-pandad-p10-gate", "deny-default", "deny-serial", "deny-profile-environment", "deny---compat-native", "deny---compat-runtime", "deny---compat-device"] if linux else [])]


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  for name in ("source", "output", "prefix", "usb-include", "usb-library"):
    parser.add_argument("--" + name, type=Path, required=True)
  parser.add_argument("--compiler", default="clang++")
  args = parser.parse_args()
  linux = platform.system() == "Linux"
  marker = os.environ.get("P2_NETWORK_DEVICE_SANDBOX")
  expected_marker = "linux-user-net-mount-private-shm" if linux else "macos-seatbelt"
  if platform.system() not in ("Darwin", "Linux") or marker != expected_marker:
    raise ValueError("Explicit supported offline sandbox required")
  if linux and os.environ.get("P8_ISOLATED_MSGQ") != "1":
    raise ValueError("Linux requires isolated private shared memory")
  output = args.output.absolute()
  if any(p.is_symlink() for p in (output, *output.parents)):
    raise ValueError("Output path symlink forbidden")
  args.source = args.source.resolve()
  args.output = output.resolve()
  args.prefix = args.prefix.resolve()
  args.usb_include = args.usb_include.resolve()
  args.usb_library = args.usb_library.resolve()
  args.output.mkdir(parents=True, exist_ok=False)
  initial_sources = tree(args.source)
  commands = []
  env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1",
             UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")

  def run(name, argv, timeout=1800, expected=0):
    result = subprocess.run(list(map(str, argv)), cwd=args.source, env=env,
                            capture_output=True, timeout=timeout)
    for stream, data in (("stdout", result.stdout), ("stderr", result.stderr)):
      (args.output / (name + "." + stream)).write_bytes(data)
    commands.append({"name": name, "argv": list(map(str, argv)), "exit_code": result.returncode,
                     "expected_exit_code": expected,
                     "stdout_sha256": digest(args.output / (name + ".stdout")),
                     "stderr_sha256": digest(args.output / (name + ".stderr"))})
    if result.returncode != expected:
      raise RuntimeError(name + " failed: " + result.stderr.decode(errors="replace")[-3000:])
    return result.stdout.decode()

  try:
    here = Path(__file__).resolve().parent
    parent_output = args.output / "p9-regression"
    run("p9-regression", [sys.executable, "-S", here / "build_p9_native.py",
                          "--source", args.source, "--output", parent_output,
                          "--prefix", args.prefix, "--compiler", args.compiler,
                          "--usb-include", args.usb_include, "--usb-library", args.usb_library])
    parent = json.loads((parent_output / "result.json").read_text())
    if parent.get("status") != "PASS_OFFLINE_HOST_ONLY" or parent.get("source_files_sha256") != initial_sources:
      raise ValueError("P9 regression result/source mismatch")
    pkg = args.source / "openpilot/selfdrive/pandad/compatibility"
    p8_output = parent_output / "p8-regression"
    daemon = p8_output / "daemon-regression"
    generated = daemon / "generated"
    cpp = generated / "openpilot/cereal/gen/cpp"
    flags = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-g", "-fno-omit-frame-pointer",
             "-fsanitize=address,undefined", "-pthread", "-D__COMMA_HARDWARE__=0"]
    for include in (generated, generated / "openpilot", cpp, args.source, args.source / "openpilot",
                    pkg, args.source / "msgq_repo", args.source / "opendbc_repo", args.usb_include):
      flags += ["-I", include]
    flags += ["-isystem", args.prefix / "capnproto/install/include"]
    dependencies = {str(Path(shutil.which(args.compiler)).resolve()): digest(Path(shutil.which(args.compiler)).resolve())}
    oracle_source = pkg / "tests/spi_freshness_firmware_oracle.c"
    oracle_flags = ["-x", "c", "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-g", "-fno-omit-frame-pointer",
                    "-fsanitize=address,undefined", "-DP10_PANDA_SPI_HEADER=\"" + str(args.source / "panda/board/drivers/spi.h") + "\"",
                    "-I", pkg / "tests/spi_freshness_oracle"]
    oracle_deps = args.output / "firmware-oracle.dependencies.d"
    run("scan-firmware-oracle", [args.compiler, *oracle_flags, "-M", "-MF", oracle_deps, oracle_source])
    for word in shlex.split(oracle_deps.read_text().replace("\\\n", " ").split(":", 1)[1]):
      dependency = Path(word)
      if not dependency.is_absolute(): dependency = args.source / dependency
      dependencies[str(dependency.resolve())] = digest(dependency.resolve())
    oracle = args.output / "firmware-oracle.o"
    run("compile-firmware-oracle", [args.compiler, *oracle_flags, "-c", oracle_source, "-o", oracle])
    scan_inputs = [(name, pkg / name) for name in SOURCES]
    scan_inputs += [(suite + "-test", pkg / "tests" / ("test_" + suite + ".cc")) for suite in SUITES]
    for name, source in scan_inputs:
      path = args.output / (name + ".dependencies.d")
      run("scan-" + name, [args.compiler, *flags, "-M", "-MF", path, source])
      for word in shlex.split(path.read_text().replace("\\\n", " ").split(":", 1)[1]):
        dependency = Path(word)
        if not dependency.is_absolute():
          dependency = args.source / dependency
        dependency = dependency.resolve()
        dependencies[str(dependency)] = digest(dependency)
    objects = []
    for index, name in enumerate(SOURCES):
      obj = args.output / (str(index) + ".o")
      run("compile-" + name, [args.compiler, *flags, "-MMD", "-MF", args.output / (str(index) + ".d"),
                              "-c", pkg / name, "-o", obj])
      objects.append(obj)
    # Match the retained worker's documented primary objects, excluding both
    # executable mains. Do not glob in its loop/fault fixture mains.
    core = [p for p in sorted(daemon.glob("[0-9][0-9].o"))
            if int(p.stem) < (28 if linux else 14) and int(p.stem) not in (7, 13)]
    p8_objects = [p8_output / (str(i) + ".o") for i in range(6 if linux else 5)]
    p9_objects = [parent_output / (str(i) + ".o") for i in range(4)]
    libs = [args.prefix / "capnproto/install/lib/libcapnp.a",
            args.prefix / "capnproto/install/lib/libkj.a", args.usb_library]
    if linux:
      libs += [args.prefix / "json11/install/lib/libjson11.a", args.prefix / "zeromq/install/lib/libzmq.a"]
    for library in libs:
      dependencies[str(library.resolve())] = digest(library.resolve())
    extra = ["-lz", "-Wl,-rpath," + str(args.usb_library.parent)] + (["-ldl", "-lrt"] if linux else [])
    suites = {}
    for suite in SUITES:
      test = args.output / (suite + "-test.o")
      binary = args.output / ("test_" + suite)
      run("compile-" + suite + "-test", [args.compiler, *flags, "-MMD", "-MF", args.output / (suite + "-test.d"),
                                         "-c", pkg / "tests" / ("test_" + suite + ".cc"), "-o", test])
      run("link-" + suite, [args.compiler, "-fsanitize=address,undefined", "-pthread",
                            *core, *p8_objects, *p9_objects, *objects, oracle, test, *libs, *extra, "-o", binary])
      suites[suite] = parse_suite(run("run-" + suite, [binary]), suite)
      suites[suite]["binary_sha256"] = digest(binary)
    groups = [group for suite in SUITES for group in suites[suite]["groups"]]
    if len(groups) != len(set(groups)):
      raise ValueError("Provider group names must be globally unique")
    actual_main = None
    if linux:
      main = args.output / "actual-main.o"
      run("compile-actual-main", [args.compiler, *[f for f in flags if f != "-Werror"],
                                  "-DSONGPLUS_P8_INTEGRATION", "-DSONGPLUS_P10_INTEGRATION", "-isystem",
                                  args.prefix / "json11/install/include", "-c", args.source / "openpilot/selfdrive/pandad/main.cc", "-o", main])
      binary = args.output / "pandad-p10"
      # Link the unchanged, explicitly offline-dispatched real main with all
      # native provider objects. No new native entry or default activation.
      daemon_objects = [p for p in sorted(daemon.glob("[0-9][0-9].o")) if int(p.stem) < 28 and int(p.stem) != 13]
      run("link-actual-pandad-p10", [args.compiler, "-fsanitize=address,undefined", "-pthread",
                                   *daemon_objects, main, *p8_objects, *p9_objects, *objects,
                                   *libs, *extra, "-o", binary])
      run("actual-pandad-p10-offline", [binary, "--compat-offline"])
      raw = run("actual-pandad-p10-owner", [binary, "--compat-runtime-offline"])
      actual_main = parse_suite(raw, "actual-pandad-p10")
      if actual_main["groups"] != ["p8_actual_main_unbound_owner_no_io"]:
        raise ValueError("Actual main changed from the unbound offline owner")
      gate = parse_suite(run("actual-pandad-p10-gate", [binary, "--compat-production-check-offline"]), "p10-main-gate")
      if gate["groups"] != ["p10_main_production_check_defaults_blocked"] or gate["summary"].get("real_device_io") is not False:
        raise ValueError("Actual production-check proof changed")
      for name, argv, code in (("deny-default", [binary], 3), ("deny-serial", [binary, "fixture-serial"], 3),
                               ("deny-profile-environment", ["env", "SONGPLUS_PANDA_PROFILE_JSON={}", binary], 2)):
        run(name, argv, timeout=10, expected=code)
      for option in ("--compat-native", "--compat-runtime", "--compat-device"):
        run("deny-" + option, [binary, option], timeout=10, expected=2)
    if tree(args.source) != initial_sources:
      raise ValueError("Source modified during validation")
    if any(digest(Path(name)) != expected for name, expected in dependencies.items()):
      raise ValueError("Native compiler/header/library dependency changed during validation")
    if [c["name"] for c in commands] != expected_commands(linux):
      raise ValueError("Worker command inventory mismatch")
    report = {"schema": "songplus-lifecycle-p10-worker/v1", "status": "PASS_OFFLINE_HOST_ONLY",
              "platform": platform.platform(), "sandbox": marker, "commands": commands,
              "summary": {"passed": len(groups), "failed": 0, "skipped": 0}, "groups": groups, "suites": suites,
              "source_files_sha256": initial_sources, "sanitizers": ["address", "undefined"],
              "native_dependencies_sha256": dependencies, "native_dependencies_unchanged": True,
              "native_usb_compiled_and_linked": True, "native_spi_linux_ioctls_compiled": linux,
              "provider_calls_injected": True, "real_device_discovery": False, "real_device_io": False,
              "real_can_tx": False, "deployment_ready": False, "profile_bound_to_real_hardware": False,
              "isolated_msgq_endpoints_opened": linux, "network_sockets_opened": False,
              "actual_pandad_relinked_with_native_providers": linux,
              "actual_main": actual_main, "production_check": gate if linux else None,
              "firmware_oracle_sha256": digest(oracle), "locked_spi_driver_sha256": digest(args.source / "panda/board/drivers/spi.h"), "actual_main_binary_sha256": digest(args.output / "pandad-p10") if linux else None,
              "p9_regression_sha256": digest(parent_output / "result.json"),
              "p9_regression": parent,
              "retained_files_sha256": {str(p.relative_to(args.output)): digest(p) for p in sorted(args.output.rglob("*"))
                                         if p.is_file() and p != args.output / "result.json"}}
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "summary": report["summary"]}))
  except Exception as error:
    (args.output / "result.json").write_text(json.dumps({"status": "FAIL", "error": str(error), "commands": commands}, indent=2) + "\n")
    raise


if __name__ == "__main__":
  main()
