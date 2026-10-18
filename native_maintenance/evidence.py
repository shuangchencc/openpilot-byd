"""Revalidate retained native-provider reports; booleans alone are not evidence."""
from __future__ import annotations

import json
from pathlib import Path

from maintenance import child_evidence as previous
from maintenance import core as old
from maintenance.integration_evidence import check_commands, groups_passed
from native_maintenance.core import ROOT, MANIFEST, PARENT, SCOPE, expected_source, require, read_json, sha, safe_path
from tools.build_p9_native import SOURCES, SUITES, expected_commands, parse_suite


def check_retained(report, base):
  actual = old.tree_hashes(base)
  actual.pop("result.json")
  require(actual and actual == report["retained_files_sha256"], "Retained native artifact/log inventory changed")


def validate_p8_worker(worker, folder, platform, sources, root):
  p8 = read_json(root / PARENT)
  require(worker["schema"] == "songplus-runtime-integration-p8-worker/v1"
          and worker["status"] == "PASS_OFFLINE_HOST_ONLY", "Fresh P8 regression failed")
  check_retained(worker, folder)
  require(worker["source_files_sha256"] == sources, "P8 regressions used another source tree")
  require(worker["groups"] == p8["required_groups"][platform], "Fresh P8 case inventory changed")
  parsed = parse_suite((folder / "runtime.stdout").read_text(), "p8-runtime")
  require(parsed["groups"] == worker["groups"] and parsed["summary"] == worker["summary"], "P8 runtime transcript changed")
  require(sha(folder / "test_runtime_integration") == worker["binary_sha256"], "P8 runtime binary changed")
  require(worker["sanitizers"] == ["address", "undefined"] and worker["real_device_io"] is False
          and worker["real_can_tx"] is False and worker["network_sockets_opened"] is False
          and worker["isolated_msgq_endpoints_opened"] is (platform == "linux"), "P8 regression scope changed")
  require(worker["msgq_adapter_compiled"] is True and worker["msgq_interface_executed"] is (platform == "linux")
          and worker["actual_pandad_relinked_with_p8"] is (platform == "linux"), "P8 actual runtime linkage missing")
  names = {"build-contract", "native-regression", "daemon-regression", "compile-test", "link-runtime", "runtime"}
  names.update("compile-" + s for s in ("runtime_integration.cc", "realtime_bridge.cc", "usb_backend.cc", "libusb_api.cc", "spi_backend.cc", "msgq_adapter.cc"))
  failures = {}
  if platform == "linux":
    names.update(("compile-actual-main", "link-actual-pandad-p8", "actual-pandad-offline", "actual-pandad-p8-owner", "deny---compat-runtime", "deny---compat-device"))
    failures = {"deny---compat-runtime": 2, "deny---compat-device": 2}
  check_commands(worker["commands"], folder, names, failures)
  build = worker["build_contract"]
  require(build == {"suite": "p8_build_contract", "passed": 2, "failed": 0, "skipped": 0,
                   "groups": p8["required_build_groups"], "actual_scons_build": False}, "P8 build contract changed")
  require(json.loads((folder / "build-contract.stdout").read_text().splitlines()[-1]) == build, "Build contract transcript changed")
  if platform == "linux":
    require(sha(folder / "pandad-p8") == worker["actual_main_p8_binary_sha256"], "Actual pandad binary changed")
    main = worker["actual_main_p8_summary"]
    transcript = (folder / "actual-pandad-p8-owner.stdout").read_text()
    require(main["suite"] == "p8_actual_main" and main["passed"] == 1 and main["failed"] == main["skipped"] == 0
            and main["real_device_io"] is False and json.loads(transcript.splitlines()[-1]) == main, "Actual main offline proof changed")
    require([line[5:] for line in transcript.splitlines() if line.startswith("PASS ")] == p8["required_linux_main_groups"], "Actual main group missing")
  native, daemon = worker["native_regression"], worker["daemon_regression"]
  require(native == read_json(folder / "native-regression/result.json") and daemon == read_json(folder / "daemon-regression/result.json"), "Nested regressions changed")
  require(native["status"] == daemon["status"] == "PASS_OFFLINE_HOST_ONLY" and native["device_access"] is False
          and daemon["hardware_open"] is False and daemon["real_can_tx"] is False, "Native/SPI regression scope changed")
  native_counts = {k: v for k, v in previous.NATIVE_COUNTS.items() if k not in ("pandad_runtime", "pandad_loop", "p6_faults")}
  native_commands = [f"{action}-{name}" for name in native_counts if name != "firmware_liveness" for action in ("compile", "test")]
  native_commands += ["compile-spi-reference", "spi-reference-golden", "spi-candidate-golden", "compiler-version", "session-oracle-vectors", "firmware-liveness"]
  previous._native(native, native_counts, "P9 " + platform, native_commands)
  for suite, expected_count in ((daemon, 22), (daemon["loop_suite"], 10), (daemon["p6_faults_suite"], 22)):
    require(groups_passed(suite) == expected_count, "Runtime/loop/fault regressions incomplete")
  # Every compile command name is derived from the frozen retained worker.
  from tools.build_panda_daemon_p6 import COMPAT_SOURCES, DAEMON_SOURCES
  files = list(COMPAT_SOURCES) + [name + ".capnp.c++" for name in ("log", "deprecated", "custom", "car")]
  files += [Path(p).name for p in DAEMON_SOURCES] if platform == "linux" else ["test_pandad_runtime.cc"]
  files += ["test_pandad_loop.cc", "test_p6_faults.cc"]
  names = {f"compile-{i:02d}-" + name.replace(".", "-") for i, name in enumerate(files)}
  names.update(("generate-cereal", "generate-services", "link", "offline-entry", "link-loop-fixture", "loop-fixture", "link-p6-faults", "p6-faults", "compiler-version", "capnp-version"))
  failures = {}
  if platform == "linux":
    names.update(previous.GUARD_NAMES)
    failures = dict.fromkeys(previous.GUARD_NAMES, 2)
  # The old worker logs are covered by this worker's complete retained hash map;
  # its command records predate per-stream hashes, so validate fixed statuses here.
  previous._commands(daemon["commands"], sorted(names), "P9 daemon", failures)
  require(daemon["actual_main_cc_compiled_and_linked"] is (platform == "linux")
          and daemon["legacy_panda_spi_compiled_and_linked"] is (platform == "linux"), "Original SPI/main linkage missing")
  return len(worker["groups"])


