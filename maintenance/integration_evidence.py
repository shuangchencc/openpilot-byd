"""Independent checks of retained P8 child logs, compiled artifacts and scopes."""
from pathlib import Path
import re

from maintenance.core import ROOT, read_json, require, safe_path, sha

VARIANTS = ("f4-debug", "f4-release", "h7-debug", "h7-release")


def groups_passed(suite):
  groups, summary = suite["groups"], suite["summary"]
  require(type(groups) is list and groups and len(groups) == len(set(groups)), "Empty/duplicate executed groups")
  require(any(k in summary for k in ("failed", "failures")) and any(k in summary for k in ("skipped", "skips")), "Missing failure/skip accounting")
  require(not any(summary.get(k, 0) for k in ("failed", "failures", "errors", "skipped", "skips")), "Group failure/skip")
  count = summary.get("passed", summary.get("groups_passed", summary.get("groups")))
  require(type(count) is int and count == len(groups), "Group count does not match execution")
  return count


def check_commands(commands, base, expected_names, failures=None):
  require(type(commands) is list and commands, "Missing command records")
  names = [c["name"] for c in commands]
  require(len(names) == len(set(names)), "Duplicate command record")
  require(set(names) == set(expected_names), "Command coverage differs from frozen scope")
  failures = failures or {}
  for item in commands:
    expected = failures.get(item["name"], 0)
    require(item.get("expected_exit_code", expected) == expected, "Report cannot redefine command success")
    actual = item["exit_code"]
    require(type(actual) is int and (actual != 0 if expected == "nonzero" else actual == expected), "Child command exit mismatch")
    if "log_sha256" in item:
      require(sha(safe_path(base, item.get("log", item["name"] + ".log"))) == item["log_sha256"], "Child command log changed")
    else:
      for stream in ("stdout", "stderr"):
        require(sha(safe_path(base, item["name"] + "." + stream)) == item[stream + "_sha256"], "Child command transcript changed")


