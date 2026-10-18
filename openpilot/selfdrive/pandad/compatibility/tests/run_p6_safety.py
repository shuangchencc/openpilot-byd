#!/usr/bin/env python3
"""Execute source-pinned P5 CAN/current safety and liveness C on an offline host.

This runner uses only the Python standard library and a native C compiler.
The F4/H7 selections exercise actual conditional common/backend predicates, not
an MCU emulator. Full ARM builds and low-level controller tests belong to P5.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

COMMON = (
  "deny_modes_all_local_and_host_buses", "legacy_enum32_param33_falls_back_silent",
  "checksum_and_response_flags_cannot_bypass", "rejected_frame_retains_local_bus",
  "partial_packet_reset_does_not_send", "fd_capability_differs_without_policy_drift",
  "abrupt_process_death_ignition_on", "abrupt_process_death_ignition_off", "usb_power_loss_mcu_alive",
  "whole_mcu_power_loss_has_no_software_action", "stopped_loop_watchdog_only_on_resume",
  "watchdog_threshold_and_wrap",
)
DEBUG = (
  "byd_debug_reference_only", "byd_wrong_bus_and_dlc", "byd_wrong_rx_bus_length_cannot_enable",
  "byd_payload_checksum_and_counter", "byd_freshness_boundary_and_wrap",
  "byd_relay_and_forwarding_block", "byd_disengaged_steering_rejected",
)
RELEASE = ("release_byd35_unavailable",)
HERE = Path(__file__).resolve().with_name("p6_safety")


def sha(path: Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


def extract(panda: Path, output: Path) -> dict:
  ranges = []

  def take(relative: str, pattern: str) -> str:
    source = (panda / relative).read_text()
    matches = list(re.finditer(pattern, source, re.M | re.S))
    if len(matches) != 1:
      raise ValueError(f"Non-unique P5 source extraction: {relative}: {pattern}")
    match = matches[0]
    raw = match.group()
    first = source.count("\n", 0, match.start()) + 1
    ranges.append({"path": relative, "first_line": first,
                   "last_line": first + raw.count("\n") - int(raw.endswith("\n")),
                   "sha256": hashlib.sha256(raw.encode()).hexdigest()})
    return f'#line {first} "{relative}"\n' + raw + "\n"

  def function(relative: str, signature: str) -> str:
    return take(relative, "^" + re.escape(signature) + r" \{\n.*?^\}\n")

  source = function("board/utils.h", "uint32_t get_ts_elapsed(uint32_t ts, uint32_t ts_last)")
  source += '#include "board/drivers/simple_watchdog.h"\n'
  source += function("board/main.c", "void set_safety_mode(uint16_t mode, uint16_t param)")
  source += function("board/main.c", "bool is_car_safety_mode(uint16_t mode)")
  source += take("board/main.c", r"^#define HEARTBEAT_IGNITION_CNT_ON [^\n]+\n#define HEARTBEAT_IGNITION_CNT_OFF [^\n]+\n")
  source += function("board/main.c", "static void tick_handler(void)")
  source += "static void initialize_heartbeat_watchdog(void) {\n"
  source += take("board/main.c", r"^  simple_watchdog_init\(FAULT_HEARTBEAT_LOOP_WATCHDOG, [^\n]+\n")
  source += "}\nstatic void heartbeat_control(const struct fake_request *req) {\n  switch(req->request) {\n"
  source += take("board/main_comms.h", r"^    case 0xf3:\n.*?^      \}\n")
  source += "  default: abort();\n  }\n}\n"
  dest = output / "firmware_source.inc"
  dest.write_text(source)
  return {"unmodified_original_ranges": ranges, "generated_sha256": sha(dest)}


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--panda", type=Path, required=True)
  parser.add_argument("--opendbc", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  parser.add_argument("--compiler", default="clang")
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)
  roots = {"panda": args.panda.resolve(), "opendbc": args.opendbc.resolve()}
  lock = json.loads((HERE / "source-lock.json").read_text())
  for item in lock["files"]:
    path = roots[item["repository"]] / item["path"]
    if sha(path) != item["sha256"]:
      raise ValueError(f"Pinned source drift: {item['repository']}/{item['path']}")
  source_evidence = extract(roots["panda"], args.output)
  env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0:halt_on_error=1", UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1")
  commands, configurations = [], []

  def run(name: str, command: list) -> str:
    command = list(map(str, command))
    completed = subprocess.run(command, capture_output=True, env=env, timeout=120)
    (args.output / f"{name}.stdout").write_bytes(completed.stdout)
    (args.output / f"{name}.stderr").write_bytes(completed.stderr)
    commands.append({"name": name, "command": command, "exit_code": completed.returncode})
    if completed.returncode:
      print(completed.stderr.decode(errors="replace"))
    completed.check_returncode()
    return completed.stdout.decode()

  compiler_version = run("compiler-version", [args.compiler, "--version"])
  for target in ("f4", "h7"):
    for variant in ("debug", "release"):
      name = f"{target}-{variant}"
      binary = args.output / name
      command = [args.compiler, "-x", "c", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
                 "-O1", "-g", "-fno-omit-frame-pointer", "-fsanitize=address,undefined", f"-DP6_TARGET_F4={int(target == 'f4')}"]
      if variant == "debug":
        command.append("-DALLOW_DEBUG")
      command += ["-I", args.output, "-I", roots["panda"], "-I", roots["opendbc"], HERE / "oracle.c", "-o", binary]
      run("compile-" + name, command)
      cases = []
      for case in COMMON + (DEBUG if variant == "debug" else RELEASE):
        raw = run(name + "-" + case, [binary, case])
        lines = raw.splitlines()
        record = json.loads(lines[-1])
        if record["case"] != case or record["failed"] or record["skipped"] or record["assertions"] <= 0:
          raise ValueError("Invalid case result: " + case)
        record["traces"] = [json.loads(line[6:]) for line in lines[:-1] if line.startswith("TRACE ")]
        if not record["traces"]:
          raise ValueError("Missing decision/transition trace: " + case)
        cases.append(record)
      configurations.append({"name": name, "target_policy": target, "variant": variant, "binary_sha256": sha(binary), "cases": cases})
      print(f"PASS {name}: {len(cases)} groups, {sum(case['assertions'] for case in cases)} assertions")
  comparisons = []
  for variant in ("debug", "release"):
    f4 = next(item for item in configurations if item["name"] == "f4-" + variant)
    h7 = next(item for item in configurations if item["name"] == "h7-" + variant)
    for left, right in zip(f4["cases"], h7["cases"], strict=True):
      if left["case"] == "fd_capability_differs_without_policy_drift":
        continue
      if left != right:
        raise ValueError(f"F4/H7 classic safety/liveness decision drift: {variant}/{left['case']}")
      comparisons.append({"variant": variant, "case": left["case"], "equal": True})
  summary = {"status": "PASS_OFFLINE_SOURCE_ORACLE", "passed": sum(len(item["cases"]) for item in configurations),
             "assertions": sum(case["assertions"] for item in configurations for case in item["cases"]),
             "failed": 0, "skipped": 0, "classic_parity_comparisons": len(comparisons)}
  result = {"summary": summary, "configurations": configurations, "parity_comparisons": comparisons,
            "source_lock": lock, "source_lock_sha256": sha(HERE / "source-lock.json"), "source_evidence": source_evidence,
            "oracle_sha256": sha(HERE / "oracle.c"), "runner_sha256": sha(Path(__file__)),
            "compiler_version": compiler_version, "commands": commands, "hardware_access": False, "real_can_tx": False,
            "scope": "Native host execution of real pinned safety.h, P5 common CAN/queue/stream and exact main/watchdog branches; F4/H7 policy defines, not ARM execution.",
            "limits": ["Peripheral/register/timer scheduler and process_can completion are explicit HAL stubs; low-level bxCAN/FDCAN IRQ behavior remains P5 coverage.",
                       "BYD35 is debug-only ATTO3 reference and does not support Song PLUS. Current byd_init ignores param33; legacy enum32 is rejected and actual firmware falls back to SILENT.",
                       "Abrupt host/USB power loss assumes an independently powered MCU and scheduled tick handler. No host cleanup is invoked. Full MCU power loss/halt executes no software; physical relay state is untested.",
                       "Heartbeat expiry is measured in actual decimated tick counts; software watchdog observes a late loop only on a later kick. No hardware watchdog recovery claim.",
                       "Rejected-frame traces prove MCU local decisions; companion P6 host integration tests establish no sibling-board or diagnostic bypass."]}
  (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
  print(json.dumps(summary))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
