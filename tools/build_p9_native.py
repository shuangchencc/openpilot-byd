#!/usr/bin/env python3
"""Compile native providers, exercising only injected USB/SPI calls offline.

The unchanged P8 worker executes first on this same source tree. Real libusb and
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

SOURCES = ("native_usb.cc", "native_spi.cc", "native_spi_probe.cc", "native_binding.cc")
SUITES = ("native_usb", "native_spi", "native_spi_probe", "native_binding")


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
  if suite in SUITES and (summary.get("suite") != "p9_" + suite or summary.get("real_device_io") is not False):
    raise ValueError(suite + " identity/device scope differs from the native offline suite")
  return {"summary": summary, "groups": groups}


def expected_commands(linux=False):
  return ["p8-regression", *["scan-" + s for s in SOURCES], *["scan-" + s + "-test" for s in SUITES],
          *["compile-" + s for s in SOURCES],
          *[name for suite in SUITES for name in ("compile-" + suite + "-test", "link-" + suite, "run-" + suite)],
          *(["link-actual-pandad-p9", "actual-pandad-p9-offline", "actual-pandad-p9-owner", "deny---compat-native", "deny---compat-runtime", "deny---compat-device"] if linux else [])]


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
    parent_output = args.output / "p8-regression"
    run("p8-regression", [sys.executable, "-S", here / "build_p8_runtime.py",
                          "--source", args.source, "--output", parent_output,
                          "--prefix", args.prefix, "--compiler", args.compiler,
                          "--usb-include", args.usb_include, "--usb-library", args.usb_library])
    parent = json.loads((parent_output / "result.json").read_text())
    if parent.get("status") != "PASS_OFFLINE_HOST_ONLY" or parent.get("source_files_sha256") != initial_sources:
      raise ValueError("P8 regression result/source mismatch")
    pkg = args.source / "openpilot/selfdrive/pandad/compatibility"
    daemon = parent_output / "daemon-regression"
    generated = daemon / "generated"
    cpp = generated / "openpilot/cereal/gen/cpp"
    flags = ["-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1", "-g", "-fno-omit-frame-pointer",
             "-fsanitize=address,undefined", "-pthread", "-D__COMMA_HARDWARE__=0"]
    for include in (generated, generated / "openpilot", cpp, args.source, args.source / "openpilot",
                    pkg, args.source / "msgq_repo", args.source / "opendbc_repo", args.usb_include):
      flags += ["-I", include]
    flags += ["-isystem", args.prefix / "capnproto/install/include"]
    dependencies = {str(Path(shutil.which(args.compiler)).resolve()): digest(Path(shutil.which(args.compiler)).resolve())}
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
    p8_objects = [parent_output / (str(i) + ".o") for i in range(6 if linux else 5)]
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
                            *core, *p8_objects, *objects, test, *libs, *extra, "-o", binary])
      suites[suite] = parse_suite(run("run-" + suite, [binary]), suite)
      suites[suite]["binary_sha256"] = digest(binary)
    groups = [group for suite in SUITES for group in suites[suite]["groups"]]
    if len(groups) != len(set(groups)):
      raise ValueError("Provider group names must be globally unique")
    actual_main = None
    if linux:
      binary = args.output / "pandad-p9"
      # Link the unchanged, explicitly offline-dispatched real main with all
      # native provider objects. No new native entry or default activation.
      daemon_objects = [p for p in sorted(daemon.glob("[0-9][0-9].o")) if int(p.stem) < 28 and int(p.stem) != 13]
      run("link-actual-pandad-p9", [args.compiler, "-fsanitize=address,undefined", "-pthread",
                                   *daemon_objects, parent_output / "actual-main.o", *p8_objects, *objects,
                                   *libs, *extra, "-o", binary])
      run("actual-pandad-p9-offline", [binary, "--compat-offline"])
      raw = run("actual-pandad-p9-owner", [binary, "--compat-runtime-offline"])
      actual_main = parse_suite(raw, "actual-pandad-p9")
      if actual_main["groups"] != ["p8_actual_main_unbound_owner_no_io"]:
        raise ValueError("Actual main changed from the unbound offline owner")
      for option in ("--compat-native", "--compat-runtime", "--compat-device"):
        run("deny-" + option, [binary, option], timeout=10, expected=2)
    if tree(args.source) != initial_sources:
      raise ValueError("Source modified during validation")
    if any(digest(Path(name)) != expected for name, expected in dependencies.items()):
      raise ValueError("Native compiler/header/library dependency changed during validation")
    if [c["name"] for c in commands] != expected_commands(linux):
      raise ValueError("Worker command inventory mismatch")
    report = {"schema": "songplus-native-p9-worker/v1", "status": "PASS_OFFLINE_HOST_ONLY",
              "platform": platform.platform(), "sandbox": marker, "commands": commands,
              "summary": {"passed": len(groups), "failed": 0, "skipped": 0}, "groups": groups, "suites": suites,
              "source_files_sha256": initial_sources, "sanitizers": ["address", "undefined"],
              "native_dependencies_sha256": dependencies, "native_dependencies_unchanged": True,
              "native_usb_compiled_and_linked": True, "native_spi_linux_ioctls_compiled": linux,
              "provider_calls_injected": True, "real_device_discovery": False, "real_device_io": False,
              "real_can_tx": False, "deployment_ready": False, "profile_bound_to_real_hardware": False,
              "isolated_msgq_endpoints_opened": linux, "network_sockets_opened": False,
              "actual_pandad_relinked_with_native_providers": linux,
              "actual_main": actual_main, "actual_main_binary_sha256": digest(args.output / "pandad-p9") if linux else None,
              "p8_regression_sha256": digest(parent_output / "result.json"),
              "p8_regression": parent,
              "retained_files_sha256": {str(p.relative_to(args.output)): digest(p) for p in sorted(args.output.rglob("*"))
                                         if p.is_file() and p != args.output / "result.json"}}
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "summary": report["summary"]}))
  except Exception as error:
    (args.output / "result.json").write_text(json.dumps({"status": "FAIL", "error": str(error), "commands": commands}, indent=2) + "\n")
    raise


if __name__ == "__main__":
  main()