def validate_native_report(report, base, root=ROOT):
  root, base = Path(root), Path(base)
  manifest = read_json(root / MANIFEST)
  require(report["schema"] == "songplus-native-p9-validation/v1" and report["status"] == "PASS_OFFLINE_HOST_ONLY", "Native build incomplete")
  require(report["manifest_sha256"] == sha(root / MANIFEST) and report["parent_manifest_sha256"] == sha(root / PARENT)
          and report["patches"] == manifest["series"], "Native patch identity changed")
  require(all(report[k] is v for k, v in SCOPE.items()), "Native validation scope changed")
  require(report["worker_sha256"] == sha(root / "tools/build_p9_native.py"), "Native worker changed")
  required = {"tools/validate_p9_native.py", "tools/build_p9_native.py", "tools/build_p8_runtime.py", "tools/run_panda_p6_host.py",
              "tools/build_panda_daemon_p6.py", "tools/p8_runtime_linux_sandbox.sh", "tools/panda_firmware_offline.sb",
              "patches/openpilot/p6/manifest.json", "patches/openpilot/p4/host-dependencies.json"}
  for directory in ("patches/openpilot/p8", "patches/openpilot/p9"):
    required.update(str(p.relative_to(root)) for p in (root / directory).iterdir() if p.is_file())
  require(set(report["validation_inputs_sha256"]) == required, "Native validation input inventory changed")
  for name, expected in report["validation_inputs_sha256"].items():
    require(sha(safe_path(root, name)) == expected, "Native validation input drift: " + name)
  source = expected_source(root)
  require(report["source_files_sha256"] == source == old.tree_hashes(base / "source"), "Native source tree changed")
  require(sha(base / "input.tar") == report["linux_input_sha256"], "Linux input archive changed")
  patches = sum(len(read_json(root / path)["series"]) for path in ("patches/openpilot/p6/manifest.json", PARENT, MANIFEST))
  names = {"git-init", "macos", "vm-mkdir", "vm-copy", "vm-bundle-hash", "vm-extract", "vm-dependencies", "linux", "vm-dependencies-after", "vm-result-copy"}
  names.update("apply-" + str(i) for i in range(patches))
  check_commands(report["commands"], base, names)
  require(set(report["platforms"]) == {"macos", "linux"}, "Both platforms required")
  counts = {"native_groups": {}, "fresh_p8_runtime_groups": {}, "fresh_native_spi_regression_platforms": 2,
            "actual_pandad_offline_entries": 1, "failed": 0, "skipped": 0}
  for platform in ("macos", "linux"):
    item, folder = report["platforms"][platform], base / platform
    require(sha(folder / "result.json") == item["result_sha256"], "Worker report changed")
    worker = read_json(folder / "result.json")
    require(worker["schema"] == "songplus-native-p9-worker/v1" and worker["status"] == "PASS_OFFLINE_HOST_ONLY", "Provider worker failed")
    check_retained(worker, folder)
    require(worker["source_files_sha256"] == source, "Provider worker source mismatch")
    require(all(worker[k] is v for k, v in SCOPE.items()), "Worker scope changed")
    require(worker["sanitizers"] == ["address", "undefined"] and worker["provider_calls_injected"] is True
            and worker["native_usb_compiled_and_linked"] is True and worker["native_spi_linux_ioctls_compiled"] is (platform == "linux")
            and worker["isolated_msgq_endpoints_opened"] is (platform == "linux") and worker["network_sockets_opened"] is False,
            "Provider compilation/isolation proof missing")
    dependencies = worker["native_dependencies_sha256"]
    require(worker["native_dependencies_unchanged"] is True and isinstance(dependencies, dict) and len(dependencies) > 20,
            "Native header/compiler/library before-after evidence missing")
    import re
    require(all(isinstance(name, str) and Path(name).is_absolute() and re.fullmatch(r"[0-9a-f]{64}", value)
                for name, value in dependencies.items()), "Native dependency hash inventory invalid")
    for name in (*SOURCES, *("tests/test_" + suite + ".cc" for suite in SUITES)):
      suffix = "openpilot/selfdrive/pandad/compatibility/" + name
      matches = [value for path, value in dependencies.items() if path.endswith("/" + suffix)]
      require(matches == [source[suffix]], "Native source dependency scan incomplete")
    if platform == "linux":
      require(any(name.endswith("/linux/spi/spidev.h") for name in dependencies), "Linux spidev header provenance missing")
    require(worker["groups"] == item["groups"] == manifest["required_groups"][platform]
            and worker["summary"] == item["summary"], "Native group inventory changed")
    counts["native_groups"][platform] = groups_passed(worker)
    failures = dict.fromkeys(("deny---compat-native", "deny---compat-runtime", "deny---compat-device"), 2) if platform == "linux" else {}
    check_commands(worker["commands"], folder, set(expected_commands(platform == "linux")), failures)
    require([c["name"] for c in worker["commands"]] == expected_commands(platform == "linux"), "Native command ordering changed")
    require(set(worker["suites"]) == set(SUITES), "Native suite missing")
    collected = []
    for suite in SUITES:
      item = worker["suites"][suite]
      parsed = parse_suite((folder / ("run-" + suite + ".stdout")).read_text(), suite)
      require(parsed["summary"] == item["summary"] and parsed["groups"] == item["groups"], "Native suite transcript mismatch")
      require(sha(folder / ("test_" + suite)) == item["binary_sha256"], "Native suite executable changed")
      collected.extend(item["groups"])
    require(collected == worker["groups"], "Native merged suite inventory changed")
    require(worker["actual_pandad_relinked_with_native_providers"] is (platform == "linux"), "Native actual-daemon linkage missing")
    if platform == "linux":
      main = parse_suite((folder / "actual-pandad-p9-owner.stdout").read_text(), "p9-main")
      require(main == worker["actual_main"] and main["groups"] == ["p8_actual_main_unbound_owner_no_io"]
              and main["summary"]["real_device_io"] is False
              and sha(folder / "pandad-p9") == worker["actual_main_binary_sha256"], "Native actual-main evidence changed")
    else:
      require(worker["actual_main"] is None and worker["actual_main_binary_sha256"] is None, "Mac actual-daemon claim invalid")
    p8_path = folder / "p8-regression/result.json"
    require(sha(p8_path) == worker["p8_regression_sha256"] and read_json(p8_path) == worker["p8_regression"], "Fresh P8 evidence changed")
    counts["fresh_p8_runtime_groups"][platform] = validate_p8_worker(worker["p8_regression"], p8_path.parent, platform, source, root)
  return counts