def resources(report, base, root=ROOT):
  import base64
  import hashlib
  import json
  import struct
  from maintenance.core import tree_hashes
  from maintenance.child_evidence import DEV_PUBLIC_KEY_SHA256, SIGNATURE_LABEL

  base, root = Path(base), Path(root)
  manifest_path = root / "patches/panda/p8/manifest.json"
  manifest = read_json(manifest_path)
  parent_path = root / "patches/panda/p5/manifest.json"
  parent = read_json(parent_path)
  require(manifest["parent_manifest_sha256"] == sha(parent_path), "Resource parent manifest drift")
  board_inputs = {name: checksum for name, checksum in manifest["result_tree"].items()
                  if name.startswith("board/") and "obj" not in Path(name).parts}
  safety_inputs = {name.removeprefix("upstream-openpilot/opendbc_repo/"): checksum
                   for name, checksum in parent["baseline_inputs"].items()
                   if name.startswith("upstream-openpilot/opendbc_repo/opendbc/safety/") and name.endswith(".h")}
  require(board_inputs and safety_inputs and board_inputs["board/crypto/certs/debug.pub"] == DEV_PUBLIC_KEY_SHA256,
          "Locked development-key/source inventory missing")
  source_identity = hashlib.sha256(json.dumps({"board": board_inputs, "safety": safety_inputs}, sort_keys=True).encode()).hexdigest()
  protocol_inputs = {"board/health.h": board_inputs["board/health.h"], "opendbc/safety/can.h": safety_inputs["opendbc/safety/can.h"]}
  artifact_names = {"build.log", "main.bin", "main.bin.signed", "main.ci", "main.d", "main.elf", "main.map", "main.o", "main.su", "startup.o"}

  def verify_development_signature(raw, signed, key_path):
    # Independently verify the retained bytes, not only the auditor's label.
    encoded = base64.b64decode(key_path.read_text().split()[1], validate=True)
    key_parts = []
    while encoded:
      require(len(encoded) >= 4, "Malformed development public key")
      size = struct.unpack_from(">I", encoded)[0]
      require(0 < size <= len(encoded) - 4, "Malformed development public key field")
      key_parts.append(encoded[4:4 + size])
      encoded = encoded[4 + size:]
    require(len(key_parts) == 3 and key_parts[0] == b"ssh-rsa", "Wrong development key algorithm")
    exponent, modulus = (int.from_bytes(value, "big") for value in key_parts[1:])
    require(modulus.bit_length() == 1024 and exponent == 65537, "Wrong development RSA key")
    require(len(raw) >= 4 and len(signed) == len(raw) + 136, "Development signature length mismatch")
    length = struct.unpack_from("<I", signed)[0]
    require(length == len(raw) + 8 and signed[4:len(raw)] == raw[4:]
            and signed[len(raw):length] == b"VERS\x02\0\0\0", "Signed application/body/version mismatch")
    padding = b"\0\x01" + b"\xff" * 0x69 + b"\0" + hashlib.sha1(signed[4:length]).digest()
    require(pow(int.from_bytes(signed[length:], "big"), exponent, modulus).to_bytes(128, "big") == padding,
            "Development signature verification failed")
    return {"scheme": "official panda development RSA1024/SHA1", "version": 2,
            "app_length_with_version": length, "signature_bytes": 128,
            "verified_with_public_key_sha256": DEV_PUBLIC_KEY_SHA256}
  require(report["schema"] == "songplus-p8-resources-offline/v1" and report["status"] == "PASS_OFFLINE_CANDIDATE", "P8 resource run incomplete")
  require(report["manifest_sha256"] == sha(manifest_path) and report["parent_manifest_sha256"] == manifest["parent_manifest_sha256"]
          and report["baselines"] == manifest["baselines"] and report["source_commit"] == manifest["series"][-1]["commit"], "Resource source identity changed")
  for key in ("device_access", "mcu_executed", "bootstub_built", "deployable"):
    require(report[key] is False, "Resource scope cannot grant target execution")
  require(report["runtime_resource_gate"] == "BLOCKED" and report["summary"]["runtime_resource_proof_passed"] is False,
          "Incomplete runtime resource proof cannot pass")
  jobs = {"resource-faults": "result.json", "protocol": "validation.json", "bxcan": "result.json",
          "fdcan": "result.json", "bsp": "result.json", "capabilities": "result.json", "safety": "safety.json",
          "liveness-f4": "result.json", "liveness-h7": "result.json"}
  expected_commands = set(jobs) | {f"{kind}-{rep}-{v}" for kind in ("build", "audit") for rep in ("first", "second") for v in VARIANTS}
  check_commands(report["commands"], base, expected_commands)
  reports = report["reports"]
  require(set(reports) == set(jobs) | {f"{rep}-{v}" for rep in ("first", "second") for v in VARIANTS}, "Resource report inventory incomplete")
  for rep in ("first", "second"):
    panda = base / rep / "panda"
    dbc = base / rep / "opendbc"
    require(tree_hashes(panda) == manifest["result_tree"], "Retained Panda source tree is not the locked P8 tree")
    actual_safety = {str(path.relative_to(dbc)): sha(safe_path(dbc, str(path.relative_to(dbc))))
                     for path in (dbc / "opendbc/safety").rglob("*.h") if path.is_file()}
    require(actual_safety == safety_inputs, "Retained safety header inventory differs from locked official baseline")
    for variant in VARIANTS:
      folder = base / rep / variant
      build, audit = reports[rep + "-" + variant]["build"], reports[rep + "-" + variant]["resources"]
      require(read_json(folder / "build.json") == build and read_json(folder / "resources.json") == audit, "Resource report/output disagreement")
      target, policy = variant.split("-")
      require(build["schema"] == "songplus-p8-app-build/v1" and build["target"] == target and build["policy"] == policy,
              "Wrong ARM build variant")
      require(build["deployable"] is False and build["bootstub_built"] is False, "ARM build trust scope changed")
      require(build["board_inputs"] == board_inputs and build["safety_inputs"] == safety_inputs
              and build["source_identity"] == source_identity and build["protocol_inputs"] == protocol_inputs,
              "ARM build inputs are not independently locked P8/official safety sources")
      require(build["version"] == f"P8-{target}-{source_identity[:12]}-{policy}"
              and build["signature_key"] == SIGNATURE_LABEL, "ARM candidate version/signing policy changed")
      require(set(build["artifacts"]) == artifact_names, "ARM artifact inventory changed")
      commands = build["commands"]
      require(type(commands) is list and len(commands) == 6
              and all(type(command) is list and command and all(type(arg) is str for arg in command) for command in commands),
              "ARM compile/link/sign command inventory changed")
      family = "stm32f4" if target == "f4" else "stm32h7"
      startup = "startup_stm32f413xx.s" if target == "f4" else "startup_stm32h7x5xx.s"
      linker = "stm32f4_flash.ld" if target == "f4" else "stm32h7_app.ld"
      require(commands[0][1:] == ["--version"]
              and commands[1][-4:] == ["-c", str(panda / "board/main.c"), "-o", str(folder / "main.o")]
              and commands[2][-4:] == ["-c", str(panda / "board" / family / startup), "-o", str(folder / "startup.o")]
              and "-T" + str(panda / "board" / family / linker) in commands[3]
              and commands[3][-4:] == [str(folder / "startup.o"), str(folder / "main.o"), "-o", str(folder / "main.elf")]
              and commands[4][1:] == ["-O", "binary", str(folder / "main.elf"), str(folder / "main.bin")]
              and commands[5][1:] == [str(panda / "board/crypto/sign.py"), str(folder / "main.bin"),
                                       str(folder / "main.bin.signed"), str(panda / "board/crypto/certs/debug")],
              "ARM compile/link/sign command targets changed")
      require(all(("-DALLOW_DEBUG" in command) is (policy == "debug") for command in commands[1:4])
              and "-fstack-usage" in commands[1] and "-fcallgraph-info=su" in commands[1], "ARM build policy/stack evidence missing")
      for name, expected in build["artifacts"].items():
        require(sha(safe_path(folder, name)) == expected, "ARM artifact drift: " + variant + "/" + name)
      require(audit["status"] == "PASS" and audit["target"] == target and audit["static_allocation_passed"] is True
              and audit["device_access"] is False, "Static resource audit incomplete")
      cookies = audit["boot_cookies"]
      addresses = {"enter_bootloader_mode": 0x2001FFFC if target == "f4" else 0x38001FFC,
                   "fault_cookie": 0x2001FFF8 if target == "f4" else 0x38003FFC}
      require(type(cookies["symbols"]) is dict and set(cookies["symbols"]) == set(addresses), "Boot cookie symbol inventory incomplete")
      require(cookies["isolated_from_live_objects"] is True and cookies["runtime_impact_validated"] is False
              and cookies["inherited_h7_overlap_removed"] is (target == "h7"), "Cookie/runtime impact claim changed")
      for name, address in addresses.items():
        require(cookies["symbols"][name] == {"start": address, "end_exclusive": address + 4,
                                             "bytes": 4, "overlapping_objects": []}, "Cookie ABI address/size/overlap changed")
      stack = audit["stack"]
      require(stack["reserved_bytes"] == (8192 if target == "f4" else 1024)
              and stack["worst_case_call_and_interrupt_depth_proven"] is False, "Static allocation cannot prove full runtime stack")
      graph = stack["direct_callgraph"]
      require(graph["complete_worst_case_bound"] is False and graph["cycles"] == []
              and type(graph["compiler_nodes"]) is int and graph["compiler_nodes"] > 0
              and 0 < graph["maximum_known_direct_chain_bytes"] <= stack["reserved_bytes"], "Incomplete or overflowing direct-stack evidence")
      require(type(stack["frames"]) is list and stack["frames"] and stack["maximum_single_frame"] == stack["frames"][0]
              and all(type(frame["bytes"]) is int and 0 <= frame["bytes"] <= stack["reserved_bytes"]
                      and frame["qualifier"] in ("static", "dynamic,bounded") for frame in stack["frames"]), "Missing/unbounded stack frames")
      gate = audit["runtime_resource_gate"]
      require(gate["status"] == "BLOCKED" and gate["deployable"] is False and type(gate["reasons"]) is list
              and len(gate["reasons"]) == 3 and all(type(reason) is str and reason for reason in gate["reasons"]), "Runtime resource gate promoted")
      expected_inputs = {str(folder / name): build["artifacts"][name]
                         for name in ("main.elf", "main.map", "main.bin", "main.bin.signed", "main.su", "main.ci")}
      public_key = panda / "board/crypto/certs/debug.pub"
      expected_inputs[str(public_key)] = DEV_PUBLIC_KEY_SHA256
      require(type(audit["inputs"]) is list and len(audit["inputs"]) == len(expected_inputs)
              and all(set(item) == {"path", "sha256"} for item in audit["inputs"])
              and {item["path"]: item["sha256"] for item in audit["inputs"]} == expected_inputs,
              "Resource audit input inventory changed")
      for path, expected in expected_inputs.items():
        require(sha(safe_path(base, str(Path(path).relative_to(base)))) == expected, "Resource audit input drift")
      require(graph["files"] == [{"path": str(folder / "main.ci"), "sha256": build["artifacts"]["main.ci"]}]
              and all(frame["source"] == str(folder / "main.su") for frame in stack["frames"]), "Stack source evidence points outside this build")
      raw, signed = (folder / "main.bin").read_bytes(), (folder / "main.bin.signed").read_bytes()
      require(audit["signature"] == verify_development_signature(raw, signed, public_key), "Reported development signature differs from verified bytes")
      require(audit["flash"]["raw_bytes"] == len(raw) and audit["flash"]["signed_bytes"] == len(signed), "Flash byte accounting drift")
  expected_pairs = {str(Path(variant) / artifact) for variant in VARIANTS for artifact in ("main.bin", "main.bin.signed", "main.elf")}
  require(len(report["reproducibility"]) == 12 and {r["artifact"] for r in report["reproducibility"]} == expected_pairs, "Reproducibility pairs missing")
  for item in report["reproducibility"]:
    require(item["byte_identical"] is True and sha(safe_path(base / "first", item["artifact"])) == item["sha256"]
            == sha(safe_path(base / "second", item["artifact"])), "Candidate bytes differ")
  for job, filename in jobs.items():
    require(read_json(base / job / filename) == reports[job], "Regression report changed: " + job)
    require(reports[job]["status"].startswith("PASS") and not any(reports[job].get(k, 0) for k in ("failed", "failures", "errors", "skipped")), "Regression failed: " + job)
  fault = reports["resource-faults"]
  require(fault["passed"] == 23 and fault["runtime_proof_completed"] is False and fault["runtime_resource_gate"] == "BLOCKED", "Resource fault coverage incomplete")
  boundaries = ("f4-flash", "f4-static", "h7-flash", "h7-static-heap", "h7-sram4-rx", "h7-sram4-tx")
  checks = {name + suffix for name in boundaries for suffix in ("-exact", "-overflow")}
  checks.update(target + suffix for target in ("f4", "h7") for suffix in
                ("-frame-overflow", "-unbounded-frame", "-direct-chain-overflow", "-recursive-chain", "-runtime-proof"))
  checks.add("inherited-overlap-rejected")
  require(len(fault["checks"]) == 23 and {c["name"] for c in fault["checks"]} == checks
          and all(c["passed"] is True for c in fault["checks"]), "Resource checks missing")
  commands = {name + suffix + action for name in boundaries for suffix in ("-exact", "-overflow") for action in ("-assemble", "-link")}
  commands.update(checks - {name + suffix for name in boundaries for suffix in ("-exact", "-overflow")})
  failures = {name: "nonzero" for name in commands if name.endswith(("-overflow-link", "-frame-overflow", "-unbounded-frame", "-direct-chain-overflow", "-recursive-chain", "-rejected"))}
  failures.update({target + "-runtime-proof": 3 for target in ("f4", "h7")})
  check_commands(fault["commands"], base / "resource-faults", commands, failures)
  require([a["groups"] for a in reports["protocol"]["protocol"]] == [15, 15]
          and sum(len(a["negative_compile_cases"]) for a in reports["protocol"]["abi"]) == 6, "Protocol regression incomplete")
  require(reports["bxcan"]["groups"] == 24 and reports["fdcan"]["groups"] == 14 and reports["bsp"]["summary"]["groups"] == 23, "Peripheral regression incomplete")
  require(reports["capabilities"]["tests_run"] == 12 and reports["safety"]["tests_run"] == len(set(reports["safety"]["selectors"])) == 49, "Capability/safety regression incomplete")
  require(all(reports["liveness-" + target]["summary"]["passed"] == 13 for target in ("f4", "h7")), "Firmware liveness regression incomplete")
  return {"arm_builds": 8, "resource_faults": 23, "reproducible_pairs": 12, "runtime_resource_proof_complete": False}


