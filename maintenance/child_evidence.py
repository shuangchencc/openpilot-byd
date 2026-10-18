"""Strict P5/P6 child-result contract for P7 release accounting.

Only current locked manifests are inputs. Historical result files are never read.
This checks recorded execution evidence; the P7 entry separately hashes reports,
binds timestamps/run IDs, and verifies retained compiler outputs and outer logs.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from maintenance.core import HOST, PANDA, read_json, require, sha
from maintenance.reproducibility import (
  ARTIFACTS,
  DEV_PUBLIC_KEY_SHA256,
  SIGNATURE_LABEL,
)

BUILDS = ("f4-debug", "f4-release", "h7-debug", "h7-release")
NATIVE_COUNTS = {"transport": ("passed", 19), "usb_backend": ("groups", 19), "spi_backend": ("groups_passed", 16),
                 "timing_budget": ("passed", 4), "native_link": ("passed", 1), "session_registry": ("groups", 19),
                 "can_router": ("passed", 22), "control_session": ("groups", 27), "p6_capabilities": ("groups", 12),
                 "p6_single_spi": ("passed", 5), "session_router_integration": ("passed", 11),
                 "firmware_liveness": ("passed", 13), "pandad_runtime": ("passed", 22),
                 "pandad_loop": ("passed", 10), "p6_faults": ("passed", 22)}
COMMON_SAFETY = ("deny_modes_all_local_and_host_buses", "legacy_enum32_param33_falls_back_silent",
                 "checksum_and_response_flags_cannot_bypass", "rejected_frame_retains_local_bus",
                 "partial_packet_reset_does_not_send", "fd_capability_differs_without_policy_drift",
                 "abrupt_process_death_ignition_on", "abrupt_process_death_ignition_off", "usb_power_loss_mcu_alive",
                 "whole_mcu_power_loss_has_no_software_action", "stopped_loop_watchdog_only_on_resume", "watchdog_threshold_and_wrap")
DEBUG_SAFETY = ("byd_debug_reference_only", "byd_wrong_bus_and_dlc", "byd_wrong_rx_bus_length_cannot_enable",
                "byd_payload_checksum_and_counter", "byd_freshness_boundary_and_wrap", "byd_relay_and_forwarding_block",
                "byd_disengaged_steering_rejected")
GUARD_NAMES = ("unknown-compat-guard", "offline-extra-guard", "serial-offline-guard", "serial-unknown-guard",
               "third-argument-guard", "multiple-compat-guard", "empty-serial-guard", "compat-value-guard")
P5_COMMANDS = tuple(x for name in BUILDS for x in ("build-" + name, "resources-" + name)) + (
  "protocol", "bxcan", "fdcan", "bsp", "capabilities", "safety", "liveness-f4", "liveness-h7",
  "compile-host-abi", "host-abi", "host-python", "compile-codec", "codec", "host-oracle", "host-transport")
P6_COMMANDS = ("compile-can-sanitized", "native-can-tests", "compile-health-probe-sanitized", "health-layout", "python-tests",
               "can-independent-oracle", "p6-macos", "p6-daemon-macos", "p6-safety-macos", "p5-f4-capability-wire", "p5-selected-safety",
               "linux-prepare-directory", "linux-copy-input", "linux-input-hash", "linux-extract", "linux-dep-libusb.h",
               "linux-dep-libusb-1.0.so.0.3.0", "linux-daemon-dependency-hashes", "p6-linux", "linux-copy-results",
               "p6-daemon-linux", "linux-copy-daemon-results", "p6-safety-linux", "linux-copy-safety-results",
               "native-cereal-consumers-macos", "native-cereal-consumers-linux", "session-independent-oracle-macos", "session-independent-oracle-linux")
CAP_HEX = "46344331010010001f00000007000000"
ABI_VALUES = [1345667394, 4, 48, 64, 72, 4, 6, 6, 64, 4290515701, 1974202998, 7]
NEGATIVE_ABI = ("old_health_58", "shrunk_can_16", "old_payload_offset_5")


def _eq(actual, expected, label):
  require(type(actual) is type(expected) and actual == expected, "Child evidence mismatch: " + label)


def _count(value, expected, label):
  _eq(value, expected, label)


def _names(value, count, label):
  require(type(value) is list and len(value) == count and all(type(v) is str and bool(v) for v in value), "Missing groups: " + label)
  require(len(set(value)) == count, "Duplicate groups: " + label)


def _hash(value, label):
  require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "Invalid digest: " + label)


def _hashes(values, label, count=None):
  require(type(values) is dict and bool(values), "Empty source/artifact evidence: " + label)
  if count is not None:
    _count(len(values), count, label)
  for name, value in values.items():
    require(type(name) is str and name and not Path(name).is_absolute() and ".." not in Path(name).parts, "Unsafe source name: " + label)
    _hash(value, label + "/" + name)


def _zeros(record, keys, label):
  for key in keys:
    _count(record[key], 0, label + "/" + key)


def _false(record, keys, label):
  for key in keys:
    _eq(record[key], False, label + "/" + key)


def _commands(rows, names, label, failures=None, log_hashes=False, code_key="exit_code", argv_key="command"):
  failures = failures or {}
  require(type(rows) is list and len(rows) == len(names) > 0, "Incomplete command transcript: " + label)
  _names([r["name"] for r in rows], len(names), label)
  require({r["name"] for r in rows} == set(names), "Unexpected command set: " + label)
  for row in rows:
    argv = row[argv_key]
    require(type(argv) is list and bool(argv) and all(type(arg) is str for arg in argv) and bool(argv[0]), "Invalid argv: " + label)
    expected = failures.get(row["name"], 0)
    _count(row[code_key], expected, label + "/" + row["name"])
    if "expected_exit_code" in row:
      _count(row["expected_exit_code"], expected, label + "/expected_exit_code")
    if "expected_compile_failure" in row:
      _eq(row["expected_compile_failure"], expected != 0, label + "/expected_compile_failure")
    if "expected_success" in row:
      _eq(row["expected_success"], expected == 0, label + "/expected_success")
    if log_hashes:
      _hash(row["log_sha256"], label + "/log_sha256")


def _python(record, count, label, ids="selectors"):
  _count(record["tests_run"], count, label)
  _zeros(record, ("failures", "errors", "skipped"), label)
  _names(record[ids], count, label)
  if ids == "passed_ids":
    _count(record["passed"], count, label)
    _eq(record["failed_ids"], [], label)
  else:
    _eq(record["status"], "PASS", label)


def _codec(record, label):
  for key, value in {"passed": 13, "failed": 0, "assertions": 47031, "fuzz_iterations": 4096, "fuzz_seed": "c0ffee11", "device_access": False}.items():
    _eq(record[key], value, label + "/" + key)


def _capabilities(record, label):
  _python(record, 12, label)
  _eq(record["actual_firmware_0xd5_hex"], CAP_HEX, label)
  _false(record, ("device_access", "deployable"), label)
  _eq(record["runtime_binding"], "unbound", label)
  for field in ("current_milliamps", "sound_output_level", "spi_error_count"):
    _eq(record["health_view_example"][field], None, label + "/" + field)


def _native(report, counts, label, commands):
  _eq(report["status"], "PASS_OFFLINE_HOST_ONLY", label)
  _false(report, ("device_access", "hardware_timing_validated", "active_pandad_built"), label)
  _eq(report["real_libusb_linked"], True, label)
  _eq(report["sanitizers"], ["address", "undefined"], label)
  suites = report["suites"]
  require(set(suites) == set(counts) | {"spi_golden"}, "Native suite inventory: " + label)
  for name, (field, count) in counts.items():
    item = suites[name]
    _count(item["summary"][field], count, label + "/" + name)
    # Frozen P2 worker records its single native-link pass only in the summary;
    # P6 adds a named group. Keep the historical schema exception explicit.
    _names(item["groups"], 0 if label == "P5 host transport" and name == "native_link" else count, label + "/" + name)
    error_keys = [key for key in ("failed", "failures") if key in item["summary"]]
    require(bool(error_keys), "Missing native failure count: " + name)
    _zeros(item["summary"], error_keys, label)
    _zeros(item["summary"], [key for key in ("skipped", "skips") if key in item["summary"]], label)
    _false(item["summary"], [key for key in ("device_access", "device_io", "device_open", "hardware_access", "real_can_tx") if key in item["summary"]], label)
  golden = suites["spi_golden"]
  _count(golden["matched"], 5, label + "/SPI goldens")
  _names(golden["vectors"], 5, label)
  _eq(golden["original_handle_constructed"], False, label)
  _commands(report["commands"], commands, label)


def _validate(p5, p6, root):
  root = Path(root)
  host, panda = read_json(root / HOST), read_json(root / PANDA)
  _eq(p5["schema"], "songplus-p5-offline-result/v1", "P5 schema")
  _eq(p6["schema"], "songplus-panda-p6-validation/v1", "P6 schema")
  _eq(p5["status"], "PASS_OFFLINE_CANDIDATE", "P5 status")
  _eq(p6["status"], "PASS_OFFLINE_HOST_ONLY", "P6 status")
  for data, manifest, name in ((p5, panda, PANDA), (p6, host, HOST)):
    _eq(data["manifest_sha256"], sha(root / name), name)
    _eq(data["baselines"], manifest["baselines"], name)
  _eq(p5["baselines"], p6["baselines"], "shared baselines")
  _eq(p5["source_commit"], panda["series"][-1]["commit"], "P5 source commit")
  _eq(p6["patch_commits"], [item["commit"] for item in host["series"]], "P6 patch commits")
  _eq(p6["p5_manifest_sha256"], sha(root / PANDA), "P6/P5 manifest")
  _false(p5, ("deployable", "bootstub_built", "device_access", "mcu_executed", "vehicle_validated", "installation_package_created"), "P5 scope")
  _false(p6, ("device_access", "real_transport_executed", "real_tx_permission_enabled", "MCU_build", "MCU_ARM_ABI_validated", "deployment_ready"), "P6 scope")
  _eq(p6["original_spi_path_unchanged"], True, "retained SPI")
  _eq(p6["macos_network_and_device_node_sandbox"], True, "host isolation")
  _eq(p6["sanitizers"], ["address", "undefined"], "host sanitizers")
  _eq(p6["validation_tools"], host["validation_tools"], "P6 validation tools")
  for name, digest in p6["validation_tools"].items():
    _eq(sha(root / name), digest, "validator source")
  _commands(p5["commands"], P5_COMMANDS, "P5 commands", log_hashes=True)
  _commands(p6["commands"], P6_COMMANDS, "P6 commands")
  board_inputs = {name: checksum for name, checksum in panda["result_tree"].items() if name.startswith("board/") and "obj" not in Path(name).parts}
  safety_inputs = {name.removeprefix("upstream-openpilot/opendbc_repo/"): checksum for name, checksum in panda["baseline_inputs"].items()
                   if name.startswith("upstream-openpilot/opendbc_repo/opendbc/safety/") and name.endswith(".h")}
  _hashes(board_inputs, "locked board sources", 141)
  _hashes(safety_inputs, "locked safety sources", 34)
  identity = hashlib.sha256(json.dumps({"board": board_inputs, "safety": safety_inputs}, sort_keys=True).encode()).hexdigest()
  reports = p5["reports"]
  require(set(reports) == set(BUILDS) | {"protocol", "bxcan", "fdcan", "bsp", "capabilities", "safety", "liveness-f4", "liveness-h7",
                                        "host-python", "host-codec", "host-cross-language", "host-transport"}, "P5 report inventory")
  for name in BUILDS:
    build, resources = reports[name]["build"], reports[name]["resources"]
    target, policy = name.split("-")
    for key, value in {"schema": "songplus-p5-app-build/v1", "target": target, "policy": policy, "source_identity": identity,
                       "version": f"P5-{target}-{identity[:12]}-{policy}", "signature_key": SIGNATURE_LABEL,
                       "board_inputs": board_inputs, "safety_inputs": safety_inputs, "deployable": False, "bootstub_built": False,
                       "protocol_inputs": {"board/health.h": board_inputs["board/health.h"], "opendbc/safety/can.h": safety_inputs["opendbc/safety/can.h"]}}.items():
      _eq(build[key], value, name + "/" + key)
    _hashes(build["artifacts"], name)
    require(set(build["artifacts"]) == ARTIFACTS, "ARM artifact inventory")
    require(type(build["commands"]) is list and len(build["commands"]) == 6, "ARM compilation transcript")
    for command in build["commands"]:
      require(type(command) is list and command and all(type(v) is str for v in command), "ARM command argv")
    compile_commands = [command for command in build["commands"] if "-c" in command]
    require(len(compile_commands) == 2 and any(any(arg.endswith("/board/main.c") for arg in command) for command in compile_commands), "ARM main/startup compile missing")
    _eq(resources["status"], "PASS", name + "/resources")
    _eq(resources["target"], target, name)
    _eq(resources["device_access"], False, name)
    _eq(resources["signature"]["verified_with_public_key_sha256"], DEV_PUBLIC_KEY_SHA256, name)
    _count(resources["signature"]["version"], 2, name)
    _count(resources["signature"]["signature_bytes"], 128, name)
  protocol = reports["protocol"]
  _eq(protocol["status"], "PASS", "protocol")
  _false(protocol, ("device_access", "mcu_execution"), "protocol")
  require(len(protocol["abi"]) == 2 and {v["target"] for v in protocol["abi"]} == {"f4", "h7"}, "ARM ABI targets")
  for item in protocol["abi"]:
    _eq(item["values"], ABI_VALUES, "ARM ABI values")
    _eq(item["negative_compile_cases"], list(NEGATIVE_ABI), "ARM ABI negative cases")
    for field in ("object_sha256", "section_sha256", "arm_wire_bitfields_sha256"):
      _hash(item[field], "ABI " + field)
  require(len(protocol["protocol"]) == 2 and {v["target"] for v in protocol["protocol"]} == {"f4", "h7_policy"}, "Protocol policies")
  for item in protocol["protocol"]:
    _count(item["groups"], 15, "protocol groups")
    _names(item["group_names"], 15, "protocol groups")
    _zeros(item, ("failed", "skipped"), "protocol")
    _count(item["assertions"], 2497 if item["target"] == "f4" else 2495, "protocol assertions")
  abi_failures = {f"negative-{target}-{case}": 1 for target in ("f4", "h7") for case in NEGATIVE_ABI}
  protocol_commands = ["arm-version", *[f"{prefix}-{target}" for target in ("f4", "h7") for prefix in ("abi", "abi-extract", "abi-wire")],
                       *abi_failures, *[f"protocol-{action}-{target}" for target in ("f4", "h7_policy") for action in ("compile", "run")]]
  _commands(protocol["commands"], protocol_commands, "protocol commands", abi_failures, log_hashes=True)
  for kind, groups, assertions in (("bxcan", 24, 745), ("fdcan", 14, 29074)):
    item = reports[kind]
    _eq(item["status"], "PASS", kind)
    _count(item["groups"], groups, kind)
    _count(item["assertions"], assertions, kind)
    _zeros(item, ("failed", "skipped"), kind)
    _eq(item["device_access"], False, kind)
  _names(reports["fdcan"]["group_names"], 14, "fdcan")
  bx_suites = reports["bxcan"]["suites"]
  require(len(bx_suites) == 2 and {v["name"] for v in bx_suites} == {"bxcan", "llbxcan"}, "bxcan suite inventory")
  for item in bx_suites:
    count = 15 if item["name"] == "bxcan" else 9
    _count(item["group_count"], count, "bxcan")
    _names(item["groups"], count, "bxcan")
    _zeros(item, ("failed", "skipped"), "bxcan")
  bsp = reports["bsp"]
  _eq(bsp["status"], "PASS", "BSP")
  _false(bsp, ("deployable", "device_access", "arm_code_executed", "bootstub_built"), "BSP")
  _eq(bsp["summary"], {"groups": 23, "native_assertions": 90, "compile_link_checks": 6, "failed": 0, "skipped": 0}, "BSP summary")
  require(len(bsp["checks"]) == 6 and len({c["name"] for c in bsp["checks"]}) == 6, "BSP checks")
  for check in bsp["checks"]:
    _eq(check["passed"], True, "BSP check")
  require(set(bsp["suites"]) == {"temperature", "gpio"}, "BSP suite inventory")
  for name, count in (("temperature", 7), ("gpio", 10)):
    _names(bsp["suites"][name]["groups"], count, name)
    _zeros(bsp["suites"][name], ("failed", "skipped"), name)
  bsp_names = ("native-compiler", "arm-compiler", "temperature-compile", "temperature-run", "gpio-compile", "gpio-run",
               "exact-static-budget-assemble", "exact-static-budget-link", "exact-app-budget-assemble", "exact-app-budget-link",
               "oversized-static-assemble", "oversized-static-link", "oversized-app-assemble", "oversized-app-link",
               "f4_compile_requires_explicit_candidate", "f4_compile_rejects_bootstub")
  _commands(bsp["commands"], bsp_names, "BSP commands", dict.fromkeys(("oversized-static-link", "oversized-app-link", *bsp_names[-2:]), 1), code_key="returncode", argv_key="argv")
  _capabilities(reports["capabilities"], "P5 capabilities")
  _capabilities(p6["firmware_capabilities"], "P6 capabilities")
  for selected in (reports["safety"], p6["selected_upstream_safety"]):
    _python(selected, 49, "selected safety")
    _false(selected, ("hardware_access", "mcu_execution"), "selected safety")
    require(set(selected["binaries"]) == {"debug", "release"}, "Selected safety variants")
    for item in selected["binaries"].values():
      _hash(item["sha256"], "selected safety binary")
    _hash(selected["log_sha256"], "selected safety log")
  for target in ("f4", "h7"):
    live = reports["liveness-" + target]
    _eq(live["status"], "PASS_OFFLINE_SOURCE_ORACLE", "P5 liveness")
    summary = live["summary"]
    _count(summary["passed"], 13, "P5 liveness")
    _count(summary["assertions"], 343, "P5 liveness assertions")
    _zeros(summary, ("failed", "skipped"), "P5 liveness")
    _names(summary["cases"], 13, "P5 liveness")
    _eq(summary["target_policy"], target, "P5 liveness policy")
    _eq(summary["actual_p5_c_executed"], True, "P5 source execution")
    _false(summary, ("hardware_tested", "real_can_tx"), "P5 liveness scope")
    _commands(live["commands"], ["compile-firmware-liveness", *summary["cases"]], "P5 liveness commands")
  _python(reports["host-python"], 90, "P5 Python", "passed_ids")
  _python(p6["python_tests"], 94, "P6 Python", "passed_ids")
  _codec(reports["host-codec"], "P5 codec")
  _codec(p6["native_summary"], "P6 codec")
  for cross in (reports["host-cross-language"], p6["independent_oracle"]):
    for key, value in {"status": "PASS", "valid_vectors": 64, "cross_language_checks": 220, "native_invocations": 1,
                       "baseline_c_layout_vector_checked": True, "assertions_enabled": True, "device_access": False}.items():
      _eq(cross[key], value, "codec independent oracle")
  basic = {k: NATIVE_COUNTS[k] for k in ("transport", "usb_backend", "spi_backend", "timing_budget", "native_link")}
  golden_commands = ["compile-spi-reference", "spi-reference-golden", "spi-candidate-golden", "compiler-version"]
  _native(reports["host-transport"], basic, "P5 host transport", [f"{action}-{name}" for name in basic for action in ("compile", "test")] + golden_commands)
  host_sources = {name.removeprefix("upstream-openpilot/"): checksum for name, checksum in host["baseline_inputs"].items()}
  host_sources.update(host["result_files"])
  for platform, source_count, daemon_command_count in (("macos", 29, 26), ("linux", 67, 48)):
    native = p6["native_" + platform]
    names = [name for name in NATIVE_COUNTS if name not in ("firmware_liveness", "pandad_runtime", "pandad_loop", "p6_faults")]
    _native(native, NATIVE_COUNTS, "P6 " + platform, [f"{action}-{name}" for name in names for action in ("compile", "test")] + golden_commands + ["session-oracle-vectors", "firmware-liveness"])
    daemon = p6["daemon_" + platform]
    _eq(daemon["status"], "PASS_OFFLINE_HOST_ONLY", "daemon")
    _false(daemon, ("legacy_main_invoked", "hardware_open", "real_can_tx"), "daemon scope")
    for field in ("active_pandad_built", "actual_main_cc_compiled_and_linked", "legacy_panda_spi_compiled_and_linked"):
      _eq(daemon[field], platform == "linux", "daemon build scope")
    _eq(daemon["sanitizers"], ["address", "undefined"], "daemon sanitizers")
    _eq(daemon["sandbox"], "macos-seatbelt" if platform == "macos" else "linux-user-net-mount", "daemon isolation")
    _eq(daemon["summary"], native["suites"]["pandad_runtime"]["summary"], "runtime summary linkage")
    _eq(daemon["groups"], native["suites"]["pandad_runtime"]["groups"], "runtime group linkage")
    for child, suite in (("loop_suite", "pandad_loop"), ("p6_faults_suite", "p6_faults")):
      _eq(daemon[child]["summary"], native["suites"][suite]["summary"], "daemon summary linkage")
      _eq(daemon[child]["groups"], native["suites"][suite]["groups"], "daemon groups linkage")
      _hash(daemon[child]["binary_sha256"], "daemon child binary")
    for suite, assertions in (("pandad_runtime", 2137), ("pandad_loop", 822), ("p6_faults", 3138)):
      summary = native["suites"][suite]["summary"]
      _count(summary["assertions"], assertions, suite)
      _zeros(summary, ("failed", "skipped"), suite)
      _false(summary, ("device_access", "real_can_tx", "schema_changed"), suite)
    _eq(native["suites"]["p6_faults"]["summary"]["abrupt_host_finally_claimed"], False, "process-death boundary")
    _hashes(daemon["source_files_sha256"], "daemon source set", source_count)
    for name, checksum in daemon["source_files_sha256"].items():
      _eq(checksum, host_sources.get(name), "daemon source pin: " + name)
    _hashes(daemon["dependency_files_sha256"], "daemon dependency set", 4 if platform == "macos" else 6)
    _hashes(daemon["generated_files_sha256"], "generated cereal", 9)
    _hash(daemon["binary_sha256"], "daemon binary")
    require(type(daemon["traces"]) is list and len(daemon["traces"]) == 11, "Native cereal traces")
    daemon_names = [row["name"] for row in daemon["commands"]]
    _count(len(daemon_names), daemon_command_count, "daemon commands")
    require({"generate-cereal", "generate-services", "link", "offline-entry", "link-loop-fixture", "loop-fixture", "link-p6-faults", "p6-faults", "compiler-version", "capnp-version"} <= set(daemon_names), "daemon invocation absent")
    _commands(daemon["commands"], daemon_names, "daemon commands", dict.fromkeys(GUARD_NAMES, 2) if platform == "linux" else {})
    if platform == "linux":
      require(set(GUARD_NAMES) <= set(daemon_names) and {"compile-13-main-cc", "compile-16-panda-cc", "compile-17-spi-cc"} <= set(daemon_names), "Actual Linux compile/guard missing")
      guard = daemon["unknown_compat_guard"]
      _count(guard["exit_code"], 2, "Linux invalid entry")
      _eq(guard["stdout"], "", "Linux invalid entry")
      require(len(guard["cases"]) == 8 and {row["name"] for row in guard["cases"]} == set(GUARD_NAMES), "Linux guard inventory")
      for row in guard["cases"]:
        _count(row["exit_code"], 2, "Linux invalid entry")
        _eq(row["stdout"], "", "Linux invalid entry")
    message = p6["runtime_message_oracles"][platform]
    for key, value in {"status": "PASS", "native_cereal_messages": 11, "topics": {"pandaStates": 5, "can": 6},
                       "cross_language_checks": 59, "routes": [0, 1, 2, 4, 5, 6], "assertions_enabled": True,
                       "actual_consumer_policy": True, "device_access": False, "real_can_tx": False}.items():
      _eq(message[key], value, "runtime message oracle")
    session = p6["session_independent_oracles"][platform]
    for key, value in {"status": "PASS", "fixture_count": 8, "route_checks": 48, "cross_language_checks": 96, "checks": 96,
                       "native_invocations": 1, "assertions_enabled": True, "device_access": False, "real_can_tx": False}.items():
      _eq(session[key], value, "session oracle")
    safety = p6["p6_safety"][platform]
    _eq(safety["summary"], {"status": "PASS_OFFLINE_SOURCE_ORACLE", "passed": 64, "assertions": 5086, "failed": 0, "skipped": 0, "classic_parity_comparisons": 30}, "P6 safety summary")
    _false(safety, ("hardware_access", "real_can_tx"), "P6 safety scope")
    require(len(safety["configurations"]) == 4 and {cfg["name"] for cfg in safety["configurations"]} == set(BUILDS), "P6 safety configurations")
    safety_commands = ["compiler-version"]
    parity_expected = set()
    for cfg in safety["configurations"]:
      target, variant = cfg["name"].split("-")
      _eq(cfg["target_policy"], target, "safety target")
      _eq(cfg["variant"], variant, "safety variant")
      expected = set(COMMON_SAFETY + (DEBUG_SAFETY if variant == "debug" else ("release_byd35_unavailable",)))
      require(len(cfg["cases"]) == len(expected) and {row["case"] for row in cfg["cases"]} == expected, "P6 safety case inventory")
      _hash(cfg["binary_sha256"], "safety binary")
      safety_commands.append("compile-" + cfg["name"])
      assertions = 0
      for row in cfg["cases"]:
        require(type(row["assertions"]) is int and row["assertions"] > 0, "Empty safety assertions")
        assertions += row["assertions"]
        _zeros(row, ("failed", "skipped"), "safety case")
        require(type(row["traces"]) is list and bool(row["traces"]), "Missing safety trace")
        safety_commands.append(cfg["name"] + "-" + row["case"])
        if row["case"] != "fd_capability_differs_without_policy_drift":
          parity_expected.add((variant, row["case"]))
      _count(assertions, 1532 if variant == "debug" else 1011, "safety assertions")
    _commands(safety["commands"], safety_commands, "P6 safety commands")
    require(len(safety["parity_comparisons"]) == 30 and {(row["variant"], row["case"]) for row in safety["parity_comparisons"]} == parity_expected, "Safety parity inventory")
    for row in safety["parity_comparisons"]:
      _eq(row["equal"], True, "Safety parity")
    lock = safety["source_lock"]
    _eq(lock["panda_commit"], p5["source_commit"], "safety Panda source")
    _eq(lock["opendbc_commit"], p5["baselines"]["upstream-openpilot/opendbc_repo"], "safety opendbc source")
    require(len(lock["files"]) == 46 and len({(row["repository"], row["path"]) for row in lock["files"]}) == 46, "Safety source completeness")
    for row in lock["files"]:
      catalog = panda["result_tree"] if row["repository"] == "panda" else safety_inputs if row["repository"] == "opendbc" else {}
      _eq(row["sha256"], catalog.get(row["path"]), "Safety source hash")
    digest = hashlib.sha256((json.dumps(lock, indent=2) + "\n").encode()).hexdigest()
    _eq(safety["source_lock_sha256"], digest, "safety source lock digest")
    prefix = "openpilot/selfdrive/pandad/compatibility/tests/"
    for key, name in (("source_lock_sha256", "p6_safety/source-lock.json"), ("oracle_sha256", "p6_safety/oracle.c"), ("runner_sha256", "run_p6_safety.py")):
      _eq(safety[key], host["result_files"].get(prefix + name), "Safety oracle source pin")
  return {"p5": {"arm_app_builds": 4, "abi_targets": 2, "abi_negative_compile_cases": 6, "protocol_groups": 30,
                  "bxcan_groups": 24, "fdcan_groups": 14, "bsp_groups_including_compile_link": 23, "capability_tests": 12,
                  "selected_safety_tests": 49, "liveness_groups": 26, "python_tests": 90, "codec_groups": 13,
                  "codec_assertions": 47031, "transport_groups": 59, "spi_goldens": 5},
          "p6": {"python_tests": 94, "codec_groups": 13, "codec_assertions": 47031,
                  "per_platform": {"native_groups_before_runtime": 168, "runtime_groups": 22, "loop_groups": 10,
                                   "compound_fault_groups": 22, "native_groups_total": 222, "safety_cases": 64,
                                   "safety_assertions": 5086, "classic_parity_comparisons": 30, "native_cereal_messages": 11,
                                   "cereal_cross_language_checks": 59}, "platforms": ["macos", "linux"]},
          "scope": "verified recorded offline execution; MCU hardware, target runtime and vehicle remain unvalidated"}


def validate_child_evidence(p5, p6, root):
  """Raise ValueError on incomplete, malformed, drifting or contradictory evidence."""
  try:
    return _validate(p5, p6, root)
  except (KeyError, IndexError, TypeError, AttributeError) as error:
    raise ValueError("Malformed child evidence: " + str(error)) from error
