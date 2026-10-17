#!/usr/bin/env python3
"""Compile/run the pinned firmware's actual liveness C with explicit host HAL stubs.

This is a source oracle, not an MCU build or hardware-watchdog experiment. Whole
input-file SHA-256 values are pinned; extracted functions/branches remain byte
for byte unchanged. The generated manifest records their original line ranges.
No candidate host state-machine implementation is imported by this oracle.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


INPUT_SHA256 = {
  'panda/board/main.c': 'c72ebafaf2891ccc7ed447db6a1839f85c1d2c4d77f92f01a77ff2db882360bc',
  'panda/board/main_comms.h': '57c717ac0434b16e6026e46d006d72a89be8661d9e51c3e1a99719b524c0a203',
  'panda/board/drivers/simple_watchdog.h': '38cd9992ac95ec35dd22a24a2896ec453407d33b98ddb9ff06b5b1a1a85d20d3',
  'panda/board/drivers/drivers.h': '6859c8d92656b382bf8754397a12e7f034f3e09ffc5703ff3b0766087baf45bb',
  'panda/board/sys/faults.h': 'c4df5dbedf40c7f30231b99a095dd4950152aafaa8dfcc7c056598f57916bb3b',
  'panda/board/sys/sys.h': '452049183c5033cf2391bc59ccc4da87e03838de50533d0f65b492749d534e89',
  'panda/board/utils.h': 'b40c8296d1a18fc5a83a0dda5c033457a2292197c562496d68121c96763580a5',
  'opendbc_repo/opendbc/safety/declarations.h': 'f6a6d2466c1131b734b763a9d65cdbd51bcf66f876e14bd662be5f8c55b0b284',
}

CASES = (
  'heartbeat_write_exact_semantics',
  'ignition_on_counter_boundary',
  'ignition_off_counter_boundary',
  'heartbeat_saturates_and_repeated_timeout',
  'engaged_false_third_mismatch',
  'engaged_mismatch_reset',
  'car_mode_cannot_disable_heartbeat',
  'nooutput_disabled_and_lost_flag',
  'timeout_peripheral_intents',
  'watchdog_strict_threshold',
  'watchdog_timer_wrap',
  'watchdog_stall_only_observed_on_resume',
  'watchdog_fault_does_not_prevent_later_heartbeat_timeout',
)


def extract(source: Path, output: Path):
  texts = {}
  for name, expected in INPUT_SHA256.items():
    raw = (source / name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
      raise ValueError('Pinned firmware source drift: ' + name)
    texts[name] = raw.decode('utf-8')
  evidence = []

  def take(name, pattern):
    matches = list(re.finditer(pattern, texts[name], re.M | re.S))
    if len(matches) != 1:
      raise ValueError('Non-unique extraction: ' + name + ': ' + pattern)
    match = matches[0]
    raw = match.group()
    first = texts[name].count('\n', 0, match.start()) + 1
    evidence.append({'source': name, 'first_line': first,
                     'last_line': first + raw.count('\n') - int(raw.endswith('\n')),
                     'sha256': hashlib.sha256(raw.encode()).hexdigest()})
    return f'#line {first} "{name}"\n' + raw + '\n'

  def function(name, signature):
    # Pinned source uses unindented top-level closing braces only. File hashes
    # above make this deliberately narrow extractor fail on any source drift.
    return take(name, '^' + re.escape(signature) + r' \{\n.*?^\}\n')

  constants = ''
  for macro in ('SAFETY_SILENT', 'SAFETY_NOOUTPUT', 'SAFETY_ELM327', 'SAFETY_ALLOUTPUT', 'SAFETY_TOYOTA'):
    constants += take('opendbc_repo/opendbc/safety/declarations.h', '^#define ' + macro + r' [^\n]+\n')
  for macro in ('FAULT_STATUS_NONE', 'FAULT_STATUS_TEMPORARY', 'FAULT_STATUS_PERMANENT',
                'FAULT_RELAY_MALFUNCTION', 'FAULT_HEARTBEAT_LOOP_WATCHDOG', 'PERMANENT_FAULTS'):
    constants += take('panda/board/sys/sys.h', '^#define ' + macro + r' [^\n]+\n')
  constants += take('panda/board/main.c', r'^#define HEARTBEAT_IGNITION_CNT_ON [^\n]+\n#define HEARTBEAT_IGNITION_CNT_OFF [^\n]+\n')
  constants += take('panda/board/drivers/drivers.h', r'^typedef struct simple_watchdog_state_t \{\n.*?^\} simple_watchdog_state_t;\n')
  (output / 'firmware_liveness_constants.h').write_text(constants)

  code = function('panda/board/utils.h', 'uint32_t get_ts_elapsed(uint32_t ts, uint32_t ts_last)')
  code += take('panda/board/sys/faults.h', r'^uint8_t fault_status = [^\n]+\nuint32_t faults = [^\n]+\n')
  code += function('panda/board/sys/faults.h', 'void fault_occurred(uint32_t fault)')
  code += function('panda/board/sys/faults.h', 'void fault_recovered(uint32_t fault)')
  code += take('panda/board/drivers/simple_watchdog.h', r'^static simple_watchdog_state_t wd_state;\n')
  code += function('panda/board/drivers/simple_watchdog.h', 'void simple_watchdog_kick(void)')
  code += function('panda/board/drivers/simple_watchdog.h', 'void simple_watchdog_init(uint32_t fault, uint32_t threshold)')
  code += function('panda/board/main.c', 'void set_safety_mode(uint16_t mode, uint16_t param)')
  code += function('panda/board/main.c', 'bool is_car_safety_mode(uint16_t mode)')
  code += function('panda/board/main.c', 'static void tick_handler(void)')
  code += '\nstatic void initialize_heartbeat_watchdog(void) {\n'
  code += take('panda/board/main.c', r'^  simple_watchdog_init\(FAULT_HEARTBEAT_LOOP_WATCHDOG, [^\n]+\n')
  code += '}\n'
  # Wrapping the exact switch branches avoids compiling unrelated USB/flash
  # handlers. Neither branch body nor control values are rewritten.
  code += '\nstatic void heartbeat_control(const struct fake_request *req) {\n  switch (req->request) {\n'
  code += take('panda/board/main_comms.h', r'^    case 0xf3:\n.*?^      \}\n')
  code += take('panda/board/main_comms.h', r'^    case 0xf8:\n.*?^      break;\n')
  code += '    default: abort();\n  }\n}\n'
  (output / 'firmware_liveness_source.h').write_text(code)
  return {'source_file_sha256': INPUT_SHA256, 'extracted_original_ranges': evidence,
          'generated_constants_sha256': hashlib.sha256(constants.encode()).hexdigest(),
          'generated_source_sha256': hashlib.sha256(code.encode()).hexdigest()}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--source', type=Path, required=True)
  parser.add_argument('--output', type=Path, required=True)
  parser.add_argument('--compiler', default='clang++')
  args = parser.parse_args()
  args.output.mkdir(parents=True, exist_ok=True)
  evidence = extract(args.source, args.output)
  env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1',
             UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
  commands = []

  def run(name, command):
    command = list(map(str, command))
    completed = subprocess.run(command, capture_output=True, env=env, timeout=60)
    (args.output / (name + '.stdout')).write_bytes(completed.stdout)
    (args.output / (name + '.stderr')).write_bytes(completed.stderr)
    commands.append({'name': name, 'command': command, 'exit_code': completed.returncode})
    completed.check_returncode()
    return completed.stdout.decode()

  binary = args.output / 'test_firmware_liveness'
  run('compile-firmware-liveness', [args.compiler, '-x', 'c', '-std=c11', '-Wall', '-Wextra', '-Werror',
      '-pedantic', '-O1', '-g', '-fno-omit-frame-pointer', '-fsanitize=address,undefined',
      '-I', args.output, Path(__file__).with_name('test_firmware_liveness.c'), '-o', binary])
  cases, traces, assertions = [], [], 0
  for name in CASES:
    # Each process resets the actual static variables in tick_handler naturally.
    raw = run(name, [binary, name])
    result = json.loads(raw.splitlines()[-1])
    if result['case'] != name or result['failed'] or result['skipped'] or not result['assertions']:
      raise ValueError('Invalid firmware oracle result: ' + name)
    cases.append(name)
    assertions += result['assertions']
    for line in raw.splitlines()[:-1]:
      if line.startswith('TRACE '):
        traces.append(line[6:])
        print(line)
    print('PASS ' + name)
  summary = {'suite': 'firmware_liveness', 'passed': len(cases), 'assertions': assertions,
             'failed': 0, 'skipped': 0, 'cases': cases, 'actual_pinned_c_executed': True,
             'hardware_tested': False, 'real_can_tx': False}
  report = {'status': 'PASS_OFFLINE_SOURCE_ORACLE', 'summary': summary, 'traces': traces,
            'source_evidence': evidence, 'commands': commands,
            'compiled_language': 'C11', 'sanitizers': ['address', 'undefined'],
            'scope': 'Exact pinned C liveness functions/branches; explicit HAL and safety-hook stubs; no MCU or hardware watchdog claim',
            'limits': ['1 Hz heartbeat counter thresholds are decimated tick counts, not an exact wall-clock expiry guarantee',
                       'Software watchdog observes a late loop only when a subsequent kick executes; it does not run during a stopped loop',
                       'uint32 timer elapsed arithmetic models a single modulo interval; multiple wraps are not observable',
                       'Fan 30/0 and IR 0 are upstream call arguments, not validation of C3XL physical cooling behavior']}
  (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
  print(json.dumps(summary))


if __name__ == '__main__':
  main()