def runtime(report, base, root=ROOT):
  from maintenance.core import tree_hashes
  base, root = Path(base), Path(root)
  manifest_path = root / "patches/openpilot/p8/manifest.json"
  manifest = read_json(manifest_path)
  require(report["schema"] == "songplus-runtime-integration-p8-validation/v1"
          and report["status"] == "PASS_OFFLINE_HOST_ONLY", "Runtime integration incomplete")
  require(report["manifest_sha256"] == sha(manifest_path) and report["parent_manifest_sha256"] == manifest["parent_manifest_sha256"]
          and report["patches"] == manifest["series"], "Runtime patch identity drift")
  require(all(report[k] is False for k in ("real_device_io", "real_can_tx", "deployment_ready")), "Runtime scope changed")
  require(report["worker_sha256"] == sha(root / "tools/build_p8_runtime.py"), "Runtime worker drift")
  required_inputs = {"tools/validate_p8_runtime.py", "tools/build_p8_runtime.py", "tools/run_panda_p6_host.py",
                     "tools/build_panda_daemon_p6.py", "tools/p8_runtime_linux_sandbox.sh", "tools/panda_firmware_offline.sb",
                     "patches/openpilot/p6/manifest.json", "patches/openpilot/p4/host-dependencies.json"}
  required_inputs.update(str(p.relative_to(root)) for p in (root / "patches/openpilot/p8").iterdir() if p.is_file())
  require(required_inputs <= set(report["validation_inputs_sha256"]), "Runtime validation dependency inventory missing")
  for name, digest in report["validation_inputs_sha256"].items():
    require(sha(safe_path(root, name)) == digest, "Runtime validator dependency drift")
  prior = read_json(root / "patches/openpilot/p6/manifest.json")
  expected_sources = {k.removeprefix("upstream-openpilot/"): v for k, v in prior["baseline_inputs"].items()}
  expected_sources.update(prior["result_files"])
  expected_sources.update({k.removeprefix("upstream-openpilot/"): v for k, v in manifest.get("additional_baseline_inputs", {}).items()})
  expected_sources.update(manifest["result_files"])
  require(report["source_files_sha256"] == expected_sources, "Runtime source tree is not the locked P8 tree")
  require(tree_hashes(base / "source") == expected_sources, "Retained runtime source changed")
  require(sha(base / "input.tar") == report["linux_input_sha256"], "Linux input archive drift")
  parent_commands = {"git-init", "macos", "vm-mkdir", "vm-copy", "vm-bundle-hash", "vm-extract", "vm-dependencies", "vm-dependencies-after", "linux", "vm-result-copy"}
  parent_commands.update("apply-" + str(i) for i in range(len(prior["series"]) + len(manifest["series"])))
  check_commands(report["commands"], base, parent_commands)
  require(set(report["platforms"]) == {"macos", "linux"}, "Both host platforms required")
  count = 0
  for platform in ("macos", "linux"):
    folder = base / platform
    item = report["platforms"][platform]
    require(sha(folder / "result.json") == item["result_sha256"], "Platform report drift")
    worker = read_json(folder / "result.json")
    retained = tree_hashes(folder)
    retained.pop("result.json")
    require(retained and retained == worker["retained_files_sha256"], "Runtime retained artifact/log inventory drift")
    require(worker["status"] == "PASS_OFFLINE_HOST_ONLY" and worker["groups"] == item["groups"]
            == manifest["required_groups"][platform] and worker["summary"] == item["summary"], "Runtime case set drift")
    count += groups_passed(worker)
    transcript = (folder / "runtime.stdout").read_text()
    require([line[5:] for line in transcript.splitlines() if line.startswith("PASS ")] == worker["groups"], "Runtime executed groups differ from log")
    import json
    require(json.loads(transcript.splitlines()[-1]) == worker["summary"], "Runtime execution summary differs from log")
    require(worker["source_files_sha256"] == report["source_files_sha256"], "Runtime workers compiled different sources")
    require(sha(folder / "test_runtime_integration") == worker["binary_sha256"], "Runtime executable drift")
    require(worker["sanitizers"] == ["address", "undefined"] and worker["real_device_io"] is False
            and worker["real_can_tx"] is False and worker["network_sockets_opened"] is False
            and worker["isolated_msgq_endpoints_opened"] is (platform == "linux"), "Runtime scope/sanitizer drift")
    require(worker["msgq_adapter_compiled"] is True and worker["msgq_interface_executed"] is (platform == "linux")
            and worker["actual_pandad_relinked_with_p8"] is (platform == "linux"), "Runtime integration linkage missing")
    worker_commands = {"build-contract", "native-regression", "daemon-regression", "compile-test", "link-runtime", "runtime"}
    worker_commands.update("compile-" + name for name in ("runtime_integration.cc", "realtime_bridge.cc", "usb_backend.cc", "libusb_api.cc", "spi_backend.cc", "msgq_adapter.cc"))
    failures = {}
    if platform == "linux":
      worker_commands.update(("compile-actual-main", "link-actual-pandad-p8", "actual-pandad-offline", "actual-pandad-p8-owner", "deny---compat-runtime", "deny---compat-device"))
      failures = {"deny---compat-runtime": 2, "deny---compat-device": 2}
    check_commands(worker["commands"], folder, worker_commands, failures)
    build = worker["build_contract"]
    require(build == {"suite": "p8_build_contract", "passed": 2, "failed": 0, "skipped": 0,
                     "groups": ["p8_scons_darwin_preserves_original_exclusion", "p8_scons_daemon_pinned_usb_does_not_leak_to_extras"],
                     "actual_scons_build": False}, "SConscript contract incomplete")
    require(json.loads((folder / "build-contract.stdout").read_text().splitlines()[-1]) == build, "SConscript transcript drift")
    if platform == "linux":
      require(sha(folder / "pandad-p8") == worker["actual_main_p8_binary_sha256"], "Actual P8 main executable drift")
      main = worker["actual_main_p8_summary"]
      require(main["suite"] == "p8_actual_main" and main["passed"] == 1 and main["failed"] == main["skipped"] == 0
              and main["real_device_io"] is False, "Actual main owner entry incomplete")
      transcript = (folder / "actual-pandad-p8-owner.stdout").read_text()
      require([line[5:] for line in transcript.splitlines() if line.startswith("PASS ")] == manifest["required_linux_main_groups"], "Main entry groups missing")
    native, daemon = worker["native_regression"], worker["daemon_regression"]
    require(native == read_json(folder / "native-regression/result.json") and daemon == read_json(folder / "daemon-regression/result.json"), "P8 regression output changed")
    require(native["status"] == daemon["status"] == "PASS_OFFLINE_HOST_ONLY", "P8 tree regression incomplete")
    require(native["device_access"] is False and daemon["hardware_open"] is False and daemon["real_can_tx"] is False, "Old regression scope changed")
    from maintenance import child_evidence as previous
    native_counts = {k: v for k, v in previous.NATIVE_COUNTS.items() if k not in ("pandad_runtime", "pandad_loop", "p6_faults")}
    native_commands = [f"{action}-{name}" for name in native_counts if name != "firmware_liveness" for action in ("compile", "test")]
    native_commands += ["compile-spi-reference", "spi-reference-golden", "spi-candidate-golden", "compiler-version", "session-oracle-vectors", "firmware-liveness"]
    previous._native(native, native_counts, "P8 " + platform, native_commands)
    for suite, expected_count in ((daemon, 22), (daemon["loop_suite"], 10), (daemon["p6_faults_suite"], 22)):
      require(groups_passed(suite) == expected_count, "Runtime/loop/fault regression groups missing")
    require(len(daemon["commands"]) == (48 if platform == "linux" else 26), "Daemon compile/guard transcript incomplete")
    previous._commands(daemon["commands"], [c["name"] for c in daemon["commands"]], "P8 daemon",
                       dict.fromkeys(previous.GUARD_NAMES, 2) if platform == "linux" else {})
    require(daemon["actual_main_cc_compiled_and_linked"] is (platform == "linux")
            and daemon["legacy_panda_spi_compiled_and_linked"] is (platform == "linux"), "Default SPI/main linkage regression missing")
  return {"runtime_groups": count, "platforms": 2}