def validate_contract(report, base, root=ROOT):
  from tools.check_songplus_native_ci import contract_names
  require(report["schema"] == "songplus-native-ci/v1" and report["status"] == "PASS" and report["mode"] == "contract", "Native source CI failed")
  require(report["failed"] == report["skipped"] == 0 and report["passed_commands"] == len(contract_names()), "Native source CI accounting invalid")
  require(report["device_access"] is False and report["deployment_ready"] is False and report["remote_ci_executed"] is False, "Source CI scope changed")
  require([c["name"] for c in report["commands"]] == contract_names(), "Source CI command order changed")
  commands = {"native-metadata": ["-B", "tools/songplus_native.py", "verify-lock", "--metadata-only"],
              "help-root": ["-B", "tools/songplus_native.py", "--help"],
              **{"help-" + name: ["-B", "tools/songplus_native.py", name, "--help"] for name in ("verify-lock", "replay", "validate", "release-check")},
              "native-tests": ["-B", "-m", "unittest", "discover", "-s", "native_maintenance", "-p", "test_*.py", "-v"]}
  python = None
  for command in report["commands"]:
    name, argv = command["name"], command["argv"]
    require(isinstance(argv, list) and len(argv) > 1 and isinstance(argv[0], str) and argv[0]
            and argv[1:] == commands[name], "Source CI argument inventory changed")
    python = argv[0] if python is None else python
    require(python == argv[0] and type(command["returncode"]) is int and type(command["expected_returncode"]) is int
            and command["returncode"] == command["expected_returncode"] == 0, "Source CI command failed")
    for stream in ("stdout", "stderr"):
      require(sha(safe_path(base, name + "." + stream + ".log")) == command[stream + "_sha256"], "Source CI transcript changed")
  import re
  transcript = (Path(base) / "native-tests.stderr.log").read_text()
  match = re.search(r"Ran (\d+) tests? in", transcript)
  require(match is not None and int(match.group(1)) > 0 and "\nOK\n" in transcript
          and "skipped=" not in transcript, "Native maintenance unittest transcript invalid")
  from native_maintenance.core import LOCK
  locked = read_json(Path(root) / LOCK)["required_tests"]
  executed = re.findall(r"^\S+ \(([^)]+)\) \.\.\. ok$", transcript, re.MULTILINE)
  require(len(executed) == len(set(executed)) == int(match.group(1)) and sorted(executed) == locked,
          "Native maintenance executed test IDs changed")
  require(report["commands"][-1]["tests_passed"] == len(locked), "Native CI claimed count changed")
  return {"commands": len(contract_names()), "maintenance_tests": int(match.group(1)), "failed": 0, "skipped": 0}