def contract(report, base, root=ROOT):
  root, base = Path(root).resolve(), Path(base)
  inventory = read_json(safe_path(root, "maintenance/integration-test-inventory.json"))
  require(inventory["schema"] == "songplus-integration-test-inventory/v1", "Unknown source test inventory")
  commands = {
    "integration-metadata": ["-B", "tools/songplus_integration.py", "verify-lock", "--metadata-only"],
    "help-root": ["-B", "tools/songplus_integration.py", "--help"],
    **{"help-" + name: ["-B", "tools/songplus_integration.py", name, "--help"]
       for name in ("verify-lock", "replay", "validate", "release-check")},
    "maintenance-tests": ["-B", "-m", "unittest", "discover", "-s", "maintenance", "-p", "test_*.py", "-v"],
    "integration-ci-tests": ["-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_songplus_integration_ci.py", "-v"],
  }
  suites = inventory["suites"]
  require(set(suites) == {"maintenance-tests", "integration-ci-tests"}, "Source test inventory scope changed")
  for name, suite in suites.items():
    require(suite["argv_suffix"] == commands[name], "Source test inventory command changed")
    test_ids = suite["test_ids"]
    require(type(test_ids) is list and test_ids and all(type(item) is str for item in test_ids)
            and test_ids == sorted(set(test_ids)), "Source test inventory IDs empty, unordered or duplicate")
    pattern = "test_*.py" if name == "maintenance-tests" else "test_songplus_integration_ci.py"
    directory = "maintenance" if name == "maintenance-tests" else "tests"
    paths = sorted(str(path.relative_to(root)) for path in safe_path(root, directory).rglob(pattern))
    require(suite["source_files"] == {path: sha(safe_path(root, path)) for path in paths},
            "Source test inventory does not cover current test files")
    modules = {Path(path).stem for path in paths}
    require(all(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*){2,}", item)
                and item.split(".")[0] in modules for item in test_ids), "Invalid source test inventory ID")
  require(report["schema"] == "songplus-integration-ci/v1" and report["status"] == "PASS"
          and report["mode"] == "contract" and type(report["failed"]) is int and type(report["skipped"]) is int
          and report["failed"] == report["skipped"] == 0, "Source contract incomplete")
  require(type(report["passed_commands"]) is int and report["passed_commands"] == len(report["commands"]) == len(commands)
          and {c["name"] for c in report["commands"]} == set(commands), "Source CI commands missing")
  python = None
  counts = {}
  for command in report["commands"]:
    name, argv = command["name"], command["argv"]
    require(type(argv) is list and len(argv) > 1 and type(argv[0]) is str and bool(argv[0])
            and not any(char in argv[0] for char in ("\0", "\n", "\r"))
            and argv[1:] == commands[name], "Source CI command differs from frozen argv suffix")
    python = argv[0] if python is None else python
    require(argv[0] == python, "Source CI Python runner changed between commands")
    require(type(command["returncode"]) is int and type(command["expected_returncode"]) is int
            and command["returncode"] == command["expected_returncode"] == 0, "Source CI command failure")
    transcripts = {}
    for stream in ("stdout", "stderr"):
      path = safe_path(base, name + "." + stream + ".log")
      require(sha(path) == command[stream + "_sha256"], "Source CI transcript changed")
      transcripts[stream] = path.read_text()
    if name in suites:
      expected_ids = suites[name]["test_ids"]
      # unittest's default TextTestRunner writes the verbose outcomes and final
      # summary to stderr. stdout from a test cannot substitute for that record.
      transcript = transcripts["stderr"]
      outcomes = re.findall(r"^(\w+) \(([\w.]+)\) \.\.\. ok$", transcript, re.MULTILINE)
      require(all(method == identity.rsplit(".", 1)[-1] for method, identity in outcomes), "Source CI test method/ID mismatch")
      ids = [identity for _, identity in outcomes]
      require(sorted(ids) == expected_ids, "Source CI executed IDs differ from frozen inventory")
      summaries = re.findall(r"^Ran (\d+) tests? in [0-9]+(?:\.[0-9]+)?s$", transcript, re.MULTILINE)
      require(type(command["tests_passed"]) is int and command["tests_passed"] == len(expected_ids)
              and summaries == [str(len(expected_ids))] and transcript.endswith("\nOK\n")
              and transcript.count("\nOK\n") == 1
              and not re.search(r"\.\.\. (?:skipped|FAIL|ERROR|expected failure|unexpected success)|^FAILED(?: |$)",
                                transcript, re.MULTILINE), "Source CI test accounting differs from transcript")
      counts[name] = len(ids)
  return {"commands": len(commands), "tests": counts}
